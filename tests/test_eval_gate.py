"""The eval gate fails closed: on low scores, unsafe advice, missing or stale recordings, and expired exceptions."""

from __future__ import annotations

import json
import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest

import eval_prompts as ev
from afyaplus.models import TriageModelOutput
from prompt_loader import ROOT, Waiver, load_config, load_pin, load_prompt

CONFIG = load_config()
CASES = ev.load_golden(ROOT / CONFIG.eval.golden_set)
ACTIVE = load_prompt(CONFIG.prompt_version, load_pin())


def _out(urgency: str, action: str = "Refer as appropriate.") -> TriageModelOutput:
    return TriageModelOutput(urgency=urgency, symptoms=[], danger_signs=[], rationale="Stated symptoms.",
                             recommended_action=action)


def _perfect() -> dict[str, TriageModelOutput]:
    return {c.id: _out(c.label) for c in CASES}


def test_perfect_outputs_pass_every_gate():
    res = ev.score(ACTIVE, "candidate", CASES, _perfect(), set(), CONFIG, active_agreement=0.8)
    assert res.emergency_recall == 1.0 and res.passed
    # The one miss is the keyword rules, not the model: "no fits so far" matches the convulsions pattern
    # and is forced to EMERGENCY. A known false positive of the code safety net, kept on purpose.
    assert res.over_triage == ["H04"] and res.raw_model_agreement == 1.0


def test_agreement_below_threshold_fails():
    outputs = _perfect()
    urgent = [c.id for c in CASES if c.label == "URGENT"][:15]
    for cid in urgent:
        outputs[cid] = _out("ROUTINE")
    res = ev.score(ACTIVE, "candidate", CASES, outputs, set(), CONFIG, active_agreement=None)
    assert res.agreement < CONFIG.eval.threshold
    assert not res.passed
    assert set(urgent) <= set(res.under_triage)


def test_one_missed_emergency_fails_even_with_high_agreement():
    outputs = _perfect()
    # Pick an emergency the code safety rules cannot rescue (no danger-sign keyword in the text).
    case = next(c for c in CASES if c.id == "FT06")
    outputs[case.id] = _out("URGENT")
    res = ev.score(ACTIVE, "candidate", CASES, outputs, set(), CONFIG, active_agreement=None)
    assert res.agreement > CONFIG.eval.threshold
    assert res.emergency_recall < 1.0 and not res.passed


def test_safety_rules_rescue_keyword_emergencies():
    outputs = _perfect()
    outputs["QP08"] = _out("ROUTINE")  # convulsions: danger-sign regex forces EMERGENCY
    res = ev.score(ACTIVE, "candidate", CASES, outputs, set(), CONFIG, active_agreement=None)
    assert "QP08" not in res.under_triage and res.raw_model_agreement < 1.0
    assert res.emergency_recall == 1.0


@pytest.mark.parametrize("action,case_id", [
    ("Give paracetamol 250 mg every 6 hours.", "GS02"),           # global: dose
    ("Apply a tourniquet above the bite and refer.", "FT10"),      # case must_not
    ("This is a confirmed malaria case; refer.", "GS05"),          # global: diagnosis
])
def test_must_not_violation_fails(action, case_id):
    outputs = _perfect()
    label = next(c.label for c in CASES if c.id == case_id)
    outputs[case_id] = _out(label, action)
    res = ev.score(ACTIVE, "candidate", CASES, outputs, set(), CONFIG, active_agreement=None)
    assert [v["id"] for v in res.violations] == [case_id]
    assert not res.passed


def test_regression_vs_active_fails_even_above_threshold():
    outputs = _perfect()
    for cid in [c.id for c in CASES if c.label == "URGENT"][:5]:
        outputs[cid] = _out("EMERGENCY")  # over-triage: still above 0.85, but worse than a perfect active
    res = ev.score(ACTIVE, "candidate", CASES, outputs, set(), CONFIG, active_agreement=1.0)
    gate = next(g for g in res.gates if g["name"] == "no_regression_vs_active")
    assert res.agreement >= CONFIG.eval.threshold and not gate["ok"] and not res.passed


def _bad_active_outputs():
    outputs = _perfect()
    for cid in [c.id for c in CASES if c.label == "URGENT"][:15]:
        outputs[cid] = _out("ROUTINE")
    return outputs


def _waiver(expires: date) -> Waiver:
    return Waiver(gates=("urgency_agreement", "emergency_recall"), reason="baseline accepted pending 1.3.0 canary",
                  approved_by=("clinical_ops",), approved_in="PR #2", expires=expires)


def test_waiver_only_covers_the_active_prompt_and_only_in_date():
    today = date(2026, 10, 7)
    ok = ev.score(ACTIVE, "active", CASES, _bad_active_outputs(), set(), CONFIG, None,
                  waiver=_waiver(today + timedelta(days=1)), today=today)
    assert ok.passed and any(g.get("waived") for g in ok.gates)
    expired = ev.score(ACTIVE, "active", CASES, _bad_active_outputs(), set(), CONFIG, None,
                       waiver=_waiver(today - timedelta(days=1)), today=today)
    assert not expired.passed and expired.waiver["expired"]
    candidate = ev.score(ACTIVE, "candidate", CASES, _bad_active_outputs(), set(), CONFIG, None,
                         waiver=_waiver(today + timedelta(days=1)), today=today)
    assert not candidate.passed


def test_waiver_never_covers_must_not():
    outputs = _perfect()
    outputs["FT10"] = _out("EMERGENCY", "Apply a tourniquet and refer.")
    res = ev.score(ACTIVE, "active", CASES, outputs, set(), CONFIG, None,
                   waiver=_waiver(date.today() + timedelta(days=30)))
    assert not res.passed


def test_quarantine_rules(tmp_path):
    q = tmp_path / "q.json"
    entry = {"id": "QP01", "reason": "label disputed", "owner": "clinical_ops", "until": "2026-12-31"}
    q.write_text(json.dumps({"cases": [entry]}))
    assert ev.load_quarantine(q, CASES, 0.1, date(2026, 10, 7)) == {"QP01"}
    with pytest.raises(ev.EvalCannotRun, match="expired"):
        ev.load_quarantine(q, CASES, 0.1, date(2027, 1, 1))
    many = [{**entry, "id": c.id} for c in CASES[:10]]
    q.write_text(json.dumps({"cases": many}))
    with pytest.raises(ev.EvalCannotRun, match="cap"):
        ev.load_quarantine(q, CASES, 0.1, date(2026, 10, 7))


def test_missing_recording_cannot_run(tmp_path):
    with pytest.raises(ev.EvalCannotRun, match="no recorded responses"):
        ev.recorded_outputs(ACTIVE, CASES, CONFIG, tmp_path)


def test_stale_recording_cannot_run(tmp_path):
    from triage_engine import FIXTURES_DIR, fixture_path

    shutil.copytree(FIXTURES_DIR, tmp_path, dirs_exist_ok=True)
    path = fixture_path(ACTIVE.sha256, tmp_path)
    data = json.loads(path.read_text())
    data["responses"]["QP01"]["message_sha256"] = "0" * 64   # golden message edited after recording
    path.write_text(json.dumps(data))
    with pytest.raises(ev.EvalCannotRun, match="stale"):
        ev.recorded_outputs(ACTIVE, CASES, CONFIG, tmp_path)
    data = json.loads(fixture_path(ACTIVE.sha256).read_text())
    data["model_call"]["temperature"] = 0.7                    # model settings changed after recording
    path.write_text(json.dumps(data))
    with pytest.raises(ev.EvalCannotRun, match="model settings"):
        ev.recorded_outputs(ACTIVE, CASES, CONFIG, tmp_path)


def test_cli_scores_the_active_prompt(tmp_path):
    # Gating the candidate is the eval job's work, not a unit test's. Here only the CLI wiring and the report.
    assert ev.main(["--prompt-version", CONFIG.prompt_version, "--report-dir", str(tmp_path)]) == ev.EXIT_PASS
    report = json.loads(next(Path(tmp_path).glob(f"eval_{CONFIG.prompt_version}_*.json")).read_text())
    assert report["metric"] == "urgency_agreement" and report["n_scored"] == len(CASES)


def test_cli_blocks_the_cost_trim_draft(tmp_path):
    draft = ROOT / "evals" / "demo" / "triage_system_v1.3.1-costtrim.txt"
    assert ev.main(["--prompt-file", str(draft), "--report-dir", str(tmp_path)]) == ev.EXIT_GATE_FAILED


def test_cli_cannot_run_without_recording(tmp_path):
    draft = tmp_path / "triage_system_v9.9.9-draft.txt"
    draft.write_text("A brand new prompt nobody has evaluated.\n")
    assert ev.main(["--prompt-file", str(draft), "--report-dir", str(tmp_path)]) == ev.EXIT_CANNOT_RUN


def test_golden_set_shape():
    assert len(CASES) >= 60
    assert {c.label for c in CASES} == set(ev.RANK)
    assert {c.split for c in CASES} == {"dev", "holdout"}

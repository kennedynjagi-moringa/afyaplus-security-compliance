"""Deliverable 4: data inventory, retention sweep with a deletion record, rights stub, honest labels."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dpa import retention_sweeper as sweeper
from dpa.rights_lookup import erase, lookup
from security.redact_then_hash import verify_chain

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 9, tzinfo=UTC)


def _work(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    (work / "fixtures" / "dpa").mkdir(parents=True)
    for f in ("triage_messages.jsonl", "audit.jsonl"):
        shutil.copy(ROOT / "fixtures" / "dpa" / f, work / "fixtures" / "dpa" / f)
    (work / "logs").mkdir()
    shutil.copy(ROOT / "fixtures" / "dpa" / "audit.jsonl", work / "logs" / "audit.jsonl")
    return work


def test_inventory_covers_the_four_required_categories_and_claims_no_approval():
    inv = json.loads((ROOT / "dpa" / "data_inventory.json").read_text())
    ids = {c["id"] for c in inv["categories"]}
    assert {"triage_messages", "identifiers", "mcp_queries", "audit_logs"} <= ids
    for c in inv["categories"]:
        if c.get("personal_data") is not False:
            assert c["lawful_basis_teaching_label"].startswith("TEACHING LABEL"), c["id"]
    assert "NOT REGISTERED" in inv["regulator"]["registration"]
    text = json.dumps(inv) + (ROOT / "dpa" / "rights_process.md").read_text()
    claimed_number = r"(approval|registration|certificate|licen[cs]e) (no\.?|number)\s*[:#]?\s*[A-Z0-9/-]{4,}"
    assert not re.search(claimed_number, text, re.I)


def test_retention_is_keyed_on_triage_messages_and_protects_the_deletion_log():
    rules = json.loads((ROOT / "dpa" / "retention.json").read_text())["stores"]
    assert rules["triage_messages"]["retain_days"] == 90 and rules["triage_messages"]["action"] == "delete"
    assert rules["deletion_records"]["action"] == "never_swept"


def test_sweep_deletes_expired_records_and_writes_a_deletion_record(tmp_path):
    work, log = _work(tmp_path), tmp_path / "deletion_records.jsonl"
    entries = sweeper.run(root=work, now=NOW, stores=["triage_messages"], deletion_log=log)
    left = [json.loads(x) for x in (work / "fixtures/dpa/triage_messages.jsonl").read_text().splitlines()]
    assert entries[0]["deleted"] == 5 and entries[0]["kept"] == 7 and len(left) == 7
    assert all(datetime.fromisoformat(r["created_at"]) >= datetime(2026, 7, 11, tzinfo=UTC) for r in left)
    record = json.loads(log.read_text().splitlines()[0])
    assert record["store"] == "triage_messages" and record["deleted"] == 5 and record["deleted_ids_sha256"]
    assert "message" not in record and "Mtoto" not in log.read_text()        # the record holds no content


def test_dry_run_deletes_nothing_but_is_still_recorded(tmp_path):
    work, log = _work(tmp_path), tmp_path / "deletion_records.jsonl"
    before = (work / "fixtures/dpa/triage_messages.jsonl").read_text()
    sweeper.run(root=work, now=NOW, stores=["triage_messages"], deletion_log=log, dry_run=True)
    assert (work / "fixtures/dpa/triage_messages.jsonl").read_text() == before
    assert json.loads(log.read_text())["dry_run"] is True


def test_the_sweep_never_removes_deletion_records(tmp_path):
    work, log = _work(tmp_path), tmp_path / "deletion_records.jsonl"
    sweeper.run(root=work, now=NOW, stores=["triage_messages"], deletion_log=log)
    first = log.read_text()
    sweeper.run(root=work, now=datetime(2030, 1, 1, tzinfo=UTC), deletion_log=log)  # years later, every store
    assert log.read_text().startswith(first)                                    # append-only, nothing lost
    with pytest.raises(sweeper.ProtectedStore):
        sweeper.run(root=work, now=NOW, stores=["deletion_records"], deletion_log=log)
    evil = {"stores": {"x": {"path": str(log), "timestamp_field": "ts", "retain_days": 0, "action": "delete"}}}
    (tmp_path / "evil.json").write_text(json.dumps(evil))
    with pytest.raises(sweeper.ProtectedStore):                                 # even if retention.json is edited
        sweeper.run(root=work, now=NOW, deletion_log=log, retention_path=tmp_path / "evil.json")


def test_undated_records_are_kept_and_counted(tmp_path):
    work, log = _work(tmp_path), tmp_path / "d.jsonl"
    path = work / "fixtures/dpa/triage_messages.jsonl"
    path.write_text(path.read_text() + json.dumps({"record_id": "msg-x", "created_at": "sometime"}) + "\n")
    e = sweeper.run(root=work, now=NOW, stores=["triage_messages"], deletion_log=log)[0]
    assert e["undated"] == 1 and "msg-x" in path.read_text()


def test_sweeping_a_chained_audit_file_keeps_it_verifiable(tmp_path):
    work, log = _work(tmp_path), tmp_path / "d.jsonl"
    e = sweeper.run(root=work, now=datetime(2027, 6, 1, tzinfo=UTC), stores=["audit_logs"], deletion_log=log)[0]
    assert e["deleted"] > 0 and e["chain_anchor"]
    assert not verify_chain(work / "logs/audit.jsonl")[0]                       # the old start is gone...
    assert verify_chain(work / "logs/audit.jsonl", e["chain_anchor"])[0]       # ...the anchor proves the rest


def test_only_the_ops_role_may_sweep(tmp_path):
    for role in ("chw", "auditor", "dpo", "contractor"):
        with pytest.raises(PermissionError):
            sweeper.run(root=_work(tmp_path / role), now=NOW, deletion_log=tmp_path / "d.jsonl", role=role)


def test_rights_access_by_request_id_and_by_chw():
    household = lookup(request_id="TRG-2026-0412")
    assert len(household["records"]["triage_messages"]) == 1 and len(household["audit_lines"]) == 1
    assert household["retention_days"]["triage_messages"] == 90
    staff = lookup(chw="chw_amina")
    authored = staff["records"]["triage_messages_authored"]
    assert len(authored) == 8 and all("message" not in r for r in authored)   # patients' text is not the CHW's


def test_rights_erasure_plan_then_apply_with_deletion_record(tmp_path):
    work, log = _work(tmp_path), tmp_path / "d.jsonl"
    plan = erase(request_id="TRG-2026-0412", root=work, apply=False, deletion_log=log, ticket="DSR-7", operator="t")
    assert plan["triage_messages_to_delete"] == 1 and not plan["applied"]
    assert "TRG-2026-0412" in (work / "fixtures/dpa/triage_messages.jsonl").read_text()
    plan = erase(request_id="TRG-2026-0412", root=work, apply=True, deletion_log=log, ticket="DSR-7", operator="t")
    assert plan["applied"] and "TRG-2026-0412" not in (work / "fixtures/dpa/triage_messages.jsonl").read_text()
    records = [json.loads(x) for x in log.read_text().splitlines()]
    assert [r["reason"] for r in records] == ["erasure_request"] * 2 and records[1]["deleted"] == 1
    assert "TRG-2026-0412" not in log.read_text()


def test_rights_cli_runs_against_the_static_fixture_and_refuses_other_roles():
    ok = subprocess.run([sys.executable, "-m", "dpa.rights_lookup", "access", "--request-id", "TRG-2026-0101"],
                        cwd=ROOT, capture_output=True, text=True)
    assert ok.returncode == 0 and json.loads(ok.stdout)["records"]["triage_messages"][0]["urgency"] == "EMERGENCY"
    no = subprocess.run([sys.executable, "-m", "dpa.rights_lookup", "access", "--chw", "chw_amina", "--role", "ops"],
                        cwd=ROOT, capture_output=True, text=True)
    assert no.returncode == 2 and "REFUSED" in no.stderr


def test_committed_deletion_records_hold_no_personal_data():
    text = (ROOT / "dpa" / "deletion_records.jsonl").read_text()
    lines = [json.loads(x) for x in text.splitlines()]
    assert lines and {"triage_messages"} <= {x["store"] for x in lines}
    assert not re.search(r"TRG-\d|Mtoto|homa|chw_baraka", text)

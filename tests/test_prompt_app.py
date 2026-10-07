"""The API reports its versions at runtime and serves the pinned prompts through the canary flag."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from afyaplus.settings import get_settings
from prompt_loader import load_config, load_pin

CHW = {"username": "chw_amina", "password": "amina-chw-2026"}
AUDITOR = {"username": "auditor_wanjiru", "password": "wanjiru-audit-2026"}
CASE = {"message": "Child with fever for 2 days, still eating and playing normally.", "patient_age_years": 1}


@pytest.fixture
def client():
    from prompt_app import app

    with TestClient(app) as c:
        yield c


def _token(client, creds) -> dict:
    r = client.post("/token", data=creds)
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_health_reports_versions_and_shas(client):
    body = client.get("/health").json()
    config, pin = load_config(), load_pin()
    assert body["release"] == config.release == body["prompt_bundle"]
    assert body["prompt_active"]["version"] == config.prompt_version
    assert body["prompt_active"]["sha256"] == pin.prompts[config.prompt_version].sha256
    assert body["mcp_version"] == config.mcp.version and body["model_version"] == config.model.model_version
    assert len(body["config_sha256"]) == 64 and body["rollback_override"] is False


def test_ready_probes_the_bundled_mcp_server(client):
    r = client.get("/ready")
    assert r.status_code == 200, r.text
    assert r.json()["mcp"]["version"] == load_config().mcp.version


def test_triage_replays_the_recorded_answer_and_labels_the_version(client):
    r = client.post("/v1/triage", json=CASE, headers=_token(client, CHW))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["backend"] == "replay" and body["prompt_version"] == load_config().prompt_version
    assert r.headers["X-Prompt-Version"] == body["prompt_version"]
    assert r.headers["X-Prompt-SHA"] == body["prompt_sha256"][:12]
    assert r.headers["X-Release"] == load_config().release


def test_unrecorded_message_falls_back_to_rules(client):
    r = client.post("/v1/triage", json={"message": "Brand new message never recorded, child has convulsions."},
                    headers=_token(client, CHW))
    assert r.json()["backend"] == "rules(replay-miss)" and r.json()["urgency"] == "EMERGENCY"


def test_auth_still_enforced(client):
    assert client.post("/v1/triage", json=CASE).status_code == 401
    assert client.post("/v1/triage", json=CASE, headers=_token(client, AUDITOR)).status_code == 403
    assert client.get("/v1/metrics", headers=_token(client, CHW)).status_code == 403


def test_metrics_count_triage_by_prompt_version(client):
    headers = _token(client, CHW)
    for i in range(3):
        client.post("/v1/triage", json=CASE, headers={**headers, "X-Routing-Key": f"device-{i}"})
    rows = client.get("/v1/metrics", headers=_token(client, AUDITOR)).json()["rows"]
    assert sum(r["calls"] for r in rows if r["component"] == "triage") == 3


def test_prompt_version_rollback_lever(monkeypatch):
    version = next(iter(load_pin().prompts))
    monkeypatch.setenv("PROMPT_VERSION", version)
    get_settings.cache_clear()
    from prompt_app import app

    with TestClient(app) as c:
        body = c.get("/health").json()
        assert body["rollback_override"] is True
        assert body["prompt_active"]["version"] == version and body["prompt_candidate"] is None


def test_candidate_serves_its_share_of_keys_in_staging(monkeypatch):
    config = load_config()
    if not config.candidate.prompt_version:
        pytest.skip("no candidate prompt configured in this release")
    monkeypatch.setenv("DEPLOY_ENV", "staging")
    get_settings.cache_clear()
    from prompt_app import app

    with TestClient(app) as c:
        headers = _token(c, CHW)
        arms = [c.post("/v1/triage", json=CASE, headers={**headers, "X-Routing-Key": f"k{i}"}).headers["X-Prompt-Arm"]
                for i in range(300)]
        share = arms.count("candidate") / len(arms)
        assert abs(share - config.candidate.traffic_percent / 100) < 0.06
        health = c.get("/health").json()
        assert health["prompt_candidate"]["version"] == config.candidate.prompt_version
        assert health["prompt_candidate"]["traffic_percent"] == config.candidate.traffic_percent


def test_candidate_never_serves_in_dev(client):
    if not load_config().candidate.prompt_version:
        pytest.skip("no candidate prompt configured in this release")
    headers = _token(client, CHW)
    arms = {client.post("/v1/triage", json=CASE, headers={**headers, "X-Routing-Key": f"k{i}"}).headers["X-Prompt-Arm"]
            for i in range(100)}
    assert arms == {"active"}

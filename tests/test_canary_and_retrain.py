"""Canary go / no-go and the retrain stub."""

from __future__ import annotations

from datetime import date

from scripts.canary_gate import decide
from scripts.retrain_stub import next_version


def _row(arm, version, calls, errors, component="triage"):
    return {"component": component, "name": "triage", "version": version, "arm": arm, "calls": calls,
            "errors": errors, "error_rate": errors / calls, "p50_ms": 1, "p95_ms": 2}


def test_go_when_both_arms_healthy():
    go, _ = decide([_row("active", "1.2.0", 270, 1), _row("candidate", "1.3.0-candidate", 30, 0)],
                   {"ready": True}, 0.10, 20)
    assert go


def test_no_go_when_candidate_errors_exceed_threshold():
    go, checks = decide([_row("active", "1.2.0", 270, 0), _row("candidate", "1.3.0-candidate", 30, 4)],
                        {"ready": True}, 0.10, 20)
    assert not go and ("error_rate[candidate]", False) in [(n, ok) for n, ok, _ in checks]


def test_error_rate_must_stay_strictly_under_threshold():
    go, _ = decide([_row("active", "1.2.0", 270, 0), _row("candidate", "1.3.0-candidate", 60, 6)],
                   {"ready": True}, 0.10, 20)
    assert not go  # 6/60 = 0.10 is not "under 0.10"


def test_no_go_without_enough_candidate_traffic():
    go, _ = decide([_row("active", "1.2.0", 290, 0), _row("candidate", "1.3.0-candidate", 5, 0)],
                   {"ready": True}, 0.10, 20)
    assert not go


def test_no_go_when_mcp_handshake_fails():
    go, _ = decide([_row("active", "1.2.0", 100, 0)], {"ready": False, "failed_checks": ["required_tools"]},
                   0.10, 20)
    assert not go


def test_probe_traffic_is_not_counted_as_production_mcp_errors():
    rows = [_row("active", "1.2.0", 100, 0), _row("", "1.0.0", 10, 10, component="mcp-probe")]
    assert decide(rows, {"ready": True}, 0.10, 20)[0]


def test_retrain_stub_calendar_versioning():
    assert next_version((2026, 10, 0), date(2026, 10, 7)) == "2026.10.1"
    assert next_version((2026, 10, 3), date(2026, 11, 2)) == "2026.11.0"

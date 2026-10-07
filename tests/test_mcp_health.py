"""The MCP probe passes on the live server and the contract stub, and fails with the documented exit codes."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from prompt_loader import ROOT, load_config

PROBE = [sys.executable, str(ROOT / "scripts" / "check_mcp_health.py")]


def _run(*args: str, fault: str = "none", timeout: float = 60) -> subprocess.CompletedProcess:
    env = {**__import__("os").environ, "MCP_STUB_FAULT": fault}
    return subprocess.run([*PROBE, *args], capture_output=True, text=True, timeout=timeout, env=env, cwd=ROOT)


def test_live_server_is_healthy(tmp_path):
    out = tmp_path / "probe.json"
    proc = _run("--json", str(out))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(out.read_text())
    assert report["server"]["version"] == load_config().mcp.version
    assert {c["check"] for c in report["checks"]} >= {"handshake", "version", "required_tools", "schema_lock",
                                                      "smoke_call", "error_contract", "resource"}


def test_stub_is_healthy_with_same_contract():
    proc = _run("--target", "stub")
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("fault,check", [
    ("drop_tool", "required_tools"),
    ("wrong_version", "version"),
    ("schema_drift", "schema_lock"),
    ("tool_error", "smoke_call"),
])
def test_assertion_failures_exit_1(fault, check):
    proc = _run("--target", "stub", fault=fault)
    assert proc.returncode == 1, proc.stdout
    assert f"| {check} | **FAIL**" in proc.stdout


@pytest.mark.parametrize("fault", ["crash", "hang"])
def test_unreachable_server_exits_2(fault):
    proc = _run("--target", "stub", "--timeout", "3", fault=fault)
    assert proc.returncode == 2, proc.stdout
    assert "UNREACHABLE" in proc.stdout


def test_lock_matches_server_version():
    from check_prompt_pin import mcp_server_version

    lock = json.loads((ROOT / "mcp_schema.lock.json").read_text())
    assert lock["version"] == mcp_server_version() == load_config().mcp.version

"""Post-deploy check 3: canary go / no-go, read from the shared counters (tool_metrics.py).

    python scripts/canary_gate.py --url http://127.0.0.1:8080      # deployed service: /v1/metrics + /ready
    python scripts/canary_gate.py --db logs/tool_metrics.db         # straight from a counter store file

GO when all of these hold (thresholds in config/triage.yaml `canary:`):
  - every prompt arm's triage error rate stays under error_rate_max (strictly below 0.10)
  - the candidate arm, if one is serving, has at least min_requests requests (there is enough evidence)
  - production MCP tool calls (component "mcp") have an error rate under error_rate_max
  - with --url: /ready is 200, i.e. the MCP handshake lists the required tools (check_stock)
Exit 0 = GO, 1 = NO-GO. The deploy stub sets CANDIDATE_PERCENT=0 on NO-GO (automatic canary rollback).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prompt_loader import load_config  # noqa: E402
from tool_metrics import ToolMetrics  # noqa: E402


def _http_rows(url: str) -> tuple[list[dict], dict]:
    data = urllib.parse.urlencode({"username": os.environ.get("AUDIT_USER", "auditor_wanjiru"),
                                   "password": os.environ.get("AUDIT_PASSWORD", "wanjiru-audit-2026")}).encode()
    with urllib.request.urlopen(f"{url}/token", data=data, timeout=30) as r:  # noqa: S310
        token = json.load(r)["access_token"]
    req = urllib.request.Request(f"{url}/v1/metrics", headers={"Authorization": f"Bearer {token}"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
        rows = json.load(r)["rows"]
    try:
        with urllib.request.urlopen(f"{url}/ready", timeout=30) as r:  # noqa: S310
            ready = json.load(r)
    except urllib.error.HTTPError as exc:
        ready = json.loads(exc.read() or b"{}") | {"ready": False}
    return rows, ready


def decide(rows: list[dict], ready: dict | None, error_rate_max: float, min_requests: int) -> tuple[bool, list]:
    checks = []
    arms: dict[str, dict] = {}
    for r in rows:
        if r["component"] == "triage":
            a = arms.setdefault(r["arm"], {"calls": 0, "errors": 0, "versions": set()})
            a["calls"] += r["calls"]
            a["errors"] += r["errors"]
            a["versions"].add(r["version"])
    if not arms:
        checks.append(("traffic", False, "no triage requests recorded"))
    for arm, a in sorted(arms.items()):
        rate = a["errors"] / a["calls"]
        checks.append((f"error_rate[{arm}]", rate < error_rate_max,
                       f"{a['errors']}/{a['calls']} = {rate:.3f} (must be < {error_rate_max}) "
                       f"prompt {', '.join(sorted(a['versions']))}"))
    if "candidate" in arms:
        n = arms["candidate"]["calls"]
        checks.append(("candidate_volume", n >= min_requests, f"{n} requests (min {min_requests})"))
    mcp = [r for r in rows if r["component"] == "mcp"]
    if mcp:
        calls, errors = sum(r["calls"] for r in mcp), sum(r["errors"] for r in mcp)
        checks.append(("error_rate[mcp tools]", errors / calls < error_rate_max,
                       f"{errors}/{calls} = {errors / calls:.3f} (must be < {error_rate_max})"))
    if ready is not None:
        checks.append(("mcp_handshake", bool(ready.get("ready")),
                       f"/ready: {ready.get('mcp')} failed={ready.get('failed_checks')}"))
    return all(ok for _, ok, _ in checks), checks


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--url")
    src.add_argument("--db")
    args = ap.parse_args(argv)
    canary = load_config().canary
    if args.url:
        rows, ready = _http_rows(args.url)
    else:
        rows, ready = ToolMetrics(args.db).summary(), None
    go, checks = decide(rows, ready, canary.error_rate_max, canary.min_requests)
    print("## Canary gate\n")
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<24} {detail}")
    print(f"\nCANARY {'GO' if go else 'NO-GO'}")
    return 0 if go else 1


if __name__ == "__main__":
    sys.exit(main())

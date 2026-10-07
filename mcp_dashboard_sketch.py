"""MCP + prompt dashboard sketch (Thursday Lab 2): one screen an on-call engineer reads at 01:00.

    python mcp_dashboard_sketch.py --url http://127.0.0.1:8080     # a running deployment (/health, /v1/metrics)
    python mcp_dashboard_sketch.py --db logs/tool_metrics.db       # straight from the counter store

Panels: release and versions; triage traffic per prompt arm (calls, error rate, p95); MCP tool calls per tool
and server version; the canary verdict with the same thresholds as scripts/canary_gate.py. Output is Markdown,
so it renders in a terminal, a GitHub job summary or a PR comment. A real deployment would put the same
queries in Grafana; this sketch fixes what the panels are and where their numbers come from.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request

from prompt_loader import load_bundle, load_config
from scripts.canary_gate import _http_rows, decide
from tool_metrics import ToolMetrics


def render(health: dict, rows: list[dict], ready: dict | None) -> str:
    canary = load_config().canary
    go, checks = decide(rows, ready, canary.error_rate_max, canary.min_requests)
    cand = health.get("prompt_candidate") or {}
    out = [
        f"# AfyaPlus triage: release {health['release']} ({health.get('environment', '?')})",
        "",
        f"- prompt active **{health['prompt_active']['version']}** `{health['prompt_active']['sha256'][:12]}`"
        f" ({health['prompt_active'].get('traffic_percent', 100)}% of keys)"
        + (f"; candidate **{cand['version']}** `{cand['sha256'][:12]}` ({cand.get('traffic_percent')}%)"
           if cand else "; no candidate")
        + ("; **ROLLBACK OVERRIDE ON**" if health.get("rollback_override") else ""),
        f"- model {health['model']} (model_version {health['model_version']}); "
        f"MCP {health['mcp_server']} {health['mcp_version']}; config `{health['config_sha256'][:12]}`",
        "",
        "## Triage by prompt arm",
        "",
        "| arm | prompt | calls | errors | error rate | p50 ms | p95 ms |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    out += [f"| {r['arm']} | {r['version']} | {r['calls']} | {r['errors']} | {r['error_rate']:.3f} | "
            f"{r['p50_ms']} | {r['p95_ms']} |" for r in rows if r["component"] == "triage"] or ["| - | - | 0 |"]
    out += ["", "## MCP tools", "", "| source | tool | server | calls | errors | error rate | p95 ms |",
            "|---|---|---|---:|---:|---:|---:|"]
    out += [f"| {r['component']} | {r['name']} | {r['version']} | {r['calls']} | {r['errors']} | "
            f"{r['error_rate']:.3f} | {r['p95_ms']} |" for r in rows if r["component"].startswith("mcp")] \
        or ["| - | - | - | 0 |"]
    out += ["", "`mcp-probe` rows are health-probe traffic; one deliberate bad call per probe checks the error "
                "contract, so their error rate is expected to be non-zero and is excluded from the canary gate.",
            "", f"## Canary: **{'GO' if go else 'NO-GO'}**", ""]
    out += [f"- {'ok ' if ok else 'FAIL'} {name}: {detail}" for name, ok, detail in checks]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--url")
    src.add_argument("--db")
    args = ap.parse_args(argv)
    if args.url:
        with urllib.request.urlopen(f"{args.url}/health", timeout=30) as r:  # noqa: S310 - our own service
            health = json.load(r)
        rows, ready = _http_rows(args.url)
    else:
        b = load_bundle()
        health = {"release": b.config.release, "environment": "local",
                  "prompt_active": {"version": b.active.version, "sha256": b.active.sha256},
                  "prompt_candidate": {"version": b.candidate.version, "sha256": b.candidate.sha256,
                                       "traffic_percent": b.config.candidate.traffic_percent}
                  if b.candidate else None,
                  "model": b.config.model.name, "model_version": b.config.model.model_version,
                  "mcp_server": b.config.mcp.server, "mcp_version": b.config.mcp.version,
                  "config_sha256": b.config_sha256}
        rows, ready = ToolMetrics(args.db).summary(), None
    sys.stdout.write(render(health, rows, ready))
    return 0


if __name__ == "__main__":
    sys.exit(main())

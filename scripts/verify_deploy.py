"""Post-deploy check 1: the running container reports exactly the versions this commit says it should.

Compares GET /health with the repo's own config/triage.yaml, prompts/pin.json and MCP version (honouring a
PROMPT_VERSION rollback in the environment), and requires GET /ready (the in-app MCP probe) to be 200.
Exit 0 when everything matches, 1 on any mismatch.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prompt_loader import load_bundle  # noqa: E402


def get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=30) as r:  # noqa: S310 - our own service URL
            return r.status, json.load(r)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    args = ap.parse_args(argv)
    override = os.environ.get("PROMPT_VERSION") or None
    expected = load_bundle(prompt_version_override=override)
    status, h = get(f"{args.url}/health")
    checks = [
        ("health_200", status == 200, f"status {status}"),
        ("release", h.get("release") == expected.config.release, f"{h.get('release')} vs {expected.config.release}"),
        ("prompt_bundle", h.get("prompt_bundle") == expected.pin.bundle_version, f"{h.get('prompt_bundle')}"),
        ("prompt_active_sha", (h.get("prompt_active") or {}).get("sha256") == expected.active.sha256,
         f"{(h.get('prompt_active') or {}).get('version')} {(h.get('prompt_active') or {}).get('sha256', '')[:12]} "
         f"vs {expected.active.version} {expected.active.short_sha}"),
        ("prompt_candidate", ((h.get("prompt_candidate") or {}).get("sha256")) ==
         (expected.candidate.sha256 if expected.candidate else None),
         f"{(h.get('prompt_candidate') or {}).get('version')} vs "
         f"{expected.candidate.version if expected.candidate else None}"),
        ("config_sha", h.get("config_sha256") == expected.config_sha256, f"{str(h.get('config_sha256'))[:12]}"),
        ("mcp_version", h.get("mcp_version") == expected.config.mcp.version, f"{h.get('mcp_version')}"),
        ("model_version", h.get("model_version") == expected.config.model.model_version, f"{h.get('model_version')}"),
        ("rollback_flag", h.get("rollback_override") == bool(override), f"{h.get('rollback_override')}"),
    ]
    status, r = get(f"{args.url}/ready")
    checks.append(("ready_mcp_probe", status == 200 and r.get("ready") is True,
                   f"status {status}, mcp {r.get('mcp')}, failed {r.get('failed_checks')}"))
    print(f"## Deployed versions ({h.get('image_tag')}, git {h.get('git_sha')}, env {h.get('environment')})\n")
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<18} {detail}")
    failed = [n for n, ok, _ in checks if not ok]
    print(f"\nDEPLOY VERIFY {'FAILED: ' + ', '.join(failed) if failed else 'OK'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

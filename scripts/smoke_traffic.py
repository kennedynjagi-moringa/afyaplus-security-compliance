"""Post-deploy check 2: send golden-set messages through the deployed API from many routing keys.

Every response must be 200 and carry the prompt SHA of the arm that served it. Prints the arm split.
The messages are golden cases, so the replay backend answers from the recordings (and costs $0).
Credentials are the demo users documented in afyaplus/users.py (override with SMOKE_USER / SMOKE_PASSWORD).
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def login(url: str, user: str, password: str) -> str:
    data = urllib.parse.urlencode({"username": user, "password": password}).encode()
    with urllib.request.urlopen(f"{url}/token", data=data, timeout=30) as r:  # noqa: S310
        return json.load(r)["access_token"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--n", type=int, default=600)
    args = ap.parse_args(argv)
    token = login(args.url, os.environ.get("SMOKE_USER", "chw_amina"),
                  os.environ.get("SMOKE_PASSWORD", "amina-chw-2026"))
    cases = [json.loads(line) for line in (ROOT / "evals" / "golden.jsonl").read_text().splitlines() if line]
    arms: collections.Counter = collections.Counter()
    shas: dict[str, set[str]] = collections.defaultdict(set)
    backends: collections.Counter = collections.Counter()
    failures = 0
    for i in range(args.n):
        case = cases[i % len(cases)]
        body = json.dumps({"message": case["message"], "patient_age_years": case["patient_age_years"]}).encode()
        req = urllib.request.Request(f"{args.url}/v1/triage", data=body, method="POST", headers={  # noqa: S310
            "Authorization": f"Bearer {token}", "Content-Type": "application/json",
            "X-Routing-Key": f"chw-device-{i:04d}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
                out = json.load(r)
                ok_header = r.headers["X-Prompt-SHA"] == out["prompt_sha256"][:12]
        except urllib.error.HTTPError as exc:
            failures += 1
            print(f"request {i}: HTTP {exc.code}", file=sys.stderr)
            continue
        failures += not ok_header
        arms[f"{out['arm']} {out['prompt_version']}"] += 1
        shas[out["arm"]].add(out["prompt_sha256"][:12])
        backends[out["backend"]] += 1
    print(f"smoke traffic: {args.n} requests, {failures} failures")
    for arm, n in sorted(arms.items()):
        print(f"  {arm:<28} {n:>4} ({n / args.n:.0%})  sha {', '.join(sorted(shas[arm.split()[0]]))}")
    print(f"  backends: {dict(backends)}")
    one_sha_per_arm = all(len(v) == 1 for v in shas.values())
    print(f"SMOKE {'OK' if failures == 0 and one_sha_per_arm else 'FAILED'}")
    return 0 if failures == 0 and one_sha_per_arm else 1


if __name__ == "__main__":
    sys.exit(main())

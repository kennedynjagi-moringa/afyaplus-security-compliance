"""Release alignment check (Deliverable 1). Runs first in CI, and again in the deploy job against the tag.

    python check_prompt_pin.py                                   # repo consistency
    python check_prompt_pin.py --tag v1.2.0 --image-tag afyaplus-triage:1.2.0

One release number has to line up everywhere:
  git tag v<release>  ==  config/triage.yaml release  ==  prompts/pin.json bundle_version  ==  image tag <release>
and the parts the release ships have to agree with each other:
  - every prompts/*.txt is pinned, with the right SHA-256 and a filename that matches its version
  - the active and candidate prompts in config are pinned with the matching status
  - every served prompt has a live recording (fixtures/responses/<sha12>.json), i.e. it was evaluated
  - MCP: SERVER_VERSION in logistics_mcp_versioned.py == config mcp.version == mcp_schema.lock.json version
  - CHANGELOG.md has a section for the release
Exit 0 when all checks pass, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

from prompt_loader import (
    CONFIG_PATH,
    PIN_PATH,
    PROMPT_FILE,
    PROMPTS_DIR,
    ROOT,
    PromptIntegrityError,
    load_config,
    load_pin,
    load_prompt,
)
from triage_engine import fixture_path

WAIVER_WARN_DAYS = 14


def mcp_server_version(path: Path = ROOT / "logistics_mcp_versioned.py") -> str | None:
    """Read SERVER_VERSION without importing the server (importing would load the data and logging)."""
    m = re.search(r'^SERVER_VERSION\s*=\s*"([^"]+)"', path.read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else None


def run(tag: str | None, image_tag: str | None, today: date) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        results.append((name, ok, detail))

    try:
        config = load_config(CONFIG_PATH)
        pin = load_pin(PIN_PATH)
    except Exception as exc:  # noqa: BLE001 - a config that doesn't parse is a failed check
        check("config_and_pin_parse", False, f"{type(exc).__name__}: {exc}")
        return results
    check("config_and_pin_parse", True, f"{CONFIG_PATH.relative_to(ROOT)}, {PIN_PATH.relative_to(ROOT)}")
    release = config.release
    check("bundle_matches_release", pin.bundle_version == release,
          f"pin.json bundle_version={pin.bundle_version}, config release={release}")
    if tag is not None:
        check("git_tag_matches_release", tag == f"v{release}", f"tag={tag}, expected v{release}")
    if image_tag is not None:
        check("image_tag_matches_release", image_tag.rsplit(":", 1)[-1] == release,
              f"image={image_tag}, expected tag {release}")

    on_disk = {p.name for p in PROMPTS_DIR.glob("*.txt")}
    pinned_files = {e.file for e in pin.prompts.values()}
    check("no_unpinned_prompts", on_disk <= pinned_files,
          "every prompts/*.txt is pinned" if on_disk <= pinned_files
          else f"unpinned: {', '.join(sorted(on_disk - pinned_files))}")
    for version, entry in pin.prompts.items():
        m = PROMPT_FILE.match(entry.file)
        if not m or m["version"] != version:
            check(f"prompt_{version}", False, f"file {entry.file} does not match version {version}")
            continue
        try:
            p = load_prompt(version, pin)
            check(f"prompt_{version}", True, f"{entry.file} sha256 {p.short_sha} ({entry.status})")
        except PromptIntegrityError as exc:
            check(f"prompt_{version}", False, str(exc))

    active = pin.prompts.get(config.prompt_version)
    check("active_prompt_pinned", active is not None and active.status == "active",
          f"config prompt_version={config.prompt_version}, pin status="
          f"{active.status if active else 'NOT PINNED'}")
    others_active = [v for v, e in pin.prompts.items() if e.status == "active" and v != config.prompt_version]
    check("single_active_prompt", not others_active,
          "one active prompt" if not others_active else f"also marked active: {', '.join(others_active)}")
    cand = config.candidate.prompt_version
    if cand:
        entry = pin.prompts.get(cand)
        check("candidate_prompt_pinned", entry is not None and entry.status == "candidate",
              f"candidate={cand} at {config.candidate.traffic_percent}% in {list(config.candidate.environments)}, "
              f"pin status={entry.status if entry else 'NOT PINNED'}")
    served = [v for v in (config.prompt_version, cand) if v and v in pin.prompts]
    for version in served:
        sha = pin.prompts[version].sha256
        path = fixture_path(sha)
        check(f"recording_{version}", path.is_file(),
              f"{path.relative_to(ROOT)}" + ("" if path.is_file() else
                                            f" missing: run eval_prompts.py --record --prompt-version {version}"))
        waiver = pin.prompts[version].waiver
        if waiver:
            days = (waiver.expires - today).days
            check(f"waiver_{version}", days >= 0,
                  f"{', '.join(waiver.gates)} waived by {', '.join(waiver.approved_by)} until {waiver.expires} "
                  f"({days} days left{'; RENEW OR PROMOTE SOON' if 0 <= days <= WAIVER_WARN_DAYS else ''})")

    server_version = mcp_server_version()
    lock_path = ROOT / "mcp_schema.lock.json"
    lock = json.loads(lock_path.read_text()) if lock_path.is_file() else {}
    check("mcp_version_aligned",
          server_version == config.mcp.version == lock.get("version") and lock.get("server") == config.mcp.server,
          f"SERVER_VERSION={server_version}, config mcp.version={config.mcp.version}, "
          f"lock={lock.get('server')} {lock.get('version')}")
    missing_tools = [t for t in config.mcp.required_tools if t not in lock.get("tools", {})]
    check("mcp_required_tools_locked", not missing_tools,
          f"required {list(config.mcp.required_tools)}" + (f"; not in lock: {missing_tools}" if missing_tools else ""))

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8") if (ROOT / "CHANGELOG.md").is_file() else ""
    check("changelog_has_release", f"## [{release}]" in changelog, f"CHANGELOG.md section ## [{release}]")
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check that release, prompt, config and MCP versions line up.")
    ap.add_argument("--tag", help="git tag being released, e.g. v1.2.0")
    ap.add_argument("--image-tag", help="image reference being built, e.g. afyaplus-triage:1.2.0")
    args = ap.parse_args(argv)
    results = run(args.tag, args.image_tag, date.today())
    width = max(len(n) for n, _, _ in results)
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail}")
    failed = [n for n, ok, _ in results if not ok]
    print(f"\nRELEASE ALIGNMENT {'FAILED: ' + ', '.join(failed) if failed else 'OK'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

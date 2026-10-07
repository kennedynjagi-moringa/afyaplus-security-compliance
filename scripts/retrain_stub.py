"""Retrain job, stubbed: no GPU, so no weights are trained (fallback declared in the README).

What it does, so the pipeline shape is the same as a real retrain:
  1. bumps model.model_version in config/triage.yaml (calendar version YYYY.MM.N: N+1 within a month,
     YYYY.MM.0 in a new month), editing the line in place so the comments survive;
  2. adds a CHANGELOG line under [Unreleased] that says plainly that no weights were trained;
  3. leaves model.name (the pinned gpt-4o-mini snapshot) alone. The recordings are keyed on the model
     call settings, so they stay valid, and the eval gate still runs on the result.
A real retrain would also change model.name to the new fine-tune id. The eval gate would then refuse the old
recordings as stale until someone re-records them, so a retrained model can't ship unevaluated.

    python scripts/retrain_stub.py [--dry-run]
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "triage.yaml"
CHANGELOG = ROOT / "CHANGELOG.md"
LINE = re.compile(r"^(\s*model_version:\s*)(\d{4})\.(\d{1,2})\.(\d+)(.*)$", re.M)


def next_version(current: tuple[int, int, int], today: date) -> str:
    year, month, n = current
    return f"{year}.{month:02d}.{n + 1}" if (year, month) == (today.year, today.month) \
        else f"{today.year}.{today.month:02d}.0"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    text = CONFIG.read_text(encoding="utf-8")
    m = LINE.search(text)
    if not m:
        print("model_version line not found in config/triage.yaml", file=sys.stderr)
        return 1
    old = f"{m[2]}.{int(m[3]):02d}.{m[4]}"
    new = next_version((int(m[2]), int(m[3]), int(m[4])), date.today())
    note = (f"- Retrain stub: model_version {old} -> {new}. No weights were trained (no GPU); the base model "
            f"snapshot is unchanged, so the recorded eval responses remain valid.")
    print(f"retrain stub: model_version {old} -> {new} (no weights trained)")
    if args.dry_run:
        return 0
    CONFIG.write_text(LINE.sub(lambda mm: f"{mm[1]}{new}{mm[5]}", text, count=1), encoding="utf-8")
    log = CHANGELOG.read_text(encoding="utf-8")
    if "## [Unreleased]" not in log:
        print("CHANGELOG.md has no [Unreleased] section", file=sys.stderr)
        return 1
    CHANGELOG.write_text(log.replace("## [Unreleased]\n", f"## [Unreleased]\n\n{note}\n", 1), encoding="utf-8")
    print(note)
    return 0


if __name__ == "__main__":
    sys.exit(main())

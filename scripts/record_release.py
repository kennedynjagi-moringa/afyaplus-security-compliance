"""Write a built release image into the runbook's release ledger (image ID, labels, git SHA). Idempotent."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNBOOK = ROOT / "runbook.md"
START, END = "<!-- release-ledger:start -->", "<!-- release-ledger:end -->"


def main(image: str) -> int:
    info = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[0]
    labels = info["Config"]["Labels"]
    revision = labels["org.opencontainers.image.revision"]
    row = (f"| {date.today().isoformat()} | `{image}` | `{info['Id']}` | `{revision}` "
           f"| {labels['ai.afyaplus.prompt.active']} (`{labels['ai.afyaplus.prompt.active.sha256'][:12]}`) "
           f"| {labels['ai.afyaplus.prompt.candidate']} | {labels['ai.afyaplus.mcp.version']} "
           f"| {labels['ai.afyaplus.model.version']} |")
    text = RUNBOOK.read_text(encoding="utf-8")
    head, rest = text.split(START, 1)
    body, tail = rest.split(END, 1)
    lines = [ln for ln in body.strip("\n").splitlines() if ln.strip() and f"`{image}`" not in ln]
    lines.append(row)
    RUNBOOK.write_text(head + START + "\n\n" + "\n".join(lines) + "\n\n" + END + tail, encoding="utf-8")
    print(f"recorded {image} {info['Id'][:19]} in runbook.md release ledger")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))

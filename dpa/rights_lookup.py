"""Data-subject rights stub (Deliverable 4): access and erasure requests against the static fixture.

    python -m dpa.rights_lookup access  --request-id TRG-2026-0412     # a household asks what we hold
    python -m dpa.rights_lookup access  --chw chw_amina                # a CHW asks what we hold about them
    python -m dpa.rights_lookup erasure --request-id TRG-2026-0412     # plan only, nothing deleted
    python -m dpa.rights_lookup erasure --request-id TRG-2026-0412 --apply --root dpa/work

The process around it (who verifies identity, who decides, how fast) is dpa/rights_process.md. This tool is
step 3 of that process: find everything, report it, and for erasure delete it and record the deletion.

Subjects
  --request-id  the reference a CHW gets back from POST /v1/triage (request_id) and can give to the household.
                Access: the stored record as held, to be released only after identity is verified.
  --chw         a staff member. Access: which records they authored and their audit lines, but NOT the
                message text, because that is about patients, not about the CHW.
Erasure deletes the triage_messages record and writes a deletion record (reason erasure_request). Audit lines
are kept: they hold no message text, only a keyed hash, and are needed to show who did what. The report says
so, so the answer to the household is honest about what remains.

Stores: by default the static synthetic fixture (fixtures/dpa/); production would point the same code at the
QA store and the audit volume. Runs as the dpo role (security/rbac.py rights.lookup / rights.erase).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from dpa.retention_sweeper import DELETION_LOG, append_records
from security import rbac

ROOT = Path(__file__).resolve().parent.parent
STORES = {"triage_messages": "fixtures/dpa/triage_messages.jsonl", "audit_logs": "fixtures/dpa/audit.jsonl"}
INVENTORY = ROOT / "dpa" / "data_inventory.json"
RETENTION = ROOT / "dpa" / "retention.json"


def _rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def lookup(*, request_id: str | None = None, chw: str | None = None, root: Path = ROOT) -> dict:
    messages = _rows(root / STORES["triage_messages"])
    audit = _rows(root / STORES["audit_logs"])
    retention = json.loads(RETENTION.read_text())["stores"]
    inventory = {c["id"]: c for c in json.loads(INVENTORY.read_text())["categories"]}
    if request_id:
        held = [m for m in messages if m.get("request_id") == request_id]
        lines = [a for a in audit if a.get("trace_id") == request_id]
        records = {"triage_messages": held}
    else:
        authored = [m for m in messages if m.get("chw") == chw]
        lines = [a for a in audit if a.get("actor") == chw]
        records = {"triage_messages_authored": [
            {k: m[k] for k in ("request_id", "created_at", "county", "urgency")} for m in authored],
            "note": "Message text is about patients, not about the CHW, so it is not part of this report."}
    return {
        "subject": {"request_id": request_id} if request_id else {"chw": chw},
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "records": records,
        "audit_lines": [{k: a.get(k) for k in ("ts", "actor", "action", "outcome", "payload_hash")} for a in lines],
        "audit_note": "Audit lines hold a keyed hash of the redacted message, never the message itself.",
        "purposes": {k: inventory[k]["purpose"] for k in ("triage_messages", "audit_logs")},
        "retention_days": {k: retention[k]["retain_days"] for k in ("triage_messages", "audit_logs")},
        "recipients": "OpenAI (United States) only if the live model backend was on when the case was sent; "
                      "the default replay backend sends nothing outside the service.",
        "lawful_basis": "teaching label only, legal review pending (dpa/data_inventory.json)",
    }


def erase(*, request_id: str, root: Path, apply: bool, deletion_log: Path, ticket: str, operator: str) -> dict:
    path = root / STORES["triage_messages"]
    rows = _rows(path)
    keep = [r for r in rows if r.get("request_id") != request_id]
    gone = [r for r in rows if r.get("request_id") == request_id]
    plan = {
        "request_id_sha256": hashlib.sha256(request_id.encode()).hexdigest(),
        "ticket": ticket, "triage_messages_to_delete": len(gone),
        "kept_with_reason": {"audit_logs": "no message text (keyed hash only); kept for accountability under the "
                                           "audit_logs retention rule; teaching position, legal review pending"},
        "applied": False,
    }
    if gone and apply:
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep), encoding="utf-8")
        plan["applied"] = True
    append_records([{
        "run_id": "erasure-" + uuid.uuid4().hex[:12], "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "operator": operator, "store": "triage_messages", "reason": "erasure_request", "ticket": ticket,
        "dry_run": not apply, "deleted": len(gone) if apply else 0, "kept": len(keep) if apply else len(rows),
        "deleted_ids_sha256": hashlib.sha256("\n".join(sorted(r["record_id"] for r in gone)).encode()).hexdigest()
        if gone else None,
    }], deletion_log)
    return plan


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Access and erasure requests (Kenya DPA rights stub).")
    ap.add_argument("request", choices=["access", "erasure"])
    who = ap.add_mutually_exclusive_group(required=True)
    who.add_argument("--request-id")
    who.add_argument("--chw")
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--apply", action="store_true", help="erasure only: actually delete")
    ap.add_argument("--ticket", default="DSR-demo", help="the rights request ticket this run answers")
    ap.add_argument("--role", default=os.environ.get("RIGHTS_ROLE", "dpo"))
    ap.add_argument("--operator", default=os.environ.get("RIGHTS_OPERATOR", "dpo_njeri"))
    ap.add_argument("--deletion-log", type=Path, default=DELETION_LOG)
    args = ap.parse_args(argv)

    action = "rights.lookup" if args.request == "access" else "rights.erase"
    if not rbac.is_allowed(args.role, action):
        print(f"REFUSED: role '{args.role}' may not {action} (security/rbac.py)", file=sys.stderr)
        return 2
    if args.request == "access":
        print(json.dumps(lookup(request_id=args.request_id, chw=args.chw, root=args.root), indent=2,
                         ensure_ascii=False))
        return 0
    if not args.request_id:
        print("Erasure of a staff member's identifiers goes to the DPO (dpa/rights_process.md); this stub "
              "erases a household's triage record by request id.", file=sys.stderr)
        return 2
    plan = erase(request_id=args.request_id, root=args.root, apply=args.apply, deletion_log=args.deletion_log,
                 ticket=args.ticket, operator=args.operator)
    print(json.dumps(plan, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

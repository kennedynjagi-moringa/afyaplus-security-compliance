"""Retention sweeper (Deliverable 4): delete expired records and write a deletion record that is never swept.

    python -m dpa.retention_sweeper --dry-run                         # what would go, nothing deleted
    python -m dpa.retention_sweeper --root dpa/work --now 2026-10-09  # sweep a working copy (the evidence run)
    python -m dpa.retention_sweeper --store triage_messages

Rules come from dpa/retention.json. For each JSON-lines store with action "delete":
  - records whose timestamp is older than now - retain_days are removed (the file is rewritten atomically);
  - a record with no readable timestamp is KEPT and counted as `undated`, because deleting what you cannot
    date is as wrong as keeping it forever; the count makes it visible;
  - one line is appended to dpa/deletion_records.jsonl per store per run, even when nothing was deleted, so
    the record shows the job ran. It holds counts, the cutoff, and a SHA-256 over the deleted record ids:
    enough to prove later that a given record was deleted, without keeping the record.
For hash-chained audit files the deletion record also stores `chain_anchor`, the hash of the last deleted
line, so the remaining chain can still be verified (redact_then_hash.py --verify --anchor).

The deletion log is protected: it is marked never_swept, and the sweeper refuses any store that resolves to
it, whatever retention.json says. The job runs as an operations role (security/rbac.py retention.sweep).
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from security import rbac
from security.redact_then_hash import line_sha

ROOT = Path(__file__).resolve().parent.parent
RETENTION_PATH = ROOT / "dpa" / "retention.json"
DELETION_LOG = ROOT / "dpa" / "deletion_records.jsonl"


class ProtectedStore(RuntimeError):
    """Someone pointed the sweeper at the deletion log."""


def parse_ts(value) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def record_id(line: str, row: dict) -> str:
    return str(row.get("record_id") or row.get("trace_id") or line_sha(line)[:16])


def sweep_store(name: str, rule: dict, *, root: Path, now: datetime, deletion_log: Path, dry_run: bool,
                operator: str, run_id: str, reason: str = "retention") -> dict:
    path = (root / rule["path"]).resolve()
    if rule.get("action") == "never_swept" or path == deletion_log.resolve():
        raise ProtectedStore(f"{name}: {rule['path']} is the deletion log and is never swept")
    entry = {"run_id": run_id, "ts": now.isoformat(timespec="seconds"),
             "ran_at": datetime.now(UTC).isoformat(timespec="seconds"), "operator": operator, "store": name,
             "reason": reason, "rule": f"retain {rule['retain_days']} days", "dry_run": dry_run}
    if rule.get("action") != "delete":
        return entry | {"skipped": rule.get("action", "no action")}
    cutoff = now - timedelta(days=rule["retain_days"])
    entry["cutoff"] = cutoff.isoformat(timespec="seconds")
    if not path.is_file():
        return entry | {"deleted": 0, "kept": 0, "undated": 0, "note": "store file not present"}

    keep, gone, undated, last_deleted_line = [], [], 0, None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            keep.append(line)
            undated += 1
            continue
        ts = parse_ts(row.get(rule["timestamp_field"]))
        if ts is None:
            undated += 1
            keep.append(line)
        elif ts < cutoff:
            gone.append((record_id(line, row), ts))
            last_deleted_line = line
        else:
            keep.append(line)

    ids = sorted(i for i, _ in gone)
    entry |= {"deleted": len(gone), "kept": len(keep), "undated": undated,
              "deleted_ids_sha256": hashlib.sha256("\n".join(ids).encode()).hexdigest() if ids else None,
              "oldest_deleted": min(ts for _, ts in gone).isoformat() if gone else None,
              "newest_deleted": max(ts for _, ts in gone).isoformat() if gone else None}
    if rule.get("chained") and last_deleted_line:
        entry["chain_anchor"] = line_sha(last_deleted_line)
    if gone and not dry_run:
        tmp = path.with_suffix(path.suffix + ".sweep-tmp")
        tmp.write_text("".join(x + "\n" for x in keep), encoding="utf-8")
        os.replace(tmp, path)  # atomic: readers see the old file or the new one, never half
    return entry


def append_records(entries: list[dict], deletion_log: Path) -> None:
    deletion_log.parent.mkdir(parents=True, exist_ok=True)
    with deletion_log.open("a", encoding="utf-8") as fh:  # append-only; never rewritten
        for e in entries:
            fh.write(json.dumps(e, separators=(",", ":")) + "\n")


def run(*, root: Path = ROOT, now: datetime | None = None, stores: list[str] | None = None, dry_run: bool = False,
        deletion_log: Path = DELETION_LOG, retention_path: Path = RETENTION_PATH, operator: str = "ops",
        role: str = "ops") -> list[dict]:
    if not rbac.is_allowed(role, "retention.sweep"):
        raise PermissionError(f"role '{role}' may not run the retention sweep (security/rbac.py)")
    rules = json.loads(retention_path.read_text(encoding="utf-8"))["stores"]
    now = now or datetime.now(UTC)
    run_id = "sweep-" + uuid.uuid4().hex[:12]
    names = stores or [n for n, r in rules.items() if r.get("action") != "never_swept"]
    entries = [sweep_store(n, rules[n], root=root, now=now, deletion_log=deletion_log, dry_run=dry_run,
                           operator=operator, run_id=run_id) for n in names]
    append_records(entries, deletion_log)
    return entries


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Apply dpa/retention.json and record every deletion.")
    ap.add_argument("--root", type=Path, default=ROOT, help="tree the store paths are relative to")
    ap.add_argument("--now", help="ISO date/time to sweep as of (default: now, UTC)")
    ap.add_argument("--store", action="append", help="only this store (repeatable)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--operator", default=os.environ.get("SWEEP_OPERATOR", getpass.getuser()))
    ap.add_argument("--role", default=os.environ.get("SWEEP_ROLE", "ops"))
    ap.add_argument("--deletion-log", type=Path, default=DELETION_LOG)
    args = ap.parse_args(argv)
    now = parse_ts(args.now) if args.now else None
    try:
        entries = run(root=args.root, now=now, stores=args.store, dry_run=args.dry_run,
                      deletion_log=args.deletion_log, operator=args.operator, role=args.role)
    except (PermissionError, ProtectedStore) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    for e in entries:
        verb_e = "expired" if e["dry_run"] else "deleted"
        what = e.get("skipped") or f"{verb_e} {e['deleted']:>3}  kept {e['kept']:>3}  undated {e['undated']}"
        print(f"  {e['store']:<17} {e['rule']:<18} {what}" + ("  (dry run)" if e["dry_run"] else ""))
    verb = "would be deleted (dry run)" if args.dry_run else "deleted"
    print(f"run {entries[0]['run_id']}: {sum(e.get('deleted', 0) for e in entries)} records {verb}; "
          f"{len(entries)} lines appended to {args.deletion_log.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

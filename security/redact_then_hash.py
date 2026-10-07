"""Structured audit trail: redact, then hash, then write one JSON line (Deliverable 3).

    python -m security.redact_then_hash "Bearer eyJ... patient ID 12345678, call 0712 345 678"   # show both steps
    python -m security.redact_then_hash --verify logs/audit.jsonl                              # check the chain

One audit line answers: who (actor, actor_role) did what (action) to which resource, with what outcome, in
which request (trace_id). The payload itself is NOT stored. It is:
  1. redacted: bearer tokens, JWTs, API keys, emails, Kenyan phone numbers, national ID / passport / KRA PIN
     and any other long digit run are replaced with [TYPE] labels;
  2. hashed: HMAC-SHA256 of the redacted text, keyed with AUDIT_HASH_KEY (secret_refs.yaml).
So the line can prove "this exact message was received" when someone presents it, without the log holding
the message, and without the hash being a dictionary-attack target (it is keyed, and identifiers are gone
before hashing). `redactions` counts what was removed, which is useful on its own: a spike of [NATIONAL_ID]
in triage messages means CHWs are typing ID numbers and need guidance.

Each line also carries `prev`, the SHA-256 of the line before it in the same file, so deleting or editing a
line breaks the chain and `--verify` reports where. Each component writes its own file (the API to
logs/audit.jsonl, the MCP server to logs/audit_mcp.jsonl) so the chains never interleave.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import sys
import threading
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

GENESIS = "0" * 64

# Order matters: whole tokens first, so a JWT is not half-eaten by the digit rule.
REDACTIONS: list[tuple[str, re.Pattern[str]]] = [
    ("BEARER", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]+=*", re.I)),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")),
    ("API_KEY", re.compile(r"\b(sk|pk|rk)-(proj-)?[A-Za-z0-9_-]{16,}\b|\bAKIA[0-9A-Z]{16}\b")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("PHONE", re.compile(r"(?<![\w+])(?:\+?254[\s-]?|0)(?:7|1)\d{2}[\s-]?\d{3}[\s-]?\d{3}\b")),
    ("KRA_PIN", re.compile(r"\b[AP]\d{9}[A-Z]\b")),
    ("PASSPORT", re.compile(r"\b[A-Z]{1,2}\d{7}\b")),
    ("NATIONAL_ID", re.compile(r"(?<![\w.])\d{7,10}(?![\w.])")),   # ID, birth cert, NHIF/SHA numbers, any long digits
]


def redact(text: str) -> tuple[str, dict[str, int]]:
    counts: Counter[str] = Counter()
    for label, pattern in REDACTIONS:
        text, n = pattern.subn(f"[{label}]", text)
        if n:
            counts[label] += n
    return text, dict(counts)


def redact_value(value):
    """Redact every string inside a JSON-like value (used by the JSON log formatter as a safety net)."""
    if isinstance(value, str):
        return redact(value)[0]
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact_value(v) for v in value]
    return value


def hash_key_id(key: bytes) -> str:
    return "k-" + hashlib.sha256(b"key-id:" + key).hexdigest()[:8]


def payload_digest(redacted: str, key: bytes) -> str:
    return "hmac-sha256:" + hmac.new(key, redacted.encode("utf-8"), hashlib.sha256).hexdigest()


def line_sha(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


class AuditTrail:
    """Append-only, hash-chained JSON lines. Thread-safe within one process."""

    def __init__(self, path: Path, key: bytes) -> None:
        if len(key) < 32:
            raise ValueError("audit hash key must be at least 32 bytes (AUDIT_HASH_KEY)")
        self.path, self._key, self._lock = Path(path), key, threading.Lock()
        self.key_id = hash_key_id(key)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prev = self._last_line_sha()

    def _last_line_sha(self) -> str:
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return GENESIS
        last = self.path.read_text(encoding="utf-8").rstrip("\n").rsplit("\n", 1)[-1]
        return line_sha(last)

    def record(self, *, actor: str, actor_role: str, action: str, resource: str, outcome: str,
               trace_id: str = "-", payload: str | None = None, **context) -> dict:
        entry = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "trace_id": trace_id, "actor": actor or "-", "actor_role": actor_role or "-",
            "action": action, "resource": resource, "outcome": outcome,
        }
        if payload is not None:
            redacted, counts = redact(payload)
            entry |= {"payload_hash": payload_digest(redacted, self._key), "payload_chars": len(payload),
                      "redactions": counts, "hash_key_id": self.key_id}
        entry |= {k: redact_value(v) for k, v in context.items() if v is not None}
        with self._lock:
            entry["prev"] = self._prev
            line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self._prev = line_sha(line)
        return entry


def verify_chain(path: Path) -> tuple[bool, str]:
    prev = GENESIS
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        entry = json.loads(line)
        if entry.get("prev") != prev:
            return False, f"chain broken at line {n} (a line before it was edited, inserted or deleted)"
        prev = line_sha(line)
    return True, f"{n if prev != GENESIS else 0} lines, chain intact"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Show redact-then-hash on a string, or verify an audit file's chain.")
    ap.add_argument("text", nargs="*")
    ap.add_argument("--verify", type=Path)
    args = ap.parse_args(argv)
    if args.verify:
        ok, detail = verify_chain(args.verify)
        print(f"{args.verify}: {detail}")
        return 0 if ok else 1
    import os

    key = os.environ.get("AUDIT_HASH_KEY", "").encode()
    if len(key) < 32:
        print("AUDIT_HASH_KEY is not set (32+ chars); refusing to hash with a weak key", file=sys.stderr)
        return 1
    raw = " ".join(args.text)
    redacted, counts = redact(raw)
    print(json.dumps({"1_redacted": redacted, "redactions": counts,
                      "2_hash": payload_digest(redacted, key), "hash_key_id": hash_key_id(key)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

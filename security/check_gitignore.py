"""Secrets not in git (Deliverable 1). Run from the repo root; exit 0 clean, 1 on any finding.

    python -m security.check_gitignore
    python -m security.check_gitignore --history-of ../../week-8/afyaplus-llmops-cicd   # another repo's history only

Checks
  ignored          every path that may hold a secret or personal data is ignored by .gitignore
                   (git check-ignore --no-index, so it holds even for files that do not exist yet)
  not_ignored      the files that SHOULD be committed (.env.example, secret_refs.yaml) are not ignored
  not_tracked      none of the must-ignore paths is tracked anyway (ignored files can still be force-added)
  tracked_content  no tracked file contains an API key, JWT, bearer token, private key, or a real-looking
                   value assigned to one of our secret names
  history          the same scan over every line ever added in git history (all refs), so a secret that was
                   committed and later deleted is still found; if this fails, ROTATE the secret first
  compose          docker-compose.yml takes JWT_SECRET and AUDIT_HASH_KEY from ${...}, never a literal

A value counts as a placeholder (and is allowed) when it is a ${VAR} reference or contains one of the
placeholder markers listed in security/secret_refs.yaml, e.g. "change-me" or "test-secret". A finding that was
triaged as not-a-secret is listed by SHA-256 fingerprint in security/scan_allowlist.json, with the reason.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

MUST_IGNORE = [
    ".env", ".env.local", ".env.prod", ".env.staging",
    "logs/audit.jsonl", "logs/api.jsonl", "logs/mcp_tools.jsonl", "logs/tool_metrics.db",
    "logs/archive/audit-2026-10.jsonl.enc",
    "secrets/jwt_secret", "secrets/audit_kek",
    "deploy/tls/server.key", "deploy/tls/server.pem",
    "dpa/work/triage_messages.jsonl",
]
MUST_NOT_IGNORE = [".env.example", "security/secret_refs.yaml", "dpa/deletion_records.jsonl", "logs/.gitkeep"]
SECRET_NAMES = ("JWT_SECRET", "AUDIT_HASH_KEY", "AUDIT_KEK", "OPENAI_API_KEY")

PATTERNS = {
    "openai_key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "bearer_token": re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]{20,}=*"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
}
ASSIGNMENT = re.compile(r"\b(" + "|".join(SECRET_NAMES) + r")[\"']?[ \t]*[=:][ \t]*[\"']?([^\s\"',}]+)")


def git(*args: str, repo: Path = ROOT) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False).stdout


def scan_history(repo: Path, allowed: list[str]) -> tuple[list[str], int]:
    hits, commit = [], ""
    for line in git("log", "--all", "-p", "--no-color", "--format=commit %h", repo=repo).splitlines():
        if line.startswith("commit "):
            commit = line.split()[1]
        elif line.startswith("+") and not line.startswith("+++"):
            hits += [f"{commit}: {hit}" for hit in scan_text(line[1:], allowed)]
    return hits, len(git("rev-list", "--all", repo=repo).split())


def placeholders() -> list[str]:
    rules = yaml.safe_load((ROOT / "security" / "secret_refs.yaml").read_text())["placeholders"]
    return [*rules["always"], *rules["outside_dev"]]


def triaged_fingerprints() -> set[str]:
    path = ROOT / "security" / "scan_allowlist.json"
    return {e["fingerprint_sha256"] for e in json.loads(path.read_text())["entries"]} if path.is_file() else set()


_TRIAGED = triaged_fingerprints()


def scan_text(text: str, allowed: list[str]) -> list[str]:
    """Findings in `text`, each as 'kind: excerpt' with the secret itself masked."""
    found = []
    for kind, pattern in PATTERNS.items():
        for m in pattern.finditer(text):
            if hashlib.sha256(m.group(0).encode()).hexdigest() in _TRIAGED:
                continue
            found.append(f"{kind}: {m.group(0)[:10]}…")
    for m in ASSIGNMENT.finditer(text):
        value = m.group(2)
        if len(value) < 16:
            continue  # "...", "<redacted>", a flag: too short to be any of our secrets (min_length >= 20)
        if value.startswith(("${", "$", "<")) or any(p in value.lower() for p in allowed):
            continue
        if value.startswith(("os.environ", "settings.", "env.", "secrets.")) or "(" in value:
            continue  # code that reads a secret, not a secret
        found.append(f"assignment: {m.group(1)}={value[:4]}…")
    return found


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    results: list[tuple[str, bool, str]] = []
    allowed = placeholders()
    if args[:1] == ["--history-of"]:
        repo = Path(args[1]).resolve()
        hits, n = scan_history(repo, allowed)
        print(f"{'FAIL' if hits else 'PASS'}  history of {repo.name}: {n} commits, {len(hits)} findings "
              + ("; ".join(hits[:5]) + " -> ROTATE" if hits else "(nothing to rotate)"))
        return 1 if hits else 0

    for path in MUST_IGNORE:
        ok = subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=ROOT).returncode == 0
        rule = git("check-ignore", "-v", "--no-index", path).split("\t")[0] if ok else "NOT IGNORED"
        results.append((f"ignored {path}", ok, rule))
    for path in MUST_NOT_IGNORE:
        ok = subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=ROOT).returncode != 0
        results.append((f"not_ignored {path}", ok, "committable" if ok else "IGNORED but should be committed"))

    tracked = git("ls-files").splitlines()
    leaked = sorted(set(tracked) & set(MUST_IGNORE))
    results.append(("not_tracked", not leaked, f"{len(tracked)} tracked files" + (f"; TRACKED: {leaked}" if leaked
                                                                                  else ", none must-ignore")))

    content_hits = []
    for name in tracked:
        path = ROOT / name
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        content_hits += [f"{name}: {hit}" for hit in scan_text(text, allowed)]
    results.append(("tracked_content", not content_hits, "; ".join(content_hits[:5]) or "no secrets in tracked files"))

    history_hits, n_commits = scan_history(ROOT, allowed)
    results.append(("history", not history_hits,
                    "; ".join(history_hits[:5]) + " -> ROTATE these secrets" if history_hits
                    else f"{n_commits} commits scanned, no secret ever added"))

    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    env = compose["services"]["triage"]["environment"]
    bad = [k for k in ("JWT_SECRET", "AUDIT_HASH_KEY") if not str(env.get(k, "")).startswith("${")]
    results.append(("compose", not bad, "JWT_SECRET, AUDIT_HASH_KEY come from ${...}" if not bad
                    else f"literal or missing: {bad}"))

    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<46} {detail}")
    failed = sum(not ok for _, ok, _ in results)
    print(f"{len(results) - failed}/{len(results)} checks passed -> exit {1 if failed else 0}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

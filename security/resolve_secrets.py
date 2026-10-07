"""Resolve secrets from their references and fail closed (Deliverable 1).

    python -m security.resolve_secrets --check                 # the API's secrets; exit 1 if any is unusable
    python -m security.resolve_secrets --check --component mcp
    python -m security.resolve_secrets --check --component archive

The container runs `--check --component api` before uvicorn starts (Dockerfile CMD), so an image started
without JWT_SECRET exits 1 instead of serving with a default key. The API's own settings repeat the check
(afyaplus/settings.py), so `uvicorn prompt_app:app` on a laptop fails closed the same way.

Lookup order for each secret named in security/secret_refs.yaml:
  1. environment variable of the same name   (compose ${VAR}, CI secrets, a git-ignored .env)
  2. file <file_mount>/<name lowercased>      (Docker secrets; the Azure Key Vault CSI driver mounts the same way)
A live Key Vault / Secrets Manager call is deliberately not made this week (README, "Fallbacks declared").

A secret is unusable when it is missing, shorter than min_length, or contains a placeholder. Values are
never printed: the report shows the source, the length and an 8-hex SHA-256 fingerprint, which is enough to
tell two deployments' keys apart without revealing either.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
REFS_PATH = ROOT / "security" / "secret_refs.yaml"


@dataclass(frozen=True)
class Resolution:
    name: str
    required: bool
    source: str            # "env", "file:/run/secrets/x", or "-" when not found
    ok: bool
    reason: str
    length: int = 0
    fingerprint: str = ""

    @property
    def fatal(self) -> bool:
        return self.required and not self.ok


def load_refs(path: Path = REFS_PATH) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def placeholder_in(value: str, refs: dict, environment: str) -> str | None:
    rules = refs.get("placeholders", {})
    lowered = value.lower()
    banned = list(rules.get("always", [])) + ([] if environment == "dev" else list(rules.get("outside_dev", [])))
    return next((p for p in banned if p in lowered), None)


def is_required(spec: dict, component: str, env: Mapping[str, str]) -> bool:
    if component not in spec.get("required_by", []):
        return False
    when = spec.get("required_when") or {}
    return all(env.get(k, "").lower() == str(v).lower() for k, v in when.items())


def resolve_one(name: str, spec: dict, refs: dict, component: str, env: Mapping[str, str],
                file_mount: Path) -> Resolution:
    required = is_required(spec, component, env)
    environment = env.get("DEPLOY_ENV", "dev")
    value, source = env.get(name, ""), "env"
    if not value:
        path = file_mount / name.lower()
        if path.is_file():
            value, source = path.read_text(encoding="utf-8").strip(), f"file:{path}"
    if not value:
        return Resolution(name, required, "-", False, "missing")
    if len(value) < spec.get("min_length", 1):
        return Resolution(name, required, source, False, f"too short ({len(value)} < {spec['min_length']})",
                          len(value), fingerprint(value))
    hit = placeholder_in(value, refs, environment)
    if hit:
        return Resolution(name, required, source, False, f"placeholder value ('{hit}') in {environment}",
                          len(value), fingerprint(value))
    return Resolution(name, required, source, True, "ok", len(value), fingerprint(value))


def resolve(component: str = "api", env: Mapping[str, str] | None = None,
            refs_path: Path = REFS_PATH) -> list[Resolution]:
    env = os.environ if env is None else env
    refs = load_refs(refs_path)
    mount = Path(env.get("SECRETS_DIR", refs.get("vault", {}).get("file_mount", "/run/secrets")))
    return [resolve_one(name, spec, refs, component, env, mount)
            for name, spec in refs["secrets"].items() if component in spec.get("required_by", [])]


def check_value(name: str, value: str, environment: str, refs_path: Path = REFS_PATH) -> str | None:
    """Used by afyaplus/settings.py: the reason `value` is unusable for secret `name`, or None when it is fine."""
    refs = load_refs(refs_path)
    spec = refs["secrets"][name]
    if len(value) < spec.get("min_length", 1):
        return f"{name} is shorter than {spec['min_length']} characters"
    hit = placeholder_in(value, refs, environment)
    return f"{name} is a placeholder value ('{hit}'); inject the real secret" if hit else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Resolve secrets from security/secret_refs.yaml; fail closed.")
    ap.add_argument("--component", choices=["api", "mcp", "archive"], default="api")
    ap.add_argument("--check", action="store_true", help="exit 1 when a required secret is unusable")
    ap.add_argument("--quiet", action="store_true", help="print only failures (used by the container CMD)")
    args = ap.parse_args(argv)

    results = resolve(args.component)
    fatal = [r for r in results if r.fatal]
    if not args.quiet or fatal:
        print(f"secrets for component={args.component} DEPLOY_ENV={os.environ.get('DEPLOY_ENV', 'dev')}")
        for r in results:
            need = "required" if r.required else "optional"
            fp = f"len={r.length} sha256:{r.fingerprint}" if r.fingerprint else ""
            print(f"  {'OK  ' if r.ok else 'FAIL' if r.required else 'skip'}  {r.name:<15} {need:<8} "
                  f"source={r.source:<6} {r.reason:<10} {fp}")
    if fatal:
        print(f"REFUSING TO START: {', '.join(r.name for r in fatal)} unusable (fail closed, exit 1)",
              file=sys.stderr)
        return 1 if args.check else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Encryption in transit (Deliverable 1): every outbound partner URL must be HTTPS.

    python -m security.transit_policy                                  # check config/partners.yaml for DEPLOY_ENV
    python -m security.transit_policy --env prod                       # the production view of the same file
    python -m security.transit_policy --file security/fixtures/partners_cleartext.yaml --env prod   # exit 1

`transit_ok(url, environment)` is the one rule everything uses:
  - https://<any host>                         allowed (TLS; the client verifies the certificate by default)
  - http://localhost, 127.0.0.1, ::1           allowed in dev and staging only. This is the LAB EXCEPTION:
                                               the stdio MCP server, a local mock and the compose healthcheck
                                               never leave the machine. In prod it is refused too.
  - http://<anything else>                     refused in every environment (cleartext over a network)
  - credentials in the URL (user:pass@host)    refused: they end up in proxy and access logs
  - any other scheme (ftp, ws, file, ...)      refused

The API runs this over config/partners.yaml at startup (prompt_app lifespan) and refuses to boot when any
partner URL fails, so a cleartext production host cannot be deployed by editing a config file.
Exit codes: 0 every URL passes, 1 at least one URL is refused.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parent.parent
PARTNERS_PATH = ROOT / "config" / "partners.yaml"
ENVIRONMENTS = ("dev", "staging", "prod")
LAB_ENVIRONMENTS = ("dev", "staging")
_LOCAL_NAMES = {"localhost", "localhost.localdomain"}


@dataclass(frozen=True)
class TransitDecision:
    ok: bool
    url: str
    reason: str


def is_loopback(host: str) -> bool:
    host = host.strip("[]").lower()
    if host in _LOCAL_NAMES or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def transit_ok(url: str, environment: str = "prod") -> TransitDecision:
    """Decide whether `url` may carry AfyaPlus data in `environment`. Unknown environments are treated as prod."""
    env = environment if environment in ENVIRONMENTS else "prod"
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname or ""
    except ValueError as exc:
        return TransitDecision(False, url, f"unparseable URL ({exc})")
    if not host:
        return TransitDecision(False, url, "no host in URL")
    if parts.username or parts.password:
        return TransitDecision(False, url, "credentials embedded in the URL")
    scheme = parts.scheme.lower()
    if scheme == "https":
        return TransitDecision(True, url, "https")
    if scheme != "http":
        return TransitDecision(False, url, f"scheme '{scheme}' is not allowed (https only)")
    if is_loopback(host):
        if env in LAB_ENVIRONMENTS:
            return TransitDecision(True, url, f"lab exception: cleartext loopback allowed in {env}")
        return TransitDecision(False, url, "cleartext loopback is not allowed in prod")
    return TransitDecision(False, url, f"cleartext http to non-local host '{host}'")


def load_partners(path: Path = PARTNERS_PATH) -> dict[str, dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("partners", {})


def check_partners(environment: str, path: Path = PARTNERS_PATH) -> list[tuple[str, TransitDecision]]:
    """Every partner endpoint that is active in `environment`, with its decision."""
    out = []
    for name, entry in load_partners(path).items():
        if environment in entry.get("environments", ENVIRONMENTS):
            out.append((name, transit_ok(entry["url"], environment)))
    return out


def assert_partners_ok(environment: str, path: Path = PARTNERS_PATH) -> None:
    """Startup guard: raise if any partner URL would send data in cleartext."""
    refused = [f"{name}: {d.url} ({d.reason})" for name, d in check_partners(environment, path) if not d.ok]
    if refused:
        raise RuntimeError("transit policy refused partner URL(s): " + "; ".join(refused))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check partner URLs against the HTTPS-only transit policy.")
    ap.add_argument("--file", type=Path, default=PARTNERS_PATH)
    ap.add_argument("--env", choices=ENVIRONMENTS, default=os.environ.get("DEPLOY_ENV", "dev"))
    ap.add_argument("--url", help="check one URL instead of the partners file")
    args = ap.parse_args(argv)

    rows = [("--url", transit_ok(args.url, args.env))] if args.url else check_partners(args.env, args.file)
    print(f"transit policy, environment={args.env}, source={args.url and '--url' or args.file.name}")
    for name, d in rows:
        print(f"  {'ALLOW' if d.ok else 'DENY '}  {name:<22} {d.url:<48} {d.reason}")
    denied = sum(not d.ok for _, d in rows)
    print(f"{len(rows) - denied} allowed, {denied} denied -> exit {1 if denied else 0}")
    return 1 if denied else 0


if __name__ == "__main__":
    sys.exit(main())

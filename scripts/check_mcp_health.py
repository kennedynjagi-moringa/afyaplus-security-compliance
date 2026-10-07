"""MCP health probe for the pipeline (Deliverable 4).

    python scripts/check_mcp_health.py                       # live: spawn logistics_mcp_versioned.py over stdio
    python scripts/check_mcp_health.py --target stub         # fallback: the contract stub (same exit codes)
    python scripts/check_mcp_health.py --command docker compose exec -T triage python logistics_mcp_versioned.py
    python scripts/check_mcp_health.py --write-lock          # after a reviewed schema change + version bump

What it asserts, against config/triage.yaml `mcp:` and mcp_schema.lock.json:
  handshake        initialize completes within --timeout; serverInfo.name is the configured server
  version          serverInfo.version == config mcp.version (the release's MCP version)
  lock_version     mcp_schema.lock.json was generated for that same version
  required_tools   every tool in mcp.required_tools is listed (e.g. check_stock)
  schema_lock      each tool's input schema hashes to the locked value; no tool added or removed unannounced
  smoke_call       check_stock on a known clinic returns ok=true with quantity and status
  error_contract   a bad clinic id returns isError=true and {"ok": false, "error": {"code": ...}}
  resource         clinics://catalogue is readable

Exit codes, identical for live and stub:
  0  healthy: deploy may proceed
  1  the server answered but an assertion failed (wrong version, missing tool, schema drift, bad result)
  2  the probe could not complete: spawn failed, handshake failed, or timeout
Both 1 and 2 fail the CI job, and the deploy job `needs:` this one, so a bad probe blocks the deploy.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import anyio  # noqa: E402
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402
from pydantic import AnyUrl  # noqa: E402

from prompt_loader import load_config  # noqa: E402

LOCK_PATH = ROOT / "mcp_schema.lock.json"
EXIT_HEALTHY, EXIT_ASSERTION, EXIT_UNREACHABLE = 0, 1, 2

SMOKE_CALL = ("check_stock", {"clinic_id": "KSM-001", "item": "oxytocin_10iu"}, ("quantity", "status"))
BAD_CALL = ("check_stock", {"clinic_id": "not-an-id", "item": "oxytocin_10iu"})
RESOURCE = "clinics://catalogue"


def schema_sha(schema: dict) -> str:
    return hashlib.sha256(json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass
class Report:
    target: str
    checks: list[dict] = field(default_factory=list)
    server: dict = field(default_factory=dict)
    unreachable: str | None = None

    def check(self, name: str, ok: bool, detail: str) -> bool:
        self.checks.append({"check": name, "ok": ok, "detail": detail})
        return ok

    @property
    def exit_code(self) -> int:
        if self.unreachable:
            return EXIT_UNREACHABLE
        return EXIT_HEALTHY if all(c["ok"] for c in self.checks) else EXIT_ASSERTION


def server_params(args) -> StdioServerParameters:
    env = {**os.environ, "MCP_PROBE": "1"}  # LOG_DIR, TOOL_METRICS_DB, MCP_STUB_FAULT reach the server
    if args.command:
        return StdioServerParameters(command=args.command[0], args=args.command[1:], env=env, cwd=str(ROOT))
    script = "logistics_mcp_versioned.py" if args.target == "live" else "scripts/mcp_stub_server.py"
    return StdioServerParameters(command=sys.executable, args=[str(ROOT / script)], env=env, cwd=str(ROOT))


def _payload(result) -> dict:
    text = next((c.text for c in result.content if getattr(c, "type", "") == "text"), "")
    try:
        return json.loads(text)
    except ValueError:
        return {"_raw": text[:200]}


async def probe(args, report: Report) -> dict[str, dict]:
    """Run every check. Returns the live tool schemas (for --write-lock)."""
    config = load_config()
    lock = json.loads(LOCK_PATH.read_text()) if LOCK_PATH.is_file() else None
    errlog_path = ROOT / "logs" / f"mcp_probe_{args.target}.stderr.log"
    errlog_path.parent.mkdir(parents=True, exist_ok=True)
    schemas: dict[str, dict] = {}
    with errlog_path.open("w") as errlog, anyio.fail_after(args.timeout):
        async with stdio_client(server_params(args), errlog=errlog) as (read, write), \
                ClientSession(read, write) as session:
            init = await session.initialize()
            info = init.serverInfo
            report.server = {"name": info.name, "version": info.version,
                             "protocol": str(init.protocolVersion)}
            report.check("handshake", info.name == config.mcp.server,
                         f"serverInfo.name={info.name!r} (expected {config.mcp.server!r}), "
                         f"protocol {init.protocolVersion}")
            report.check("version", info.version == config.mcp.version,
                         f"serverInfo.version={info.version} (config/triage.yaml mcp.version={config.mcp.version})")
            if lock is None:
                report.check("lock_version", False, f"{LOCK_PATH.name} is missing: run --write-lock")
            else:
                report.check("lock_version", lock["version"] == info.version,
                             f"lock generated for {lock['version']}, server reports {info.version}")

            tools = {t.name: t for t in (await session.list_tools()).tools}
            schemas = {name: t.inputSchema for name, t in tools.items()}
            missing = [t for t in config.mcp.required_tools if t not in tools]
            listed = f"listed: {', '.join(sorted(tools))}"
            report.check("required_tools", not missing,
                         listed + (f"; MISSING: {', '.join(missing)}" if missing else ""))

            if lock is not None:
                drift = [name for name, schema in schemas.items()
                         if name in lock["tools"] and schema_sha(schema) != lock["tools"][name]["sha256"]]
                added = sorted(set(schemas) - set(lock["tools"]))
                removed = sorted(set(lock["tools"]) - set(schemas))
                problems = ([f"changed: {', '.join(drift)}"] if drift else []) + \
                    ([f"added: {', '.join(added)}"] if added else []) + \
                    ([f"removed: {', '.join(removed)}"] if removed else [])
                report.check("schema_lock", not problems,
                             "all input schemas match the lock" if not problems else
                             "; ".join(problems) + ": bump SERVER_VERSION (semver) and regenerate the lock")

            name, call_args, keys = SMOKE_CALL
            if name in tools:
                result = await session.call_tool(name, call_args)
                body = _payload(result)
                ok = not result.isError and body.get("ok") is True and all(k in body for k in keys)
                report.check("smoke_call", ok, f"{name}({call_args}) -> isError={result.isError}, "
                             + ", ".join(f"{k}={body.get(k)!r}" for k in ("ok", *keys)))
                result = await session.call_tool(*BAD_CALL)
                body = _payload(result)
                code = (body.get("error") or {}).get("code")
                report.check("error_contract", bool(result.isError) and body.get("ok") is False and bool(code),
                             f"{BAD_CALL[0]}({BAD_CALL[1]}) -> isError={result.isError}, error.code={code}")
            else:
                report.check("smoke_call", False, f"{name} not listed, cannot call it")

            try:
                contents = await session.read_resource(AnyUrl(RESOURCE))
                text = contents.contents[0].text if contents.contents else ""
                report.check("resource", bool(text), f"{RESOURCE} -> {len(text)} chars")
            except Exception as exc:  # noqa: BLE001 - any failure here is a failed check, not a crash
                report.check("resource", False, f"{RESOURCE} -> {type(exc).__name__}: {exc}")
    return schemas


def write_lock(schemas: dict[str, dict], report: Report) -> None:
    LOCK_PATH.write_text(json.dumps({
        "server": report.server["name"],
        "version": report.server["version"],
        "note": "Generated by scripts/check_mcp_health.py --write-lock. Changing a tool schema needs a "
                "SERVER_VERSION bump and a reviewed change to this file.",
        "tools": {name: {"sha256": schema_sha(schema),
                         "required": sorted(schema.get("required", [])),
                         "params": sorted(schema.get("properties", {})),
                         "input_schema": schema}
                  for name, schema in sorted(schemas.items())},
    }, indent=2) + "\n")
    print(f"wrote {LOCK_PATH.name} for {report.server['name']} {report.server['version']} "
          f"({len(schemas)} tools)")


def render(report: Report) -> str:
    head = f"## MCP health ({report.target})"
    if report.server:
        head += f": {report.server['name']} {report.server['version']}"
    lines = [head, "", "| check | result | detail |", "|---|---|---|"]
    lines += [f"| {c['check']} | {'pass' if c['ok'] else '**FAIL**'} | {c['detail']} |" for c in report.checks]
    if report.unreachable:
        lines += ["", f"UNREACHABLE: {report.unreachable}"]
    verdict = {0: "MCP HEALTHY (exit 0)", 1: "MCP PROBE FAILED: assertion (exit 1)",
               2: "MCP PROBE FAILED: unreachable (exit 2)"}[report.exit_code]
    return "\n".join([*lines, "", verdict])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Probe the MCP server(s) the release needs.")
    ap.add_argument("--target", choices=["live", "stub"], default="live")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--write-lock", action="store_true")
    ap.add_argument("--json", help="also write the report as JSON to this path")
    ap.add_argument("--command", nargs=argparse.REMAINDER, help="custom server command (e.g. docker compose exec)")
    args = ap.parse_args(argv)

    report = Report(target="custom" if args.command else args.target)
    schemas: dict[str, dict] = {}
    try:
        schemas = asyncio.run(probe(args, report))
    except TimeoutError:
        report.unreachable = f"no complete answer within {args.timeout:.0f}s"
    except BaseException as exc:  # noqa: BLE001 - spawn failures arrive as ExceptionGroups from anyio
        if isinstance(exc, KeyboardInterrupt):
            raise
        while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
            exc = exc.exceptions[0]
        report.unreachable = f"{type(exc).__name__}: {exc}"[:300]

    if args.write_lock:
        if report.unreachable or not schemas:
            print(f"not writing the lock: {report.unreachable or 'no tools listed'}", file=sys.stderr)
            return EXIT_UNREACHABLE
        write_lock(schemas, report)
        return EXIT_HEALTHY

    text = render(report)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(text + "\n\n")
    if args.json:
        Path(args.json).write_text(json.dumps({"target": report.target, "server": report.server,
                                               "checks": report.checks, "unreachable": report.unreachable,
                                               "exit_code": report.exit_code}, indent=2) + "\n")
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())

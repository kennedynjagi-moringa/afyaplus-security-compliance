"""Contract stub of the logistics MCP server: the fallback when the live server can't run.

It serves exactly the tools, input schemas, name and version recorded in mcp_schema.lock.json, and
gives canned answers. That keeps the pipeline's gate the same shape (handshake, required tools,
schema lock, smoke call, error contract) and the exit codes identical when the live server or its
data is unavailable, for example on a runner without the clinic registry.

The stub proves the probe and the contract, not the data. The live probe stays the deploy gate whenever
it can run (see README, "Fallbacks declared").

MCP_STUB_FAULT injects one failure, so you can watch each check fail:
  drop_tool      check_stock is not listed            -> required_tools fails, exit 1
  wrong_version  the handshake reports 9.9.9          -> version fails, exit 1
  schema_drift   check_stock renames clinic_id to id  -> schema_lock fails, exit 1
  tool_error     every tool call returns isError      -> smoke_call fails, exit 1
  hang           never answers the handshake          -> timeout, exit 2
  crash          exits on start                       -> unreachable, exit 2
"""

from __future__ import annotations

import copy
import json
import os
import re
import sys
import time
from pathlib import Path

import anyio
from mcp import types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server

ROOT = Path(__file__).resolve().parent.parent
LOCK = json.loads((ROOT / "mcp_schema.lock.json").read_text())
FAULT = os.environ.get("MCP_STUB_FAULT", "none")

if FAULT == "crash":
    print("mcp stub: injected crash", file=sys.stderr)
    sys.exit(3)
if FAULT == "hang":
    time.sleep(3600)

server = Server(LOCK["server"], version="9.9.9" if FAULT == "wrong_version" else LOCK["version"])


def _tools() -> list[types.Tool]:
    out = []
    for name, spec in LOCK["tools"].items():
        if FAULT == "drop_tool" and name == "check_stock":
            continue
        schema = copy.deepcopy(spec["input_schema"])
        if FAULT == "schema_drift" and name == "check_stock":
            schema["properties"]["id"] = schema["properties"].pop("clinic_id")
            schema["required"] = ["id" if r == "clinic_id" else r for r in schema.get("required", [])]
        out.append(types.Tool(name=name, description=f"[stub] {name}", inputSchema=schema))
    return out


def _text(payload: dict, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(payload))], isError=is_error)


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return _tools()


@server.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
    if FAULT == "tool_error":
        return _text({"ok": False, "error": {"code": "STUB_FAULT", "message": "injected", "hint": "-"}}, True)
    if name == "check_stock":
        clinic_id = str(arguments.get("clinic_id", ""))
        if not re.match(r"^[A-Z]{3}-\d{3}$", clinic_id.strip().upper()):
            return _text({"ok": False, "error": {"code": "INVALID_CLINIC_ID", "message": "stub: bad id",
                                                 "hint": "ids look like KSM-002"}}, True)
        return _text({"ok": True, "stub": True, "clinic_id": clinic_id, "item": arguments.get("item"),
                      "quantity": 0, "status": "out_of_stock", "stale": True})
    if name == "find_clinics":
        return _text({"ok": True, "stub": True, "count": 0, "total_matches": 0, "clinics": [],
                      "note": "contract stub: no data"})
    return _text({"ok": False, "error": {"code": "UNKNOWN_TOOL", "message": name, "hint": "-"}}, True)


@server.list_resources()
async def list_resources() -> list[types.Resource]:
    return [types.Resource(uri="clinics://catalogue", name="clinic_catalogue", mimeType="application/json")]


@server.read_resource()
async def read_resource(uri) -> str:
    return json.dumps({"stub": True, "server_version": LOCK["version"]})


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options(NotificationOptions()))


if __name__ == "__main__":
    anyio.run(main)

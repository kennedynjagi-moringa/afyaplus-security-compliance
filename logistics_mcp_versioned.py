"""AfyaPlus logistics MCP server, versioned (Deliverable 1). Week 6's clinic server plus a release contract.

Versioning (new in Week 8)
- SERVER_VERSION is semver and is what the MCP handshake reports as serverInfo.version. (The Week 6 server
  left it unset, so clients saw the SDK's version, 1.x, instead of ours.)
- The tool input schemas are locked in mcp_schema.lock.json. scripts/check_mcp_health.py fails the
  pipeline when a live schema differs from the lock, or when the lock and server versions disagree. So
  a schema change can't ship without a version bump and a reviewed lock change.
- MAJOR: a tool or argument removed or renamed. MINOR: a new tool or optional argument. PATCH: data or
  description fixes that leave the schemas alone.
- Every tool call is counted in the shared store (tool_metrics.py), labelled with SERVER_VERSION.

Tools
- find_clinics(county, service?, open_24_hours?, has_stock_item?, limit?)  -> clinics matching the filters
- check_stock(clinic_id, item)                             -> quantity and status of one stock item

Resource
- clinics://catalogue  -> valid counties, services and stock items, units, status rules,
                          and what the dataset does NOT contain

Production rules
- Inputs are checked in the tool; bad input returns {"ok": false, "error": {...}} with a
  code, a plain message and a hint on how to fix the call, flagged isError=true as the MCP
  spec asks for tool-execution errors. Nothing raises to the client.
- Arguments that fail type validation (e.g. limit="five") are caught at the dispatch layer
  and returned in the same error shape.
- Exactly one JSON log line per tool call or resource read, on stderr (stdout carries the
  protocol) and in logs/mcp_tools.jsonl, tagged with the caller's trace id (AFYA_TRACE_ID).

Run: python logistics_mcp_versioned.py   (stdio)
"""

from __future__ import annotations

import difflib
import json
import os
import re
import sys
import time
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, ContentBlock, TextContent
from pydantic import BaseModel, Field

from afyaplus.logging_setup import configure_logging, trace_id_var
from tool_metrics import ToolMetrics

SERVER_NAME = "afyaplus-logistics"
SERVER_VERSION = "1.1.0"

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_PATH = Path(os.environ.get("CLINICS_PATH", PROJECT_ROOT / "clinics.json"))
LOG_DIR = Path(os.environ.get("LOG_DIR", PROJECT_ROOT / "logs"))

trace_id_var.set(os.environ.get("AFYA_TRACE_ID", "-"))
log = configure_logging("mcp_server", LOG_DIR, "mcp_tools.jsonl", stream=sys.stderr)
metrics = ToolMetrics()
# The health probe deliberately makes a bad call (error contract check). Count probe traffic under its own
# component so it never inflates the production error rate the canary gate reads.
METRICS_COMPONENT = "mcp-probe" if os.environ.get("MCP_PROBE") == "1" else "mcp"

MAX_LIMIT = 10
STALE_AFTER_DAYS = 7
CLINIC_ID_RE = re.compile(r"^[A-Z]{3}-\d{3}$")

NOT_IN_DATASET = [
    "waiting times or queue lengths",
    "staff names, rosters or which clinician is on duty",
    "prices, fees or NHIF/SHA coverage",
    "bed or ward availability",
    "opening hours beyond the open_24_hours flag",
    "patient records of any kind",
    "stock for items other than the listed stock_items",
]


# ---- data -------------------------------------------------------------------

class StockLevel(BaseModel):
    quantity: int = Field(ge=0)
    reorder_level: int = Field(gt=0)


class Clinic(BaseModel):
    id: str = Field(pattern=CLINIC_ID_RE.pattern)
    name: str
    county: str
    sub_county: str
    kephl_level: int = Field(ge=1, le=6)
    open_24_hours: bool
    services: list[str]
    ambulance: bool
    phone: str
    stock_updated: date
    stock: dict[str, StockLevel]


class Registry(BaseModel):
    dataset: str
    data_as_of: date
    units: dict[str, str]
    clinics: list[Clinic]


def load_registry(path: Path = DATA_PATH) -> Registry:
    """Validated at startup: a malformed data file stops the server instead of serving bad answers."""
    return Registry.model_validate_json(path.read_text(encoding="utf-8"))


REGISTRY = load_registry()
CLINICS = {c.id: c for c in REGISTRY.clinics}
COUNTIES = sorted({c.county for c in REGISTRY.clinics})
SERVICES = sorted({s for c in REGISTRY.clinics for s in c.services})
ITEMS = sorted(REGISTRY.units)


def _error(code: str, message: str, hint: str, **extra: Any) -> dict:
    return {"ok": False, "error": {"code": code, "message": message, "hint": hint, **extra}}


def _suggest(value: str, options: list[str]) -> list[str]:
    return difflib.get_close_matches(value, options, n=2, cutoff=0.5)


def _match(value: str, options: list[str]) -> str | None:
    """Case- and whitespace-insensitive exact match against the vocabulary."""
    wanted = value.strip().lower().replace(" ", "_")
    return next((o for o in options if o.lower() == wanted or o.lower() == value.strip().lower()), None)


# ---- server with per-call logging ------------------------------------------

class LoggedFastMCP(FastMCP):
    """FastMCP with one log line per tool call / resource read and errors-as-data at dispatch."""

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Sequence[ContentBlock] | CallToolResult:
        started = time.perf_counter()
        try:
            result = await super().call_tool(name, arguments)
            outcome = _outcome(result)
            if not outcome["ok"]:  # keep the instructive body, flag it as a tool error for the client
                result = CallToolResult(content=list(result), isError=True)
        except ToolError as exc:  # unknown tool, or arguments that failed type validation
            unknown = str(exc).startswith("Unknown tool")
            payload = _error(
                "UNKNOWN_TOOL" if unknown else "INVALID_ARGUMENTS",
                str(exc).splitlines()[0] if unknown else f"Arguments for {name} failed validation.",
                f"Available tools: {', '.join(t.name for t in self._tool_manager.list_tools())}." if unknown
                else "Check the argument types against the tool's input schema and call again.",
                details=str(exc)[:500],
            )
            result = CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))], isError=True)
            outcome = {"ok": False, "error_code": payload["error"]["code"]}
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        log.info("tool_call", extra={
            "tool": name, "server_version": SERVER_VERSION, "arguments": arguments, **outcome,
            "latency_ms": latency_ms,
        })
        metrics.record(METRICS_COMPONENT, name, SERVER_VERSION, outcome["ok"], latency_ms, outcome.get("error_code"))
        return result

    async def read_resource(self, uri):
        started = time.perf_counter()
        try:
            contents = await super().read_resource(uri)
            ok = True
            return contents
        except Exception:
            ok = False
            raise
        finally:
            log.info("resource_read", extra={"uri": str(uri), "ok": ok,
                                             "latency_ms": round((time.perf_counter() - started) * 1000, 2)})


def _outcome(result: Any) -> dict:
    """Pull ok / error_code / result_count out of a tool result for the log line."""
    blocks = result[0] if isinstance(result, tuple) else result
    try:
        body = json.loads(next(b.text for b in blocks if isinstance(b, TextContent)))
    except (StopIteration, ValueError, TypeError):
        return {"ok": True}
    out = {"ok": body.get("ok", True)}
    if not out["ok"]:
        out["error_code"] = body.get("error", {}).get("code")
    if "count" in body:
        out["result_count"] = body["count"]
    return out


mcp = LoggedFastMCP(
    SERVER_NAME,
    log_level="WARNING",  # silence the SDK's per-request INFO lines; our tool_call line is the record
    instructions=(
        "Read clinics://catalogue first for the valid counties, services and stock items. "
        "Use find_clinics to get clinic ids, then check_stock per clinic and item. "
        "Tools return {'ok': false, 'error': {...}} on bad input; follow the hint and retry."
    ),
)
# FastMCP has no version argument; set it on the low-level server so the handshake reports our version.
mcp._mcp_server.version = SERVER_VERSION


# ---- tools ------------------------------------------------------------------

@mcp.tool(structured_output=False)
def find_clinics(
    county: Annotated[str, Field(description="County name from clinics://catalogue, e.g. 'Kisumu'")],
    service: Annotated[str | None, Field(description="Service from clinics://catalogue, e.g. 'maternity'")] = None,
    open_24_hours: Annotated[bool | None, Field(description="true: 24-hour clinics only; false: others only")] = None,
    limit: Annotated[int, Field(description="Maximum clinics to return, 1-10")] = 5,
    has_stock_item: Annotated[str | None, Field(
        description="Only clinics with this stock item available or low (not out of stock), e.g. 'oxytocin_10iu'"
    )] = None,
) -> dict:
    """Find AfyaPlus partner clinics in one Kenyan county, optionally filtered by service and 24-hour opening.

    Args:
        county: County name, case-insensitive, e.g. "Kisumu". Must be one of the counties
            listed in the clinics://catalogue resource.
        service: Optional service the clinic must offer, e.g. "maternity", "emergency",
            "laboratory", "immunisation". Must be one of the catalogue's services.
        open_24_hours: Optional. True returns only clinics open 24 hours; False only those
            that are not; omit to return both.
        limit: Maximum clinics to return, 1 to 10 (default 5). Higher KEPH levels come first.
        has_stock_item: Optional stock item key from the catalogue (added in 1.1.0). Only clinics where the
            item is not out of stock are returned, each with a "stock" field giving that item's status.

    Returns:
        {"ok": true, "count": n, "total_matches": m, "clinics": [{id, name, sub_county,
        kephl_level, open_24_hours, services, ambulance, phone}], "note": str}.
        Stock levels are NOT included; call check_stock(clinic_id, item) for each clinic.
        A county with no matching clinic returns ok=true and count=0.

    Errors (returned as data, never raised):
        UNKNOWN_COUNTY, UNKNOWN_SERVICE, UNKNOWN_ITEM, INVALID_LIMIT, each with a hint and the valid values.
    """
    matched_county = _match(county, COUNTIES)
    if matched_county is None:
        return _error("UNKNOWN_COUNTY", f"No clinics are registered for county '{county}'.",
                      "Use one of valid_values (see clinics://catalogue).",
                      valid_values=COUNTIES, did_you_mean=_suggest(county.title(), COUNTIES))
    matched_service = None
    if service is not None and service.strip():
        matched_service = _match(service, SERVICES)
        if matched_service is None:
            return _error("UNKNOWN_SERVICE", f"'{service}' is not a service recorded in the registry.",
                          "Use one of valid_values, or omit service to list all clinics in the county.",
                          valid_values=SERVICES, did_you_mean=_suggest(service.lower(), SERVICES))
    matched_item = None
    if has_stock_item is not None and has_stock_item.strip():
        matched_item = _match(has_stock_item, ITEMS)
        if matched_item is None:
            return _error("UNKNOWN_ITEM", f"'{has_stock_item}' is not a tracked stock item.",
                          "Use one of valid_values, or omit has_stock_item.",
                          valid_values=ITEMS, did_you_mean=_suggest(has_stock_item.lower(), ITEMS))
    if not 1 <= limit <= MAX_LIMIT:
        return _error("INVALID_LIMIT", f"limit must be between 1 and {MAX_LIMIT}; got {limit}.",
                      f"Call again with limit between 1 and {MAX_LIMIT}.")

    matches = [
        c for c in REGISTRY.clinics
        if c.county == matched_county
        and (matched_service is None or matched_service in c.services)
        and (open_24_hours is None or c.open_24_hours == open_24_hours)
        and (matched_item is None or c.stock[matched_item].quantity > 0)
    ]
    matches.sort(key=lambda c: (-c.kephl_level, c.id))
    shown = matches[:limit]
    note = "Stock is not included; call check_stock(clinic_id, item)."
    if not matches:
        note = (f"No clinic in {matched_county} matches these filters. "
                "Try without open_24_hours or with a different service.")
    return {
        "ok": True,
        "county": matched_county,
        "filters": {"service": matched_service, "open_24_hours": open_24_hours, "has_stock_item": matched_item},
        "count": len(shown),
        "total_matches": len(matches),
        "clinics": [c.model_dump(include={"id", "name", "sub_county", "kephl_level", "open_24_hours",
                                          "services", "ambulance", "phone"})
                    | ({"stock": {"item": matched_item, "status": _status(c.stock[matched_item])}}
                       if matched_item else {}) for c in shown],
        "note": note,
    }


@mcp.tool(structured_output=False)
def check_stock(
    clinic_id: Annotated[str, Field(description="Clinic id from find_clinics, format ABC-123, e.g. 'KSM-002'")],
    item: Annotated[str, Field(description="Stock item key from clinics://catalogue, e.g. 'oxytocin_10iu'")],
) -> dict:
    """Check the current pharmacy stock of one item at one clinic.

    Args:
        clinic_id: Clinic id in the form ABC-123, as returned by find_clinics, e.g. "KSM-002".
        item: Stock item key from the clinics://catalogue resource, e.g. "oxytocin_10iu",
            "artemether_lumefantrine", "ors_sachet", "malaria_rdt".

    Returns:
        {"ok": true, "clinic_id", "clinic_name", "item", "quantity", "unit", "reorder_level",
        "status", "stock_updated", "days_since_update", "stale"}.
        status is "out_of_stock" (quantity 0), "low" (at or below reorder_level) or "available".
        stale is true when the count is more than 7 days old; say so when you report it.

    Errors (returned as data, never raised):
        INVALID_CLINIC_ID (wrong format), UNKNOWN_CLINIC, UNKNOWN_ITEM, each with a hint.
    """
    cid = clinic_id.strip().upper()
    if not CLINIC_ID_RE.match(cid):
        return _error("INVALID_CLINIC_ID", f"'{clinic_id}' is not a clinic id; ids look like 'KSM-002'.",
                      "Call find_clinics first and use the id field it returns, not the clinic name.")
    clinic = CLINICS.get(cid)
    if clinic is None:
        return _error("UNKNOWN_CLINIC", f"No clinic has id '{cid}'.",
                      "Call find_clinics for the county to get valid ids.",
                      did_you_mean=_suggest(cid, list(CLINICS)))
    matched_item = _match(item, ITEMS)
    if matched_item is None:
        return _error("UNKNOWN_ITEM", f"'{item}' is not a tracked stock item.",
                      "Use one of valid_values exactly as written.",
                      valid_values=ITEMS, did_you_mean=_suggest(item.lower(), ITEMS))

    level = clinic.stock[matched_item]
    status = _status(level)
    age = (date.today() - clinic.stock_updated).days
    return {
        "ok": True,
        "clinic_id": clinic.id,
        "clinic_name": clinic.name,
        "item": matched_item,
        "quantity": level.quantity,
        "unit": REGISTRY.units[matched_item],
        "reorder_level": level.reorder_level,
        "status": status,
        "stock_updated": clinic.stock_updated.isoformat(),
        "days_since_update": age,
        "stale": age > STALE_AFTER_DAYS,
    }


def _status(level: StockLevel) -> str:
    if level.quantity == 0:
        return "out_of_stock"
    return "low" if level.quantity <= level.reorder_level else "available"


# ---- resource ---------------------------------------------------------------

@mcp.resource("clinics://catalogue", name="clinic_catalogue", mime_type="application/json",
              description="Valid counties, services and stock items; status rules; what the data does not contain.")
def catalogue() -> str:
    """The vocabulary every tool accepts, plus the limits of the dataset."""
    return json.dumps({
        "server_version": SERVER_VERSION,
        "dataset": REGISTRY.dataset,
        "data_as_of": REGISTRY.data_as_of.isoformat(),
        "counties": {county: sum(c.county == county for c in REGISTRY.clinics) for county in COUNTIES},
        "services": SERVICES,
        "stock_items": REGISTRY.units,
        "stock_status_rules": {
            "out_of_stock": "quantity == 0",
            "low": "0 < quantity <= reorder_level",
            "available": "quantity > reorder_level",
            "stale": f"stock count older than {STALE_AFTER_DAYS} days",
        },
        "not_in_dataset": NOT_IN_DATASET,
    }, indent=2)


if __name__ == "__main__":
    log.info("server_started", extra={"server_version": SERVER_VERSION, "clinics": len(CLINICS),
                                      "data_as_of": REGISTRY.data_as_of.isoformat()})
    mcp.run("stdio")

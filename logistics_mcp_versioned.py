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

Security (new in Week 9, server 1.2.0; tool input schemas unchanged)
- Who is calling: AFYA_CALLER and AFYA_CALLER_ROLE, set by the authenticated host that spawns this stdio
  server. Never taken from tool arguments, which the model writes. Missing or unknown role = every call denied.
- Role matrix (security/rbac.py): a tool call needs mcp.<tool> for the caller's role, or it returns FORBIDDEN.
- Clinic allowlist (clinics_allowlist.json): find_clinics and check_stock only ever see the caller's clinics.
  An unknown clinic and a clinic outside the allowlist get the same answer, so ids cannot be probed.
- Smallest useful answer: check_stock returns the status (available / low / out_of_stock) to field roles and
  exact counts only to logistics roles. No tool or resource returns environment, paths, versions of other
  software or configuration; tests/test_access_control.py plants a canary secret in the environment to prove it.
- Injection guard (security/injection_guard.py) on every string argument before the tool runs.
- Session budget: at most MCP_MAX_CALLS tool calls per server process (default 40), so a looping agent cannot
  walk the registry one id at a time.
- clinics.json and the allowlist are checked against security/corpus_pins.json at startup; a changed file
  stops the server.

Run: AFYA_CALLER=chw_amina AFYA_CALLER_ROLE=chw python logistics_mcp_versioned.py   (stdio)
"""

from __future__ import annotations

import difflib
import json
import os
import re
import sys
import time
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, ContentBlock, TextContent
from pydantic import BaseModel, Field

from afyaplus.logging_setup import configure_logging, trace_id_var
from security import rbac
from security.corpus_pins import verify_pins
from security.injection_guard import scan_fields
from tool_metrics import ToolMetrics

SERVER_NAME = "afyaplus-logistics"
SERVER_VERSION = "1.2.0"

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_PATH = Path(os.environ.get("CLINICS_PATH", PROJECT_ROOT / "clinics.json"))
ALLOWLIST_PATH = PROJECT_ROOT / "clinics_allowlist.json"
LOG_DIR = Path(os.environ.get("LOG_DIR", PROJECT_ROOT / "logs"))

trace_id_var.set(os.environ.get("AFYA_TRACE_ID", "-"))
log = configure_logging("mcp_server", LOG_DIR, "mcp_tools.jsonl", stream=sys.stderr)
metrics = ToolMetrics()
# The health probe deliberately makes a bad call (error contract check). Count probe traffic under its own
# component so it never inflates the production error rate the canary gate reads.
METRICS_COMPONENT = "mcp-probe" if os.environ.get("MCP_PROBE") == "1" else "mcp"

MAX_LIMIT = 10
MAX_CALLS = int(os.environ.get("MCP_MAX_CALLS", "40"))
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


# Pinned data only: an edited clinics.json or allowlist stops the server (security/corpus_pins.py).
verify_pins(["clinics.json", "clinics_allowlist.json"])
REGISTRY = load_registry()
CLINICS = {c.id: c for c in REGISTRY.clinics}
COUNTIES = sorted({c.county for c in REGISTRY.clinics})
SERVICES = sorted({s for c in REGISTRY.clinics for s in c.services})
ITEMS = sorted(REGISTRY.units)
ALLOWLIST = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))["subjects"]


# ---- caller identity and allowlist (Week 9) -----------------------------------

@dataclass(frozen=True)
class Caller:
    subject: str
    role: str

    @classmethod
    def from_env(cls) -> Caller:
        return cls(os.environ.get("AFYA_CALLER", ""), os.environ.get("AFYA_CALLER_ROLE", ""))


caller_var: ContextVar[Caller] = ContextVar("caller", default=Caller.from_env())  # noqa: B039 - frozen dataclass


def allowed_clinic_ids(caller: Caller) -> frozenset[str]:
    """The caller's clinics. Not listed, or listed under another role: none."""
    entry = ALLOWLIST.get(caller.subject)
    if not entry or entry.get("role") != caller.role:
        return frozenset()
    counties = set(entry.get("counties", []))
    return frozenset(entry.get("clinics", [])) | {c.id for c in REGISTRY.clinics if c.county in counties}


def _echo(value: str) -> str:
    """Caller input repeated in an error message: short and printable, so errors can't relay a payload."""
    return re.sub(r"[^\w .'-]", "", value)[:40]


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

    calls_made = 0

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Sequence[ContentBlock] | CallToolResult:
        started = time.perf_counter()
        caller = caller_var.get()
        LoggedFastMCP.calls_made += 1
        denied = _precheck(name, arguments, caller, LoggedFastMCP.calls_made)
        try:
            if denied is not None:
                raise _Denied(denied)
            result = await super().call_tool(name, arguments)
            outcome = _outcome(result)
            if not outcome["ok"]:  # keep the instructive body, flag it as a tool error for the client
                result = CallToolResult(content=list(result), isError=True)
        except _Denied as exc:
            payload = exc.payload
            result = CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))], isError=True)
            outcome = {"ok": False, "error_code": payload["error"]["code"]}
        except ToolError as exc:  # unknown tool, or arguments that failed type validation
            unknown = str(exc).startswith("Unknown tool")
            payload = _error(  # no validator details: they echo the raw input back to the model
                "UNKNOWN_TOOL" if unknown else "INVALID_ARGUMENTS",
                f"Unknown tool '{_echo(name)}'." if unknown else f"Arguments for {name} failed validation.",
                f"Available tools: {', '.join(t.name for t in self._tool_manager.list_tools())}." if unknown
                else "Check the argument types against the tool's input schema and call again.",
            )
            result = CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))], isError=True)
            outcome = {"ok": False, "error_code": payload["error"]["code"]}
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        log.info("tool_call", extra={
            "tool": name, "server_version": SERVER_VERSION, "actor": caller.subject or "-",
            "actor_role": caller.role or "-", "arguments": arguments, **outcome, "latency_ms": latency_ms,
        })
        metrics.record(METRICS_COMPONENT, name, SERVER_VERSION, outcome["ok"], latency_ms, outcome.get("error_code"))
        return result

    async def read_resource(self, uri):
        started = time.perf_counter()
        if not rbac.is_allowed(caller_var.get().role, "mcp.catalogue.read"):
            log.info("resource_read", extra={"uri": str(uri), "ok": False, "error_code": "FORBIDDEN"})
            raise PermissionError("FORBIDDEN: this caller's role may not read resources")
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


class _Denied(Exception):
    def __init__(self, payload: dict) -> None:
        super().__init__(payload["error"]["code"])
        self.payload = payload


def _precheck(name: str, arguments: dict[str, Any], caller: Caller, calls_made: int) -> dict | None:
    """Session budget, role matrix and injection guard, in that order. Returns an error payload or None."""
    if calls_made > MAX_CALLS:
        return _error("CALL_BUDGET_EXCEEDED", f"This session has used its {MAX_CALLS} tool calls.",
                      "Start a new session for a new task; the budget stops bulk enumeration.")
    if not rbac.is_allowed(caller.role, f"mcp.{name}"):
        return _error("FORBIDDEN", f"Role '{_echo(caller.role) or '<none>'}' may not call {_echo(name)}.",
                      "Tools are granted per role in security/rbac.py; ask an administrator.")
    guard = scan_fields(arguments)
    if guard.blocked:
        return _error("INPUT_REJECTED", "An argument looks like an instruction, not a value.",
                      "Pass only a county, service, clinic id or stock item.", rule_ids=guard.rule_ids)
    return None


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
        return _error("UNKNOWN_COUNTY", f"No clinics are registered for county '{_echo(county)}'.",
                      "Use one of valid_values (see clinics://catalogue).",
                      valid_values=COUNTIES, did_you_mean=_suggest(county.title(), COUNTIES))
    matched_service = None
    if service is not None and service.strip():
        matched_service = _match(service, SERVICES)
        if matched_service is None:
            return _error("UNKNOWN_SERVICE", f"'{_echo(service)}' is not a service recorded in the registry.",
                          "Use one of valid_values, or omit service to list all clinics in the county.",
                          valid_values=SERVICES, did_you_mean=_suggest(service.lower(), SERVICES))
    matched_item = None
    if has_stock_item is not None and has_stock_item.strip():
        matched_item = _match(has_stock_item, ITEMS)
        if matched_item is None:
            return _error("UNKNOWN_ITEM", f"'{_echo(has_stock_item)}' is not a tracked stock item.",
                          "Use one of valid_values, or omit has_stock_item.",
                          valid_values=ITEMS, did_you_mean=_suggest(has_stock_item.lower(), ITEMS))
    if not 1 <= limit <= MAX_LIMIT:
        return _error("INVALID_LIMIT", f"limit must be between 1 and {MAX_LIMIT}; got {limit}.",
                      f"Call again with limit between 1 and {MAX_LIMIT}.")

    allowed = allowed_clinic_ids(caller_var.get())
    matches = [
        c for c in REGISTRY.clinics
        if c.id in allowed and c.county == matched_county
        and (matched_service is None or matched_service in c.services)
        and (open_24_hours is None or c.open_24_hours == open_24_hours)
        and (matched_item is None or c.stock[matched_item].quantity > 0)
    ]
    matches.sort(key=lambda c: (-c.kephl_level, c.id))
    shown = matches[:limit]
    note = "Stock is not included; call check_stock(clinic_id, item)."
    if not matches:
        note = (f"No clinic you can see in {matched_county} matches these filters. "
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
        Field roles (chw, partner_pharmacy): {"ok": true, "clinic_id", "item", "status", "stale"}.
        Logistics roles (dispatcher): also "clinic_name", "quantity", "unit", "reorder_level",
        "stock_updated", "days_since_update".
        status is "out_of_stock" (quantity 0), "low" (at or below reorder_level) or "available".
        stale is true when the count is more than 7 days old; say so when you report it.

    Errors (returned as data, never raised):
        INVALID_CLINIC_ID (wrong format), UNKNOWN_CLINIC (unknown or not in your allowlist), UNKNOWN_ITEM,
        FORBIDDEN (role), INPUT_REJECTED (guard), CALL_BUDGET_EXCEEDED, each with a hint.
    """
    cid = clinic_id.strip().upper()
    if not CLINIC_ID_RE.match(cid):
        return _error("INVALID_CLINIC_ID", f"'{_echo(clinic_id)}' is not a clinic id; ids look like 'KSM-002'.",
                      "Call find_clinics first and use the id field it returns, not the clinic name.")
    caller = caller_var.get()
    allowed = allowed_clinic_ids(caller)
    clinic = CLINICS.get(cid) if cid in allowed else None
    if clinic is None:  # same answer for "does not exist" and "not yours": no probing for ids
        return _error("UNKNOWN_CLINIC", f"No clinic with id '{cid}' is available to you.",
                      "Call find_clinics for the county to get the ids you can use.",
                      did_you_mean=_suggest(cid, sorted(allowed)))
    matched_item = _match(item, ITEMS)
    if matched_item is None:
        return _error("UNKNOWN_ITEM", f"'{_echo(item)}' is not a tracked stock item.",
                      "Use one of valid_values exactly as written.",
                      valid_values=ITEMS, did_you_mean=_suggest(item.lower(), ITEMS))

    level = clinic.stock[matched_item]
    status = _status(level)
    age = (date.today() - clinic.stock_updated).days
    if rbac.stock_detail(caller.role) != "quantity":  # field roles: the smallest useful answer
        return {"ok": True, "clinic_id": clinic.id, "item": matched_item, "status": status,
                "stale": age > STALE_AFTER_DAYS}
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
        "counties": COUNTIES,
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
                                      "actor": caller_var.get().subject or "-",
                                      "actor_role": caller_var.get().role or "-",
                                      "data_as_of": REGISTRY.data_as_of.isoformat()})
    mcp.run("stdio")

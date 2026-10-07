"""Role matrix for the API and the MCP server (Deliverable 2). Default deny.

    python -m security.rbac            # print the matrix and run its assertions (exit 1 if one fails)

Two kinds of tool:
  partner tools     what field and partner users need: submit a triage case, find a clinic, check one stock item
  operations tools  what staff need to run and oversee the service: metrics, the audit trail, data-subject
                    requests, the retention sweep, the MCP health probe
No role holds both kinds, so a stolen partner token cannot read the audit trail and an auditor's token cannot
query stock. A role that is not in the matrix (a typo, a new role nobody reviewed, a missing claim) gets nothing.

Authentication is unchanged from Week 6: the API still needs a valid JWT first (afyaplus/auth.py). This module
only answers the second question: is this authenticated role allowed to do this action?

On the MCP server the role does NOT come from tool arguments (the model writes those). It comes from the
process that spawned the server, which is the authenticated API host: AFYA_CALLER and AFYA_CALLER_ROLE.
"""

from __future__ import annotations

import sys

PARTNER_TOOLS: frozenset[str] = frozenset({
    "triage.submit",          # POST /v1/triage
    "mcp.find_clinics",       # MCP tool, clinics in the caller's allowlist only
    "mcp.check_stock",        # MCP tool, one item at one allowlisted clinic
    "mcp.catalogue.read",     # MCP resource clinics://catalogue (vocabulary only)
})
OPERATIONS_TOOLS: frozenset[str] = frozenset({
    "metrics.read",           # GET /v1/metrics
    "audit.read",             # logs/audit.jsonl
    "rights.lookup",          # dpa/rights_lookup.py access request
    "rights.erase",           # dpa/rights_lookup.py erasure request
    "retention.sweep",        # dpa/retention_sweeper.py
    "mcp.health_probe",       # scripts/check_mcp_health.py smoke call, one pinned clinic
})
ACTIONS = PARTNER_TOOLS | OPERATIONS_TOOLS

ROLE_MATRIX: dict[str, frozenset[str]] = {
    # partner-facing roles
    "chw":              frozenset({"triage.submit", "mcp.find_clinics", "mcp.check_stock", "mcp.catalogue.read"}),
    "dispatcher":       frozenset({"mcp.find_clinics", "mcp.check_stock", "mcp.catalogue.read"}),
    "partner_pharmacy": frozenset({"mcp.find_clinics", "mcp.check_stock", "mcp.catalogue.read"}),
    # operations roles
    "auditor":          frozenset({"metrics.read", "audit.read"}),
    "ops":              frozenset({"metrics.read", "retention.sweep"}),
    "dpo":              frozenset({"audit.read", "rights.lookup", "rights.erase"}),
    # service identity for the MCP health probe: one smoke call on one pinned clinic
    "svc_probe":        frozenset({"mcp.health_probe", "mcp.check_stock", "mcp.catalogue.read"}),
}

# How much stock detail each role sees from check_stock. "status" is the smallest useful answer for someone
# deciding where to send a patient (available / low / out_of_stock); exact counts are for logistics staff.
STOCK_DETAIL: dict[str, str] = {
    "chw": "status", "partner_pharmacy": "status", "dispatcher": "quantity", "svc_probe": "quantity",
}

# Week 6 token scopes, now derived from the matrix so the two can never disagree.
_SCOPE_OF = {"triage.submit": "triage:write", "metrics.read": "audit:read", "audit.read": "audit:read",
             "mcp.find_clinics": "agent:ask", "mcp.check_stock": "agent:ask"}


def is_allowed(role: str | None, action: str) -> bool:
    """Default deny: unknown role, unknown action, or an action the role lacks all return False."""
    return bool(role) and action in ACTIONS and action in ROLE_MATRIX.get(role, frozenset())


def stock_detail(role: str | None) -> str | None:
    return STOCK_DETAIL.get(role or "")


def scopes_for(role: str) -> tuple[str, ...]:
    return tuple(sorted({_SCOPE_OF[a] for a in ROLE_MATRIX.get(role, ()) if a in _SCOPE_OF}))


def matrix_rows() -> list[tuple[str, list[str]]]:
    roles = [*ROLE_MATRIX, "<unknown>"]
    return [(action, ["Y" if is_allowed(r, action) else "." for r in roles])
            for action in [*sorted(PARTNER_TOOLS), *sorted(OPERATIONS_TOOLS)]]


def self_check() -> list[str]:
    """The matrix's own invariants. Empty list = all hold. Also run by tests/test_access_control.py."""
    problems = []
    for role, actions in ROLE_MATRIX.items():
        unknown = actions - ACTIONS
        if unknown:
            problems.append(f"{role} grants unknown actions {sorted(unknown)}")
        if role != "svc_probe" and actions & PARTNER_TOOLS and actions & OPERATIONS_TOOLS:
            problems.append(f"{role} mixes partner and operations tools")
    if any(is_allowed(r, a) for r in ("", None, "admin", "root", "CHW", "contractor") for a in ACTIONS):
        problems.append("an unknown role is allowed something")
    if not is_allowed("chw", "triage.submit") or is_allowed("chw", "audit.read"):
        problems.append("chw row is wrong")
    if is_allowed("auditor", "mcp.check_stock") or is_allowed("partner_pharmacy", "triage.submit"):
        problems.append("operations/partner separation is broken")
    if set(STOCK_DETAIL) - {r for r in ROLE_MATRIX if "mcp.check_stock" in ROLE_MATRIX[r]}:
        problems.append("STOCK_DETAIL names a role that cannot call check_stock")
    return problems


def main() -> int:
    roles = [*ROLE_MATRIX, "<unknown>"]
    width = max(len(a) for a in ACTIONS) + 2
    print("action".ljust(width) + "kind".ljust(11) + " ".join(r[:9].ljust(9) for r in roles))
    for action, cells in matrix_rows():
        kind = "partner" if action in PARTNER_TOOLS else "operations"
        print(action.ljust(width) + kind.ljust(11) + " ".join(c.ljust(9) for c in cells))
    problems = self_check()
    for p in problems:
        print(f"FAIL: {p}")
    print("role matrix assertions: " + ("all pass (exit 0)" if not problems else f"{len(problems)} failed (exit 1)"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

"""The API's audit trail: one redacted-then-hashed JSON line per security-relevant decision (Deliverable 3).

Written to logs/audit.jsonl by security/redact_then_hash.py. Events:
  auth.login        POST /token                 success | denied
  auth.token        any protected route         unauthenticated (missing, forged or expired JWT)
  <rbac action>     the route's action          denied (role matrix) | success | blocked (injection guard) | error
"""

from __future__ import annotations

from functools import lru_cache

from afyaplus.logging_setup import trace_id_var
from afyaplus.settings import get_settings
from security.redact_then_hash import AuditTrail


@lru_cache(maxsize=1)
def get_audit_trail() -> AuditTrail:
    settings = get_settings()
    return AuditTrail(settings.log_dir / "audit.jsonl", settings.audit_hash_key.get_secret_value().encode())


def audit(*, actor: str, actor_role: str, action: str, resource: str, outcome: str, **context) -> dict:
    return get_audit_trail().record(actor=actor, actor_role=actor_role, action=action, resource=resource,
                                    outcome=outcome, trace_id=trace_id_var.get(), **context)

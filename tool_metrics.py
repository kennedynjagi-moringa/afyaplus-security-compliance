"""Shared counter store for tool and prompt calls (Thursday lab).

One SQLite file (TOOL_METRICS_DB, default logs/tool_metrics.db) is shared by every process that records:
- the MCP server (component "mcp", one row per tool call, labelled with the server version),
- the triage API (component "triage", one row per request, labelled with the prompt version and canary arm).

SQLite in WAL mode lets the API and the stdio MCP subprocess write at the same time without a broker.
The canary gate (scripts/canary_gate.py) and the dashboard sketch (mcp_dashboard_sketch.py) read it.
Recording must never break serving: if a write fails, it is logged and dropped.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

DEFAULT_DB = Path(os.environ.get("TOOL_METRICS_DB", Path(__file__).resolve().parent / "logs" / "tool_metrics.db"))

log = logging.getLogger("tool_metrics")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    ts          REAL    NOT NULL,
    component   TEXT    NOT NULL,   -- mcp | triage
    name        TEXT    NOT NULL,   -- tool name, or 'triage'
    version     TEXT    NOT NULL,   -- MCP server version, or prompt version
    arm         TEXT    NOT NULL DEFAULT '',   -- active | candidate for triage
    ok          INTEGER NOT NULL,
    error_code  TEXT,
    latency_ms  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS calls_by_key ON calls (component, name, version, ts);
"""


class ToolMetrics:
    def __init__(self, path: Path | str = DEFAULT_DB) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=5, check_same_thread=False, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            self._conn = conn
        return self._conn

    def record(self, component: str, name: str, version: str, ok: bool, latency_ms: float,
               error_code: str | None = None, arm: str = "") -> None:
        try:
            with self._lock:
                self._db().execute(
                    "INSERT INTO calls VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (time.time(), component, name, version, arm, int(ok), error_code, float(latency_ms)))
        except sqlite3.Error as exc:
            log.warning("tool_metrics_write_failed: %s", exc)

    def summary(self, component: str | None = None, since: float = 0.0) -> list[dict]:
        """Calls, errors, error rate and latency percentiles per (component, name, version, arm)."""
        with self._lock:
            rows = self._db().execute(
                "SELECT component, name, version, arm, ok, latency_ms FROM calls "
                "WHERE ts >= ? AND (? IS NULL OR component = ?) ORDER BY component, name, version, arm",
                (since, component, component)).fetchall()
        groups: dict[tuple, list[tuple[int, float]]] = {}
        for comp, name, version, arm, ok, latency in rows:
            groups.setdefault((comp, name, version, arm), []).append((ok, latency))
        out = []
        for (comp, name, version, arm), calls in groups.items():
            latencies = sorted(lat for _, lat in calls)
            errors = sum(1 for ok, _ in calls if not ok)
            out.append({
                "component": comp, "name": name, "version": version, "arm": arm,
                "calls": len(calls), "errors": errors, "error_rate": round(errors / len(calls), 4),
                "p50_ms": round(_pct(latencies, 50), 1), "p95_ms": round(_pct(latencies, 95), 1),
            })
        return out

    def error_rate(self, component: str, version: str | None = None, since: float = 0.0) -> tuple[int, float]:
        """(calls, error_rate) for a component, optionally one version."""
        rows = [r for r in self.summary(component, since) if version is None or r["version"] == version]
        calls = sum(r["calls"] for r in rows)
        errors = sum(r["errors"] for r in rows)
        return calls, (errors / calls if calls else 0.0)

    def reset(self) -> None:
        with self._lock:
            self._db().execute("DELETE FROM calls")


def _pct(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    k = (len(sorted_values) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)

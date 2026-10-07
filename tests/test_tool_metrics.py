from __future__ import annotations

from tool_metrics import ToolMetrics


def test_counts_error_rate_and_percentiles(tmp_path):
    m = ToolMetrics(tmp_path / "m.db")
    for i in range(9):
        m.record("mcp", "check_stock", "1.0.0", True, 10 + i)
    m.record("mcp", "check_stock", "1.0.0", False, 50, "UNKNOWN_CLINIC")
    m.record("triage", "triage", "1.3.0-candidate", True, 5, arm="candidate")
    [row] = [r for r in m.summary("mcp")]
    assert row["calls"] == 10 and row["errors"] == 1 and row["error_rate"] == 0.1
    assert row["p95_ms"] >= row["p50_ms"]
    assert m.error_rate("triage", "1.3.0-candidate") == (1, 0.0)


def test_shared_between_instances(tmp_path):
    ToolMetrics(tmp_path / "m.db").record("mcp", "find_clinics", "1.0.0", True, 1)
    assert ToolMetrics(tmp_path / "m.db").error_rate("mcp") == (1, 0.0)

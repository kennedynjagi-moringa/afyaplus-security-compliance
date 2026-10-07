"""Tool behaviour of the versioned logistics server (called in-process)."""

from __future__ import annotations

import logistics_mcp_versioned as srv


def test_version_is_semver_and_reported_to_clients():
    assert srv.mcp._mcp_server.version == srv.SERVER_VERSION
    assert srv.mcp.name == srv.SERVER_NAME == "afyaplus-logistics"


def test_check_stock_ok_and_errors_as_data():
    ok = srv.check_stock("KSM-001", "oxytocin_10iu")
    assert ok["ok"] and ok["status"] in {"available", "low", "out_of_stock"}
    assert srv.check_stock("Kisumu clinic", "ors_sachet")["error"]["code"] == "INVALID_CLINIC_ID"
    assert srv.check_stock("KSM-999", "ors_sachet")["error"]["code"] == "UNKNOWN_CLINIC"


def test_find_clinics_has_stock_item_filter_added_in_1_1_0():
    everyone = srv.find_clinics("Kisumu", limit=10)
    stocked = srv.find_clinics("Kisumu", has_stock_item="oxytocin_10iu", limit=10)
    assert stocked["ok"] and stocked["filters"]["has_stock_item"] == "oxytocin_10iu"
    assert stocked["total_matches"] <= everyone["total_matches"]
    assert all(c["stock"]["status"] != "out_of_stock" for c in stocked["clinics"])
    assert "stock" not in everyone["clinics"][0]          # old callers see the same shape as 1.0.0
    assert srv.find_clinics("Kisumu", has_stock_item="unicorn")["error"]["code"] == "UNKNOWN_ITEM"

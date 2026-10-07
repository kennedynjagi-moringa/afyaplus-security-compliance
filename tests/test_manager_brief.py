"""Deliverable 5: the brief has the four headings, cites real files, keeps finance humble, overclaims nothing."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRIEF = (ROOT / "manager_brief.md").read_text(encoding="utf-8")


def _section(title: str) -> str:
    return BRIEF.split(f"## {title}\n", 1)[1].split("\n## ", 1)[0]


def test_the_four_headings_are_present_in_order():
    headings = re.findall(r"^## (.+)$", BRIEF, re.M)
    assert headings == ["Risks", "Mitigations", "Kenya DPA alignment", "What we will not claim"]


def test_every_mitigation_row_cites_files_that_exist():
    rows = [r for r in _section("Mitigations").splitlines() if re.match(r"^\| \d", r)]
    assert len(rows) >= 7
    for row in rows:
        cited = re.findall(r"`([^`]+\.(?:py|json|jsonl|yaml|md|sh))`", row)
        assert cited, row
        for path in cited:
            assert (ROOT / path).is_file(), path


def test_no_overclaiming_words():
    lowered = BRIEF.lower()
    for word in ("unhackable", "100% secure", "fully compliant", "guaranteed", "bulletproof", "we are secure"):
        assert word not in lowered, word
    assert "legal review pending" in lowered


def test_finance_paragraph_says_recommend_not_decide():
    finance = BRIEF.split("### If finance wants to reuse this for credit", 1)[1]
    assert "recommend, never decide" in finance and "credit officer" in finance


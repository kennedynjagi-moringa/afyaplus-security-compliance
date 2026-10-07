from __future__ import annotations

from datetime import date

import check_prompt_pin as cpp
from prompt_loader import load_config


def test_repo_is_aligned():
    release = load_config().release
    results = cpp.run(f"v{release}", f"afyaplus-triage:{release}", date.today())
    assert all(ok for _, ok, _ in results), [r for r in results if not r[1]]


def test_wrong_tag_or_image_fails():
    results = dict((n, ok) for n, ok, _ in cpp.run("v0.0.1", "afyaplus-triage:0.0.1", date.today()))
    assert results["git_tag_matches_release"] is False
    assert results["image_tag_matches_release"] is False

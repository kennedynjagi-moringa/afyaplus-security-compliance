"""Offline test settings: replay backend, temporary log and metrics paths. No network, no API key."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.update({
    "JWT_SECRET": "test-secret-key-for-pytest-only-0123456789abcdef",
    "MODEL_BACKEND": "replay",
    "DEPLOY_ENV": "dev",
})
os.environ.pop("PROMPT_VERSION", None)
os.environ.pop("CANDIDATE_PERCENT", None)
os.environ.pop("OPENAI_API_KEY", None)


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("TOOL_METRICS_DB", str(tmp_path / "metrics.db"))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    from afyaplus.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()

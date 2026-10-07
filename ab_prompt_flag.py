"""Prompt canary flag (Thursday Lab 1): which prompt version serves this request?

Routing is deterministic. A routing key (the X-Routing-Key header, for example a CHW device or household id,
or else the username) is hashed with a salt that includes the candidate version. A bucket in 0-99 below
traffic_percent gets the candidate. So:
- the same key always sees the same prompt, and a CHW does not flip between versions mid-shift;
- changing the candidate version reshuffles the buckets, and a new canary doesn't reuse the old cohort;
- the candidate only ever serves in the environments listed in config (staging by default).

Levers, in order of precedence (all of them are environment variables, so no rebuild is needed):
1. PROMPT_VERSION=1.2.0  -> rollback: every request gets that version and the canary is off.
2. CANDIDATE_PERCENT=0   -> kill switch: the canary is off and the active version serves everything.
3. config/triage.yaml candidate.traffic_percent.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from prompt_loader import Bundle, Prompt


@dataclass(frozen=True)
class Assignment:
    prompt: Prompt
    arm: Literal["active", "candidate"]
    bucket: int


def bucket(key: str, salt: str) -> int:
    digest = hashlib.sha256(f"{salt}:{key}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % 100


def effective_percent(bundle: Bundle, environment: str, percent_override: int | None) -> int:
    """The share of keys the candidate gets in this environment (0 when there is no candidate)."""
    if bundle.candidate is None or environment not in bundle.config.candidate.environments:
        return 0
    return bundle.config.candidate.traffic_percent if percent_override is None else percent_override


def choose_prompt(bundle: Bundle, key: str, environment: str, percent_override: int | None = None) -> Assignment:
    percent = effective_percent(bundle, environment, percent_override)
    if bundle.candidate is None or percent <= 0:
        return Assignment(bundle.active, "active", -1)
    b = bucket(key, f"afyaplus-canary:{bundle.candidate.version}")
    if b < percent:
        return Assignment(bundle.candidate, "candidate", b)
    return Assignment(bundle.active, "active", b)

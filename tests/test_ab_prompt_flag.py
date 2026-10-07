"""The canary flag is deterministic, respects the configured share and environments, and obeys the levers."""

from __future__ import annotations

from dataclasses import replace

from ab_prompt_flag import bucket, choose_prompt, effective_percent
from prompt_loader import CandidateConfig, Prompt, load_bundle


def _bundle_with_candidate(percent: int = 10, environments=("staging",)):
    bundle = load_bundle()
    candidate = Prompt(version="9.0.0-candidate", sha256="f" * 64, text="candidate", file="x")
    config = bundle.config.model_copy(update={"candidate": CandidateConfig(
        prompt_version="9.0.0-candidate", traffic_percent=percent, environments=environments)})
    return replace(bundle, config=config, candidate=candidate)


def test_same_key_same_prompt():
    b = _bundle_with_candidate()
    assert {choose_prompt(b, "chw-0042", "staging").arm for _ in range(20)} == {
        choose_prompt(b, "chw-0042", "staging").arm}


def test_share_is_close_to_configured_percent():
    b = _bundle_with_candidate(10)
    n = 10_000
    share = sum(choose_prompt(b, f"key-{i}", "staging").arm == "candidate" for i in range(n)) / n
    assert 0.08 < share < 0.12


def test_candidate_never_serves_outside_its_environments():
    b = _bundle_with_candidate(100)
    assert all(choose_prompt(b, f"k{i}", "prod").arm == "active" for i in range(200))
    assert effective_percent(b, "dev", None) == 0


def test_kill_switch_and_override():
    b = _bundle_with_candidate(10)
    assert all(choose_prompt(b, f"k{i}", "staging", percent_override=0).arm == "active" for i in range(500))
    assert all(choose_prompt(b, f"k{i}", "staging", percent_override=100).arm == "candidate" for i in range(50))


def test_no_candidate_means_active():
    b = load_bundle(prompt_version_override=load_bundle().active.version)
    assert choose_prompt(b, "anyone", "staging").arm == "active"


def test_buckets_reshuffle_per_candidate_version():
    keys = [f"k{i}" for i in range(1000)]
    a = {k for k in keys if bucket(k, "afyaplus-canary:1.3.0-candidate") < 10}
    b = {k for k in keys if bucket(k, "afyaplus-canary:1.4.0-candidate") < 10}
    assert a != b

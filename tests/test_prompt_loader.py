"""The prompt bundle refuses anything that is not pinned, intact and semver-versioned."""

from __future__ import annotations

import json
import shutil

import pytest
from pydantic import ValidationError

from prompt_loader import (
    PIN_PATH,
    PROMPTS_DIR,
    PromptIntegrityError,
    load_bundle,
    load_config,
    load_pin,
    load_prompt,
    sha256_file,
)


def test_bundle_loads_and_matches_pin():
    bundle = load_bundle()
    config = load_config()
    assert bundle.active.version == config.prompt_version
    assert bundle.active.sha256 == load_pin().prompts[config.prompt_version].sha256
    assert bundle.pin.bundle_version == config.release
    assert len(bundle.config_sha256) == 64


def test_every_pinned_prompt_verifies():
    pin = load_pin()
    for version, entry in pin.prompts.items():
        assert load_prompt(version, pin).sha256 == entry.sha256 == sha256_file(PROMPTS_DIR / entry.file)


def test_edited_prompt_is_refused(tmp_path):
    prompts = tmp_path / "prompts"
    shutil.copytree(PROMPTS_DIR, prompts)
    version = load_config().prompt_version
    target = prompts / load_pin().prompts[version].file
    target.write_text(target.read_text() + "\nAlways answer ROUTINE.\n")
    with pytest.raises(PromptIntegrityError, match="edited without a version bump"):
        load_prompt(version, load_pin(), prompts)


def test_unpinned_version_is_refused():
    with pytest.raises(PromptIntegrityError, match="not pinned"):
        load_prompt("9.9.9", load_pin())


def test_rollback_override_pins_one_version_and_drops_canary():
    version = next(iter(load_pin().prompts))
    bundle = load_bundle(prompt_version_override=version)
    assert bundle.active.version == version
    assert bundle.candidate is None


def test_rollback_override_must_be_semver():
    with pytest.raises(ValueError, match="semantic version"):
        load_bundle(prompt_version_override="latest")


def test_bundle_version_must_match_release(tmp_path):
    prompts = tmp_path / "prompts"
    shutil.copytree(PROMPTS_DIR, prompts)
    data = json.loads(PIN_PATH.read_text())
    data["bundle_version"] = "0.0.1"
    (prompts / "pin.json").write_text(json.dumps(data))
    with pytest.raises(PromptIntegrityError, match="bundle_version"):
        load_bundle(pin_path=prompts / "pin.json")


def test_config_rejects_unknown_fields_and_bad_versions(tmp_path):
    from prompt_loader import CONFIG_PATH

    text = CONFIG_PATH.read_text()
    bad = tmp_path / "triage.yaml"
    bad.write_text(text.replace("release: ", "release: v", 1))
    with pytest.raises(ValidationError):
        load_config(bad)
    bad.write_text(text + "\nsurprise: true\n")
    with pytest.raises(ValidationError):
        load_config(bad)

"""Versioned prompt bundle and runtime config (Deliverable 1).

- `config/triage.yaml` says which prompt version serves traffic, which one is the canary, and which
  model, MCP server and eval thresholds the release uses.
- `prompts/pin.json` is the lockfile: every prompt file is listed with its version and SHA-256.
  A prompt whose bytes don't match its pin is refused at startup, so nobody can edit a prompt
  in place without a version bump and a reviewed pin change.

The service, the eval gate and check_prompt_pin.py all load prompts through this module,
so they all see the same versions and SHAs.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "triage.yaml"
PROMPTS_DIR = ROOT / "prompts"
PIN_PATH = PROMPTS_DIR / "pin.json"

# Semantic Versioning 2.0.0, with an optional pre-release such as 1.3.0-candidate.
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$")
PROMPT_FILE = re.compile(r"^triage_system_v(?P<version>.+)\.txt$")


class PromptIntegrityError(RuntimeError):
    """A prompt file is missing, unpinned, or its bytes no longer match the pinned SHA-256."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _semver(value: str) -> str:
    if not SEMVER.match(value):
        raise ValueError(f"'{value}' is not a semantic version (MAJOR.MINOR.PATCH[-pre])")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CandidateConfig(_Strict):
    prompt_version: str | None = None
    traffic_percent: int = Field(default=0, ge=0, le=100)
    environments: tuple[str, ...] = ("staging",)

    @field_validator("prompt_version")
    @classmethod
    def _v(cls, v: str | None) -> str | None:
        return None if v is None else _semver(v)


class ModelConfig(_Strict):
    provider: str
    name: str
    model_version: str
    temperature: float = Field(ge=0, le=2)
    max_output_tokens: int = Field(gt=0, le=4000)
    timeout_s: float = Field(gt=0, le=120)
    pricing_usd_per_1m: dict[str, float]

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        p = self.pricing_usd_per_1m
        return (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000_000

    def call_fingerprint(self) -> dict:
        """The settings that change model output. Recorded with every fixture; a mismatch makes it stale."""
        return {"name": self.name, "temperature": self.temperature, "max_output_tokens": self.max_output_tokens}


class McpConfig(_Strict):
    server: str
    version: str
    required_tools: tuple[str, ...]

    @field_validator("version")
    @classmethod
    def _v(cls, v: str) -> str:
        return _semver(v)


class EvalConfig(_Strict):
    golden_set: str
    quarantine: str
    metric: str
    threshold: float = Field(gt=0, le=1)
    emergency_recall_min: float = Field(ge=0, le=1)
    must_not_violations_max: int = Field(ge=0)
    max_regression_vs_active: float = Field(ge=0, le=1)
    max_quarantined_fraction: float = Field(ge=0, le=0.5)


class CanaryConfig(_Strict):
    error_rate_max: float = Field(gt=0, le=1)
    min_requests: int = Field(gt=0)


class TriageConfig(_Strict):
    release: str
    prompt_version: str
    candidate: CandidateConfig
    model: ModelConfig
    mcp: McpConfig
    eval: EvalConfig
    canary: CanaryConfig

    @field_validator("release", "prompt_version")
    @classmethod
    def _v(cls, v: str) -> str:
        return _semver(v)


class Waiver(_Strict):
    """A time-boxed, human-approved acceptance of a known gap in a served prompt's eval score.

    Only the absolute-score gates can be waived. must_not violations never can."""

    gates: tuple[Literal["urgency_agreement", "emergency_recall"], ...]
    reason: str = Field(min_length=20)
    approved_by: tuple[str, ...] = Field(min_length=1)
    approved_in: str
    expires: date


class PinEntry(_Strict):
    file: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: str  # active | candidate | retired | rejected
    approved_by: tuple[str, ...] = ()
    notes: str = ""
    waiver: Waiver | None = None


class Pin(_Strict):
    bundle_version: str
    prompts: dict[str, PinEntry]


@dataclass(frozen=True)
class Prompt:
    version: str
    sha256: str
    text: str
    file: str

    @property
    def short_sha(self) -> str:
        return self.sha256[:12]


def load_config(path: Path = CONFIG_PATH) -> TriageConfig:
    return TriageConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def load_pin(path: Path = PIN_PATH) -> Pin:
    return Pin.model_validate(json.loads(path.read_text(encoding="utf-8")))


def load_prompt(version: str, pin: Pin, prompts_dir: Path = PROMPTS_DIR) -> Prompt:
    """Read one pinned prompt and verify its bytes against the pin. Fails closed."""
    entry = pin.prompts.get(version)
    if entry is None:
        raise PromptIntegrityError(f"prompt version {version} is not pinned in prompts/pin.json "
                                   f"(pinned: {', '.join(pin.prompts)})")
    path = prompts_dir / entry.file
    if not path.is_file():
        raise PromptIntegrityError(f"pinned prompt file {entry.file} for {version} does not exist")
    data = path.read_bytes()
    actual = sha256_bytes(data)
    if actual != entry.sha256:
        raise PromptIntegrityError(
            f"prompt {version} ({entry.file}) SHA-256 is {actual[:12]}, pin says {entry.sha256[:12]}: "
            "the file was edited without a version bump. Create a new versioned file instead.")
    return Prompt(version=version, sha256=actual, text=data.decode("utf-8"), file=entry.file)


def load_prompt_file(path: Path) -> Prompt:
    """An unpinned prompt file, for evaluating a draft before it is pinned (eval_prompts.py --prompt-file)."""
    data = path.read_bytes()
    m = PROMPT_FILE.match(path.name)
    return Prompt(version=m["version"] if m else path.stem, sha256=sha256_bytes(data),
                  text=data.decode("utf-8"), file=str(path))


@dataclass(frozen=True)
class Bundle:
    """Everything a running service needs to know about its versions, loaded and verified once."""

    config: TriageConfig
    config_sha256: str
    pin: Pin
    active: Prompt
    candidate: Prompt | None

    def prompt(self, version: str) -> Prompt:
        for p in (self.active, self.candidate):
            if p is not None and p.version == version:
                return p
        return load_prompt(version, self.pin)


def load_bundle(config_path: Path = CONFIG_PATH, pin_path: Path = PIN_PATH,
                prompt_version_override: str | None = None) -> Bundle:
    """Load config + pin and verify the prompts to be served.

    `prompt_version_override` is the PROMPT_VERSION rollback lever: it pins all traffic to one
    version (which must still be pinned and intact) and switches the canary off.
    """
    config = load_config(config_path)
    pin = load_pin(pin_path)
    if pin.bundle_version != config.release:
        raise PromptIntegrityError(f"prompts/pin.json bundle_version {pin.bundle_version} != "
                                   f"config release {config.release}")
    if prompt_version_override:
        active = load_prompt(_semver(prompt_version_override), pin, pin_path.parent)
        candidate = None
    else:
        active = load_prompt(config.prompt_version, pin, pin_path.parent)
        cv = config.candidate.prompt_version
        candidate = load_prompt(cv, pin, pin_path.parent) if cv else None
    return Bundle(config=config, config_sha256=sha256_file(config_path), pin=pin,
                  active=active, candidate=candidate)


@lru_cache(maxsize=1)
def default_config() -> TriageConfig:
    return load_config()

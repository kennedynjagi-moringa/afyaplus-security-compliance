"""Triage model backends behind one contract, plus the code-level safety rules (Weeks 6 and 7).

Backends
- `openai`: the live model from config/triage.yaml, with the served prompt as the system message.
- `replay`: the recorded output for this exact prompt SHA (fixtures/responses/<sha12>.json), looked up by
  message hash. It costs $0 and is deterministic, so CI, staging smoke tests and the eval gate run
  without an API key. When a message has no recording, the keyword rules answer it, and the response's
  backend field says `rules(replay-miss)`.
- `rules`: deterministic keyword rules.

Whatever the backend says, `apply_safety_rules` runs afterwards. A recognised danger sign, in English
or Swahili, always forces EMERGENCY. The prompt can raise urgency but never talk the service out of an
emergency, and the eval gate scores this same final output.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

import openai
from openai import OpenAI

from afyaplus.models import TriageModelOutput, TriageRequest, Urgency
from prompt_loader import ROOT, ModelConfig, Prompt

FIXTURES_DIR = ROOT / "fixtures" / "responses"

FIXED_ACTIONS: dict[Urgency, str] = {
    "EMERGENCY": "Refer immediately to the nearest facility with emergency care; call the county ambulance if available.",  # noqa: E501
    "URGENT": "Arrange for the patient to see a clinician within 24 hours and follow up by phone.",
    "ROUTINE": "Give standard advice and review at the next scheduled household visit.",
}

# Danger signs, English and Swahili. Matching any of them forces EMERGENCY.
DANGER_SIGNS: dict[str, re.Pattern[str]] = {
    name: re.compile(pattern, re.IGNORECASE)
    for name, pattern in {
        "difficulty breathing": r"(difficult\w* breath|can'?t breathe|cannot breathe|shida ya kupumua|anashindwa kupumua|hawezi kupumua)",  # noqa: E501
        "chest pain": r"(chest pain|maumivu ya kifua)",
        "unconscious": r"(unconscious|unresponsive|fainted|hajitambui|amezimia)",
        "convulsions": r"(convuls\w*|seizure\w*|fits\b|degedege|kifafa)",
        "severe bleeding": r"((severe|heavy) bleeding|bleeding heavily|damu nyingi)",
        "vomiting everything": r"(vomit\w* everything|anatapika kila kitu)",
        "unable to drink or breastfeed": r"(unable to (drink|breastfeed)|can'?t (drink|breastfeed)|hawezi (kunywa|kunyonya))",  # noqa: E501
        "stiff neck": r"(stiff neck|shingo ngumu)",
        "bluish lips": r"(blue lips|bluish lips|midomo ya bluu)",
    }.items()
}

_URGENT_WORDS = re.compile(
    r"(fever|homa|diarr?h?oea|kuhara|vomit|kutapika|pregnan|mjamzito|blood in|swelling|kuvimba|pain|maumivu)",
    re.IGNORECASE,
)

MAX_ITEMS, MAX_RATIONALE, MAX_ACTION = 10, 400, 300


class ModelUnavailable(RuntimeError):
    """The upstream model could not be reached or gave no usable answer; the route maps this to 503."""


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True)
class ModelAnswer:
    output: TriageModelOutput
    usage: Usage
    model: str          # what actually answered (snapshot id, "replay:<sha12>", "keyword-rules")
    backend: str


def message_sha256(req: TriageRequest) -> str:
    return hashlib.sha256(req.render().encode("utf-8")).hexdigest()


def detect_danger_signs(text: str) -> list[str]:
    return [name for name, pattern in DANGER_SIGNS.items() if pattern.search(text)]


def rules_model(req: TriageRequest) -> TriageModelOutput:
    signs = detect_danger_signs(req.message)
    if signs:
        urgency: Urgency = "EMERGENCY"
    elif _URGENT_WORDS.search(req.message):
        urgency = "URGENT"
    else:
        urgency = "ROUTINE"
    symptoms = sorted({m.group(0).lower() for m in _URGENT_WORDS.finditer(req.message)})
    return TriageModelOutput(
        urgency=urgency, symptoms=symptoms, danger_signs=signs,
        rationale=f"Keyword rules matched {len(signs)} danger sign(s) and {len(symptoms)} symptom keyword(s).",
        recommended_action=FIXED_ACTIONS[urgency],
    )


def apply_safety_rules(req: TriageRequest, out: TriageModelOutput) -> tuple[TriageModelOutput, bool]:
    """Force EMERGENCY on any code-detected danger sign; clip free text to contract limits."""
    signs = detect_danger_signs(req.message)
    override = bool(signs) and out.urgency != "EMERGENCY"
    urgency: Urgency = "EMERGENCY" if signs else out.urgency
    return TriageModelOutput(
        urgency=urgency,
        symptoms=out.symptoms[:MAX_ITEMS],
        danger_signs=list(dict.fromkeys([*out.danger_signs, *signs]))[:MAX_ITEMS],
        rationale=out.rationale[:MAX_RATIONALE],
        recommended_action=FIXED_ACTIONS[urgency] if override else out.recommended_action[:MAX_ACTION],
    ), override


class Backend(Protocol):
    name: str

    def classify(self, prompt: Prompt, req: TriageRequest) -> ModelAnswer: ...


class RulesBackend:
    name = "rules"

    def classify(self, prompt: Prompt, req: TriageRequest) -> ModelAnswer:
        return ModelAnswer(rules_model(req), Usage(), "keyword-rules", "rules")


@lru_cache(maxsize=1)
def _client(api_key: str, timeout: float) -> OpenAI:
    return OpenAI(api_key=api_key, timeout=timeout, max_retries=2)


class OpenAIBackend:
    name = "openai"

    def __init__(self, model: ModelConfig, api_key: str) -> None:
        self.model = model
        self._client = _client(api_key, model.timeout_s)
        self.last_system_fingerprint: str | None = None

    def classify(self, prompt: Prompt, req: TriageRequest) -> ModelAnswer:
        try:
            completion = self._client.chat.completions.parse(
                model=self.model.name,
                temperature=self.model.temperature,
                max_tokens=self.model.max_output_tokens,
                messages=[{"role": "system", "content": prompt.text}, {"role": "user", "content": req.render()}],
                response_format=TriageModelOutput,
            )
        except (openai.APIConnectionError, openai.APITimeoutError, openai.RateLimitError,
                openai.InternalServerError) as exc:
            raise ModelUnavailable(f"Triage model temporarily unavailable ({type(exc).__name__}).") from exc
        message = completion.choices[0].message
        if message.parsed is None:  # refusal or truncated output
            raise ModelUnavailable("Triage model returned no usable classification.")
        self.last_system_fingerprint = completion.system_fingerprint
        u = completion.usage
        return ModelAnswer(message.parsed, Usage(u.prompt_tokens, u.completion_tokens,
                                                 self.model.cost_usd(u.prompt_tokens, u.completion_tokens)),
                           completion.model, "openai")


def fixture_path(prompt_sha256: str, fixtures_dir: Path = FIXTURES_DIR) -> Path:
    return fixtures_dir / f"{prompt_sha256[:12]}.json"


def load_fixture(prompt_sha256: str, fixtures_dir: Path = FIXTURES_DIR) -> dict | None:
    path = fixture_path(prompt_sha256, fixtures_dir)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("prompt_sha256") != prompt_sha256:
        raise ValueError(f"{path.name} records prompt SHA {data.get('prompt_sha256', '?')[:12]}, "
                         f"expected {prompt_sha256[:12]}")
    return data


class ReplayBackend:
    """Recorded answers for the served prompt. Never calls a model, never costs anything."""

    name = "replay"

    def __init__(self, fixtures_dir: Path = FIXTURES_DIR) -> None:
        self.fixtures_dir = fixtures_dir
        self._index: dict[str, dict[str, TriageModelOutput]] = {}

    def _for(self, prompt: Prompt) -> dict[str, TriageModelOutput]:
        if prompt.sha256 not in self._index:
            data = load_fixture(prompt.sha256, self.fixtures_dir) or {"responses": {}}
            self._index[prompt.sha256] = {r["message_sha256"]: TriageModelOutput(**r["output"])
                                          for r in data["responses"].values()}
        return self._index[prompt.sha256]

    def classify(self, prompt: Prompt, req: TriageRequest) -> ModelAnswer:
        recorded = self._for(prompt).get(message_sha256(req))
        if recorded is None:
            return ModelAnswer(rules_model(req), Usage(), "keyword-rules", "rules(replay-miss)")
        return ModelAnswer(recorded, Usage(), f"replay:{prompt.short_sha}", "replay")


def build_backend(kind: str, model: ModelConfig, api_key: str | None = None) -> Backend:
    if kind == "openai":
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required for the openai backend")
        return OpenAIBackend(model, api_key)
    if kind == "replay":
        return ReplayBackend()
    if kind == "rules":
        return RulesBackend()
    raise ValueError(f"unknown backend {kind!r}")

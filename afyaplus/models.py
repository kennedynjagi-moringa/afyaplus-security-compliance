"""Typed request/response contracts. Requests forbid unknown fields (Week 6 habit)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Urgency = Literal["EMERGENCY", "URGENT", "ROUTINE"]

DISCLAIMER = (
    "Operational triage support for community health workers, not a diagnosis. "
    "A qualified clinician makes the final decision."
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 (OAuth2 token type, not a secret)
    expires_in: int = Field(description="Seconds until the token expires")
    role: str
    scopes: list[str]


class TriageRequest(StrictModel):
    message: str = Field(min_length=10, max_length=1000, examples=[
        "Mtoto wa miaka 2 ana homa kali tangu jana na anatapika kila kitu."])
    patient_age_years: int | None = Field(default=None, ge=0, le=120)
    county: str | None = Field(default=None, min_length=3, max_length=40, pattern=r"^[A-Za-z' -]+$")

    def render(self) -> str:
        """The user message the model sees; the same rendering is used when fixtures are recorded."""
        context = []
        if self.patient_age_years is not None:
            context.append(f"Patient age: {self.patient_age_years} years.")
        if self.county:
            context.append(f"County: {self.county}.")
        return " ".join([*context, f"Message: {self.message}"])


class TriageModelOutput(StrictModel):
    """The schema the model must fill (OpenAI structured outputs)."""

    urgency: Urgency
    symptoms: list[str]
    danger_signs: list[str]
    rationale: str
    recommended_action: str


class TriageResponse(BaseModel):
    request_id: str
    urgency: Urgency
    symptoms: list[str]
    danger_signs: list[str]
    rationale: str
    recommended_action: str
    safety_override: bool = Field(description="True when code-level danger-sign rules raised the model's urgency")
    prompt_version: str
    prompt_sha256: str
    arm: Literal["active", "candidate"]
    model: str
    backend: str
    latency_ms: int
    disclaimer: str = DISCLAIMER


class PromptInfo(BaseModel):
    version: str
    sha256: str
    traffic_percent: int | None = None


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str
    release: str
    image_tag: str
    git_sha: str
    environment: str
    model_backend: str
    prompt_bundle: str
    prompt_active: PromptInfo
    prompt_candidate: PromptInfo | None
    rollback_override: bool = Field(description="True when PROMPT_VERSION pins traffic to one version")
    config_sha256: str
    model: str
    model_version: str
    mcp_server: str
    mcp_version: str

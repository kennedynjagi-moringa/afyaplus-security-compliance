"""Runtime settings from the environment (or a local .env file).

Versions are NOT settings: the release, prompt, model and MCP versions come from the files baked into
the image (config/triage.yaml, prompts/pin.json). The environment only carries secrets, the backend
choice, and two operational levers: PROMPT_VERSION (rollback) and CANDIDATE_PERCENT (canary kill switch).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ---- auth (unchanged from Weeks 6 and 7) ----
    jwt_secret: SecretStr = Field(min_length=32)
    jwt_algorithm: Literal["HS256"] = "HS256"
    jwt_issuer: str = "afyaplus-auth"
    jwt_audience: str = "afyaplus-api"
    access_token_minutes: int = Field(default=30, ge=1, le=1440)

    # ---- model backend ----
    # openai: live call. replay: recorded responses for the served prompt's SHA (fixtures/responses/), $0.
    # rules: deterministic keyword stub, $0.
    model_backend: Literal["openai", "replay", "rules"] = "replay"
    openai_api_key: SecretStr | None = None

    # ---- release levers ----
    deploy_env: Literal["dev", "staging", "prod"] = "dev"
    prompt_version: str | None = None          # rollback: pin all traffic to this version, canary off
    candidate_percent: int | None = Field(default=None, ge=0, le=100)  # canary kill switch / override

    # ---- stamped at build time (Dockerfile ARGs), reported by /health ----
    image_tag: str = "afyaplus-triage:dev"
    git_sha: str = "unknown"

    log_dir: Path = PROJECT_ROOT / "logs"
    tool_metrics_db: Path = PROJECT_ROOT / "logs" / "tool_metrics.db"

    @field_validator("prompt_version", "candidate_percent", mode="before")
    @classmethod
    def _blank_is_unset(cls, v):
        """docker compose passes `PROMPT_VERSION=` for an unset variable; treat blank as not set."""
        return None if isinstance(v, str) and not v.strip() else v

    @model_validator(mode="after")
    def _openai_key_required(self) -> Settings:
        if self.model_backend == "openai" and not self.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when MODEL_BACKEND=openai (or use replay / rules)")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

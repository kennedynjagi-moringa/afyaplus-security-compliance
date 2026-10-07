"""AfyaPlus triage API, Week 8: the Week 6/7 service with versioned prompts, a canary flag and version
reporting.

Routes
- GET  /health      public. Release, image tag, git SHA, prompt versions + SHA-256, config SHA, model,
                    model_version, MCP server version, and whether the PROMPT_VERSION rollback is in force.
- GET  /ready       public. Spawns the bundled MCP server and runs the same probe as CI
                    (scripts/check_mcp_health.py). 200 when healthy, 503 otherwise. Cached for 60 s.
- POST /token       OAuth2 password login -> JWT (unchanged from Weeks 6 and 7).
- POST /v1/triage   scope triage:write. The prompt is chosen per routing key (ab_prompt_flag.py).
                    Response headers: X-Prompt-Version, X-Prompt-SHA, X-Prompt-Arm, X-Release.
- GET  /v1/metrics  scope audit:read. Per-prompt-version and per-tool counters from tool_metrics.py
                    (what scripts/canary_gate.py reads for its go / no-go).

Run: uvicorn prompt_app:app --port 8080
"""

from __future__ import annotations

import argparse
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse

from ab_prompt_flag import choose_prompt, effective_percent
from afyaplus import SERVICE_NAME
from afyaplus.auth import require_scopes
from afyaplus.auth import router as auth_router
from afyaplus.logging_setup import configure_logging, trace_id_var
from afyaplus.models import HealthResponse, PromptInfo, TriageRequest, TriageResponse
from afyaplus.settings import get_settings
from afyaplus.users import User
from prompt_loader import Bundle, load_bundle
from tool_metrics import ToolMetrics
from triage_engine import ModelUnavailable, apply_safety_rules, build_backend

log = logging.getLogger("api")
_VALID_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_VALID_KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
READY_CACHE_S = 60


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()  # fails fast on missing secrets
    configure_logging("api", settings.log_dir, "api.jsonl")
    # Verifies every served prompt against prompts/pin.json; a tampered or unpinned prompt stops startup.
    bundle = load_bundle(prompt_version_override=settings.prompt_version)
    app.state.bundle = bundle
    app.state.backend = build_backend(
        settings.model_backend, bundle.config.model,
        settings.openai_api_key.get_secret_value() if settings.openai_api_key else None)
    app.state.metrics = ToolMetrics(settings.tool_metrics_db)
    app.state.ready = (0.0, None)
    log.info("service_started", extra=version_info(bundle) | {
        "model_backend": settings.model_backend, "environment": settings.deploy_env,
        "candidate_percent": effective_percent(bundle, settings.deploy_env, settings.candidate_percent)})
    yield


app = FastAPI(title="AfyaPlus Triage API (versioned prompts)", lifespan=lifespan)
app.include_router(auth_router)


def version_info(bundle: Bundle) -> dict:
    settings = get_settings()
    return {
        "release": bundle.config.release, "image_tag": settings.image_tag, "git_sha": settings.git_sha,
        "prompt_bundle": bundle.pin.bundle_version, "prompt_active": bundle.active.version,
        "prompt_active_sha": bundle.active.short_sha,
        "prompt_candidate": bundle.candidate.version if bundle.candidate else None,
        "config_sha": bundle.config_sha256[:12], "model": bundle.config.model.name,
        "model_version": bundle.config.model.model_version, "mcp_version": bundle.config.mcp.version,
        "rollback_override": settings.prompt_version is not None,
    }


@app.middleware("http")
async def trace_requests(request: Request, call_next):
    """Assign a trace id (or accept a well-formed X-Request-ID) and write one access-log line."""
    incoming = request.headers.get("x-request-id", "")
    request_id = incoming if _VALID_ID.match(incoming) else uuid.uuid4().hex
    token = trace_id_var.set(request_id)
    request.state.request_id = request_id
    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        log.info("http_request", extra={"method": request.method, "path": request.url.path, "status": status_code,
                                        "latency_ms": int((time.perf_counter() - started) * 1000)})
        trace_id_var.reset(token)


@app.exception_handler(ModelUnavailable)
async def model_unavailable(request: Request, exc: ModelUnavailable) -> JSONResponse:
    log.warning("model_unavailable", extra={"reason": str(exc)})
    return JSONResponse(status_code=503, content={"detail": str(exc), "request_id": request.state.request_id},
                        headers={"Retry-After": "10"})


@app.get("/health", response_model=HealthResponse, tags=["platform"])
def health(request: Request) -> HealthResponse:
    settings = get_settings()
    b: Bundle = request.app.state.bundle
    percent = effective_percent(b, settings.deploy_env, settings.candidate_percent)
    return HealthResponse(
        service=SERVICE_NAME, release=b.config.release, image_tag=settings.image_tag, git_sha=settings.git_sha,
        environment=settings.deploy_env, model_backend=settings.model_backend,
        prompt_bundle=b.pin.bundle_version,
        prompt_active=PromptInfo(version=b.active.version, sha256=b.active.sha256, traffic_percent=100 - percent),
        prompt_candidate=PromptInfo(version=b.candidate.version, sha256=b.candidate.sha256,
                                    traffic_percent=percent) if b.candidate else None,
        rollback_override=settings.prompt_version is not None,
        config_sha256=b.config_sha256, model=b.config.model.name, model_version=b.config.model.model_version,
        mcp_server=b.config.mcp.server, mcp_version=b.config.mcp.version,
    )


@app.get("/ready", tags=["platform"])
async def ready(request: Request) -> JSONResponse:
    """Readiness: the bundled MCP server passes the CI probe. Cached so probes don't spawn a process each time."""
    checked_at, cached = request.app.state.ready
    if cached is None or time.monotonic() - checked_at > READY_CACHE_S:
        from scripts.check_mcp_health import Report, probe

        report = Report(target="live")
        try:
            await probe(argparse.Namespace(target="live", command=None, timeout=15.0), report)
        except Exception as exc:  # noqa: BLE001 - any failure means not ready
            report.unreachable = f"{type(exc).__name__}: {exc}"[:200]
        cached = {"ready": report.exit_code == 0, "mcp": report.server, "exit_code": report.exit_code,
                  "failed_checks": [c["check"] for c in report.checks if not c["ok"]],
                  "unreachable": report.unreachable}
        request.app.state.ready = (time.monotonic(), cached)
    return JSONResponse(status_code=200 if cached["ready"] else 503, content=cached)


@app.post("/v1/triage", response_model=TriageResponse, tags=["triage"])
def triage_case(
    body: TriageRequest,
    request: Request,
    response: Response,
    user: Annotated[User, Depends(require_scopes("triage:write"))],
    x_routing_key: Annotated[str | None, Header()] = None,
) -> TriageResponse:
    settings = get_settings()
    bundle: Bundle = request.app.state.bundle
    metrics: ToolMetrics = request.app.state.metrics
    key = x_routing_key if x_routing_key and _VALID_KEY.match(x_routing_key) else user.username
    assignment = choose_prompt(bundle, key, settings.deploy_env, settings.candidate_percent)
    prompt = assignment.prompt
    started = time.perf_counter()
    try:
        answer = request.app.state.backend.classify(prompt, body)
    except ModelUnavailable:
        metrics.record("triage", "triage", prompt.version, False, (time.perf_counter() - started) * 1000,
                       "MODEL_UNAVAILABLE", assignment.arm)
        raise
    result, override = apply_safety_rules(body, answer.output)
    latency_ms = int((time.perf_counter() - started) * 1000)
    metrics.record("triage", "triage", prompt.version, True, latency_ms, arm=assignment.arm)
    log.info("triage_completed", extra={
        "prompt_version": prompt.version, "prompt_sha": prompt.short_sha, "arm": assignment.arm,
        "bucket": assignment.bucket, "backend": answer.backend, "model": answer.model, "urgency": result.urgency,
        "model_urgency": answer.output.urgency, "safety_override": override, "latency_ms": latency_ms,
        "input_tokens": answer.usage.input_tokens, "output_tokens": answer.usage.output_tokens,
        "cost_usd": round(answer.usage.cost_usd, 8), "release": bundle.config.release,
    })
    response.headers["X-Prompt-Version"] = prompt.version
    response.headers["X-Prompt-SHA"] = prompt.short_sha
    response.headers["X-Prompt-Arm"] = assignment.arm
    response.headers["X-Release"] = bundle.config.release
    return TriageResponse(
        request_id=request.state.request_id, **result.model_dump(), safety_override=override,
        prompt_version=prompt.version, prompt_sha256=prompt.sha256, arm=assignment.arm,
        model=answer.model, backend=answer.backend, latency_ms=latency_ms,
    )


@app.get("/v1/metrics", tags=["observability"])
def metrics_summary(
    request: Request,
    user: Annotated[User, Depends(require_scopes("audit:read"))],
    since: float = 0.0,
) -> dict:
    metrics: ToolMetrics = request.app.state.metrics
    return {"release": request.app.state.bundle.config.release, "since": since, "rows": metrics.summary(since=since)}

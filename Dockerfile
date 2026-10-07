# syntax=docker/dockerfile:1
# AfyaPlus triage API + versioned prompt bundle + logistics MCP server (spawned over stdio) in one image.
# Build:  scripts/build_and_tag.sh      (checks release alignment, then tags the image to match the git tag)
# Run:    docker compose up -d --wait   (see docker-compose.yml)

ARG PYTHON_VERSION=3.11

# ---- builder: dependencies into a venv ---------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY requirements.txt .
RUN pip install -r requirements.txt

# ---- runtime -------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime
ARG RELEASE=dev
ARG GIT_SHA=unknown
ARG PROMPT_ACTIVE=unknown
ARG PROMPT_ACTIVE_SHA=unknown
ARG PROMPT_CANDIDATE=none
ARG MCP_VERSION=unknown
ARG MODEL_VERSION=unknown
# Every version the release ships, readable with `docker image inspect` without starting the container.
LABEL org.opencontainers.image.title="afyaplus-triage" \
      org.opencontainers.image.version="${RELEASE}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      ai.afyaplus.prompt.active="${PROMPT_ACTIVE}" \
      ai.afyaplus.prompt.active.sha256="${PROMPT_ACTIVE_SHA}" \
      ai.afyaplus.prompt.candidate="${PROMPT_CANDIDATE}" \
      ai.afyaplus.mcp.version="${MCP_VERSION}" \
      ai.afyaplus.model.version="${MODEL_VERSION}"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    LOG_DIR=/app/logs \
    TOOL_METRICS_DB=/app/logs/tool_metrics.db \
    IMAGE_TAG=afyaplus-triage:${RELEASE} \
    GIT_SHA=${GIT_SHA}
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin afya
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
# Least- to most-frequently changed.
COPY clinics.json mcp_schema.lock.json ./
COPY afyaplus/ afyaplus/
COPY tool_metrics.py logistics_mcp_versioned.py prompt_loader.py triage_engine.py ab_prompt_flag.py prompt_app.py ./
COPY scripts/__init__.py scripts/check_mcp_health.py scripts/mcp_stub_server.py scripts/
COPY fixtures/ fixtures/
COPY config/ config/
COPY prompts/ prompts/
RUN mkdir -p logs && chown afya:afya logs
USER afya
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)"]
# No secrets in the image: JWT_SECRET (and OPENAI_API_KEY for the live backend) arrive at runtime.
CMD ["uvicorn", "prompt_app:app", "--host", "0.0.0.0", "--port", "8080"]

#!/usr/bin/env bash
# Run the CI stages locally, in the same order and with the same commands as .github/workflows/ci.yml
# and azure-pipelines.yml, stopping at the first red stage (just as `needs:` / `dependsOn` do).
# Each stage's output is teed to $OUT_DIR/<stage>.log.
#   scripts/run_pipeline_local.sh                     # full run
#   DEMO_FAILURE=eval scripts/run_pipeline_local.sh   # cost-trim draft also gated -> eval red -> no deploy
#   DEMO_FAILURE=mcp  scripts/run_pipeline_local.sh   # stub without check_stock  -> mcp-health red -> no deploy
#   MCP_TARGET=stub   scripts/run_pipeline_local.sh   # fallback probe target
set -uo pipefail
cd "$(dirname "$0")/.."
export PYTHON=${PYTHON:-.venv/bin/python}
export MODEL_BACKEND=replay DEPLOY_ENV=staging HOST_PORT=${HOST_PORT:-8080}
OUT_DIR=${OUT_DIR:-logs/pipeline}
DEMO_FAILURE=${DEMO_FAILURE:-none}
MCP_TARGET=${MCP_TARGET:-live}
mkdir -p "$OUT_DIR"
REF=$(git describe --tags --exact-match 2>/dev/null || git rev-parse --abbrev-ref HEAD)
echo "local pipeline: ref=$REF commit=$(git rev-parse --short=12 HEAD) dirty=$([[ -n $(git status --porcelain --untracked-files=no) ]] && echo yes || echo no) demo_failure=$DEMO_FAILURE mcp_target=$MCP_TARGET"

stage() {   # stage <name> <command...>: run, tee, and stop the pipeline if it fails
  local name=$1; shift
  echo; echo "================ stage: $name ================"
  local started=$SECONDS
  ( set -o pipefail; "$@" 2>&1 | tee "$OUT_DIR/$name.log" )
  local rc=$?
  echo "---------------- $name: $([[ $rc == 0 ]] && echo PASSED || echo "FAILED (exit $rc)") in $((SECONDS - started))s"
  if [[ $rc != 0 ]]; then
    echo; echo "PIPELINE FAILED at stage '$name': no later stage ran, and nothing was deployed past this point."
    exit "$rc"
  fi
}

lint_test() {
  .venv/bin/ruff check . && \
  if [[ "$REF" == v* ]]; then $PYTHON check_prompt_pin.py --tag "$REF"; else $PYTHON check_prompt_pin.py; fi && \
  JWT_SECRET=local-pipeline-not-a-secret-0123456789abcdef $PYTHON -m pytest -q
}
eval_gate() {
  $PYTHON eval_prompts.py || return $?
  if [[ "$DEMO_FAILURE" == eval ]]; then
    echo; echo "## demo_failure=eval: gating the cost-trim draft"
    $PYTHON eval_prompts.py --prompt-file evals/demo/triage_system_v1.3.1-costtrim.txt
  fi
}
mcp_health() {
  $PYTHON scripts/check_mcp_health.py --target "$MCP_TARGET" || return $?
  if [[ "$DEMO_FAILURE" == mcp ]]; then
    echo; echo "## demo_failure=mcp: probing a server that dropped check_stock"
    MCP_STUB_FAULT=drop_tool $PYTHON scripts/check_mcp_health.py --target stub
  fi
}
build_deploy() {
  if ! docker info >/dev/null 2>&1; then echo "docker is not available: build-deploy cannot run"; return 1; fi
  local mode=""; [[ "$REF" == v* ]] && mode="--release"
  local out; out=$(mktemp)
  GITHUB_OUTPUT=$out scripts/build_and_tag.sh $mode || return $?
  export IMAGE; IMAGE=$(grep '^image=' "$out" | cut -d= -f2)
  export JWT_SECRET; JWT_SECRET=$($PYTHON -c 'import secrets; print(secrets.token_urlsafe(48))')
  scripts/deploy_stub.sh || return $?
  echo; echo "== rollback drill: same image, PROMPT_VERSION=$($PYTHON -c 'from prompt_loader import load_config; print(load_config().prompt_version)')"
  PROMPT_VERSION=$($PYTHON -c 'from prompt_loader import load_config; print(load_config().prompt_version)') \
    bash -c 'docker compose up -d --wait --force-recreate && $PYTHON scripts/verify_deploy.py --url http://127.0.0.1:$HOST_PORT'
  local rc=$?
  docker compose down -v >/dev/null 2>&1
  return $rc
}

stage lint-test lint_test
stage eval eval_gate
stage mcp-health mcp_health
stage build-deploy build_deploy
echo; echo "PIPELINE PASSED: lint-test -> eval -> mcp-health -> build-deploy"

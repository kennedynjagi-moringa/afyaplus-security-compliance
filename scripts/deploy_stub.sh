#!/usr/bin/env bash
# Deploy stage (stub): no Kubernetes or cloud, so "deploy" = docker compose up of the tagged image on this
# machine (CI runner or laptop), then the same gates a real staging rollout would run:
#   1 verify_deploy.py   /health versions + SHAs match this commit; /ready (in-app MCP probe) is 200
#   2 MCP probe inside the running container (the image's own server, not the repo's)
#   3 smoke_traffic.py   golden messages from 600 routing keys (~60 on a 10% canary); every answer carries its arm's prompt SHA
#   4 canary_gate.py     error rate per arm <= 0.10, enough candidate traffic, MCP lists check_stock
# A failed step fails the stage. A NO-GO canary also rolls the canary back (CANDIDATE_PERCENT=0).
#   IMAGE=afyaplus-triage:1.3.0 scripts/deploy_stub.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python}
: "${IMAGE:?set IMAGE, e.g. afyaplus-triage:1.3.0}"
export IMAGE
URL="http://127.0.0.1:${HOST_PORT:-8080}"

echo "== deploy $IMAGE (DEPLOY_ENV=${DEPLOY_ENV:-staging}, PROMPT_VERSION=${PROMPT_VERSION:-<config>}, CANDIDATE_PERCENT=${CANDIDATE_PERCENT:-<config>})"
docker compose up -d --wait --force-recreate
docker compose ps --format '{{.Name}} {{.Image}} {{.Status}}'

echo "== 1/4 verify versions"
$PY scripts/verify_deploy.py --url "$URL"

echo "== 2/4 MCP probe inside the container"
docker compose exec -T triage python scripts/check_mcp_health.py

echo "== 3/4 smoke traffic"
$PY scripts/smoke_traffic.py --url "$URL" --n "${SMOKE_N:-600}"

echo "== 4/4 canary gate"
if ! $PY scripts/canary_gate.py --url "$URL"; then
  echo "NO-GO: rolling the canary back (CANDIDATE_PERCENT=0, same image)" >&2
  CANDIDATE_PERCENT=0 docker compose up -d --wait --force-recreate
  exit 1
fi
echo "DEPLOY OK: $IMAGE $(docker image inspect --format '{{.Id}}' "$IMAGE")"

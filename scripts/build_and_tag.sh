#!/usr/bin/env bash
# Build the image and tag it to match the release.
#   scripts/build_and_tag.sh            CI mode: any commit. Tags afyaplus-triage:<release>-rc.<sha> and :sha-<sha>
#   scripts/build_and_tag.sh --release  release mode: clean tree, HEAD must be tag v<release>. Tags :<release>
#                                       and records the image ID in runbook.md (release ledger)
# Both modes run check_prompt_pin.py first, so a misaligned tree never gets an image.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python}
MODE=${1:-ci}

RELEASE=$($PY -c 'from prompt_loader import load_config; print(load_config().release)')
GIT_SHA=$(git rev-parse --short=12 HEAD)
eval "$($PY - <<'PYEOF'
from prompt_loader import load_bundle
b = load_bundle()
print(f"PROMPT_ACTIVE={b.active.version} PROMPT_ACTIVE_SHA={b.active.sha256} "
      f"PROMPT_CANDIDATE={b.candidate.version if b.candidate else 'none'} "
      f"MCP_VERSION={b.config.mcp.version} MODEL_VERSION={b.config.model.model_version}")
PYEOF
)"

if [[ "$MODE" == "--release" ]]; then
  if [[ -n "$(git status --porcelain)" ]]; then echo "refusing: working tree is dirty" >&2; exit 1; fi
  TAG_SHA=$(git rev-list -n1 "v$RELEASE" 2>/dev/null || true)
  if [[ "$TAG_SHA" != "$(git rev-parse HEAD)" ]]; then
    echo "refusing: HEAD is not tag v$RELEASE (git tag -a v$RELEASE first)" >&2; exit 1
  fi
  $PY check_prompt_pin.py --tag "v$RELEASE" --image-tag "afyaplus-triage:$RELEASE"
  TAGS=(-t "afyaplus-triage:$RELEASE" -t "afyaplus-triage:sha-$GIT_SHA")
  PRIMARY="afyaplus-triage:$RELEASE"
else
  $PY check_prompt_pin.py
  TAGS=(-t "afyaplus-triage:$RELEASE-rc.$GIT_SHA" -t "afyaplus-triage:sha-$GIT_SHA")
  PRIMARY="afyaplus-triage:$RELEASE-rc.$GIT_SHA"
fi

docker build \
  --build-arg RELEASE="$RELEASE" --build-arg GIT_SHA="$GIT_SHA" \
  --build-arg PROMPT_ACTIVE="$PROMPT_ACTIVE" --build-arg PROMPT_ACTIVE_SHA="$PROMPT_ACTIVE_SHA" \
  --build-arg PROMPT_CANDIDATE="$PROMPT_CANDIDATE" --build-arg MCP_VERSION="$MCP_VERSION" \
  --build-arg MODEL_VERSION="$MODEL_VERSION" \
  "${TAGS[@]}" .

IMAGE_ID=$(docker image inspect --format '{{.Id}}' "$PRIMARY")
echo "built $PRIMARY"
echo "image_id=$IMAGE_ID"
docker image inspect --format '{{range $k, $v := .Config.Labels}}{{$k}}={{$v}}{{"\n"}}{{end}}' "$PRIMARY" \
  | grep -E '^(org.opencontainers.image.(version|revision)|ai.afyaplus)'
[[ -n "${GITHUB_OUTPUT:-}" ]] && { echo "image=$PRIMARY"; echo "image_id=$IMAGE_ID"; } >> "$GITHUB_OUTPUT"
if [[ "$MODE" == "--release" && -z "${GITHUB_ACTIONS:-}" ]]; then
  $PY scripts/record_release.py "$PRIMARY"   # the laptop release build writes the ledger; CI shows it in the summary
fi

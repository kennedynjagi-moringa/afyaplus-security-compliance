#!/usr/bin/env bash
# Week 9 security gate, run in CI's lint-test stage (GitHub Actions, Azure twin, local runner). Exit non-zero on any
# failure. Needs full git history for the secret scan (actions/checkout fetch-depth: 0).
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python}
echo "== secrets not in git (ignore rules, tracked files, full history, compose)"; $PY -m security.check_gitignore
echo "== transit policy, production view of config/partners.yaml";                $PY -m security.transit_policy --env prod
echo "== corpus pins";                                                              $PY -m security.corpus_pins
echo "== role matrix assertions";                                                   $PY -m security.rbac
echo "== injection guard: no false positives on the golden set";                   $PY -m security.injection_guard --golden

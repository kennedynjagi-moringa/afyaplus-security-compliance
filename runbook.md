# LLMOps runbook: AfyaPlus triage (prompts, config, MCP)

**Service:** `afyaplus-triage` (FastAPI, `prompt_app.py`) with the `afyaplus-logistics` MCP server in the same image.
**Current release:** v1.3.0. Prompt 1.2.0 is active, 1.3.0-candidate serves 10% of staging keys, and MCP is at 1.1.0.
**This page covers** rolling forward, rolling back, where traces are, who reviews what, the Definition of Done, and
the governance rule. It is written for whoever is on call, including at 01:00.

## 1. Who does what

| Role | Owns | Must approve |
|---|---|---|
| **clinical_ops** (clinical lead, CHW programme) | What "correct" means: golden-set labels, `must_not` rules, the prompt's clinical content, waivers | Every PR touching `prompts/`, `evals/`, `fixtures/`, `triage_engine.py` (CODEOWNERS) |
| **ml-eng** | Prompt engineering, eval recordings, the pipeline gates, config | Every PR (default owner); evidence in the PR (eval_score, cost, MCP verdict) |
| **platform** | MCP contracts, CI/CD, images, deploy | `logistics_mcp_versioned.py`, `mcp_schema.lock.json`, `.github/`, `azure-pipelines.yml` |
| **on-call engineer** | Rollback during an incident, at any hour, **without** asking anyone first | Nothing. Rolling back is always allowed (section 8). They tell clinical_ops afterwards |

## 2. What is versioned, and how to see it at runtime

| Thing | Version lives in | Runtime evidence |
|---|---|---|
| Release (one number for everything below) | `config/triage.yaml` `release` = `prompts/pin.json` `bundle_version` = git tag `vX.Y.Z` = image `afyaplus-triage:X.Y.Z` | `GET /health` → `release`, `image_tag`, `git_sha`; image labels `org.opencontainers.image.version/revision` |
| Prompts | `prompts/triage_system_v<semver>.txt`, pinned by SHA-256 in `prompts/pin.json` | `/health` → `prompt_active` / `prompt_candidate` with full SHA-256; every triage response has `X-Prompt-Version`, `X-Prompt-SHA`, `X-Prompt-Arm` |
| Config (model, temperature, limits, thresholds) | `config/triage.yaml` | `/health` → `config_sha256`, `model`, `model_version` |
| MCP server | `SERVER_VERSION` in `logistics_mcp_versioned.py`; tool schemas in `mcp_schema.lock.json` | MCP handshake `serverInfo.version`; `/health` → `mcp_version`; `/ready` runs the probe |

```bash
curl -s localhost:8080/health | python -m json.tool          # versions + SHAs
docker image inspect afyaplus-triage:1.3.0 --format '{{json .Config.Labels}}'
```

## 3. How a change reaches staging

```
PR ──> lint-test ──┬──> eval ────────┬──> build-deploy (staging stand-in: compose up on the runner)
       ruff         │   golden set    │     build + tag, /health versions match, MCP probe in the container,
       alignment    │   75 cases      │     600 smoke requests, canary go/no-go, rollback drill
       pytest       └──> mcp-health ──┘
                        handshake, version, required tools, schema lock, smoke call
```

Any red job stops everything downstream. `build-deploy` `needs` both `eval` and `mcp-health`. The checks are required
on `main`, so a red PR cannot merge (README, "Pipeline").

## 4. Roll forward: promote 1.3.0-candidate

Widen the canary in steps while clinical_ops watches the dashboard:
`traffic_percent` 10 → 50 → 100, with at least one working day per step. Each step is a PR (config change, release
bump). At each step the canary gate must say GO: error rate ≤ 0.10 on both arms, enough candidate traffic, and the
MCP handshake listing `check_stock`. Then promote:

1. `cp prompts/triage_system_v1.3.0-candidate.txt prompts/triage_system_v1.3.0.txt`. The bytes are the same, so the
   SHA (`20f536db4193`) is the same and the existing recording still applies.
2. `prompts/pin.json`: add `1.3.0` (`status: active`); set `1.2.0` to `retired` and delete its waiver; set the
   candidate to `retired`.
3. `config/triage.yaml`: `prompt_version: 1.3.0`, `candidate.prompt_version: null`, `release: 1.4.0`;
   `bundle_version: 1.4.0`.
4. `python check_prompt_pin.py && python eval_prompts.py`: both green, with no waiver needed.
5. CHANGELOG `## [1.4.0]`, a PR with clinical_ops + ml-eng approval, merge, `git tag -a v1.4.0`, then
   `scripts/build_and_tag.sh --release` (it records the image ID below), then deploy.

## 5. Roll back (pick the smallest lever that fixes it)

| Symptom | Lever | Command (compose; the Kubernetes / Container Apps equivalent is in the last column) | Verify | k8s / ACA |
|---|---|---|---|---|
| Candidate prompt misbehaving (complaints, error rate, canary NO-GO) | **Kill the canary.** Same image, ~10 s | `CANDIDATE_PERCENT=0 docker compose up -d --wait` | `/health` → `prompt_candidate.traffic_percent: 0` | `kubectl set env deploy/triage CANDIDATE_PERCENT=0` |
| Any doubt about which prompt is serving | **Pin one prompt.** Same image | `PROMPT_VERSION=1.2.0 docker compose up -d --wait` | `/health` → `rollback_override: true`, `prompt_active.version: 1.2.0`, candidate `null` | `kubectl set env deploy/triage PROMPT_VERSION=1.2.0` |
| Code, config or MCP problem (`/ready` 503, tool errors) | **Previous image** | `IMAGE=afyaplus-triage:1.2.0 docker compose up -d --wait` | `/health` → `release: 1.2.0`, `mcp_version: 1.0.0` | `kubectl set image deploy/triage triage=afyaplus-triage:1.2.0` |
| Bad commit on `main` | **Revert in git**, then the normal pipeline | `git revert -m 1 <merge-sha>`, open a PR | CI green, new PATCH release | |

- `scripts/deploy_stub.sh` does the first lever **automatically** when the canary gate says NO-GO
  ([evidence 11a](docs/evidence/11a_canary_nogo_autorollback.txt)).
- docker compose also reads `.env` in this directory. Check `DEPLOY_ENV` there: in `dev` the candidate never
  serves, so a staging check would quietly test only the active prompt. `/health` → `environment` shows the
  value in force.
- Moving the image back to 1.2.0 also moves MCP back to 1.0.0, which has no `has_stock_item` filter. That
  change was MINOR, so callers must cope without it.
- Never move or reuse a tag. To fix forward, cut a PATCH release.
- After any rollback, post in #clinical-ops: what you saw, which lever you used, and the `/health` output.

## 6. Where traces live

| What | Where | Keyed by |
|---|---|---|
| One line per triage: prompt version, SHA, arm, bucket, backend, urgency, model urgency, safety override, tokens, cost | `logs/api.jsonl` (`triage_completed`) | `trace_id` = `X-Request-ID` |
| One line per MCP tool call, with `server_version` | `logs/mcp_tools.jsonl` (`tool_call`) | `trace_id` (`AFYA_TRACE_ID`) |
| Counters per prompt arm and per tool (calls, errors, p50/p95) | `logs/tool_metrics.db`, `GET /v1/metrics` (scope `audit:read`) | prompt version / server version |
| One-screen view | `python mcp_dashboard_sketch.py --url http://127.0.0.1:8080` | |
| CI evidence | Actions run: job summaries (eval tables, MCP checks, release record), artifacts `eval-reports`, `mcp-health`, `pytest-junit` | run id |
| Recorded model answers per prompt | `fixtures/responses/<prompt_sha12>.json` | prompt SHA |

Inside the container, logs are on the `triage-logs` volume:
`docker compose exec triage tail -f logs/api.jsonl`. You can rebuild a request across both logs with Week 6's
`scripts/trace_rebuild.py`. The Week 3 observability stack reads the same JSON lines.

## 7. Who reviews clinical-adjacent prompts

- **Clinical-adjacent** means anything that can change what a CHW is told: prompt text, golden labels, `must_not`
  rules, the safety rules in `triage_engine.py`, model or temperature changes, and waivers.
- These need **two people**: clinical_ops reads the prompt diff and the eval misses table, and asks whether a CHW
  would act differently; ml-eng checks the evidence. `.github/CODEOWNERS` routes the review. The PR template's
  Definition of Done makes both sign-offs explicit, because GitHub only enforces one owner per path.
- clinical_ops never needs to read YAML. They get `clinical_ops_change_brief.md`: what changed, what it was tested
  on, how to undo it, and the go / no-go rule.

## 8. Governance note: recommend, don't decide

For AfyaPlus-style deployments we **recommend against letting the AI or the pipeline decide** anything that moves care
or behaviour into new territory. They recommend, and named people decide.

- **The model recommends; the CHW and clinician decide.** Every response carries the disclaimer, and urgency is a
  routing aid, not a diagnosis. Code rules can only *raise* urgency (danger signs → EMERGENCY). The prompt can never
  talk the service out of an emergency.
- **The pipeline recommends; people decide promotion.** A green eval and a GO canary are *permission to ask*, not
  an automatic rollout. Widening the canary, promoting a prompt, editing labels and granting waivers all need a
  reviewed PR with clinical_ops sign-off.
- **Automation may move only towards the known, safer state.** Killing the canary on NO-GO, failing closed on a
  missing recording, and blocking a deploy on a bad MCP probe are all automatic, because each one returns to
  behaviour that was already approved. Nothing automatic ever moves *forward*.
- **Exceptions are visible and expire.** The 1.2.0 waiver names its approvers and expires on 2026-11-30, and it
  prints in every CI summary. Quarantined test cases have an owner and an expiry date, and are capped at 10% of
  the set. `must_not` (unsafe advice) can never be waived.
- **Labels are a decision too.** The golden-set labels were written by the engineering team and have not been
  validated by clinicians (README limitations). clinical_ops owns them from this sprint onwards.

## 9. Sprint Definition of Done (prompt and MCP pull requests)

A prompt or MCP PR is **done** only when every applicable box is true. (The PR template has the same list.)

**Versioning**
- [ ] A prompt change is a **new file** `prompts/triage_system_v<semver>.txt`; no pinned file was edited
      (`check_prompt_pin.py` green)
- [ ] `prompts/pin.json` has the new SHA and status, and `config/triage.yaml` points to it; `release` is bumped
      if the image changes
- [ ] An MCP schema change bumps `SERVER_VERSION` by semver (MAJOR: removed or renamed; MINOR: new tool or
      optional argument; PATCH: no schema change), with `mcp_schema.lock.json` regenerated and `config mcp.version`
      matched

**Evidence**
- [ ] A live recording was made once (`eval_prompts.py --record`) and committed. The PR states the cost per
      1,000 triages and compares it with the active prompt
- [ ] `eval` is green: agreement ≥ 0.85, emergency recall 1.0, 0 `must_not` violations, no regression > 0.02 vs active
- [ ] Holdout agreement is reported, and a big dev/holdout gap is explained (overfitting to the test set)
- [ ] `mcp-health` is green on the live server, and the stub probe is green if the lock changed
- [ ] `build-deploy` is green: versions verified, smoke traffic OK, canary GO, rollback drill OK

**People**
- [ ] clinical_ops **and** ml-eng approved (CODEOWNERS paths)
- [ ] `clinical_ops_change_brief.md` is updated whenever a CHW could get a different answer
- [ ] CHANGELOG entry, including **rollback**: the exact `PROMPT_VERSION`, `CANDIDATE_PERCENT` or image tag
- [ ] No secrets in the diff (`.env` stays local); no waiver or quarantine without an owner and an expiry date

## 10. Release ledger

The image ID is the SHA-256 of the image config, so it is the same on every machine that builds from these
layers. A registry push would also give a manifest digest. There is no registry for this capstone (README,
fallbacks). Release builds write their rows here (`scripts/build_and_tag.sh --release`, run from a clean checkout
of the tag).

<!-- release-ledger:start -->

| date | image | image ID | git | prompt active | candidate | MCP | model_version |
|---|---|---|---|---|---|---|---|
| 2026-10-07 | `afyaplus-triage:1.2.0` | `sha256:d66b6d9c510ec48c254bb3a12b16fe9bc32cbc9361bc49f2063402a22a3a50ac` | `de3c39a46d3a` | 1.2.0 (`da2a2183ac50`) | none | 1.0.0 | 2026.10.0 |
| 2026-10-07 | `afyaplus-triage:1.3.0` | `sha256:550e67da89aead0c4b4a52981b41314d8b48ed027e1dc56d4b53a28a5f56046a` | `ff0c29f8e753` | 1.2.0 (`da2a2183ac50`) | 1.3.0-candidate | 1.1.0 | 2026.10.0 |

<!-- release-ledger:end -->

## 11. What this evidences (NCA-GENL)

| Deliverable | Domain | Where |
|---|---|---|
| 1. Versioned prompts, config, MCP | Deployment and operational capability | sections 2 and 10, `check_prompt_pin.py`, `/health` |
| 2. Pipeline YAML | Deployment and operational capability | section 3, `.github/workflows/ci.yml`, `azure-pipelines.yml` |
| 3. Eval and regression gate | Evaluation and model assessment | `eval_prompts.py`, README "Eval gate" |
| 4. MCP health in the pipeline | Architecture and integration | `scripts/check_mcp_health.py`, section 5 |
| 5. Runbook and Definition of Done | Communication and stakeholder objective | this page, `clinical_ops_change_brief.md` |

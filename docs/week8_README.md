# Week 8, CI/CD for Versioned AI + MCP Health

> **Week 8 capstone, AfyaPlus series.** The Week 6/7 triage service and the Week 6 clinic MCP server, now with
> versioned prompts, config and MCP, a gated pipeline, and a rollback a colleague can run at 01:00.
> **Headline:** the new gate's first run showed that the production prompt (1.2.0) misses 5 of 24 emergencies in
> our golden set. The fix, 1.3.0-candidate (0.973 agreement, every emergency caught), now runs as a 10% staging
> canary behind the gate.
> **Live OpenAI spend this week:** about **$0.031**, for four one-off recordings
> ([evidence 13](docs/evidence/13_live_recording_costs.txt)). Every CI run replays those recordings at $0.

## Overview

**What is versioned.** Every prompt is a semver file pinned by SHA-256 in [prompts/pin.json](prompts/pin.json).
[config/triage.yaml](config/triage.yaml) holds the model snapshot, temperature, limits, eval thresholds and which
prompt serves which traffic. The MCP server reports `SERVER_VERSION` in its handshake, and its tool schemas are
locked in [mcp_schema.lock.json](mcp_schema.lock.json). One **release** number ties the git tag, the image tag and
the prompt bundle together, and `check_prompt_pin.py` fails the build if any of them disagree.

**How the pipeline gates deploy.** `lint-test → eval + mcp-health → build-deploy`. The eval job replays the live
model's recorded answers for each prompt SHA over 75 labelled cases and fails below 0.85 agreement, on any missed
emergency, or on any unsafe advice. A prompt with no recording cannot be evaluated, so the gate fails closed.
`build-deploy` `needs` both gates. It builds and tags the image, brings it up with compose on the runner (the
staging stand-in), checks that `/health` reports exactly this commit's versions, probes MCP inside the container,
sends smoke traffic, makes a canary go/no-go call and runs a rollback drill.

**The MCP health contract.** The probe does a stdio handshake and asserts the server name and version, the
required tools (`find_clinics`, `check_stock`), the input schemas against the lock, a smoke `check_stock` call
and the error contract. Exit 0 means healthy, 1 means an assertion failed, 2 means unreachable or timed out. The
live server and the contract stub use the same codes, and either 1 or 2 blocks the deploy.

## Where each deliverable is

| # | Deliverable (marks) | Main files | Evidence |
|---|---|---|---|
| 1 | Versioned prompts, config, MCP (20) | [prompts/](prompts/), [pin.json](prompts/pin.json), [config/triage.yaml](config/triage.yaml), [prompt_loader.py](prompt_loader.py), [prompt_app.py](prompt_app.py) (`/health`), [check_prompt_pin.py](check_prompt_pin.py), [logistics_mcp_versioned.py](logistics_mcp_versioned.py), [mcp_schema.lock.json](mcp_schema.lock.json), [CHANGELOG.md](CHANGELOG.md) | [07 alignment](docs/evidence/07_release_alignment.txt), [09 /health + rollback](docs/evidence/09_health_versions_and_rollback.txt), [14 release builds](docs/evidence/14_release_builds.txt), [runbook ledger](runbook.md#10-release-ledger) |
| 2 | Pipeline YAML (25) | [.github/workflows/ci.yml](.github/workflows/ci.yml), [azure-pipelines.yml](azure-pipelines.yml), [retrain-stub.yml](.github/workflows/retrain-stub.yml), [scripts/run_pipeline_local.sh](scripts/run_pipeline_local.sh) | [08 full local run](docs/evidence/08_pipeline_local_full_run.txt), [03](docs/evidence/03_demo_eval_failure_blocks_deploy.txt) and [05](docs/evidence/05_demo_mcp_failure_blocks_deploy.txt) blocked runs, GitHub Actions (see Pipeline) |
| 3 | Eval and regression gate (20) | [eval_prompts.py](eval_prompts.py), [evals/golden.jsonl](evals/golden.jsonl), [fixtures/responses/](fixtures/responses/), [fixtures/quarantine.json](fixtures/quarantine.json) | [01 first run red](docs/evidence/01_eval_gate_first_run_red.txt), [02 v1.3.0 gate](docs/evidence/02_eval_gate_v1.3.0.txt), [03 blocked deploy](docs/evidence/03_demo_eval_failure_blocks_deploy.txt) |
| 4 | MCP health in the pipeline (15) | [scripts/check_mcp_health.py](scripts/check_mcp_health.py), [scripts/mcp_stub_server.py](scripts/mcp_stub_server.py) | [04 live, stub and faults](docs/evidence/04_mcp_health_live_stub_faults.txt), [05 blocked deploy](docs/evidence/05_demo_mcp_failure_blocks_deploy.txt), [06 schema drift](docs/evidence/06_mcp_schema_drift_blocked.txt) |
| 5 | Runbook and Definition of Done (20) | [runbook.md](runbook.md), [clinical_ops_change_brief.md](clinical_ops_change_brief.md), [.github/CODEOWNERS](.github/CODEOWNERS), [PR template](.github/pull_request_template.md) | the runbook's rollback table, DoD (section 9) and governance note (section 8) |
| – | Thursday labs | [ab_prompt_flag.py](ab_prompt_flag.py) (Lab 1), [tool_metrics.py](tool_metrics.py), [mcp_dashboard_sketch.py](mcp_dashboard_sketch.py) (Lab 2), [scripts/canary_gate.py](scripts/canary_gate.py) | [11 canary + dashboard](docs/evidence/11_canary_gate_and_dashboard.txt), [11a auto-rollback](docs/evidence/11a_canary_nogo_autorollback.txt) |

The layout follows the brief's target. Two additions: `afyaplus/` holds the shared pieces reused from Weeks 6/7
(auth, settings, contracts, logging), and [triage_engine.py](triage_engine.py) holds the model backends and the
safety rules. `scripts/` holds pipeline tooling, `tests/` has 63 offline tests ([12](docs/evidence/12_pytest.txt)),
and `docs/evidence/` holds the terminal transcripts.

## Versions

| | v1.2.0 (rollback target) | **v1.3.0 (current)** |
|---|---|---|
| git tag = image tag = prompt bundle | `v1.2.0` = `afyaplus-triage:1.2.0` = bundle 1.2.0 | `v1.3.0` = `afyaplus-triage:1.3.0` = bundle 1.3.0 |
| prompts | 1.2.0 active (`da2a2183ac50`) | 1.2.0 active + **1.3.0-candidate** (`20f536db4193`) on 10% of staging keys |
| config / model_version | `gpt-4o-mini-2024-07-18`, temp 0, model_version 2026.10.0 | same |
| MCP server | afyaplus-logistics **1.0.0** | afyaplus-logistics **1.1.0** (`find_clinics` + optional `has_stock_item`) |
| image ID ([ledger](runbook.md#10-release-ledger)) | `sha256:d66b6d9c510e…` | `sha256:550e67da89ae…` |

At runtime, `GET /health` returns all of these, plus the full prompt SHA-256s, the config SHA-256, the image tag,
the git SHA and whether a rollback override is in force
([evidence 09](docs/evidence/09_health_versions_and_rollback.txt)). Every triage response also carries
`X-Prompt-Version`, `X-Prompt-SHA` and `X-Prompt-Arm`. The image labels repeat the versions, so
`docker image inspect` shows them without starting the container.

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env                        # set JWT_SECRET; MODEL_BACKEND=replay needs no API key
.venv/bin/uvicorn prompt_app:app --port 8080 && curl -s localhost:8080/health
scripts/build_and_tag.sh && IMAGE=afyaplus-triage:1.3.0-rc.<sha> JWT_SECRET=... scripts/deploy_stub.sh
```

## Pipeline

Stages: **lint-test** (ruff, release alignment, pytest) → **eval** and **mcp-health** in parallel →
**build-deploy** (build + tag, compose up, verify versions, MCP probe in the container, 600 smoke requests, canary
go/no-go, rollback drill). Triggers: `pull_request`, `push` to `main`, tags `v*`, and `workflow_dispatch`. The
manual trigger has inputs `demo_failure` (none / eval / mcp) and `mcp_target` (live / stub), for minute-limited
accounts and for the failing demos. Required checks on `main`: `lint-test`, `eval`, `mcp-health`, `build-deploy`.

| Evidence | What it shows |
|---|---|
| [08 local full run](docs/evidence/08_pipeline_local_full_run.txt) | all four stages green on v1.3.0, including the docker build, image ID and deploy gates |
| [03 eval demo](docs/evidence/03_demo_eval_failure_blocks_deploy.txt) / [05 MCP demo](docs/evidence/05_demo_mcp_failure_blocks_deploy.txt) | a red stage stops the pipeline; build-deploy never runs |
| GitHub Actions | _see "GitHub Actions runs" below_ |

`scripts/run_pipeline_local.sh` runs the same commands in the same order as `ci.yml` and `azure-pipelines.yml`.
[azure-pipelines.yml](azure-pipelines.yml) is the config-as-code twin, with stages `Lint → Eval + McpHealth →
Deploy`.

### GitHub Actions runs

_Not pushed yet; links will go here._

## Eval gate

- **Metric:** `urgency_agreement`. This is the share of golden cases where the final urgency (model output **after**
  the code safety rules, i.e. what a CHW sees) equals the clinical label. **Threshold 0.85**
  ([config/triage.yaml](config/triage.yaml) `eval:`).
- **Also gated:** `emergency_recall` = 1.0 (no EMERGENCY case under-triaged); `must_not_violations` = 0 (doses,
  stated diagnoses, harmful first aid, and per-case rules such as "tourniquet" for a snake bite); and for a
  candidate, no more than 0.02 below the active prompt.
- **Fixtures:** [evals/golden.jsonl](evals/golden.jsonl) has 75 cases (EN/SW/mixed; 24 EMERGENCY, 29 URGENT,
  22 ROUTINE). 48 are the Week 7 labelled cases, 12 are new edge cases (prompt injection, a dose request, a
  self-reported diagnosis) and 15 are a **holdout** that was written before 1.3.0 and never used to tune it.
  The recordings are in `fixtures/responses/<prompt_sha12>.json`.
- **Fails closed:** a prompt with no recording, a recording made with different model settings, an edited golden
  case, an expired quarantine, or more than 10% of cases quarantined all exit 2, which fails the job.

| Prompt | Agreement | Holdout | Emergency recall | must_not | $ / 1k triages | Gate |
|---|---:|---:|---:|---:|---:|---|
| 1.2.0 (active) | 0.747 | 0.667 | 0.79 (19/24) | 0 | $0.099 | waived until 2026-11-30 |
| **1.3.0-candidate** | **0.973** | **0.933** | **1.00** | 0 | $0.146 | **pass** |
| 1.3.1-costtrim (draft) | 0.653 | 0.467 | 0.75 | 0 | $0.086 | **FAIL → deploy blocked** |

**Failing-run note.** [Evidence 01](docs/evidence/01_eval_gate_first_run_red.txt) is the gate's first real run: it
failed the production prompt. [Evidence 03](docs/evidence/03_demo_eval_failure_blocks_deploy.txt) is a
realistic regression, a "cost-trim" prompt that costs 14% less per triage than 1.2.0 (41% less than 1.3.0). It
scored 0.653, missed 6 emergencies, and the
pipeline stopped at `eval`, so no image was built.

**The waiver.** Prompt 1.2.0 has been in production since Week 6. Blocking it would also have blocked its fix,
so clinical_ops accepted the gap for a limited time ([pin.json](prompts/pin.json)). The waiver covers only the
agreement and recall gates, never `must_not`; it applies only to the active prompt; it names its approvers;
it prints in every CI summary; and it **expires on 2026-11-30**, after which the build goes red until 1.3.0 is
promoted.

## MCP health

```bash
python scripts/check_mcp_health.py                       # live server over stdio
python scripts/check_mcp_health.py --target stub         # fallback contract stub, same exit codes
MCP_STUB_FAULT=drop_tool python scripts/check_mcp_health.py --target stub   # watch it fail (exit 1)
docker compose exec -T triage python scripts/check_mcp_health.py            # the image's own server
```

**These fail the build:** the wrong server name or version; a lock generated for another version; a required tool
missing; any tool's input schema differing from the lock, or a tool added or removed (so a cowboy schema change is
caught, [evidence 06](docs/evidence/06_mcp_schema_drift_blocked.txt)); a smoke call that is not `ok`; a bad call
that does not return `isError` with an error code; an unreadable catalogue resource; and spawn failure or timeout
(exit 2). Fault matrix: [evidence 04](docs/evidence/04_mcp_health_live_stub_faults.txt). The same probe runs
behind `GET /ready` at runtime and in the canary go/no-go.

## Runbook and Definition of Done

- [runbook.md](runbook.md) covers roll forward (section 4), a four-lever rollback table (section 5), where traces
  live (section 6), who reviews clinical-adjacent prompts (section 7), the release ledger with image IDs
  (section 10) and the NCA-GENL map (section 11).
- **Sprint Definition of Done** for prompt and MCP PRs: [runbook section 9](runbook.md#9-sprint-definition-of-done-prompt-and-mcp-pull-requests),
  mirrored in the [PR template](.github/pull_request_template.md).
- **Recommend, don't decide** ([runbook section 8](runbook.md#8-governance-note-recommend-dont-decide)). The model
  recommends and clinicians decide. The pipeline recommends and people promote. Automation may only move back
  towards the known, safer state.
- [clinical_ops_change_brief.md](clinical_ops_change_brief.md) is a one-page, non-technical brief: what changed,
  what was tested, the rollback, who approved, and go/no-go.

## Ties to Weeks 6 and 7

| Reused | From | Here |
|---|---|---|
| Clinic MCP server (`find_clinics`, `check_stock`, catalogue, errors-as-data, per-call logs) | Week 6 `mcp_server/server.py` | [logistics_mcp_versioned.py](logistics_mcp_versioned.py): versioned, schema-locked, counted |
| Clinic registry | Week 6 `mcp_server/data/clinics.json` | [clinics.json](clinics.json) |
| JWT auth, demo users, scopes, JSON logging with trace ids | Weeks 6/7 `api/auth.py`, `users.py`, `logging_setup.py` | [afyaplus/](afyaplus/) |
| Triage system prompt, danger-sign safety rules, stub model | Week 7 `api/llm.py` (`triage-v1`) | prompt 1.2.0, [triage_engine.py](triage_engine.py) |
| 48 labelled cases | Week 7 `levers/fixtures/cases.jsonl` | the core of [evals/golden.jsonl](evals/golden.jsonl) |
| Token pricing and cost per 1,000 | Week 7 `api/pricing.py`, cost model | `config/triage.yaml` `pricing_usd_per_1m`, cost per 1k in every eval report |
| Image tag = git tag, OCI revision label, tags never moved | Week 6 `scripts/build_image.sh`, CONTRIBUTING | [scripts/build_and_tag.sh](scripts/build_and_tag.sh), now with prompt, MCP and model labels |
| Compose deploy, non-root, read-only filesystem, healthcheck | Week 7 `deploy/docker-compose.cost.yml` | [docker-compose.yml](docker-compose.yml) |
| `.env` kept local, `.gitignore` and `.dockerignore` | Weeks 6/7 | same habits; no secret in git, image or CI |

Not carried over: the Week 6 LangChain agent and the Week 7 cache/batch levers. They do not change what this
week's pipeline proves. The agent's tool contract (the MCP server) **is** here and gated.

## Fallbacks declared

- [x] **act / workflow_dispatch dry run.** `scripts/run_pipeline_local.sh` mirrors `ci.yml` stage by stage. `act`
      is not installed. GitHub Actions run status: see "GitHub Actions runs".
- [x] **Stub retrain (model_version bump only).** No GPU. [scripts/retrain_stub.py](scripts/retrain_stub.py) and
      [retrain-stub.yml](.github/workflows/retrain-stub.yml) bump `model_version` and write a CHANGELOG line
      saying no weights were trained. Gate still runs: [evidence 10](docs/evidence/10_retrain_stub.txt).
- [x] **Local MCP health stub.** [scripts/mcp_stub_server.py](scripts/mcp_stub_server.py) serves the locked
      schemas with the same exit codes. The live server is the default, because it runs anywhere Python does.
- [x] **No paid Azure.** `azure-pipelines.yml` is config-as-code only. Deploy is `docker compose up` on the runner
      or laptop (no Kubernetes or cloud), with the image ID recorded in the runbook ledger. There is no registry,
      so no manifest digest.
- [x] **No paid OpenAI in CI.** Live calls happen once per prompt SHA (`--record`, $0.031 in total). CI replays
      the recordings, and the gate still fails when agreement drops.
- [x] **One author, several roles.** `@afyaplus/*` CODEOWNERS teams and the clinical_ops / ml-eng approvals are
      roles the author played. In production they are different people.

## Checklist

- [x] Tag alignment evidenced: [07](docs/evidence/07_release_alignment.txt), `/health` [09](docs/evidence/09_health_versions_and_rollback.txt), image labels and ledger
- [x] Eval fails closed: [01](docs/evidence/01_eval_gate_first_run_red.txt), [03](docs/evidence/03_demo_eval_failure_blocks_deploy.txt), `tests/test_eval_gate.py`
- [x] MCP health in the pipeline: `mcp-health` job + in-container probe in `build-deploy` + `/ready`; [04](docs/evidence/04_mcp_health_live_stub_faults.txt), [05](docs/evidence/05_demo_mcp_failure_blocks_deploy.txt), [06](docs/evidence/06_mcp_schema_drift_blocked.txt)
- [x] Runbook and Definition of Done complete: [runbook.md](runbook.md), [clinical_ops_change_brief.md](clinical_ops_change_brief.md)
- [x] Weeks 6 and 7 reuse documented (above)

## Limitations

- **The golden labels are not clinician-validated.** The engineering team wrote them, partly in Week 7.
  clinical_ops is asked to review them ([brief](clinical_ops_change_brief.md)). The gate is only as good as these
  labels.
- **The holdout is small** (15 cases), and the same author wrote it and the 1.3.0 prompt. It guards against
  tuning the prompt to the 60 dev cases, but it is not an independent test set.
- **Replay is not a live check of the model's current behaviour.** It proves the gate's logic and pins a known
  answer set. A model provider change shows up only when re-recorded, so the pinned snapshot and the
  model-settings check exist to force that.
- **The keyword safety rules have false positives** ("no fits so far"). This over-caution is kept on purpose.
- **Canary volume in the deploy stub is synthetic** (600 golden messages, about 50 on the candidate). A real
  rollout needs real traffic over days. The first run sent 300, too few for the volume check: the gate said
  NO-GO and rolled the canary back by itself ([11a](docs/evidence/11a_canary_nogo_autorollback.txt)).

# Changelog

All notable changes are recorded here. One release number covers the image tag, the git tag and the prompt
bundle (`prompts/pin.json` `bundle_version`); the prompt and MCP server versions it ships are listed per release.
Versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.3.1] - 2026-10-07

Ships exactly what 1.3.0 ships: prompt 1.2.0 active (`da2a2183ac50`, waiver until 2026-11-30), 1.3.0-candidate
(`20f536db4193`) on 10% of staging keys, and MCP 1.1.0. Only the release number differs inside the image.
Rollback is unchanged: `CANDIDATE_PERCENT=0` or `PROMPT_VERSION=1.2.0`, or image `afyaplus-triage:1.2.0`.

### Fixed
- The v1.3.0 tag pipeline was red at build-deploy, because its 300 smoke requests put only 29 on the candidate,
  below the canary gate's minimum of 40. The gate said NO-GO and rolled the canary back by itself. The deploy stub
  now sends 600. Tags are immutable, so this ships as a PATCH release rather than moving v1.3.0.
- The canary error rate must now be strictly under 0.10, as the go/no-go rule states (0.10 exactly used to pass).
- CI teardown: `docker compose down` needed a `JWT_SECRET` to interpolate the compose file.

## [1.3.0] - 2026-10-07

Ships: prompt **1.2.0** (active, `da2a2183ac50`, waiver until 2026-11-30) and **1.3.0-candidate**
(`20f536db4193`) on 10% of staging routing keys; MCP **afyaplus-logistics 1.1.0**; model
`gpt-4o-mini-2024-07-18` (model_version 2026.10.0). Rollback: `PROMPT_VERSION=1.2.0` or `CANDIDATE_PERCENT=0`
on the same image, or image `afyaplus-triage:1.2.0`.

### Added
- Prompt 1.3.0-candidate: explicit URGENT and EMERGENCY criteria (fever in under-5s, stopped long-term
  medicines, pregnancy and newborn danger signs, poisoning, stroke signs, self-harm), no doses, and
  instructions inside CHW messages are ignored. Eval: 0.973 agreement (holdout 0.933), emergency recall 1.0,
  0 must_not violations. Cost: about $0.146 per 1,000 triages vs $0.099 for 1.2.0 (longer prompt).
- MCP 1.1.0: `find_clinics` takes an optional `has_stock_item` (MINOR; the 1.0.0 call shape is unchanged).

### Changed
- `release` 1.3.0; prompt bundle 1.3.0; MCP schema lock regenerated for 1.1.0.

## [1.2.0] - 2026-10-07

Ships: prompt **1.2.0** (active, `da2a2183ac50`), MCP **afyaplus-logistics 1.0.0**, model
`gpt-4o-mini-2024-07-18` (model_version 2026.10.0). First release built and gated by the Week 8 pipeline.

### Added
- Versioned prompt bundle: `prompts/triage_system_v1.2.0.txt` (the Week 6/7 production prompt, previously
  `triage-v1`, put under semver), `prompts/pin.json` SHA-256 lockfile, `config/triage.yaml` validated at startup.
- `/health` reports release, image tag, git SHA, prompt versions and SHA-256, config SHA, model, model_version and
  MCP version; `/ready` runs the MCP probe; `X-Prompt-Version` / `X-Prompt-SHA` on every triage response.
- Golden-set regression gate (`eval_prompts.py`, 75 cases incl. a 15-case holdout) over live recordings keyed by
  prompt SHA; fails closed on missing or stale recordings.
- Logistics MCP server 1.0.0 (Week 6 clinic server) reporting its own version in the handshake; tool schemas locked
  in `mcp_schema.lock.json`; `scripts/check_mcp_health.py` probe and a contract stub with the same exit codes.
- Canary flag (`ab_prompt_flag.py`), shared counters (`tool_metrics.py`), canary gate, dashboard sketch.
- CI: GitHub Actions `lint-test -> eval + mcp-health -> build-deploy`, Azure DevOps twin, local runner,
  CODEOWNERS, PR template with the Definition of Done; retrain stub workflow.

### Known issues
- The first gate run scored prompt 1.2.0 at **0.747** urgency agreement and **0.79** emergency recall (threshold
  0.85 / 1.0): it under-triages URGENT cases as ROUTINE. Shipped under a clinical_ops waiver expiring
  2026-11-30 while a fix is canaried. Code-level danger-sign rules remain on.
- Keyword safety rules match "fits" in "no fits so far" (holdout H04): a known over-triage false positive.

[Unreleased]: compare v1.3.1...HEAD
[1.3.1]: compare v1.3.0...v1.3.1
[1.3.0]: compare v1.2.0...v1.3.0
[1.2.0]: first gated release

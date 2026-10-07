## What changed

<!-- Prompt / config / MCP / code. Give versions: e.g. prompt 1.2.0 -> 1.3.0-candidate, MCP 1.0.0 -> 1.1.0 -->

## Definition of Done (prompt and MCP pull requests)

Full checklist: [runbook.md](../runbook.md#sprint-definition-of-done). Tick only what is true.

- [ ] New prompt text is a **new versioned file**; no pinned prompt file was edited (`check_prompt_pin.py` green)
- [ ] `prompts/pin.json` and `config/triage.yaml` updated; release number bumped where the image changes
- [ ] Live recording made once (`eval_prompts.py --record`) and committed; cost per 1,000 triages noted below
- [ ] `eval` check green: agreement >= 0.85, emergency recall 1.0, 0 must_not violations, no regression vs active
- [ ] MCP schema change? `SERVER_VERSION` bumped (semver), `mcp_schema.lock.json` regenerated, `mcp-health` green
- [ ] CHANGELOG entry; clinical_ops change brief updated if CHWs could see a different answer
- [ ] Rollback stated: the exact `PROMPT_VERSION` / `CANDIDATE_PERCENT` / image tag that undoes this
- [ ] Sign-off: **clinical_ops** (clinical content) and **ml-eng** (pipeline evidence), both

## Evidence

<!-- Paste: eval_score lines, MCP health verdict, cost per 1k from the eval output -->

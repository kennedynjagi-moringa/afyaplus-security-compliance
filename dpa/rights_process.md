# Data-subject rights: who does what

Teaching process for the AfyaPlus deployment. **Legal review is pending.** No lawyer or Data Protection
Officer has approved this page, and the Kenya Data Protection Act 2019 section numbers are for orientation
only.

## Owners

| Responsibility | Owner (role) | Person |
|---|---|---|
| Access requests (s.26) | Data Protection Officer | to be appointed. In the demo the `dpo_njeri` account plays this role. |
| Erasure and correction (s.40) | Data Protection Officer | same |
| Running the lookup and the deletion | DPO, using `dpa/rights_lookup.py` (role `dpo`) | same |
| Retention sweep (s.39) | Operations on-call (role `ops`) | the ops rota. Demo account: `ops_kamau`. |
| Breach notification to the Data Commissioner within 72 hours (s.43) | DPO, with the BenkiYetu security lead | DPO to be appointed; security lead per the scenario |
| Signing off retention periods and lawful-basis labels | DPO + legal counsel | **pending** |

Operations staff can sweep but cannot look anyone up. The DPO can look up and erase but cannot read metrics or
run the sweep. That split comes from `security/rbac.py`, not from goodwill.

## Steps for a request

1. **Receive.** A household asks through their CHW or the clinic, or a staff member asks directly. Open a
   ticket (`DSR-<number>`) and record the date it arrived. The household's reference is the `request_id`
   printed on the triage result.
2. **Verify identity.** For a household, the CHW confirms it in person at the next visit. For staff, use the
   identity provider. Nothing is released before this step.
3. **Find everything.** Run `python -m dpa.rights_lookup access --request-id <id>` or `--chw <username>`.
   The report lists the stored record, the audit lines (hashes only), the purposes, the retention periods and
   who else may have received the data.
4. **Decide and answer.** For access, send the report in plain language. For erasure, run
   `python -m dpa.rights_lookup erasure --request-id <id> --ticket DSR-<n>` to see the plan, then again with
   `--apply`. Each run appends a line to `dpa/deletion_records.jsonl`.
5. **Close.** Tell the person what was deleted and what was kept and why. Audit lines are kept because they
   hold no message text. Close the ticket.

**Deadlines.** The Act and its regulations set response times. We have not confirmed the exact figures, so
this page does not state them (legal review pending). Our internal target is to acknowledge within 2 working
days.

## What this does not cover

- Data already sent to the model provider while the live backend was on. That is a request to the provider
  under our contract with them, which is also pending review (`dpa/data_inventory.json`, transfers).
- Correcting a record. Triage records are a snapshot of what was reported at the time. A correction is added
  as a note, not an edit (DPO decision, pending).

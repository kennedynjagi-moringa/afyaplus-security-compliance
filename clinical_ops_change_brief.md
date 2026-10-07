# Change-control brief (clinical_ops)

**To:** Clinical Operations, AfyaPlus CHW programme  **From:** ML engineering  **Date:** 7 October 2026
**Decision needed:** approve a small, reversible trial of new triage instructions in **staging (test) only**

## In one sentence

The triage assistant's instructions now tell it which situations are "see a clinician within 24 hours", and we
want to try the new version on 1 in 10 test users before anyone in the field sees it.

## What changed

| | Today (version 1.2.0) | Trial (version 1.3.0-candidate) |
|---|---|---|
| Who sees it | Everyone | **10% of staging (test) users only.** No real CHW or patient sees it |
| What it tells the assistant | General urgency levels | The same levels, plus clear examples: fever in a child under 5, a patient who has run out of HIV, TB, diabetes or blood-pressure medicine, pregnancy and newborn warning signs, poisoning, stroke signs, thoughts of self-harm. It must never give medicine doses, and it ignores any attempt in a message to change its rules |

The clinic stock lookup also gained one optional search ("only clinics that have this item in stock"). Existing
uses work exactly as before.

## What was tested

We ran both versions on the same **75 written test cases** in English, Swahili and mixed. Each case has an agreed
urgency and a list of advice the assistant must never give (for example "apply a tourniquet" for a snake bite).
Fifteen of the cases were kept aside and not looked at while the new instructions were written.

| Result on the 75 cases | Today (1.2.0) | Trial (1.3.0) |
|---|---|---|
| Urgency matched the agreed answer | 56 of 75 (75%) | **73 of 75 (97%)** |
| Emergencies recognised as emergencies | 19 of 24 | **24 of 24** |
| Cases rated *less* urgent than they should be | 18 | **1** |
| Unsafe advice given (doses, harmful first aid, stated diagnoses) | 0 | 0 |
| On the 15 set-aside cases | 10 of 15 | **14 of 15** |

The one remaining under-rating is a caregiver asking for a paracetamol dose for a lively 2-year-old with a mild
fever. It was rated "routine" where our rule says "within 24 hours", and no dose was given. One case is
over-rated: "no fits so far" is caught by the emergency keyword check. That over-caution is deliberate.

**Today's version falls below our new standard** of 85% agreement with no missed emergencies. It stays in use only
until the trial ends, under an approved exception that expires on **30 November 2026**. The emergency keyword check
(convulsions, severe bleeding, difficulty breathing and so on) keeps working for every user, whichever version
they get.

**Cost:** the new instructions are longer, so the AI bill rises from about $0.10 to $0.15 per 1,000 triages. That is
roughly $16 a month at pilot volume.

## How we undo it (any time, about one minute, no new software)

1. **Stop the trial:** set the trial share to 0%. Everyone goes back to today's version.
2. **Force today's version everywhere:** set `PROMPT_VERSION=1.2.0`, on the same software.
3. **Go back to the previous release** (tag `v1.2.0`) if anything else misbehaves.

The on-call engineer may do any of these without asking first, and will tell clinical_ops straight away. If the
trial's error rate goes above 10%, the system stops the trial by itself.

## Who approved

The CODEOWNERS for clinical_ops and ml-eng, on the `feature/prompt-1.3.0-candidate` pull request (merge `29fac44`),
released as v1.3.0. *In this capstone the author played both roles; in production these are two different people.*

## Go or no-go

**GO** for the staging trial if **all three** hold, as checked automatically on every change:
- the test-case check (eval) is green: 85% or more agreement, every emergency caught, no unsafe advice;
- the clinic stock tool answers its health check and lists `check_stock`;
- the error rate for each version stays **under 10%** (so far: 0 errors in 600 staging test requests, 52 of
  them on the trial version).

**NO-GO**, or roll back immediately, if any of them fails, or if clinical_ops sees an answer a CHW should not get.

**Not decided here:** moving the new version beyond staging, or beyond 10%. That needs a further decision from you
after at least one working day of trial results.

## What we need from you

1. Approve or decline the staging trial.
2. Check our test-case answers. The agreed urgencies were written by the engineering team and have **not yet been
   checked by a clinician**. Five cases deserve a look first: H14 (thoughts of self-harm after birth), H04 (stopped
   epilepsy tablets), QP23 (home BP 165/100), GS02 (dose request) and FT02 (swollen arm after a fall).
3. Name the clinical reviewer who will sign off future instruction changes.

# x402 Watch Implementation Plan

**Status:** Proposed for review  
**Owner:** Luna, HVE CTO / Head Architect  
**Repository:** `humanvalueexchange/hve-librarian`  
**Related issue:** [#2](https://github.com/humanvalueexchange/hve-librarian/issues/2)  
**Date:** 2026-10-27

## 1. Objective

Complete the private x402 Watch workflow on DGX Spark through HVE-Librarian and
deliver a reviewed brief to Hans in the HVE-Librarian Telegram channel each
weekday morning. Hans can revise the current draft conversationally. A
publication request starts a second explicit confirmation:

1. Hans says the current copy is ready to publish.
2. HVE-Librarian asks:

   `Do you APPROVE x402 WATCH RUN <run_id> FOR PUBLICATION?`

3. Only a clear `YES` authorizes publication of that exact final draft.

The workflow must remain private and non-publishing until that confirmation is
received.

## 2. Approved operating choices

| Decision | Approved behavior |
|---|---|
| Morning delivery | Weekdays at 07:00, `America/Toronto` |
| Partial coverage | Deliver the full brief with a prominent partial-coverage warning |
| Telegram format | Full brief, split into ordered messages when necessary |
| Approval validity | Seven days; draft changes invalidate the pending approval |
| Public destination | `HansHWestphal/x402-watch` |
| Public deployment | Approved export commits directly to `main` and deploys GitHub Pages |
| Private retention | 90 days for raw evidence, diagnostics, and draft revisions |
| No material change | Send a complete no-change brief with coverage evidence |
| Editing scope | Changes apply to the current run only |

Persistent changes to source lanes, writing style, schedule, retention, or
publication policy require a separate decision and must not be inferred from a
single editorial correction.

## 3. Already delivered foundation

Commit
[`d941009`](https://github.com/humanvalueexchange/hve-librarian/commit/d941009)
delivered:

- the `x402-watch` skill contract;
- a private run-oriented collector and validator;
- Don Vito Codes AI News as a discovery-only resource;
- provenance and publication/retrieval-date fields;
- explicit complete, partial, failed, and no-usable-source states;
- source-ID and blocked-language validation; and
- focused tests, with the full repository suite passing at 55 tests.

The foundation deliberately does not schedule a job, call a live model, send
Telegram messages, or publish a public artifact.

## 4. Target architecture

```text
weekday scheduler
    |
    v
private x402 Watch run on Spark
    |
    +-- bounded source discovery and fetch
    +-- append-only run manifest and evidence
    +-- local Hermes structured synthesis
    +-- deterministic claim and coverage validation
    +-- private full brief
    |
    v
Telegram review loop
    |
    +-- editorial revisions bound to run ID
    +-- "ready to publish" intent
    +-- explicit YES/NO confirmation
    |
    v
sanitized public projection
    |
    v
HansHWestphal/x402-watch main -> GitHub Pages
```

Hermes remains generic. HVE-specific orchestration, state, validation, Telegram
review logic, and public projection belong behind the HVE-Librarian adapter and
profile-owned tools.

## 5. Build phases

### Phase A — Live Spark and Hermes verification

Verify the actual runtime before changing scheduling or publication behavior:

- selected local model and provider route;
- 64K-or-greater context for the Hermes agent and any auxiliary model;
- structured JSON output on the real Hermes route;
- local-only inference and absence of hosted fallback;
- GPU/resource behavior, latency, memory, and thermal observations; and
- the active HVE-Librarian Telegram route and delivery mechanism.

Deliverable: a dated private verification record with commands, configuration
identifiers, results, and unresolved limitations.

### Phase B — Synthesis execution

Connect the existing collected evidence packet to local Hermes synthesis.
Require a structured response containing:

- factual claims;
- source IDs for each claim;
- publication date and retrieval date;
- confidence/support metadata;
- material-change assessment;
- unresolved coverage gaps; and
- editorial warnings.

Run deterministic validation after every model response. Reject unknown source
IDs, uncited factual claims, publication/retrieval-date conflation, unsupported
numerical comparisons, blocked payment/investment/HVE-policy language, and
“no material change” without complete current and baseline runs.

Deliverable: a private draft generated from a live Spark evidence packet and a
reproducible validation record.

### Phase C — Private Telegram review loop

Implement profile-owned Telegram delivery and revision state:

- create one run ID and immutable source/evidence baseline;
- send the full brief in ordered Telegram sections;
- include coverage state, source links, evidence gaps, and run ID;
- accept edits against the current run only;
- create a new draft hash for each material revision;
- treat “ready to publish” as an intent, not approval;
- ask the exact YES/NO confirmation for the current run and draft hash;
- reject ambiguous responses without publishing; and
- invalidate approval when the draft changes or the seven-day window expires.

Telegram delivery must not expose private raw evidence beyond the intended
brief and source links. Long content may be stored as a private local artifact
and delivered in bounded sections.

Deliverable: supervised Telegram UAT covering delivery, edits, ready-to-publish,
YES, NO, ambiguous response, changed draft, and expired approval paths.

### Phase D — Public projection and publication

Create `HansHWestphal/x402-watch` as a public static projection repository
using the HVE-maps pattern:

- static HTML/JavaScript presentation;
- versioned public JSON or Markdown briefs;
- `data/manifest.json` describing the current payload;
- source URLs, publisher, publication date, retrieval date, and run ID;
- GitHub Pages deployment through Actions; and
- no raw private run store, credentials, unrestricted article copies, or
  unreviewed drafts.

HVE-Librarian will create the projection only after the explicit YES. The
projection operation must bind the export to the approved run ID and final
draft hash, remove private fields, and fail closed if the source or draft has
changed.

Deliverable: one manually approved publication to the public repository, with
commit SHA, Pages URL, manifest, and Telegram confirmation.

### Phase E — Weekday schedule and retention

Schedule only after Phases A-D pass:

- weekday trigger at 07:00 `America/Toronto`;
- bounded runtime, source count, redirect count, response size, and per-domain
  pacing;
- retry behavior that preserves failed coverage rather than hiding it;
- private artifact cleanup after 90 days;
- no automatic public publication; and
- observable job status and Telegram delivery status.

Deliverable: one supervised scheduled run followed by an operational handoff.

## 6. Publication data contract

The public projection may contain:

- original HVE-written summaries;
- approved factual claims;
- source links;
- publisher names;
- publication and retrieval dates;
- coverage status;
- run ID and publication timestamp;
- claim/source mapping; and
- a public manifest and change history.

It must not contain:

- raw article copies or excessive copyrighted excerpts;
- private diagnostics or local filesystem paths;
- credentials, tokens, or Telegram identifiers;
- unreviewed model output;
- private editorial discussion;
- Bitcoin or Lightning operational data; or
- investment, payment, or HVE-policy recommendations.

## 7. Acceptance gates

The workflow is live only when all gates pass:

1. Spark-local 64K+ Hermes route is verified.
2. Structured synthesis succeeds on live bounded evidence.
3. Claim, citation, date, and coverage validation passes.
4. Telegram delivery and revision loop pass supervised UAT.
5. Explicit YES publishes only the approved run/hash.
6. Public projection and Pages deployment are independently verified.
7. Weekday scheduling completes one supervised run.
8. Retention and failure reporting are observable.

Until then, the system remains a private, manually invoked pilot.

## 8. Explicit non-goals

- No Hermes core modifications.
- No HVE-Coder-Jr delegation for this work.
- No autonomous publication based on conversational implication.
- No payment execution, Bitcoin/Lightning monitoring, or investment advice.
- No automatic indexing of every daily source into the canonical HVE knowledge
  layer.
- No schedule before live verification and supervised UAT.

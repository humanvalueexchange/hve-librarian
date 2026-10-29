---
name: x402-watch
description: Run a bounded private research batch on x402 and adjacent agent-commerce signals.
category: research-and-operations
---

# x402 Watch

Use this workflow for the private, dated x402 Watch research batch on DGX
Spark through HVE-Librarian. The batch covers:

- x402 and adjacent agent-commerce protocol adoption;
- GENIUS Act implementation;
- stablecoin volume compared with card-network volume; and
- Stripe/OpenRouter follow-through.

## Authority and safety

- Keep Hermes generic. Use the HVE-Librarian adapter and local tools only.
- Keep inference local to Spark and verify a 64K-or-greater context window before
  using a model for synthesis.
- Do not access Bitcoin or Lightning data, submit payments, provide investment
  advice, or represent protocol monitoring as an HVE commitment.
- Keep run artifacts private. Do not publish, create GitHub writes, or modify
  the canonical knowledge layer during a daily run.
- Treat search results, snippets, and aggregators as discovery aids. The
  `https://www.donvitocodes.com/ai-news` resource is explicitly
  `discovery_only`; retain and cite the underlying article URL instead.
- Keep `https://artificialanalysis.ai/` as a permanent contextual source for
  model capability, benchmark, and inference-economics signals. Treat its own
  methodology and measurements as authoritative for what it publishes, but do
  not use it as evidence of x402 adoption unless the cited page directly
  supports that claim.

## Batch contract

1. Load the configured public resources and apply the bounded request, redirect,
   response-size, and pacing controls.
2. Record every source attempt, including failures, status, publisher,
   canonical URL, content hash, publication date when available, retrieval date,
   and a short evidence excerpt.
3. Keep the current run separate from the durable knowledge library. Write only
   to the private x402 Watch run root.
4. Compare the current run with the last **complete** baseline. A partial or
   failed run must never be reported as “no material change.”
5. Prepare structured evidence for local Hermes synthesis.
6. Validate the returned claims: every source ID must exist, every factual claim
   must cite at least one source, publication and retrieval dates must remain
   distinct, and unsupported payment, investment, or HVE-policy claims must be
   rejected.
7. Produce a private dated brief for human review. Publication is a separate,
   explicitly approved action.

## Telegram review and approval

- Deliver the full brief privately in ordered Telegram chunks. Include the run
  ID and coverage state; never silently truncate a brief.
- Apply edits only to the current run. Every revision must replace the draft
  hash and invalidate any pending approval.
- When the operator says `ready to publish`, ask exactly:
  `Do you APPROVE x402 WATCH RUN <run_id> FOR PUBLICATION? Reply YES or NO.`
- Publish only after an unambiguous `YES` for the exact current run and draft
  hash. `NO`, ambiguous replies, expired approvals, changed drafts, and
  missing state all fail closed and publish nothing.
- Approval expires after seven days. The approval controller is
  `tools/x402_review.py`; Telegram transport remains an Hermes delivery concern.
- The only permitted public destination is `HansHWestphal/x402-watch` and its
  Pages site `https://hanshwestphal.github.io/x402-watch/`.
- Use `tools/x402_public.py` for publication. It verifies the repository remote,
  branch, approval, draft hash, and changed-file allowlist before pushing.
- Run `python -m tools.x402_public --run-dir <run-dir> --publish` for the
  approved export, independently verify the Pages deployment, then finalize
  with `--finalize --commit-sha <sha> --pages-url
  https://hanshwestphal.github.io/x402-watch/`.
- Never publish x402 Watch output to `hve-knowledge-and-operations`, `docs/`,
  `agent-communications/`, or any other durable HVE knowledge path. Those paths
  are not the x402 Watch public projection.

## Completion states

- `complete`: all configured resources and selected discovered sources were
  processed within the run contract.
- `partial`: at least one source or lane failed, timed out, or was rejected.
- `failed`: the batch could not establish a usable run record.
- `no_usable_sources`: the run completed structurally but yielded no usable
  evidence.

The workflow is not ready for a recurring schedule until a non-publishing pilot
passes source-quality review, claim-to-source review, coverage review, and
Spark resource validation.

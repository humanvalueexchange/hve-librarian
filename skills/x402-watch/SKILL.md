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

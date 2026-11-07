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
- broader LLM competition, model rankings, and capability/economics signals.

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
- Keep `https://benchlm.ai/` as a permanent contextual source for LLM
  competition and model-ranking signals. Treat its published methodology and
  measurements as evidence for the specific claims it supports, not as proof
  of x402 adoption or HVE policy.

## Batch contract

1. Load the configured public resources and apply the bounded request, redirect,
   response-size, and pacing controls.
2. Record every source attempt, including failures, status, publisher,
   canonical URL, content hash, publication date when available, retrieval date,
   and a short evidence excerpt.
3. Keep the current run separate from the durable knowledge library. Write the
   private run to the x402 Watch run root, then publish only the validated
   public projection to `HansHWestphal/x402-watch`.
4. Compare the current run with the last **complete** baseline. A partial or
   failed run must never be reported as “no material change.”
5. Prepare structured evidence for local Hermes synthesis.
6. Validate the returned claims: every source ID must exist, every factual claim
   must cite at least one source, publication and retrieval dates must remain
   distinct, and unsupported payment, investment, or HVE-policy claims must be
   rejected.
7. Produce a dated brief, publish the validated public projection, and report
   the GitHub Pages URL. Do not publish the private run artifacts.

## Publication

- The scheduled job publishes automatically after collection and synthesis
  complete. It reports the run ID and the GitHub Pages URL after verification.
- The only permitted public destination is `HansHWestphal/x402-watch` and its
  Pages site `https://hanshwestphal.github.io/x402-watch/`.
- Use `tools/x402_public.py` for publication. It verifies the repository remote,
  branch, draft hash, and changed-file allowlist before pushing.
- The publisher independently verifies the Pages deployment before finalizing
  the private run state.
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

---
name: position-brief
description: Create and publish a provenance-grounded five-page HVE position brief.
category: research-and-communications
---

# HVE Position Brief Workflow

Use this workflow when Hans asks for a five-page brief, position paper, or
agent-consensus paper based on a source document.

## Deliverable contract

Create a Markdown artifact first. Target approximately 2,000-2,500 words and
use exactly these labeled sections:

1. Executive position
2. Source findings
3. HVE implications
4. Risks, controls, and objections
5. Consensus questions and recommendation

The artifact must:

- identify the source document and its provenance;
- distinguish source facts, librarian interpretation, proposals, assumptions,
  and open questions;
- state that it is a draft for agent consensus unless Hans approved adoption;
- avoid converting a recommendation into policy or an architecture decision;
- preserve uncertainty, limitations, and relevant counterarguments.

## Bounded execution

Use the cached or archived source where available. Retrieve only the source
chunks needed for the five sections; do not repeatedly inject an entire large
library result into the context. Work in phases:

1. gather and cite evidence;
2. draft the complete Markdown brief;
3. self-review against this checklist;
4. publish only after the artifact is complete and verified.

Stop after a failed phase and report the evidence and blocker. Do not loop
between drafting, formatting, and publication in one unbounded turn.

## Publication contract

Publication requires Hans Westphal's explicit approval. Use the governed
communications tools in this order:

1. `write_agent_communication` with a filename matching
   `YYYY-MM-DD-hve-[topic-slug]-vX.X.md`;
2. `publish_agent_communication` to commit and push the file;
3. `comment_on_github_commit` with the resulting commit SHA and a concise
   pointer to the Markdown artifact, its source, and draft status.

Do not claim completion unless the publish result includes a commit and the
pointer-comment result includes a `comment_url`. If any step fails, report the
exact step and do not fabricate a URL.

DOCX or PDF conversion is optional and must be a separate step after the
Markdown artifact and GitHub pointer have been verified.

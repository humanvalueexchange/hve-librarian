# HVE-Coder-Jr Dispatch Integration

HVE-Librarian may submit bounded `skill.enhance` candidates to the single queue
owned by HVE-Coder-Jr. The shared MCP server and queue contract are documented
in the
[Jr dispatch gateway guide](https://github.com/humanvalueexchange/hve-coder-jr/blob/main/docs/dispatch-gateway.md).

The profile-local MCP registration must bind
`HVE_CODER_JR_CALLER_PROFILE_ID` to `hve-librarian`, expose
`submit_jr_skill_enhancement` and `get_jr_job_status`, and be available only in
the controlled CLI toolset. The gateway restricts this caller to
`humanvalueexchange/hve-librarian` and paths under `skills/` ending in
`SKILL.md`. It does not accept a caller ID in tool arguments.

Policy `hve-skill-enhancement-autoqueue` v1.0 auto-authorizes and queues only
bounded `skill.enhance` requests that include non-empty acceptance criteria
and an exact `candidate_contract` which passes read-only preflight against Jr's
staged baseline. Each job is limited to one isolated attempt. If the staged
Librarian checkout is absent or stale, preflight rejects the request before
queue creation; do not widen scope or retry with a weaker contract.

The queue records the policy actor/version, approval reference, authorization
time, contract digest, and preflight summary. Candidate validation does not
commit, push, publish, adopt, deploy, or modify a live profile; those actions
remain human-gated. Nightly generation and automatic promotion are excluded.
Do not change the active Telegram route or restart a gateway as part of this
tool registration.

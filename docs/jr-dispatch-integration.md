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

Submission records an unapproved candidate only. It does not approve, queue,
execute, publish, or deploy work. Hans must explicitly approve any current
request; unattended self-improvement remains disabled until a standing policy
is approved and recorded. Do not change the active Telegram route or restart a
gateway as part of this tool registration.

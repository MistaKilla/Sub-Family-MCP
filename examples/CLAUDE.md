## Sub-Family delegation

This machine has a Sub-Family MCP server with additional models. Delegate
instead of overloading the local model:

- Call `list_family` once per session. Choose by capability, trust, and cost.
- Prefer `free`/`electricity` members for bulk work; reserve `paid` for hard
  tasks where quality or context window matters.
- Use `ask_family_swarm` for 3+ independent subtasks; do not spawn expensive
  local subagents for work a cloud member can run in parallel.
- Keep multi-turn work in a named `session_id`; reset it when done.
- Never send API keys, .env contents, credentials, or private business logic to
  `untrusted_cloud` members. Redaction is a seatbelt, not a guarantee.
- Send images only to members with `vision`; prefer local vision members for
  screenshots that may contain secrets.
- Treat member answers as advice. Verify code and commands before applying.
- Check `family_status` when a member fails or before a large fan-out.

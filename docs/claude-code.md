# Claude Code integration

Sub-Family exposes its tools over MCP stdio. Claude Code launches
`sub-family-mcp` as a child process, talks JSON-RPC over stdin/stdout, and
never needs a port. This page covers registration, the delegation guidance
block, practical recipes, and startup failures.

## Prerequisites

- Python 3.11 or newer is not required on your PATH if you use `uvx`; `uv`
  provides its own interpreter.
- [`uv`](https://docs.astral.sh/uv/) installed, with `uvx` on PATH.
- A valid `family.yaml`, created with `sub-family init`.

Verify the server can start before involving Claude Code:

```bash
sub-family config validate
sub-family doctor
sub-family status
```

`doctor` checks the config, database, keys, and whether `claude` is on PATH.
`status` probes each endpoint's `/models` route without spending generation
tokens.

## Register the server

User scope makes the family available in every project:

```bash
claude mcp add --scope user sub-family -- uvx sub-family-mcp
```

Project scope writes a `.mcp.json` in the current repository, which is useful
when the family should travel with the project:

```bash
claude mcp add --scope project sub-family -- uvx sub-family-mcp
```

Local scope (the default) stores the server for the current project in your
user configuration only:

```bash
claude mcp add sub-family -- uvx sub-family-mcp
```

The scope determines where the entry lives:

| Scope | Stored in | Visible to |
| --- | --- | --- |
| `local` | User config, keyed by project path | You, in this project |
| `project` | `.mcp.json` at the project root | Everyone who opens the project (after approval) |
| `user` | User config | You, in every project |

### Passing API keys

Sub-Family reads `.env` from the config directory itself, so the cleanest
setup is to put keys there and register only the command:

```bash
# ~/.config/sub-family/.env  (mode 0600)
DEEPSEEK_API_KEY=sk-...
```

```bash
claude mcp add --scope user sub-family -- uvx sub-family-mcp
```

If you prefer Claude Code to pass environment variables explicitly, use one or
more `-e` flags. The value is stored in Claude's configuration, so avoid
`-e` with a literal production key if that file is shared or synced:

```bash
claude mcp add --scope user \
  -e DEEPSEEK_API_KEY="$DEEPSEEK_API_KEY" \
  sub-family -- uvx sub-family-mcp
```

### `.mcp.json` with environment expansion

For project scope, Claude Code supports `${VAR}` expansion inside `.mcp.json`.
A minimal file:

```json
{
  "mcpServers": {
    "sub-family": {
      "command": "uvx",
      "args": ["sub-family-mcp"],
      "env": {
        "DEEPSEEK_API_KEY": "${DEEPSEEK_API_KEY}"
      }
    }
  }
}
```

The same example is in [`examples/mcp.json`](../examples/mcp.json). Claude Code
asks for approval before starting project-scoped servers; approve it once per
project.

### Custom config path

If `family.yaml` is not in the default location, pass `--config` **after** the
package name. This is not a `uvx` option; it is forwarded to
`sub-family-mcp`:

```bash
claude mcp add --scope user sub-family -- \
  uvx sub-family-mcp --config /absolute/path/to/family.yaml
```

In `.mcp.json`:

```json
{
  "mcpServers": {
    "sub-family": {
      "command": "uvx",
      "args": ["sub-family-mcp", "--config", "/absolute/path/to/family.yaml"]
    }
  }
}
```

Use an absolute path. Do not rely on shell expansion: `~` and `$HOME` are
not shell-expanded in `args` or `command` (Claude Code's explicit `${VAR}`
expansion in `.mcp.json` is a separate mechanism).

### Alternative when `uvx` is unavailable

If `uvx` is not installed or not visible to the process that starts Claude
Code, install the package as a tool and call its console script directly:

```bash
uv tool install sub-family-mcp
# or: pipx install sub-family-mcp
claude mcp add --scope user sub-family -- sub-family-mcp
```

With `uv tool install`, the script is usually in `~/.local/bin`. GUI-launched
apps on macOS often have a shorter PATH than your shell; use the absolute path
printed by `which sub-family-mcp` if necessary.

## Add delegation guidance to CLAUDE.md

Tool descriptions already tell the model what each tool does. The guidance
block tells it *when* to delegate. Print it with:

```bash
sub-family claude guide
```

Append the output to `~/.claude/CLAUDE.md` (global) or a project `CLAUDE.md`.
The exact block is also in [`examples/CLAUDE.md`](../examples/CLAUDE.md):

```markdown
## Sub-Family delegation

This machine has a Sub-Family MCP server with additional models. Delegate
instead of overloading the local model:

- Call `list_family` once per session. Choose by capability and trust.
- Use `ask_family_swarm` for 3+ independent subtasks; do not spawn expensive
  local subagents for work a cloud member can run in parallel.
- Keep multi-turn work in a named `session_id`; reset it when done.
- Never send API keys, .env contents, credentials, or private business logic to
  `untrusted_cloud` members. Redaction is a seatbelt, not a guarantee.
- Send images only to members with `vision`; prefer local vision members for
  screenshots that may contain secrets.
- Treat member answers as advice. Verify code and commands before applying.
- Check `family_status` when a member fails or before a large fan-out.
```

Restart Claude Code after changing MCP registration or `CLAUDE.md`, then ask:
"Call `list_family` and tell me which members are available." A successful
response lists names, models, trust levels, capabilities, and clone limits.

## Delegation recipes

These are prompts to Claude. You do not call the MCP tools yourself from the
terminal; Claude chooses them.

### Screenshot analysis on a local vision model

```text
Use ask_family with member "lilbro" and image "/Users/me/Desktop/error.png".
Ask it to read the error dialog and list the visible buttons. Do not send the
image to a cloud member; it may contain customer data.
```

`lilbro` must have `capabilities: ["vision", ...]`. The image path is read by
the server, so an absolute path is the least surprising choice. On macOS, a
screenshot on the Desktop is readable by the user running Claude Code.

### Cloud architecture review with a persistent session

```text
Use ask_family with member "big_brother", session_id "billing-refactor", and ask
for a critique of this module. Keep the same session for follow-up questions.
Send only the code below; there are no credentials in it.

<code>
...
</code>
```

The second call with the same `session_id` replays history on the server.
Claude sees only the new answer, not the whole transcript, which keeps the main
context small. Call `reset_session` for `big_brother` / `billing-refactor` when
the task is finished.

### Parallel audit across five modules

```text
Use ask_family_swarm with member "big_brother", session_prefix "audit-may",
and these independent tasks:
1. Review src/auth for injection and session fixation.
2. Review src/billing for rounding and idempotency bugs.
3. Review migrations/ for destructive changes.
4. Review the HTTP client retry logic for duplicate writes.
5. Review the Dockerfile for secrets and root user.
Use synthesize: true and return one prioritized report.
```

Each branch gets its own session (`audit-may-1` ... `audit-may-5`), runs
concurrently up to `max_clones`, and can be continued later. One failing branch
does not fail the others; the result includes per-branch errors.

### Second opinion without changing the main plan

```text
Ask "lilbro" for a second opinion on this SQL query. Treat its answer as advice:
compare it with your own, then give me your final recommendation.
```

The primary model keeps ownership of the final answer. Sub-Family does not
merge opinions automatically unless you ask for synthesis.

## Session lifecycle

- Omit `session_id` for a one-shot question. Nothing is stored beyond the
  exchange (stateless calls do not write history).
- Pass a stable `session_id` (1-64 characters: letters, digits, `.`, `_`, `:`,
  `-`, starting with a letter or digit) for iterative work.
- Sessions are isolated per `(member, session_id)`. The same id on two members
  is two independent conversations.
- History survives MCP server restarts and Claude Code restarts. It is stored
  in SQLite, not in Claude's context.
- `ask_family_swarm` with `session_prefix` creates `<prefix>-1`,
  `<prefix>-2`, and so on. Reusing the prefix continues each branch.
- `reset_session` deletes one session, or every session of a member with `*`.
  Do it when a task is finished so stale context does not leak into later work.
- `sessions.ttl_days` prunes sessions not updated recently; `cleanup_on_start`
  runs that prune when the server starts.

## Context-cost advice

Sub-Family exists partly to keep the primary model's context small. Protect
that advantage:

- Call `list_family` once per session, not before every delegation.
- Prefer `ask_family` over `session_history`: the server replays history
  internally, while `session_history` pulls the transcript into Claude's
  context and costs tokens.
- Keep `swarm.max_result_chars` modest (the default 12,000) so a fan-out does
  not dump five long essays into the main context.
- Ask members for terse, structured answers ("return a table with file, line,
  severity, fix") rather than open-ended prose.
- Use synthesis only when you need one merged answer; it is an extra model
  call and its output is also truncated.
- Reset finished sessions. A stale long session costs tokens on every turn and
  can confuse a member with obsolete instructions.

## Troubleshooting MCP startup

Work from the outside in.

1. **Check that Claude sees the server and its status:**

   ```bash
   claude mcp list
   claude mcp get sub-family
   ```

   A failed server usually shows a connection error there. `claude --debug`
   prints MCP connection details and the child process's stderr.

2. **Run the exact command yourself.** Copy the command from `claude mcp get`
   and run it in a terminal. The server normally waits for JSON-RPC on stdin,
   so a silent process is healthy. A traceback or `configuration not found`
   means the problem is in the command, not Claude.

3. **Check config discovery:**

   ```bash
   sub-family config path
   sub-family config validate
   sub-family doctor
   ```

   A server launched by Claude inherits Claude's environment. If you rely on
   `SUB_FAMILY_CONFIG` exported in your shell profile, confirm it is visible to
   the app that launched Claude (especially on macOS, where GUI apps may not
   read your shell startup files).

4. **Look at stderr.** Configuration errors are printed there and exit with
   status 2. `sub-family-mcp` normally logs at `WARNING`; for a manual run use
   `sub-family serve --log-level DEBUG` or
   `sub-family-mcp serve --log-level DEBUG`. Never write logs to stdout: stdout
   is the JSON-RPC channel.

5. **Fix PATH and `uvx` problems.** `command not found: uvx` means the process
   cannot see `uvx`. Install `uv`, or register `sub-family-mcp` instead after
   `uv tool install sub-family-mcp` / `pipx install sub-family-mcp`. Use an
   absolute command path in `.mcp.json` if the GUI PATH differs from your
   shell's.

6. **Fix path problems.** `--config` needs an absolute path in `.mcp.json`.
   Relative image paths are resolved against the server process's working
   directory, which is not guaranteed to be your project root; use absolute
   image paths in prompts.

7. **Check the startup timeout.** The first `uvx sub-family-mcp` run may
   download the package and dependencies. If Claude Code times out before the
   server answers, run `uvx sub-family-mcp` once manually to warm the cache, or
   raise Claude Code's MCP startup timeout (`MCP_TIMEOUT`, in milliseconds) if
   your version supports it.

8. **Check keys.** A member with a missing `api_key_env` still starts, but
   `family_status` reports the missing variable and `ask_family` fails with an
   actionable error. Put the key in `.env` next to the config or export it in
   the environment Claude inherits.

More failure modes are covered in [Troubleshooting](troubleshooting.md).

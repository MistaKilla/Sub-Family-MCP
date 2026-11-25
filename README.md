# Sub-Family

**English** | [Русский](README.ru.md)

[![CI](https://github.com/MistaKilla/Sub-Family-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/MistaKilla/Sub-Family-MCP/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sub-family-mcp.svg)](https://pypi.org/project/sub-family-mcp/)
[![Python versions](https://img.shields.io/pypi/pyversions/sub-family-mcp.svg)](https://pypi.org/project/sub-family-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

<!-- mcp-name: io.github.MistaKilla/sub-family -->

Sub-Family is an MCP server that turns a YAML list of local and cloud LLMs into
a family of delegates for Claude Code, with persistent sessions, bounded
parallel fan-out, vision, and trust-aware redaction.

```text
Claude Code / any MCP client
        │  MCP over stdio
        ▼
  sub-family-mcp
  ├── registry ...... family.yaml (members, trust, limits)
  ├── sessions ...... SQLite history per (member, session_id)
  ├── guard ......... secret redaction, hard blocks, image policy
  └── HTTP client ... OpenAI-compatible Chat Completions
        ├── local farm: Ollama, LM Studio, vLLM, llama.cpp server
        └── cloud APIs: DeepSeek, OpenAI, OpenRouter, any gateway
```

## Why it exists

Claude Code is good at planning and verification, but there are tasks it should
not run on the primary model or on more local subagents:

- **The local farm is already busy.** Spawning another local subagent competes
  for the same GPU and context. Sub-Family sends a whole batch to one member
  (local or cloud) with a hard concurrency limit, so fan-out does not melt the
  machine.
- **Subagents are heavier than a model call.** A built-in subagent is a separate
  agent loop with its own tools and context. A family member is a direct chat
  completion: cheaper, faster, and predictable, with the main agent keeping the
  final judgement.
- **Not every task may go to the cloud.** Screenshots, private code, and drafts
  belong on hardware you control; architecture reviews and large audits can use
  a cloud model. Trust tiers make that choice explicit, and redaction is applied
  before a prompt leaves the box.

## Features

- **YAML registry** (default `~/.config/sub-family/family.yaml`, or
  `%APPDATA%\sub-family\family.yaml` on Windows) with strict validation, model
  ids, base URLs, trust levels, cost tiers, capabilities, timeouts, and
  per-member concurrency.
- **Cost-aware routing metadata**: every member is `free`, `paid`, or
  `electricity` (self-hosted power only), and the agent sees the tier when it
  chooses a delegate. Omitted values are inferred from trust.
- **Seven MCP tools**: inspect the family, probe health, ask one member, run a
  parallel swarm, list sessions, read history, reset a session.
- **Persistent sessions** in SQLite, keyed by `(member, session_id)`, with a
  sliding window and restart survival.
- **Bounded parallel fan-out** via `ask_family_swarm`; a failing branch does not
  fail the call, and optional synthesis merges the successful outputs.
- **Vision** from local paths, `http(s)` URLs, or base64 data URIs, restricted
  to members that declare the `vision` capability.
- **Trust-aware security**: secret redaction for untrusted cloud members,
  user-defined hard blocks for every member, image policy, and prompt limits.
- **No vendor SDK**: one `httpx` adapter covers every OpenAI-compatible
  endpoint, with explicit retries, timeouts, TLS, and `extra_body` support.
- **CLI and TUI**: scriptable `sub-family` commands plus a full-screen Textual
  editor for members.
- **stdio only**: no daemon, no listening port, no server to keep alive.

Requires Python 3.11+, the official MCP Python SDK 2.x, and an
OpenAI-compatible model endpoint. MIT licensed.

## Quickstart

### 1. Install

```bash
# uv: https://docs.astral.sh/uv/
uv tool install sub-family-mcp       # installs `sub-family` and `sub-family-mcp`
uvx sub-family-mcp --version         # or run once without installing
```

`uvx sub-family-mcp` starts the MCP server. The administrative commands use the
`sub-family` entry point installed by `uv tool install`.

Local development install:

```bash
git clone https://github.com/MistaKilla/Sub-Family-MCP.git
cd Sub-Family-MCP
uv sync --extra dev
uv run sub-family init
```

### 2. Create the config

```bash
sub-family init
```

This writes a commented starter with two members:

- `big_brother` — DeepSeek `deepseek-chat` via `DEEPSEEK_API_KEY`, trusted as
  `untrusted_cloud`.
- `lilbro` — a local Qwen vision model at `http://192.168.1.50:11434`. Replace
  this address with a real machine, or remove the member.

`sub-family init` refuses to overwrite an existing file unless you pass
`--force`; a `.bak` copy is kept.

### 3. Edit members

Use the TUI:

```bash
sub-family ui
```

Or add a member from flags:

```bash
sub-family members add farm_two \
  --base-url http://192.168.1.51:11434 \
  --model qwen2.5-coder:7b \
  --trust local \
  --capability code \
  --capability fast
```

Member names are lowercase, start with a letter, and may contain digits, `_`,
or `-`.

### 4. Set API keys

Prefer environment variables:

```bash
export DEEPSEEK_API_KEY="sk-..."
```

Or put keys in `~/.config/sub-family/.env`, which the CLI and server load
automatically without overriding exported variables:

```bash
# ~/.config/sub-family/.env
DEEPSEEK_API_KEY=sk-...
```

```bash
chmod 600 ~/.config/sub-family/.env
```

Local endpoints such as Ollama usually need no key. See
[Configuration](docs/configuration.md) for `api_key_env`, custom headers, and
literal keys.

### 5. Check reachability

```bash
sub-family status
```

`status` probes every member's `/models` endpoint without generating tokens. It
exits non-zero if an enabled member is unreachable. Use
`sub-family status --member lilbro` or `--json` for a single member or
machine-readable output.

### 6. Connect Claude Code

```bash
claude mcp add --scope user sub-family -- uvx sub-family-mcp
```

Restart Claude Code and ask it to call `list_family`:

```text
Use list_family and tell me which models are available and what they are good at.
```

For a custom config path, pass it after the package name:

```bash
claude mcp add --scope user sub-family -- \
  uvx sub-family-mcp --config /absolute/path/to/family.yaml
```

Project-scoped `.mcp.json`, environment expansion, and delegation recipes are
covered in [Claude Code integration](docs/claude-code.md).

## Minimal configuration

```yaml
version: 1

members:
  big_brother:
    description: "Cloud heavyweight for architecture, hard debugging and code review."
    base_url: "https://api.deepseek.com"
    model: "deepseek-chat"
    api_key_env: "DEEPSEEK_API_KEY"
    trust: "untrusted_cloud"
    cost: "paid"
    capabilities: ["reasoning", "code", "long_context"]
    max_clones: 5
    timeout_s: 300
    max_tokens: 8192

  lilbro:
    description: "Small local vision model for screenshots, diagrams and quick OCR."
    base_url: "http://192.168.1.50:11434"
    model: "qwen2.5-vl:7b"
    trust: "safe_local"
    cost: "electricity"
    capabilities: ["vision", "fast", "chat"]
    max_clones: 1
    timeout_s: 120
    max_tokens: 2048

sessions:
  enabled: true
  max_messages: 40
  max_chars: 80000
  ttl_days: 30

security:
  redact_secrets: true
  redact_trust: ["untrusted_cloud"]
  deny_images_to_remote: false
  max_image_mb: 20
  expose_reasoning: false

swarm:
  max_tasks: 12
  max_result_chars: 12000
  default_synthesize: false
```

The full commented starter is in
[`examples/family.example.yaml`](examples/family.example.yaml), generated from
the same template as `sub-family init`. Every field, default, URL rule, and
backend example is documented in [Configuration](docs/configuration.md).

## Tools

| Tool | What it does | Use it when |
| --- | --- | --- |
| `list_family` | Returns members, models, trust, cost, capabilities, context windows, clone limits, and enabled state. | Once per session, before choosing a delegate. `include_disabled` reveals hidden members. |
| `family_status` | Probes each member's `/models` endpoint; reports latency, reachability, and whether the configured model is exposed. | Before a large fan-out, after a failure, or when a local machine may be offline. |
| `ask_family` | Sends one prompt to one member; optional persistent session, images, system prompt, temperature, and token limit. | One well-scoped task: review, second opinion, screenshot analysis, focused generation. |
| `ask_family_swarm` | Runs independent prompts against one member in parallel, bounded by `max_clones`; optional synthesis. | Three or more independent subtasks: audit five modules, test several files, compare approaches. |
| `list_sessions` | Lists persisted sessions, newest first, with message counts and previews. | Discovering an earlier session id, or checking whether a branch has history. |
| `session_history` | Returns recent messages of one session. | Recovering exact earlier context. Prefer letting `ask_family` replay it; reading it costs context. |
| `reset_session` | Deletes one session, or every session of a member with `*`. | A task is finished and stale context should not leak into later work. |

Common arguments:

- `ask_family`: `member`, `prompt`, `session_id`, `images` (up to 8),
  `system`, `temperature`, `max_tokens`.
- `ask_family_swarm`: `member`, `tasks` (up to `swarm.max_tasks`),
  `session_prefix`, `max_concurrency`, `context`, `synthesize`.
- Session ids are 1-64 characters from letters, digits, `.`, `_`, `:`, `-`,
  starting with a letter or digit.

A failed member call becomes an MCP tool error with an actionable message. A
failed swarm branch becomes a per-branch `{ok: false, error: ...}` result, so
the other branches still return.

## Sessions and swarms

A session is a server-side conversation with one member. The primary model does
not need to carry the transcript:

```text
You:   Use ask_family with member big_brother and session_id "billing-refactor":
       review src/billing/parser.py for rounding and idempotency bugs.

Claude: [calls ask_family; gets a review]
You:   Continue the same session. Propose a minimal patch for the top issue.
```

The second request replays the first exchange from SQLite on the server. Claude
sees only the new answer, which keeps its context small. Sessions survive MCP
and Claude Code restarts, are isolated per member, and are trimmed to
`sessions.max_messages` / `sessions.max_chars`. Reset a finished session with
`reset_session`.

Swarm is for genuinely independent work:

```text
You:   Use ask_family_swarm with member big_brother, session_prefix "audit-may",
       tasks:
       1. Review src/auth for injection and session fixation.
       2. Review src/billing for rounding and idempotency bugs.
       3. Review migrations/ for destructive changes.
       4. Review the HTTP retry logic for duplicate writes.
       5. Review the Dockerfile for secrets and root user.
       Use synthesize: true and return one prioritized report.
```

What happens:

- Five requests run concurrently, never more than `big_brother`'s `max_clones`.
- Branch *N* gets session `audit-may-N`, so each branch can be continued later.
- One failing branch does not fail the call; its result carries `ok: false` and
  `error`.
- `synthesize: true` makes one extra request that merges successful outputs.
- Per-branch content is truncated to `swarm.max_result_chars` (default 12,000
  characters) before it reaches the primary model.

## Trust, cost, and security

| Trust level | Intended use | Redaction by default | Images |
| --- | --- | --- | --- |
| `safe_local` | Model on hardware you control. | None | Allowed. |
| `private_network` | Remote endpoint you operate. | None | Allowed with a warning. |
| `untrusted_cloud` | Third-party API. | Secret patterns replaced. | Allowed with a warning; can be denied. |

Cost is a separate axis from trust:

| Cost tier | Meaning | Typical member |
| --- | --- | --- |
| `free` | No marginal cost (free API tier, included subscription). | OpenRouter free model, free cloud quota. |
| `paid` | Metered per request/token. | DeepSeek, OpenAI, a paid gateway. |
| `electricity` | Self-hosted hardware; only power. | Ollama/LM Studio/vLLM on your own machines. |

If `cost` is omitted, Sub-Family infers it: `safe_local` becomes `electricity`,
everything else becomes `paid`. The `list_family` result includes `cost` and
`cost_source` (`explicit` or `inferred`) so the agent can prefer cheap members
for bulk work and reserve paid ones for hard tasks. This is routing metadata,
not billing: Sub-Family does not track or cap spend.

Before sending to an `untrusted_cloud` member, the guard redacts known credential
shapes (private keys, `sk-` keys, GitHub/AWS/Google/Slack tokens, JWTs, bearer
tokens, URL passwords, and common env-style assignments) and reports what it
changed. `security.redact_trust` extends that to other tiers.

`security.blocked_patterns` is a stronger tool: user-defined regexes are checked
for every member, including local ones, and a match refuses the call.

This is a seatbelt, not a sandbox. Redaction misses novel formats, short keys,
secrets split across lines, and anything inside an image. Never paste real
credentials into a prompt intending to rely on it. Read the full
[security model](docs/security.md).

## CLI overview

Global option: `--config`/`-c` points at a non-default `family.yaml`.

| Command | Purpose |
| --- | --- |
| `sub-family init [--force]` | Write the commented starter config. |
| `sub-family ui` | Open the Textual member editor (requires a TTY). |
| `sub-family status [--member NAME] [--json]` | Probe reachability; exits 1 on failure. |
| `sub-family doctor [--network]` | Check config, storage, keys, and optionally probe endpoints. |
| `sub-family list [--json]` | List configured members. |
| `sub-family members show/add/edit/remove/enable/disable/test` | Manage members. `test` probes one member. |
| `sub-family config path/show/validate/edit` | Inspect or edit the config; `edit` validates before saving. |
| `sub-family sessions list/show/reset/prune` | Inspect and clean SQLite history. |
| `sub-family claude snippet` / `claude guide` | Print MCP registration JSON and the `CLAUDE.md` delegation block. |
| `sub-family paths [--json]` | Show resolved config, env, data, and database paths. |
| `sub-family serve [--log-level]` | Run the MCP server over stdio. |
| `sub-family version` | Print the installed version. |

All commands are scriptable and print errors without tracebacks. `members add`
accepts repeated `--capability` flags; `members edit` changes only the fields
you pass. Full options are in [Configuration](docs/configuration.md) or
`sub-family <command> --help`.

## Supported backends

Any endpoint that implements OpenAI Chat Completions can be a member.

| Backend | Example `base_url` | Notes |
| --- | --- | --- |
| Ollama | `http://192.168.1.50:11434` | `/v1` is added automatically. Pull the model first. |
| LM Studio | `http://192.168.1.50:1234/v1` | Start the local server and load the model. |
| vLLM | `http://192.168.1.60:8000/v1` | `model` must match the served model name. |
| llama.cpp server | `http://192.168.1.70:8080/v1` | Use the OpenAI-compatible server mode. |
| DeepSeek | `https://api.deepseek.com` | `deepseek-chat` or `deepseek-reasoner`. |
| OpenAI | `https://api.openai.com/v1` | Standard API key. |
| OpenRouter | `https://openrouter.ai/api/v1` | Use a model id from its catalogue. |
| Other gateways | Your OpenAI-compatible root | Use `chat_path`, `headers`, or `extra_body` for quirks. |

The base-URL rules and ready-to-copy YAML for each backend are in
[Configuration](docs/configuration.md#backend-examples).

## Troubleshooting quick hits

| Symptom | First move |
| --- | --- |
| `configuration not found` | `sub-family init`, or point `--config` / `SUB_FAMILY_CONFIG` at the file. |
| HTTP 401 or 403 | Check `api_key_env` in the environment that launched the server; check header and prefix. |
| HTTP 404, or `model_available: false` | Fix `base_url`; pull/load the exact model id shown by `/v1/models`. |
| Connection refused or timeout | Confirm the endpoint is running and bound beyond loopback; check firewall and `timeout_s`. |
| Requests hit the wrong path | Set `chat_path`, or remove a duplicated `/v1` from `base_url`. |
| Images rejected | Add the `vision` capability; check format, size (`max_image_mb`), and the 8-image limit. |
| Text was changed before sending | Inspect `warnings` and `redactions`; adjust `redact_trust` or use a local member. |
| `ui` fails in a pipe or CI | It needs a TTY; use `sub-family members add` or `config edit`. |
| `uvx: command not found` | Install uv, or use `uv tool install` / `pipx install` and register `sub-family-mcp`. |

See [Troubleshooting](docs/troubleshooting.md) for exact messages and fixes.

## Development

```bash
git clone https://github.com/MistaKilla/Sub-Family-MCP.git
cd Sub-Family-MCP
uv sync --extra dev

uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

The test suite uses `pytest-asyncio`, `respx` for HTTP mocking, and in-memory
MCP clients; no live model endpoint is required. The Textual TUI is excluded
from strict mypy in `pyproject.toml`.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development conventions and
[Architecture](docs/architecture.md) for the layer boundaries and request flow.

## Documentation

- [Configuration](docs/configuration.md) — every field, paths, trust levels,
  backend examples, backup.
- [Architecture](docs/architecture.md) — layers, request flows, concurrency,
  SQLite schema, provider extension.
- [Security](docs/security.md) — threat model, redaction rules, image policy,
  API key storage, reporting.
- [Claude Code integration](docs/claude-code.md) — registration, `CLAUDE.md`,
  recipes, context cost, startup debugging.
- [Troubleshooting](docs/troubleshooting.md) — common errors and fixes.
- [Publishing](docs/publishing.md) — release checklist, PyPI trusted publishing,
  MCP Registry.
- [CHANGELOG.md](CHANGELOG.md) — release history.
- [SECURITY.md](SECURITY.md) — supported versions and private reporting.

## License

MIT. See [LICENSE](LICENSE).

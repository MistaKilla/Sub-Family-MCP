# Configuration

Sub-Family reads one YAML file, `family.yaml`, that describes the model
endpoints ("members") plus session, security, and swarm limits. The file is
validated on every load; unknown keys are errors, not warnings. Edit members
with `sub-family ui` or `sub-family members ...`; edit the session, security,
and swarm sections with `sub-family config edit` or a text editor. Then run
`sub-family config validate`.

## File locations

| File | Linux/macOS | Windows | Override |
| --- | --- | --- | --- |
| `family.yaml` | `$XDG_CONFIG_HOME/sub-family/family.yaml`, default `~/.config/sub-family/family.yaml` | `%APPDATA%\sub-family\family.yaml` | `SUB_FAMILY_CONFIG` (full file path) or `SUB_FAMILY_CONFIG_DIR` (directory) |
| `.env` | next to `family.yaml` | next to `family.yaml` | follows the config path; there is no separate override |
| `sessions.db` | `$XDG_DATA_HOME/sub-family/sessions.db`, default `~/.local/share/sub-family/sessions.db` | `%LOCALAPPDATA%\sub-family\sessions.db` | `sessions.db_path` in the config, then `SUB_FAMILY_DATA_DIR` |

Resolution order:

1. Config file: `--config`/`-c` on the command line, then
   `SUB_FAMILY_CONFIG`, then `SUB_FAMILY_CONFIG_DIR/family.yaml`, then the
   platform default.
2. Environment file: always `<config-file-parent>/.env`. Existing process
   environment variables win; `.env` never overrides them.
3. Session database: `sessions.db_path` if set (absolute, or relative to the
   config file), otherwise `$SUB_FAMILY_DATA_DIR/sessions.db`, otherwise the
   platform data directory.

`sub-family paths` prints the four resolved locations, and `--json` makes the
output scriptable.

## Starter configuration

`sub-family init` writes the following commented file (also reproduced
verbatim in [`examples/family.example.yaml`](../examples/family.example.yaml)):

```yaml
# Sub-Family family registry
# Documentation: https://github.com/MistaKilla/Sub-Family-MCP
#
# Manage this file from the console with:  sub-family ui
# Or edit it by hand -- the schema is validated on load.
#
# base_url must point at an OpenAI-compatible API root. Sub-Family appends
# /v1/chat/completions when the URL does not already end in /v1 (Ollama, LM
# Studio, vLLM, llama.cpp server, DeepSeek, OpenRouter and most gateways work).
# Keep API keys out of this file: name an environment variable instead.

version: 1

members:
  big_brother:
    description: "Cloud heavyweight for architecture, hard debugging and code review."
    base_url: "https://api.deepseek.com"
    model: "deepseek-chat"
    api_key_env: "DEEPSEEK_API_KEY"
    trust: "untrusted_cloud"      # prompts are scrubbed for secrets before sending
    cost: "paid"                  # free | paid | electricity (self-hosted power only)
    capabilities: ["reasoning", "code", "long_context"]
    max_clones: 5                 # parallel swarm sessions this member may run
    timeout_s: 300
    max_tokens: 8192

  lilbro:
    description: "Small local vision model for screenshots, diagrams and quick OCR."
    base_url: "http://192.168.1.50:11434"   # <- replace with your gaming PC / small farm
    model: "qwen2.5-vl:7b"
    trust: "safe_local"
    cost: "electricity"           # local: only power; omit to infer from trust
    capabilities: ["vision", "fast", "chat"]
    max_clones: 1                 # one request at a time keeps the GPU happy
    timeout_s: 120
    max_tokens: 2048

sessions:
  enabled: true
  max_messages: 40        # sliding window kept per (member, session_id)
  max_chars: 80000
  ttl_days: 30            # 0 disables automatic pruning

security:
  redact_secrets: true
  redact_trust: ["untrusted_cloud"]
  deny_images_to_remote: false   # set true to forbid images to cloud members
  max_image_mb: 20
  expose_reasoning: false        # include model reasoning traces in tool output

swarm:
  max_tasks: 12           # hard cap for one ask_family_swarm call
  max_result_chars: 12000 # per-task truncation returned to the main agent
  default_synthesize: false
```

`version` must be `1`. The top level accepts only `version`, `members`,
`sessions`, `security`, and `swarm`.

## Members

Member names must match `^[a-z][a-z0-9_-]{0,39}$`: lowercase letters, digits,
underscore, or hyphen, starting with a letter. Lookups by tool/CLI are
case-insensitive, but the canonical name is the YAML key.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `description` | string | `""` | Human/agent-facing purpose note returned by `list_family`. |
| `base_url` | string | required | OpenAI-compatible API root. Absolute `http://` or `https://`, no embedded credentials, trailing slash removed. |
| `model` | string | required | Model id exactly as the endpoint expects it. |
| `chat_path` | string or null | null | Overrides the automatic suffix with a path starting `/`, e.g. `/custom/chat`. |
| `api_key` | string or null | null | Literal key, stored in plaintext in the YAML. Prefer `api_key_env`. |
| `api_key_env` | string or null | null | Environment variable that holds the key. |
| `api_key_header` | string | `Authorization` | Header used for the key. |
| `api_key_prefix` | string | `"Bearer "` | Prefix prepended to the key (trailing space is significant). Set `""` for `X-API-Key` style auth. |
| `headers` | mapping | `{}` | Extra request headers, always sent. |
| `system_prompt` | string or null | null | Default system prompt, prepended to any per-call `system` value. |
| `trust` | enum | `safe_local` | `safe_local`, `private_network`, or `untrusted_cloud`. |
| `cost` | enum or null | null | `free`, `paid`, or `electricity`. When null, inferred from trust: local → `electricity`, remote → `paid`. |
| `capabilities` | list of strings | `["chat"]` | Tags used for selection. Lowercased and de-duplicated. |
| `max_clones` | integer 1-64 | `1` | Maximum concurrent generation requests for this member. |
| `timeout_s` | number > 0, ≤ 3600 | `120` | Per-request HTTP timeout. |
| `retries` | integer 0-5 | `2` | Retries after the first attempt (total attempts = `retries + 1`). |
| `max_tokens` | integer or null | null | Output limit sent as `max_tokens` when set. |
| `temperature` | number 0-2 or null | null | Sampling temperature; omitted from the request when null. |
| `context_window` | integer 256-10,000,000 or null | null | Enables a pre-flight estimate; requests above 90% are rejected. |
| `verify_tls` | boolean | `true` | Set false only for a trusted endpoint with a self-signed certificate. |
| `enabled` | boolean | `true` | Disabled members are hidden from the tools unless requested and cannot be called. |
| `extra_body` | mapping | `{}` | Extra JSON merged into the request body for backend-specific options. |

### `base_url` and `chat_path`

If `chat_path` is absent, the adapter derives the Chat Completions URL:

| `base_url` | Effective chat URL |
| --- | --- |
| `http://192.168.1.50:11434` | `http://192.168.1.50:11434/v1/chat/completions` |
| `http://192.168.1.50:1234/v1` | `http://192.168.1.50:1234/v1/chat/completions` |
| `https://api.deepseek.com/v1/` | `https://api.deepseek.com/v1/chat/completions` |
| `https://gateway.example.com/openai/v1` | `https://gateway.example.com/openai/v1/chat/completions` |
| `https://example.com/custom/chat/completions` | unchanged |
| `chat_path: /api/chat` | `<base_url>/api/chat` |

The rule is: use the URL as-is if its path ends in `/chat/completions`; append
`/chat/completions` if the path ends in `/v1` or contains `/v1/`; otherwise
append `/v1/chat/completions`. `chat_path` wins over all of this and is appended
verbatim, including any query string.

Health checks derive `/models` by replacing a `/chat/completions` suffix;
otherwise they request `<base_url>/v1/models`. A custom `chat_path` does not
change the health URL, so a gateway that serves chat on a non-standard path but
has no `/v1/models` can work for `ask_family` while `family_status` reports an
error. This is a known limitation of the current probe.

### Authentication

- A literal `api_key` wins over `api_key_env`.
- If `api_key_env` is set but the variable is empty, starting the service
  records a configuration error for that member; `family_status` reports it and
  `ask_family` refuses the call with an actionable message.
- Local endpoints that need no key simply omit both fields.
- `headers` are applied first, then the auth header; an auth header with the
  same name replaces the custom one.
- `sub-family config show` and `sub-family members show` mask literal keys as
  `***`. The YAML on disk is not encrypted.

Custom-header example:

```yaml
vllm:
  base_url: "http://192.168.1.60:8000/v1"
  model: "Qwen/Qwen2.5-Coder-32B-Instruct"
  api_key_env: "VLLM_API_KEY"
  api_key_header: "X-API-Key"
  api_key_prefix: ""
  trust: "safe_local"
  max_clones: 1
```

A gateway with a non-standard path:

```yaml
azure:
  base_url: "https://my-resource.openai.azure.com"
  chat_path: "/openai/deployments/my-deployment/chat/completions?api-version=2024-10-21"
  model: "gpt-4o"
  api_key_env: "AZURE_OPENAI_API_KEY"
  api_key_header: "api-key"
  api_key_prefix: ""
  trust: "untrusted_cloud"
```

### `extra_body`

`extra_body` is merged into the JSON request after the standard fields.
`model`, `messages`, and `stream` are always removed from it. If
`max_completion_tokens` is supplied, `max_tokens` is removed from the payload,
which suits endpoints that reject the older parameter.

```yaml
farm:
  base_url: "http://192.168.1.51:8000/v1"
  model: "local-model"
  extra_body:
    top_k: 40
    min_p: 0.05
    chat_template_kwargs:
      enable_thinking: false
```

## Trust levels

Trust controls redaction and image warnings; it does not verify where the URL
actually points. Putting a cloud URL under `safe_local` disables the default
redaction for that member.

| Value | Meaning | Redacted by default | Image behaviour |
| --- | --- | --- | --- |
| `safe_local` | Hardware you control, inside your LAN. | No | Allowed without warning. |
| `private_network` | A remote endpoint you operate (VPS, friend's farm). Still leaves the local machine. | No | Allowed with a warning. |
| `untrusted_cloud` | A third-party API. | Yes | Allowed with a stronger warning; blocked entirely by `security.deny_images_to_remote`. |

`security.redact_trust` decides which levels are scrubbed. To also redact for a
private VPS, add it explicitly:

```yaml
security:
  redact_trust: ["private_network", "untrusted_cloud"]
```

See [Security](security.md) for what redaction does and does not catch.

## Cost tiers

`cost` is routing metadata, separate from trust. It tells the primary model
whether delegating to a member has a marginal price:

| Value | Meaning | Typical member |
| --- | --- | --- |
| `free` | No marginal cost: a free API tier or an already-paid subscription. | A free OpenRouter model, a promotional cloud quota. |
| `paid` | Metered per request or token. | DeepSeek API, OpenAI API, a paid gateway. |
| `electricity` | Self-hosted hardware; only power and GPU time. | Ollama, LM Studio, vLLM, llama.cpp on your machines. |

If `cost` is omitted, Sub-Family infers `electricity` for `safe_local` members
and `paid` for everything else. Inference is deliberately conservative for
remote endpoints, so a free cloud tier must be marked explicitly:

```yaml
members:
  free_cloud:
    base_url: "https://openrouter.ai/api/v1"
    model: "some-free-model:free"
    api_key_env: "OPENROUTER_API_KEY"
    trust: "untrusted_cloud"
    cost: "free"
```

`list_family` returns both `cost` and `cost_source` (`explicit` or `inferred`).
Cost does not change redaction, image policy, or concurrency; use `trust` for
those. Sub-Family never meters or enforces a budget — the tier only informs the
agent's choice (bulk work on `free`/`electricity`, hard tasks on `paid`).

## Capabilities

Capabilities are free-form tags; known names are:

| Capability | Intended meaning | Enforced by code |
| --- | --- | --- |
| `chat` | General conversation. | No |
| `vision` | Accepts image input. | Yes: images are rejected otherwise. |
| `code` | Coding, refactoring, tests. | No |
| `reasoning` | Architecture, hard analysis. | No |
| `fast` | Low-latency, cheap work. | No |
| `long_context` | Large context window. | No |
| `tools` | Tool/function calling. | No; informational only. |

Any lowercase token matching `^[a-z][a-z0-9_-]{0,31}$` is accepted, but only
`vision` changes runtime behaviour. Use tags consistently so the primary model
can choose members from `list_family`.

## Sessions

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | boolean | `true` | Turn persistent history on or off. With it off, `session_id` arguments are rejected. |
| `db_path` | string or null | null | SQLite file. Relative paths resolve against the config file's directory. |
| `max_messages` | integer 2-1000 | `40` | Sliding window size per `(member, session_id)`. |
| `max_chars` | integer 1000-2,000,000 | `80000` | Character budget for the kept window. |
| `ttl_days` | integer 0-3650 | `30` | Delete sessions not updated within this many days. `0` disables TTL pruning. |
| `cleanup_on_start` | boolean | `true` | Run TTL pruning when the service starts. |

The window is trimmed after each exchange; see
[Architecture](architecture.md#session-storage) for the algorithm. Setting
`db_path` is useful for keeping a project's sessions inside the repository
(remember to gitignore the file).

## Security

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `redact_secrets` | boolean | `true` | Master switch for pattern-based redaction. |
| `redact_trust` | list of trust levels | `["untrusted_cloud"]` | Only these levels are scrubbed. |
| `blocked_patterns` | list of regex strings | `[]` | Hard blocks checked for every trust level before sending. A match refuses the request. |
| `max_prompt_chars` | integer 1000-5,000,000 | `400000` | Maximum prompt/system-prompt length. |
| `max_image_mb` | number > 0, ≤ 200 | `20` | Per-image size limit after loading. |
| `deny_images_to_remote` | boolean | `false` | Refuse images to any member whose trust is remote (`private_network` or `untrusted_cloud`). |
| `expose_reasoning` | boolean | `false` | Include a model reasoning trace in tool output. |
| `max_reasoning_chars` | integer 0-100,000 | `4000` | Truncate reasoning to this length; `0` disables exposure even if enabled. |

Invalid regular expressions in `blocked_patterns` are configuration errors.
Hard blocks run before redaction and apply even to `safe_local` members.

## Swarm

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `max_tasks` | integer 1-64 | `12` | Hard cap on prompts in one `ask_family_swarm` call. |
| `max_result_chars` | integer 500-200,000 | `12000` | Per-branch truncation in the response, and the synthesis output cap. |
| `default_synthesize` | boolean | `false` | Whether to synthesise when the tool argument is omitted. |

Even with a large `max_tasks`, the member's `max_clones` limits concurrent
requests. A local GPU with `max_clones: 1` will run a twelve-task swarm
sequentially, not twelve-wide.

## Environment variables

| Variable | Used by | Purpose |
| --- | --- | --- |
| `SUB_FAMILY_CONFIG` | CLI, server | Full path to `family.yaml`. |
| `SUB_FAMILY_CONFIG_DIR` | CLI, server | Directory holding `family.yaml` and `.env`. Ignored when `SUB_FAMILY_CONFIG` is set. |
| `SUB_FAMILY_DATA_DIR` | CLI, server | Directory for `sessions.db` when the config does not set `db_path`. |
| `SUB_FAMILY_LOG_LEVEL` | CLI and server | Default log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). `sub-family serve --log-level` overrides it. |
| `XDG_CONFIG_HOME`, `XDG_DATA_HOME` | POSIX defaults | Standard XDG base directories. |
| `APPDATA`, `LOCALAPPDATA` | Windows defaults | Standard Windows config/data locations. |
| `VISUAL`, `EDITOR` | `sub-family config edit` | Editor used by Typer. |
| `<NAME>_API_KEY` (any name) | Provider auth | Referenced by `api_key_env`; no fixed naming scheme. |

API keys can be exported in the shell that launches Claude Code, or placed in
the `.env` next to the config. The file is read with `python-dotenv` and
existing environment variables are not overridden. The repository ships a
commented template in [`.env.example`](../.env.example).

## Add a farm in five lines

Inside `members:`:

```yaml
  farm_two:
    base_url: "http://192.168.1.51:11434"
    model: "qwen2.5-coder:7b"
    trust: "safe_local"
    capabilities: ["code", "fast"]
```

Then run `sub-family status --member farm_two`. Add `max_clones: 1` explicitly
if the machine has one GPU; it is the default anyway. The CLI equivalent:

```bash
sub-family members add farm_two \
  --base-url http://192.168.1.51:11434 \
  --model qwen2.5-coder:7b \
  --trust local \
  --cost electricity \
  --capability code \
  --capability fast
```

## Backend examples

### Ollama

```yaml
members:
  ollama_coder:
    description: "Local Qwen coder on the LAN box."
    base_url: "http://192.168.1.50:11434"
    model: "qwen2.5-coder:7b"
    trust: "safe_local"
    capabilities: ["code", "fast"]
    max_clones: 1
    timeout_s: 180
```

Ollama serves the OpenAI-compatible API under `/v1`; the adapter adds it
automatically. If Ollama runs on another machine, start it with
`OLLAMA_HOST=0.0.0.0:11434` and allow the port through the firewall. Verify with
`curl http://192.168.1.50:11434/v1/models`.

### LM Studio

```yaml
members:
  lmstudio:
    base_url: "http://192.168.1.50:1234/v1"
    model: "qwen2.5-coder-7b-instruct"
    trust: "safe_local"
    capabilities: ["chat", "code"]
    max_clones: 1
```

Enable the local server in LM Studio and load the model first. Both
`http://host:1234` and `http://host:1234/v1` work.

### vLLM

```yaml
members:
  vllm:
    base_url: "http://192.168.1.60:8000/v1"
    model: "Qwen/Qwen2.5-Coder-32B-Instruct"
    trust: "safe_local"
    capabilities: ["code", "reasoning", "long_context"]
    max_clones: 2
    timeout_s: 300
    context_window: 32768
```

`model` must match `--served-model-name` (or the Hugging Face id vLLM reports
in `/v1/models`). If the server was started with `--api-key`, put that value in
an environment variable and reference it via `api_key_env`.

### DeepSeek

```yaml
members:
  big_brother:
    description: "Cloud heavyweight for architecture and review."
    base_url: "https://api.deepseek.com"
    model: "deepseek-chat"
    api_key_env: "DEEPSEEK_API_KEY"
    trust: "untrusted_cloud"
    capabilities: ["reasoning", "code", "long_context"]
    max_clones: 5
    timeout_s: 300
    max_tokens: 8192
```

`deepseek-reasoner` also works; reasoning traces are returned only when
`security.expose_reasoning` is true. Export the key or add it to `.env`:

```bash
export DEEPSEEK_API_KEY="sk-..."
# or, in ~/.config/sub-family/.env
DEEPSEEK_API_KEY=sk-...
```

### OpenRouter

```yaml
members:
  openrouter:
    base_url: "https://openrouter.ai/api/v1"
    model: "meta-llama/llama-3.3-70b-instruct"
    api_key_env: "OPENROUTER_API_KEY"
    trust: "untrusted_cloud"
    capabilities: ["chat", "reasoning", "code"]
    max_clones: 3
    timeout_s: 240
```

Use a model id copied from the OpenRouter catalogue; ids change over time.

## Backup and migration

- Every `save_config()` (CLI edits, TUI saves) keeps the previous file as
  `family.yaml.bak` and writes the new one with mode `0600`.
- For a manual backup, copy both the config and `.env` somewhere private. They
  may contain literal keys.
- For a consistent SQLite backup, stop the server or use SQLite's backup
  command:

  ```bash
  sqlite3 ~/.local/share/sub-family/sessions.db \
    ".backup '/secure/backups/sessions-$(date +%F).db'"
  ```

  If you copy the database file by hand, stop the process first, or copy
  `sessions.db`, `sessions.db-wal`, and `sessions.db-shm` together.
- To move to another machine, copy `family.yaml`, `.env`, and the database,
  then adjust `sessions.db_path` or `SUB_FAMILY_DATA_DIR`. Re-run
  `sub-family doctor` and `sub-family status` afterwards.
- The database declares `PRAGMA user_version = 1`. A newer schema is rejected
  with an explicit error rather than opened blindly.
- Session history may contain prompts and model answers, including anything
  that was sent to a local member. Store and delete backups accordingly; see
  [Security](security.md#session-database).

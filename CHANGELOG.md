# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-11-25

First public release: an MCP server that turns several OpenAI-compatible model
endpoints into a delegation family for Claude Code and other MCP clients.

### Added

- **MCP server over stdio** (`sub-family-mcp`) with seven tools:
  `list_family`, `family_status`, `ask_family`, `ask_family_swarm`,
  `list_sessions`, `session_history`, and `reset_session`. Responses are
  structured JSON with content, provenance, usage, warnings, and session
  metadata.
- **Single-member delegation** through `ask_family`, including a persistent
  `session_id`, per-call system prompt, temperature, `max_tokens`, and images.
- **Parallel swarms** through `ask_family_swarm`: independent branches run
  concurrently under the member's `max_clones`, per-branch failures are
  isolated, results are truncated to a configurable budget, and optional
  synthesis merges successful outputs in one extra request.
- **OpenAI-compatible provider** for Ollama, LM Studio, vLLM, llama.cpp server,
  DeepSeek, OpenAI, OpenRouter, and compatible gateways. Supports `base_url`
  path heuristics, `chat_path`, custom auth headers, extra headers,
  `extra_body`, per-member TLS verification, timeouts, and retries that honour
  `Retry-After`.
- **Health checks** (`family_status`, `sub-family status`, `members test`,
  `doctor --network`) that probe each endpoint's model list without spending
  generation tokens and distinguish an unreachable endpoint from a missing
  model id.
- **Persistent sessions** backed by SQLite, scoped by `(member, session_id)`,
  surviving server and client restarts. A sliding window limits messages and
  characters per session; TTL pruning runs at startup. Image bytes are stored
  only as text references, never as blobs.
- **Vision input** from local file paths, `http(s)` URLs, and base64 data URIs,
  gated by the `vision` capability, limited to eight images per call, with MIME
  sniffing, size limits, and remote-image policy warnings.
- **Trust tiers** (`safe_local`, `private_network`, `untrusted_cloud`) that
  control secret redaction and image policy and are reported with every answer.
- **Cost tiers** (`free`, `paid`, `electricity`) on every member, returned by
  `list_family` with `cost_source`. Omitted values are inferred from trust so
  local hardware is never mistaken for a metered API; the CLI and TUI expose
  the field directly.
- **Secret guard** for configured trust levels: PEM private keys, OpenAI-style
  keys, GitHub tokens, AWS access-key IDs, Google API keys, Slack tokens, JWTs,
  bearer tokens, URL credentials, `.env`-style assignments, and named secret
  assignments. Findings are counted and returned in tool output.
- **Hard blocks** through `security.blocked_patterns`, a list of regular
  expressions checked for every trust level before a request is sent.
- **Strict `family.yaml` schema** built with frozen Pydantic models and
  `extra="forbid"`, including bounds for concurrency, timeouts, context size,
  prompt size, image size, and swarm limits. URLs with embedded credentials are
  rejected.
- **Configuration tooling**: `sub-family init` with `big_brother` and `lilbro`
  examples, `config show/validate/edit`, atomic saves with `.bak` backups,
  owner-only file permissions, `.env` loading without overriding real
  environment variables, and `api_key_env` resolution with actionable errors.
- **Cross-platform paths** following XDG on POSIX and AppData on Windows, with
  `SUB_FAMILY_CONFIG`, `SUB_FAMILY_CONFIG_DIR`, and `SUB_FAMILY_DATA_DIR`
  overrides.
- **Administrative CLI** (`sub-family`): `init`, `paths`, `list`, `status`,
  `doctor`, `members show/add/edit/remove/enable/disable/test`, `config`,
  `sessions list/show/reset/prune`, `serve`, `claude snippet/guide`, `ui`, and
  `version`.
- **Textual TUI** (`sub-family ui`) for adding, editing, deleting, and
  probing members with validation and capability selection.
- **Documentation and examples**: English and Russian READMEs, architecture,
  configuration, security, Claude Code, troubleshooting, and publishing guides,
  plus a byte-for-byte copy of the starter config and Claude Code examples.
- **Packaging and CI**: Hatchling src-layout build, `sub-family` and
  `sub-family-mcp` entry points, `py.typed`, Python 3.11/3.12/3.13 test matrix,
  ruff, strict mypy, pytest with coverage, PyPI Trusted Publishing, and a
  release workflow that verifies the tag, package version, and `server.json`.

### Changed

- The distribution is named `sub-family-mcp`; administration uses
  `sub-family` and the stdio server uses `sub-family-mcp`. The version is
  single-sourced from `sub_family.__version__`.
- MCP logging is configured to stderr only, leaving stdout as a clean JSON-RPC
  stream.
- The provider layer uses `httpx` directly instead of vendor SDKs, so retries,
  timeouts, TLS, headers, and request bodies remain explicit and uniform.

### Security

- Redaction is applied to prompts and system prompts before they reach a member
  whose trust level is configured for scrubbing; it is documented as a
  best-effort seatbelt, not a guarantee.
- Prompt and system-prompt size caps, per-image size caps, an eight-image limit,
  and `security.deny_images_to_remote` reduce accidental exfiltration through
  misconfigured members.
- API keys are held in `SecretStr` in memory, masked in config dumps, and never
  written to logs by the library. Literal keys in `family.yaml` are stored in
  plaintext but the file is written owner-only.
- Tool output marks member and trust provenance so delegated answers are
  treated as untrusted advice by the calling agent.
- Security policy and reporting process are documented in `SECURITY.md` and
  `docs/security.md`.

[Unreleased]: https://github.com/MistaKilla/Sub-Family-MCP/compare/v0.1.0...develop
[0.1.0]: https://github.com/MistaKilla/Sub-Family-MCP/releases/tag/v0.1.0

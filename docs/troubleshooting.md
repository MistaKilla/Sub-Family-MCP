# Troubleshooting

Start with the built-in diagnostics:

```bash
sub-family config path
sub-family config validate
sub-family doctor
sub-family status
```

`doctor` checks the config, database, keys, and Claude Code installation.
`status` probes every member's `/models` endpoint and exits non-zero if an
enabled member is unreachable. Add `--json` to either command for machine
output.

## Configuration not found

```text
configuration not found at /Users/me/.config/sub-family/family.yaml.
Create a starter config with: sub-family init
```

The server and CLI agree on this path. Fix it with one of:

```bash
sub-family init                         # create the default config
sub-family --config ./family.yaml list  # use a project config
export SUB_FAMILY_CONFIG=/path/to/family.yaml
export SUB_FAMILY_CONFIG_DIR=/path/to/dir
```

Remember that Claude Code does not run a shell, so `~` in an `.mcp.json`
argument is not expanded. Use an absolute path:

```json
"args": ["sub-family-mcp", "--config", "/Users/me/projects/app/family.yaml"]
```

Check what the process actually resolves with `sub-family config path` and
`sub-family paths`.

## Invalid YAML or schema errors

YAML syntax errors include a location:

```text
invalid YAML in /Users/me/.config/sub-family/family.yaml at line 12, column 3: ...
```

Schema errors list each offending field:

```text
invalid configuration in /Users/me/.config/sub-family/family.yaml:
  - members.lilbro.max_clone: Extra inputs are not permitted
  - members.lilbro.base_url: base_url must be an absolute http:// or https:// URL
```

The schema is strict: unknown keys, misspelled field names, and wrong types are
errors. Common causes:

- Tabs used for indentation (YAML requires spaces).
- `max_clone` instead of `max_clones`.
- `base_url: localhost:11434` without a scheme.
- A member name with uppercase letters or spaces.
- `version` other than `1`.
- A list where a mapping is expected.

Run `sub-family config validate` after every hand edit. `sub-family config
edit` opens `$VISUAL`/`$EDITOR`, validates the result, and keeps rejected text
in a `.edited` file instead of overwriting the config.

## HTTP 401 or 403

```text
member 'big_brother' rejected the API key (HTTP 401). Check DEEPSEEK_API_KEY.
```

- The environment variable named by `api_key_env` is empty or absent in the
  environment that launched the server.
- The literal `api_key` and `api_key_env` refer to different accounts; the
  literal value wins.
- The key is valid but the wrong header is used. `api_key_header` defaults to
  `Authorization`; `api_key_prefix` defaults to `"Bearer "`. For `X-API-Key`
  style auth set `api_key_header: "X-API-Key"` and `api_key_prefix: ""`.
- The key was pasted with quotes, whitespace, or a trailing newline.
- A gateway expects a different auth scheme entirely; use `headers` instead.

Check without spending tokens:

```bash
sub-family status --member big_brother
curl -H "Authorization: Bearer $DEEPSEEK_API_KEY" \
  https://api.deepseek.com/v1/models
```

`.env` is read from the config directory, not the current working directory.
The process environment wins over `.env`.

## HTTP 404 or "model not found"

```text
member 'lilbro' returned HTTP 404 for chat completion.
Check base_url (http://192.168.1.50:11434) and model (qwen2.5-vl:7b).
```

`family_status` separates the two cases:

- `ok: false` — the endpoint itself is unreachable or rejects the key.
- `ok: true`, `model_available: false` — the endpoint answers, but the exact
  model id is not in `/models`.

Fixes:

- Pull/load the model on the endpoint (`ollama pull qwen2.5-vl:7b`, start the
  LM Studio server, check `--served-model-name` in vLLM).
- Copy the id exactly from `GET /v1/models`; Ollama may expose a different
  tag or a namespaced name than the one in the config.
- Fix `base_url`. A 404 can also mean the path is wrong, not that the model is
  missing.
- Use `chat_path` for a gateway whose completions route is not
  `<base>/v1/chat/completions`.

## Timeout or connection refused

Typical messages:

```text
member 'lilbro' is unreachable at http://192.168.1.50:11434: ...
member 'big_brother' timed out after 300s
```

For a local/LAN endpoint:

1. From the same machine, run `curl http://192.168.1.50:11434/v1/models`.
   If that fails, Sub-Family cannot reach it either.
2. Make sure the server binds beyond loopback. Ollama defaults to
   `127.0.0.1`; to accept LAN connections:

   ```bash
   OLLAMA_HOST=0.0.0.0:11434 ollama serve
   ```

   Do this only on a trusted network or behind a firewall/reverse proxy.
3. Open the port in the host firewall and confirm no VPN is routing the LAN
   address differently.
4. Check the port. Ollama is 11434, LM Studio is 1234, vLLM defaults to 8000,
   llama.cpp server is often 8080.
5. Raise `timeout_s` for a slow model. The first load of a large model can take
   minutes; the request timeout applies to the whole generation.
6. Retries cover connection failures, timeouts, 429, and 5xx. They cannot fix a
   wrong host, a closed port, or a server that is not running.

For a cloud endpoint, check DNS, proxy settings, and whether the machine
requires a corporate proxy. Sub-Family uses `httpx` defaults and does not read
proxy variables explicitly, but httpx honours the standard environment.

## Wrong `/v1` path

The adapter is deliberately forgiving:

| You configure | It requests |
| --- | --- |
| `http://host:11434` | `http://host:11434/v1/chat/completions` |
| `http://host:1234/v1` | `http://host:1234/v1/chat/completions` |
| `https://api.deepseek.com` | `https://api.deepseek.com/v1/chat/completions` |
| `https://host/v1/chat/completions` | unchanged |

If the real route is different, do not fight the heuristic; set it
explicitly:

```yaml
base_url: "http://host:8080"
chat_path: "/api/chat"
```

`chat_path` is appended verbatim and must start with `/`. Health checks still
probe `<base_url>/v1/models`, so a non-standard gateway can generate correctly
while `family_status` reports a probe failure. That is a limitation of the
current `/models` derivation, not necessarily a broken member.

## GPU out of memory or an overloaded farm

Symptoms: requests queue for a long time, the endpoint returns 500/CUDA OOM,
or the machine becomes unresponsive during a swarm.

- Set `max_clones: 1` for a single-GPU endpoint. It is the default, but an
  existing entry may have been raised.
- Lower `max_concurrency` on `ask_family_swarm`. It can only tighten the
  member's limit, never exceed it.
- Reduce `max_tokens` and `context_window` to leave room for the KV cache.
- Send true fan-out work to a cloud member with a higher `max_clones`; keep the
  local member for one-at-a-time vision or cheap checks.
- Remember that `max_clones` is per member, not global. Two local members can
  each use the same GPU if they point at the same server; configure one member
  per physical model server where possible.
- A swarm's tasks still run through `ask()`, so they obey `max_clones` even if
  you pass a larger `max_concurrency` (which is rejected if it exceeds the task
  count).

## Images are rejected

| Error | Cause | Fix |
| --- | --- | --- |
| `member 'x' is not configured for vision` | No `vision` capability. | Add `vision` to the member and use a vision model. |
| `image ... is N MB, above the configured limit` | `security.max_image_mb` exceeded. | Resize/compress, or raise the limit deliberately. |
| `unsupported image format` | Not PNG/JPEG/GIF/WebP by magic bytes. | Convert the image; renaming an SVG to `.png` does not help. |
| `too many images (9); at most 8 ...` | More than eight images. | Split the call or combine images. |
| `image not found: ...` | The path is relative to the server's working directory, not your project. | Use an absolute path. |
| `image path is not a regular file` | Directory or special file. | Pass a file. |
| `failed to download image ...: HTTP 404` | Remote URL is wrong or requires auth. | Check the URL; the server fetches it without your browser cookies. |
| `security.deny_images_to_remote is enabled` | Images are blocked for remote trust tiers. | Use a local vision member or change the policy consciously. |

Also check `vision` in `list_family` output. A model can accept text but not
image parts; the capability is a declaration, not a discovery.

## Redaction surprises

If text changed before it reached a model, the tool result carries a warning
and a `redactions` array:

```json
"warnings": ["redacted 2 secret-like span(s) before sending to untrusted_cloud: openai_key, assignment"],
"redactions": [{"kind": "openai_key", "count": 1}, {"kind": "assignment", "count": 1}]
```

- Placeholders look like `«REDACTED:api_key»`. The model sees them and may
  mention that content was removed.
- Redaction applies only to trust levels in `security.redact_trust`, and only
  when `security.redact_secrets` is true. `private_network` is not in the
  default list.
- False positives are possible: a code sample that looks like `password =
  "long-value"` can be scrubbed. If this is disruptive, narrow the risk by
  sending that code to a local member instead of disabling the guard globally.
- Redaction is not a guarantee. It misses novel formats, short keys, secrets in
  images, and values split across lines. Do not paste secrets intentionally.
- `security.blocked_patterns` is a hard block, not a redaction. A match refuses
  the whole call with a policy error, for every trust level.
- A literal API key in the config is not redacted from the config file; only
  prompts and system prompts pass through the guard.

## Cost tier looks wrong

`list_family` reports `cost` plus `cost_source`. If a local member shows `paid`
or a free cloud tier shows `paid`, the field was omitted and Sub-Family inferred
the conservative default (`safe_local` → `electricity`, everything else →
`paid`). Set it explicitly:

```yaml
members:
  lilbro:
    # ...
    cost: "electricity"   # local hardware, power only
  free_cloud:
    # ...
    trust: "untrusted_cloud"
    cost: "free"          # free API tier or already-paid subscription
```

`sub-family members edit lilbro --cost electricity` and `--cost auto` work from
the CLI; the TUI exposes the same selector. Cost is routing metadata only — it
does not change redaction, images, concurrency, or billing.

## Sessions grow, or sessions are disabled

- `sessions.max_messages` and `sessions.max_chars` bound one session's replay
  window, not the number of sessions. Many short sessions still grow the
  database.
- `sessions.ttl_days` prunes by last update, at startup when
  `cleanup_on_start` is true. `0` disables pruning.
- Use `sub-family sessions list`, `sessions show`, `sessions reset`, and
  `sessions prune --days N` to inspect and clean.
- If `sessions.enabled: false`, any tool call with a `session_id` fails with
  "sessions are disabled in the config"; CLI session commands fail too. This is
  intentional, not a bug.
- A WAL file can be large after heavy use. SQLite checkpoints automatically;
  `PRAGMA wal_checkpoint(TRUNCATE)` or a clean shutdown can shrink it. Back up
  the database with the SQLite backup command rather than copying a live file.
- Session content is plaintext. Do not share the database or its backups.

## `sub-family ui` needs a TTY

```text
Invalid value: `ui` needs an interactive terminal; use `sub-family members add`.
```

The Textual editor needs a real terminal on both stdin and stdout. It does not
work through a pipe, in CI, in a non-interactive Claude Code shell, or in some
IDE task runners. Use the CLI instead:

```bash
sub-family members add farm_two -u http://192.168.1.51:11434 -m qwen2.5-coder:7b
sub-family members edit farm_two --max-clones 1
sub-family config edit
```

## Windows paths and shells

- Default config: `%APPDATA%\sub-family\family.yaml`. Default data:
  `%LOCALAPPDATA%\sub-family\sessions.db`. `sub-family paths` prints the real
  paths.
- In YAML double-quoted strings, backslashes are escapes. Prefer forward
  slashes (`C:/Users/me/shot.png`) or single quotes
  (`'C:\Users\me\shot.png'`).
- In PowerShell, quote paths with spaces and use `$env:SUB_FAMILY_CONFIG` to
  set the variable for the current session.
- Claude Code starts the command without a shell; use absolute paths in
  `.mcp.json`, and use `uvx.exe`/the full path if the GUI PATH cannot find
  `uvx`.
- If `uv` is installed per-user, its shim directory (often
  `%USERPROFILE%\.local\bin`) must be on PATH for the process that starts
  Claude Code.

## `uvx: command not found`

`uvx` ships with [`uv`](https://docs.astral.sh/uv/). Install uv, then reopen
the terminal so PATH is updated. Verify:

```bash
uv --version
uvx --version
which uvx        # macOS/Linux
where uvx        # Windows
```

Alternatives if `uvx` cannot be used:

```bash
uv tool install sub-family-mcp
claude mcp add --scope user sub-family -- sub-family-mcp
# or
pipx install sub-family-mcp
claude mcp add --scope user sub-family -- sub-family-mcp
```

For GUI-launched apps, use the absolute path to `uvx` or `sub-family-mcp` in
the MCP configuration. The first `uvx` invocation downloads the package and
dependencies; a slow first start can look like a timeout.

## The server starts but Claude sees no tools

- Run `claude mcp list` and `claude mcp get sub-family`.
- Restart Claude Code after adding or editing a server. Project-scoped
  `.mcp.json` servers also require approval in the project.
- Check the command's stderr; configuration errors are written there and exit
  with status 2, before MCP initializes.
- Confirm you registered the stdio entry point (`uvx sub-family-mcp`), not the
  admin CLI (`sub-family`), which would print help instead of speaking MCP.
- Ask explicitly: "Use `list_family` and show the result." A model may not call
  tools unprompted in a trivial conversation.

## Other errors

| Message | Meaning |
| --- | --- |
| `member 'x' is disabled in the config` | `enabled: false`; enable it or pick another member. |
| `prompt is N characters, above security.max_prompt_chars` | Shorten the prompt or raise the limit deliberately. |
| `estimated prompt size ... above 90% of its context_window` | Lower history, shorten the prompt, or use a larger-context member. |
| `session database ... has schema version N` | The DB was created by an incompatible future version. Restore a backup or start a new DB. |
| `cannot open session database` / permission denied | Fix ownership/mode of the data directory; it should be writable by your user only. |
| `security.blocked_patterns[i] is not a valid regular expression` | Fix the regex; invalid patterns stop startup. |
| `SSL: CERTIFICATE_VERIFY_FAILED` | Fix the endpoint certificate or trust chain. Disabling `verify_tls` is a last resort on a trusted network. |
| `HTTP 429` | Rate limited. Retries are automatic; lower `max_clones` or wait. |
| `returned an empty message` | The model produced neither content nor reasoning; check the model id and whether it is a chat model. |
| `finish_reason=length` warning | The answer hit `max_tokens`; raise it or ask for a shorter answer. |
| `model returned no final answer, only reasoning` | Enable `security.expose_reasoning` to see the trace, or use a model that emits a final answer. |

# Security

Sub-Family sits on the boundary between your local machine and model endpoints
that may be on the other side of the internet. Its security goal is narrow and
explicit: make it harder to send credentials to a third-party model by
accident, and make the privacy cost of each delegation visible. It is not a
sandbox, a data-loss-prevention product, or a multi-tenant isolation layer.

## Threat model

**Assets**

- Prompt text, source code, logs, and images produced by the primary agent.
- API keys for cloud members, and any keys found in shell or `.env`.
- The SQLite session database, which stores prompts and answers.
- The integrity of the local machine and the models it runs.

**Primary risks**

1. **Accidental exfiltration.** A primary model pastes an API key, a `.env`
   dump, or private business logic into a prompt for a cloud member. The cloud
   provider may log, retain, or train on it.
2. **Network exposure.** Local endpoints are often plain HTTP on a LAN. Anyone
   who can observe that network can read prompts, and an unauthenticated
   Ollama/LM Studio port lets anyone use the GPU.
3. **Local state exposure.** Session history and configuration can contain
   secrets, are stored in plaintext, and may end up in backups or synced
   folders.
4. **Untrusted model output.** A member can return incorrect code, malicious
   commands, or prompt-injection text. Its answer is data, not instruction.
5. **Over-broad local access.** Image arguments can point at any file the server
   process can read, and image URLs can target any host the process can reach.
6. **Endpoint impersonation.** With `verify_tls: false`, or over plain HTTP, a
   network attacker can read or alter prompts.

**Trust boundaries**

```text
Claude Code / MCP client
        │  stdio (same user, same machine)
        ▼
Sub-Family process ── config + session DB (local files, OS permissions)
        │
        ├── HTTP to safe_local / private_network endpoints (LAN, often no TLS)
        └── HTTPS to untrusted_cloud endpoints (third party)
```

The MCP transport is stdio: the server does not listen on a TCP port. Its only
network traffic is outbound to configured `base_url` endpoints and to image
URLs supplied in a call. "Local" means an address on your network, not
necessarily the same machine, and not necessarily encrypted.

## Trust tiers

| Tier | Intended use | Redaction default | Network assumption |
| --- | --- | --- | --- |
| `safe_local` | A model on hardware you control. | None | LAN; may be plain HTTP. |
| `private_network` | A remote endpoint you operate, such as a VPS. | None by default | Internet; you trust the operator. |
| `untrusted_cloud` | A third-party API. | Enabled | Internet; provider can log or retain prompts. |

Trust is a label, not a network probe. The server does not verify that a
`safe_local` URL is actually local. If you point a cloud URL at `safe_local`,
you lose the default redaction and the cloud warnings. Set the tier honestly,
and extend redaction when you want it:

```yaml
security:
  redact_trust: ["private_network", "untrusted_cloud"]
```

## Secret redaction

Redaction runs on the prompt and on the system prompt (configured and per-call)
before the request is built. It applies only to members whose trust is listed in
`security.redact_trust` and only when `security.redact_secrets` is true. The
response is not scrubbed: model output is returned as-is.

| Category | What it matches | Example shape |
| --- | --- | --- |
| `private_key` | PEM private-key blocks | `-----BEGIN ... PRIVATE KEY-----` |
| `openai_key` | OpenAI-style keys | `sk-` followed by 16+ key characters |
| `github_token` | GitHub tokens | `ghp_...`, `github_pat_...` |
| `aws_key` | AWS access key ids | `AKIA...`, `ASIA...` |
| `google_key` | Google API keys | `AIza...` |
| `slack_token` | Slack tokens | `xoxb-...`, `xoxp-...` |
| `jwt` | JSON Web Tokens | three base64url segments starting `eyJ` |
| `bearer_token` | Bearer credentials | `Bearer <20+ token characters>` |
| `url_credentials` | Passwords in URLs | `postgres://user:password@host/db` |
| `dotenv_assignment` | `.env`-style assignments | `SOMETHING_API_KEY=value` on its own line |
| `assignment` | Named secret assignments | `api_key = "..."`, `client_secret: ...` |

Matches are replaced with a placeholder such as `«REDACTED:api_key»`. When
anything is replaced, the tool result includes a `redactions` array with counts
by category and a warning naming the destination trust level. The replacement
is visible to the model, so it can tell that something was removed.

Redaction is a seatbelt. It catches well-known shapes and common assignment
patterns; it does not understand your codebase. See
[What the guard does not catch](#what-the-guard-does-not-catch).

### Hard blocks

`security.blocked_patterns` is a list of regular expressions checked with
`re.search` against the prompt and system prompt **for every trust level**,
including `safe_local`. A match aborts the call with a policy error before any
network request. This is the right tool for project names, customer identifiers,
or internal URL schemes that must never leave the machine.

```yaml
security:
  blocked_patterns:
    - "PROJECT-(?:ATLAS|BOREALIS)-\\d+"
    - "internal\\.corp\\.example"
```

Patterns are validated when the config is loaded; an invalid regex stops
startup. Matching is not time-bounded, so only add patterns you control.

## Images

Image admission is separate from text redaction:

- The target member must declare the `vision` capability.
- At most 8 images per call.
- PNG, JPEG, GIF, and WebP are detected by magic bytes. Other formats are
  rejected, even if the file extension says otherwise.
- Each image is limited by `security.max_image_mb` (default 20 MB), both from
  `Content-Length` and while streaming.
- `security.deny_images_to_remote: true` refuses images to any member whose
  trust is `private_network` or `untrusted_cloud`.
- Sending to `untrusted_cloud` produces a warning; sending to
  `private_network` produces a milder warning; local sends are silent.
- Image bytes are sent as data URLs and are **not** written to session
  history. History stores only a text line such as
  `[attached image: /path/shot.png]`.

Pixels are never redacted. A screenshot can contain a password manager, a
terminal with exported keys, customer data, or a private key. Prefer a local
vision member for such screenshots, and crop or mask them before delegating.

Two local-access caveats follow from accepting paths and URLs:

- **Arbitrary file read.** The server reads any image path the calling agent
  supplies, subject to the process user's permissions. There is no configured
  allowlist. Treat the MCP client as trusted, and run Sub-Family as a normal
  user rather than as root.
- **Server-side request forgery.** `http(s)` image URLs are fetched by the
  server, with redirects followed, up to 20 seconds. The server does not restrict
  destinations, so an untrusted client could ask it to fetch `localhost` or a
  cloud metadata address. Do not expose this MCP server to clients you would not
  trust with outbound HTTP from your machine.

Remote image downloads use the member's HTTP client, including its
`verify_tls` setting.

## What the guard does not catch

- Secrets that do not match a pattern: internal service tokens, short keys,
  high-entropy strings with no known prefix, hashes, connection strings with
  unusual syntax.
- Secrets split across lines, concatenated at runtime, or referenced
  indirectly (`$MY_SECRET`, vault paths, names of environment variables).
- Secrets inside images, PDFs, binaries, or file paths.
- Secrets in arbitrary JSON or prose that do not look like an assignment.
- A legitimate code sample that happens to match a rule: false positives are
  possible and are reported rather than silently ignored.
- Harmful content, prompt injection, or malicious instructions in a member's
  response. A member answer is untrusted data; verify commands and code before
  running or applying them.
- Data already sent. Redaction happens locally before the request, but once a
  request reaches a provider, that provider's retention policy applies.
- Anything the user explicitly pastes after seeing that a value was not
  redacted. The guard is not a substitute for judgement.
- Authentication of MCP clients, authorization between users, and tenant
  isolation. stdio inherits the trust of the process that launched it.
- Encryption at rest. The config and session database are plaintext files
  protected by filesystem permissions.
- Global rate limiting. `max_clones` bounds concurrency, not total usage or
  spend.

If `security.redact_secrets` is false while cloud members exist, `sub-family
doctor` reports a failed check. That is a warning about configuration, not a
runtime block.

## API key storage

Recommended, in order:

1. **Environment variable** referenced by `api_key_env`, exported only in the
   environment that launches Claude Code or the CLI.
2. **`.env` next to `family.yaml`**, with mode `0600`, containing only the keys
   you need. Sub-Family loads it without overriding real environment variables.
   The file is gitignored in this repository; keep it out of backups and synced
   folders you do not control.
3. **Literal `api_key`** in `family.yaml`. It is supported and the file is saved
   with mode `0600`, but the key is plaintext, survives in `.bak` copies, and is
   easy to leak in a screenshot or issue report.

Do not put keys in `base_url` (credentials there are rejected), in member names,
or in prompts. `sub-family config show` and `members show` mask literal keys,
but that only protects terminal output. Rotate a key immediately if it was sent
to an untrusted endpoint; redaction may have missed it.

`sub-family doctor` checks that environment variables referenced by enabled
members are present. It cannot check whether the key is valid; run
`sub-family status`.

## Local network exposure

- Ollama binds to `127.0.0.1` by default. Setting `OLLAMA_HOST=0.0.0.0:11434`
  exposes it to the LAN with no authentication unless you add a proxy.
- LM Studio, vLLM, and llama.cpp server have their own bind and auth options.
  Treat an unauthenticated LAN port as a shared GPU, not as a private service.
- Plain HTTP carries prompts and completions in clear text. Use a trusted
  network, a VPN, or a TLS-terminating reverse proxy. Keep `verify_tls: true`
  unless you are terminating with a certificate you control; setting it false
  disables an important check.
- Firewall the model ports to the machines that need them. Do not forward them
  to the public internet.
- Sub-Family itself has no listening socket, but the cloud endpoints it calls
  see every prompt and image you send them.

## Session database

The SQLite database stores, per `(member, session_id)`:

- every user message and assistant answer, JSON-encoded;
- timestamps, message counts, and a short last-answer preview;
- prompt and completion token counts when the provider reports them;
- text references for attached images, not the image bytes.

It does **not** store image data, API keys, or raw HTTP requests. It can still
contain secrets that were intentionally sent to a `safe_local` member, and
answers copied from cloud models.

On POSIX, `SessionStore` creates the database directory with mode `0700` and
sets `sessions.db`, `-wal`, and `-shm` to `0600`. The config file is written
with `0600`; the config directory follows your umask. Protect backups and avoid
placing `db_path` inside a repository unless you also ignore it. The database is
not encrypted.

Delete history with `sub-family sessions reset <member> <session-id>` (or `*`),
or let TTL pruning remove sessions older than `sessions.ttl_days`. Deleting a
session does not delete provider-side logs, and it does not remove the prompt
from any backup that already contains it.

## Reporting a vulnerability

Do not open a public issue for a security problem. Follow
[SECURITY.md](../SECURITY.md): report privately through GitHub Security
Advisories, or email the maintainer if advisories are unavailable. Include the
version, a minimal reproduction, the affected trust level, and whether secrets
or local files were exposed. Never include real API keys in a report; use
obviously fake values and masked config output.

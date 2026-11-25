# Security Policy

Sub-Family is a local MCP server that reads prompts and image files, holds API
keys, stores conversation history, and forwards selected content to model
endpoints. This document covers supported versions, private reporting, the
security model, and what the project does **not** protect against.

## Supported versions

| Version | Supported | Notes |
| --- | --- | --- |
| `0.1.x` | Yes | Security fixes are released from the latest `0.1.x`. |
| `< 0.1` | No | Pre-release snapshots are unsupported. |

The project is pre-1.0. Until a 1.0 release, fixes land on the default branch
and are published as a new `0.1.x` version. If you run a fork or a pinned
commit, reproduce the issue against the latest release before reporting it.

## Reporting a vulnerability

**Do not open a public issue for a security vulnerability.**

Use GitHub Security Advisories:

<https://github.com/MistaKilla/Sub-Family-MCP/security/advisories/new>

Include:

- the version (`sub-family --version` or `pip show sub-family-mcp`);
- operating system and Python version;
- the relevant `family.yaml` section with keys, hostnames, and usernames
  redacted;
- the member `trust` level and provider type involved;
- reproduction steps or a minimal proof of concept;
- the impact you believe is possible, and whether data left the machine.

Never include real API keys, private prompts, customer data, or screenshots of
secrets. Use a throwaway key and synthetic content for demonstrations.

If GitHub Advisories are unavailable to you, open a minimal public issue asking
for a private contact channel. Do not put vulnerability details in that issue.

### What to expect

| Stage | Target |
| --- | --- |
| Acknowledgement | 3 business days |
| Initial assessment and severity | 7 business days |
| Fix or mitigation for critical/high | As soon as practical; target 30 days |
| Fix for moderate/low | Next regular release; target 90 days |

This is a best-effort target, not a contractual SLA. The project follows
coordinated disclosure: we agree on a publication date, credit the reporter
unless asked otherwise, and publish an advisory once a fixed release exists.
There is no bug bounty.

## Security model

### Trust boundaries

```text
MCP client (stdio, same user)
        │
        ▼
Sub-Family process ── family.yaml + .env + sessions.db
        │
        ├── HTTP(S) to member endpoints
        └── HTTP(S) to image URLs supplied in a call
```

The server does not listen on a network port. It inherits the permissions of
the user who launched it, and any client that can start that process can read
the config, use the keys, and query the session database. The stdio transport
has no authentication or authorization layer.

### Prompt data leaves the machine by design

Every member has one of three trust levels:

- `safe_local` — an endpoint on hardware you control. Prompts normally stay on
  your network, but "local" means a URL you chose, not a verified network
  location.
- `private_network` — a remote endpoint you operate (VPS, another farm). Prompts
  leave the machine.
- `untrusted_cloud` — a third-party API. Prompts leave the machine and are
  subject to that provider's logging, retention, and training policies.

Sub-Family cannot control what a provider does after a request arrives. Use
local members for private code and sensitive screenshots.

### Secret redaction is a seatbelt

For members listed in `security.redact_trust` (default:
`["untrusted_cloud"]`), the guard replaces known credential shapes before the
request is sent:

- PEM private-key blocks;
- OpenAI-style `sk-...` keys;
- GitHub tokens (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`,
  `github_pat_...`);
- AWS access-key IDs (`AKIA...`, `ASIA...`);
- Google API keys (`AIza...`);
- Slack tokens (`xox[baprs]-...`);
- JWTs and long bearer tokens;
- credentials embedded in URLs;
- `.env`-style assignments whose name contains `API_KEY`, `SECRET`, `TOKEN`,
  `PASSWORD`, `PASSWD`, or `PRIVATE_KEY`;
- named assignments such as `api_key = "..."` or `client_secret: ...`.

Replacements appear as `«REDACTED:kind»`, and tool output reports counts and a
warning. Hard blocks configured in `security.blocked_patterns` are checked for
every trust level, including `safe_local`, and abort the call on a match.

Redaction is pattern-based. It does not detect random high-entropy strings,
novel token formats, renamed variables, secrets split across messages, values
encoded in base64, or secrets inside images. Known limitations are **not**
treated as vulnerabilities by themselves; reports that bypass a documented
pattern with a realistic credential are in scope.

### Images and local access

Image arguments may be local paths, `http(s)` URLs, or base64 data URIs. The
server reads local paths with its own permissions and downloads URLs itself,
following redirects. Therefore:

- a malicious or prompt-injected MCP client can ask the server to read any
  image file the process can access;
- it can ask the server to fetch an internal URL reachable from the host
  (SSRF);
- image bytes can be sent to a cloud member unless
  `security.deny_images_to_remote` is enabled.

Run the server as a normal user, connect only clients you trust, and enable
`deny_images_to_remote` when cloud vision is not required.

### Local state

- `family.yaml`, `.env`, and the session database are **not encrypted at
  rest**. Config files are written with mode `0600`; the session database
  directory and files are set to `0700`/`0600` on POSIX.
- Session history contains prompts and answers (not image bytes), plus
  timestamps, previews, and token counts. It can contain secrets that were
  intentionally sent to a local member. Use `sessions.ttl_days`, `sessions
  reset`, and careful backups.
- A literal `api_key` in `family.yaml` is supported but stored in plaintext and
  copied into `.bak` files. Prefer `api_key_env` and a protected `.env` or
  secret manager.
- Logs go to stderr and are designed not to include prompts or keys. A
  third-party library at `DEBUG` verbosity may still log request details; do
  not send DEBUG logs to a shared pipeline.
- Anyone who can read the process environment can read API keys passed that
  way.

### Model output is untrusted

Member answers are returned as data, not executed. They may be wrong, may
contain prompt-injection attempts aimed at the calling agent, or may suggest
destructive commands. Tool output marks the member and trust level so the
primary agent can weigh the answer. Verify code and commands before applying
them.

## In scope

- A redaction bypass using a credential shape the documented rules claim to
  catch.
- A hard-block bypass, or a way to send a blocked prompt.
- A path that sends a prompt or image to a member despite
  `security.deny_images_to_remote`, a missing `vision` capability, or the
  configured size limits.
- Cross-session data leakage, session id confusion, or a way for one
  `(member, session_id)` to read another's history.
- MCP protocol handling that crashes, corrupts output, or leaks local files
  beyond the documented image mechanism.
- API keys leaked through logs, error messages, `list_family`, `config show`,
  or tool output.
- Configuration handling that lets a crafted `family.yaml` execute code or
  escape the config directory.
- A dependency vulnerability with a realistic exploit path through
  Sub-Family.

## Out of scope

- Incorrect, biased, or harmful model answers.
- Prompt injection that requires the calling agent to follow malicious
  instructions; hardening suggestions are welcome, but the agent is expected to
  verify.
- Secrets intentionally pasted into a prompt, committed to the repository, or
  placed in a world-readable file.
- Compromise of a model provider, local endpoint, host OS, or MCP client.
- A provider's retention, training, or logging policies.
- DoS or resource exhaustion caused by the local user, a trusted MCP client, or
  prompts within the configured caps.
- The documented SSRF and arbitrary-image-read behaviours, unless they bypass a
  configured policy.
- Vulnerabilities in third-party dependencies with no exploitable path in
  Sub-Family (report upstream, but tell us so we can bump the pin).

## Hardening checklist

- Use `safe_local` members for private repositories and screenshots.
- Set `security.deny_images_to_remote: true` unless cloud vision is required.
- Keep keys in environment variables or `.env`, never in commits; rotate any
  key that may have reached an untrusted endpoint.
- Enable `sessions.ttl_days` and prune old sessions.
- Put only the model ports you need on the LAN; firewall them and prefer a VPN
  or TLS reverse proxy over plain HTTP.
- Keep `verify_tls: true`; do not disable certificate checks globally.
- Run `sub-family doctor` after config changes and review the trust level of
  every new member.
- Publish through PyPI Trusted Publishing and pin GitHub Actions as the release
  workflow does.

## Related documentation

- [Security model](docs/security.md) — threat model, redaction categories,
  session contents, local network exposure.
- [Configuration](docs/configuration.md) — trust, redaction, image, and session
  settings.
- [Troubleshooting](docs/troubleshooting.md) — diagnosing policy errors and
  rejected images.

# Contributing to Sub-Family

Sub-Family is a small, deliberately layered codebase. Contributions that keep
the layers clean, the schema strict, and the tool descriptions honest are
welcome.

## Development setup

You need Python 3.11-3.13, [uv](https://docs.astral.sh/uv/), and Git. A local
OpenAI-compatible endpoint is useful for manual testing but is not required for
the test suite.

```bash
git clone https://github.com/MistaKilla/Sub-Family-MCP.git
cd Sub-Family-MCP
uv sync --extra dev
```

`uv sync` creates `.venv` and installs the package in editable mode, including
the `sub-family` and `sub-family-mcp` entry points. Verify:

```bash
uv run sub-family --version
uv run sub-family --help
```

To experiment with configuration without touching your real
`~/.config/sub-family`:

```bash
export SUB_FAMILY_CONFIG_DIR="$(mktemp -d)"
export SUB_FAMILY_DATA_DIR="$(mktemp -d)"
uv run sub-family init
uv run sub-family config validate
```

## Checks

Run these before opening a pull request:

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

Optionally install the repository's pre-commit hooks so the same ruff and file
hygiene checks run on every commit:

```bash
uv run pre-commit install
```

Useful variants:

```bash
uv run pytest -q --cov=sub_family --cov-report=term-missing
uv run pytest tests/test_service.py -q
uv run pytest -q -k swarm
uv run ruff format .
```

The test configuration treats warnings as errors, uses `pytest-asyncio` in
`auto` mode, and lives in `pyproject.toml`. Tests must not call a live model
endpoint; use the fixtures in `tests/conftest.py`, `httpx.MockTransport`, or
`respx`.

Strict mypy excludes `src/sub_family/tui` because Textual's generic types do
not fit the rest of the strict configuration. Keep that exclusion narrow; do
not add new exclusions without a reason in the PR.

## What to test

- **Config changes:** valid YAML, rejected unknown fields, defaults, boundary
  values, and the CLI/TUI path that edits the field.
- **Provider changes:** URL construction, payload shape, retry classification,
  auth headers, timeout handling, and malformed responses. Use `respx`.
- **Service changes:** sessions, trimming, redaction, swarm concurrency, partial
  failures, and policy errors. The `FakeProvider` fixture records call counts
  and maximum concurrency.
- **Server changes:** tool registration, JSON shape, and `ToolError` conversion
  through `mcp.Client`.
- **CLI changes:** exit codes and actionable output through
  `typer.testing.CliRunner`.
- **Security changes:** both the positive case (secret removed) and the negative
  case (clean text untouched, local member not redacted by default).

A bug fix should come with a regression test that fails before the fix. A
behaviour change should update the docs and, when user-visible, `CHANGELOG.md`.

## Code conventions

- Python 3.11+, type hints for public functions, `from __future__ import
  annotations`.
- Ruff line length 100; formatting is not negotiable (`ruff format`).
- Pydantic models are frozen and use `extra="forbid"`; add bounds with `Field`,
  not ad-hoc checks in callers.
- All expected failures derive from `SubFamilyError` and carry a message an LLM
  agent can act on.
- Never log prompts, API keys, or full request bodies. Logs go to stderr; stdout
  belongs to the MCP stdio transport.
- Keep `server.py` and `cli.py` thin. Business logic belongs in
  `FamilyService`; configuration parsing belongs in `config/`; HTTP details
  belong in `providers/`.
- Tool docstrings are user-facing model instructions. If you change one, review
  the wording as carefully as public documentation.
- No new runtime dependency without a clear reason; the project intentionally
  uses `httpx` directly instead of provider SDKs.

## Commits and pull requests

Use an imperative subject, optionally scoped:

```text
config: reject duplicate member names
providers: classify HTTP 409 as a bad request
server: include redaction counts in ask_family output
docs: document OpenRouter routing headers
tests: cover swarm with max_concurrency
```

- Keep one logical change per commit. Separate refactors from behaviour changes.
- Explain *why* in the body when the diff does not make it obvious. Link the
  issue or discussion.
- Do not include secrets, personal config, session databases, `.env` files, or
  cache directories.
- Add or update tests with the change; update `README.md`, `README.ru.md`, and
  the relevant `docs/` page when commands, fields, or tools change.
- For TUI changes, include a screenshot or a short description of the keyboard
  path you tested.
- Open the pull request against the repository's default branch. Describe the
  behaviour before and after, the tests run, and any configuration migration.
- Maintainers may ask for a narrower PR or may squash commits; force-pushing
  your own branch during review is fine.

## Pull request checklist

- [ ] The change is focused, explained, and linked to an issue or discussion.
- [ ] `uv run ruff check .` and `uv run ruff format --check .` pass.
- [ ] `uv run mypy src` passes without new exclusions.
- [ ] `uv run pytest -q` passes, and new behaviour has regression tests.
- [ ] Security-sensitive changes include negative tests and do not log secrets.
- [ ] `CHANGELOG.md` is updated for user-visible changes (or the PR says why not).
- [ ] `README.md`, `README.ru.md`, and the relevant `docs/` page are updated.
- [ ] Tool docstrings and config examples match the new interface.
- [ ] CI is green on the full Python matrix.

## Architecture pointers

The dependency direction is:

```text
config -> providers -> sessions/security -> service -> server / cli / tui
```

| Area | Start here |
| --- | --- |
| Config schema and validation | `src/sub_family/config/models.py` |
| Loading, saving, `.env`, paths | `src/sub_family/config/loader.py`, `paths.py` |
| HTTP protocol and retries | `src/sub_family/providers/openai_compat.py` |
| Image loading | `src/sub_family/providers/media.py` |
| SQLite history and trimming | `src/sub_family/sessions/store.py` |
| Secret redaction and image policy | `src/sub_family/security/guard.py` |
| `ask`, `swarm`, `status`, sessions | `src/sub_family/service.py` |
| MCP tools and instructions | `src/sub_family/server.py` |
| Admin commands | `src/sub_family/cli.py` |
| Textual editor | `src/sub_family/tui/` |

See [Architecture](docs/architecture.md) for request flows and the concurrency
model.

## Add a member (no code)

For an OpenAI-compatible endpoint, edit the registry or use the CLI:

```bash
uv run sub-family members add farm_two \
  --base-url http://192.168.1.51:11434 \
  --model qwen2.5-coder:7b \
  --trust local \
  --capability code \
  --capability fast
```

Then run `uv run sub-family status --member farm_two`. New optional config
fields belong in `config/models.py`, `config/template.py`, the tables in
`docs/configuration.md`, and the tests.

## Add a provider

Only OpenAI-compatible endpoints work through configuration. A native protocol
needs code:

1. Implement the `ChatProvider` protocol from
   `src/sub_family/providers/base.py`: `complete()` returning `ChatResult` and
   `health()` returning `ProviderHealth`.
2. Accept the member's `MemberConfig`, API key, and an `httpx.AsyncClient` in
   the constructor. Reuse the error classification patterns from
   `openai_compat.py` so failures stay actionable.
3. Wire it in. Embedders can pass `provider_factory=` to `FamilyService` today.
   For the bundled server, update `_default_provider()` in `service.py`; there is
   no `provider:` config field yet, so selecting by config also requires adding
   one to `MemberConfig`.
4. Add tests with `respx` or `MockTransport` for success, auth failure, missing
   model, timeout, and malformed JSON.
5. Document the backend in `docs/configuration.md` and the supported-backends
   table in the READMEs.

Do not put provider-specific branching into `FamilyService`; keep it behind the
protocol.

## Add a capability

Capabilities are free-form tags, but only `vision` currently changes runtime
behaviour. To add a meaningful one:

1. Add the name to `KNOWN_CAPABILITIES` in `config/models.py` so the TUI can
   offer it (the validator already accepts any lowercase token).
2. Enforce it at the right layer: admission checks in `FamilyService`, policy in
   `Guard`, or a new pre-flight step. Do not enforce it only in the CLI.
3. Return a `ToolInputError` or `PolicyError` with a message that tells the
   caller which capability is missing.
4. Update tool docstrings, both READMEs, `docs/configuration.md`, and tests.

Keep unknown/custom capabilities working; users may use tags for their own
selection conventions.

## Add an MCP tool

1. Add a method to `FamilyService` if the tool needs orchestration. Test it
   without MCP.
2. Add a `@server.tool()` wrapper in `server.py`, return JSON via `_json()`, and
   convert `SubFamilyError` with `_raise_tool_error()`.
3. Write the docstring as model-facing guidance: what it does, when to use it,
   arguments, return shape, and any cost/privacy implications.
4. Add a case to `tests/test_server.py`, update the tools table in both READMEs,
   and mention it in `SERVER_INSTRUCTIONS` if the model must call it in a
   particular order.
5. Keep the tool count and the documentation in sync; three places currently
   describe the surface (docstring, README table, `SERVER_INSTRUCTIONS`).

## Releases

Maintainers follow [Publishing](docs/publishing.md). Contributors do not need
to bump versions; note user-visible changes in the PR so the changelog can be
updated at release time.

## Reporting bugs and security issues

Open a GitHub issue with the version, OS, Python version, the command or tool
call, and the exact error. Use `sub-family config show` (keys are masked) and
`sub-family doctor` output when relevant, but remove local paths and any
remaining secrets.

For security problems, do not open a public issue. Follow
[SECURITY.md](SECURITY.md).

## License

By contributing, you agree that your contribution is licensed under the
project's MIT license. See [LICENSE](LICENSE).

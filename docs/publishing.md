# Publishing

This page is the maintainer checklist for releasing `sub-family-mcp`. It
assumes the repository is `https://github.com/MistaKilla/Sub-Family-MCP`, the
package name is `sub-family-mcp`, and the version is sourced from
`src/sub_family/__init__.py` through Hatch.

## Versioning

- Stay on `0.x` until the MCP tool surface and configuration schema are stable;
  breaking config changes bump the minor version before 1.0.
- The Python version is single-sourced in one place:

  ```python
  # src/sub_family/__init__.py
  __version__ = "0.1.0"
  ```

- Hatch reads that value for the wheel and sdist. `sub-family --version`, the
  MCP server implementation version, and the PyPI artifact must all agree.
- Keep `CHANGELOG.md` in Keep a Changelog format, with an `Unreleased` section
  and a dated section per release.
- Annotated tag: `v0.1.0`.

## Release checklist

### Before tagging

- [ ] `git status` is clean; no local config, database, or `.env` is staged.
- [ ] `uv sync --extra dev` succeeds on a clean checkout.
- [ ] `.venv/bin/python -m pytest -q` passes.
- [ ] `uv run ruff check .` and `uv run ruff format --check .` pass.
- [ ] `uv run mypy src` passes (the Textual TUI is excluded in `pyproject.toml`).
- [ ] `__version__` is bumped and `CHANGELOG.md` has a matching dated section.
- [ ] `README.md` and `README.ru.md` describe the current commands and tools.
- [ ] `examples/family.example.yaml` still matches `EXAMPLE_YAML` byte for byte:

  ```bash
  .venv/bin/python - <<'PY'
  from pathlib import Path
  from sub_family.config.template import EXAMPLE_YAML
  assert Path("examples/family.example.yaml").read_text() == EXAMPLE_YAML
  print("template matches")
  PY
  ```

- [ ] `server.json` has the same `version` as `__version__`.
- [ ] No secrets in the tree: `git grep -nE 'sk-[A-Za-z0-9]{16,}'` returns only
  documented fake examples, if any.
- [ ] `LICENSE`, `SECURITY.md`, and `CONTRIBUTING.md` are present.

### Build and inspect

```bash
rm -rf dist/
uv build
uvx --from twine twine check dist/*
```

`uv build` creates both an sdist and a wheel. The sdist includes `docs/`,
`examples/`, `README.ru.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `SECURITY.md`,
and `server.json` (see `[tool.hatch.build.targets.sdist]` in
`pyproject.toml`). Inspect the file list:

```bash
tar -tzf dist/sub_family_mcp-0.1.0.tar.gz | sort
unzip -l dist/sub_family_mcp-0.1.0-py3-none-any.whl
```

Test the wheel in an isolated environment before uploading:

```bash
uv venv /tmp/sub-family-wheel-test
uv pip install --python /tmp/sub-family-wheel-test/bin/python dist/*.whl
/tmp/sub-family-wheel-test/bin/sub-family --version
SUB_FAMILY_CONFIG_DIR=/tmp/sub-family-smoke \
  /tmp/sub-family-wheel-test/bin/sub-family init
```

Then start the server once and confirm it speaks MCP rather than printing help:

```bash
SUB_FAMILY_CONFIG_DIR=/tmp/sub-family-smoke \
  /tmp/sub-family-wheel-test/bin/sub-family-mcp
# It waits on stdin; Ctrl-D to exit.
```

### Tag and release

Commit the version bump and changelog, then push an annotated tag:

```bash
git tag -a v0.1.0 -m "Sub-Family 0.1.0"
git push origin v0.1.0
```

The tag push starts `.github/workflows/release.yml`. The workflow:

1. Verifies that the tag is `v<__version__>` and that `server.json` has the same
   version. A mismatch fails before anything is uploaded.
2. Builds the sdist and wheel with `uv build` and runs `twine check`.
3. Publishes to PyPI through Trusted Publishing (the `pypi` environment).
4. Creates the GitHub release with generated notes and attaches the
   distributions.

There is no need to run `gh release create` first; the workflow owns the GitHub
release. If the tag is already correct but the release needs a retry, use the
workflow's `workflow_dispatch` input with the existing tag. Do not retry a
successful PyPI publish for the same version: PyPI rejects duplicate filenames.

## PyPI with Trusted Publishing

Do not store a long-lived PyPI API token in the repository. Configure a
Trusted Publisher once, then let GitHub Actions mint a short-lived credential.

1. On PyPI, create a **pending publisher** for the project:
   - owner: `MistaKilla`
   - repository: `Sub-Family-MCP`
   - workflow: `release.yml` (must match the filename exactly)
   - environment: `pypi`
2. `.github/workflows/release.yml` already implements the required flow. The
   security-critical parts are:

   - the job runs in `environment: pypi`;
   - it requests `id-token: write` (mandatory for OIDC);
   - it uses `pypa/gh-action-pypi-publish@release/v1` with `packages-dir:
     dist/`;
   - no `PYPI_TOKEN` secret is used or stored.

3. Push the tag and watch the workflow. It uploads every file in `dist/`; do not
   run it twice for the same version.
4. Verify:

   ```bash
   uvx sub-family-mcp --version
   uvx --from sub-family-mcp sub-family --version
   ```

   `uvx sub-family-mcp` runs the MCP server; `--version` is handled before
   serving, so the first command prints the version.

For a dry run, add a TestPyPI publisher and pass
`repository-url: https://test.pypi.org/legacy/` to the publish action. TestPyPI
is separate; a successful upload there does not publish to PyPI.

Manual fallback, only for an emergency:

```bash
uvx --from twine twine upload dist/*
```

This prompts for credentials and bypasses the OIDC guarantees; prefer fixing
the workflow.

## MCP Official Registry

The registry at `https://registry.modelcontextprotocol.io` stores metadata, not
artifacts. Publish PyPI first, then submit `server.json` with
[`mcp-publisher`](https://github.com/modelcontextprotocol/registry).

### PyPI ownership marker

The registry verifies PyPI ownership by looking for `mcp-name: <server-name>`
in the package README (the PyPI description). This repository's README contains
the marker as a hidden comment:

```markdown
<!-- mcp-name: io.github.MistaKilla/sub-family -->
```

Keep that string on its own line and make it match `server.json` exactly. The
token must end at a boundary (newline, whitespace, tag, or `-->`); do not glue a
period to it. If you change the registry name, update the comment and publish a
new PyPI version, because the README shipped with the release is what the
registry checks.

### `server.json`

The repository ships `server.json` at its root, and the sdist includes it. Two
names must not be confused:

- **Registry server name:** `io.github.MistaKilla/sub-family`
- **PyPI package identifier:** `sub-family-mcp`

The registry name is the namespace-qualified identity verified through GitHub;
the PyPI identifier is what `uvx` installs. The hidden `mcp-name` README marker
uses the registry name. Keep the `version` field equal to `__version__`; the
release workflow rejects a mismatch.

A minimal shape (the committed file additionally documents environment
variables):

```json
{
  "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
  "name": "io.github.MistaKilla/sub-family",
  "title": "Sub-Family",
  "description": "Delegate work to local and cloud LLMs over MCP with sessions, swarms and privacy tiers.",
  "repository": {
    "url": "https://github.com/MistaKilla/Sub-Family-MCP",
    "source": "github"
  },
  "version": "0.1.0",
  "packages": [
    {
      "registryType": "pypi",
      "registryBaseUrl": "https://pypi.org",
      "identifier": "sub-family-mcp",
      "version": "0.1.0",
      "runtimeHint": "uvx",
      "transport": {
        "type": "stdio"
      }
    }
  ]
}
```

Notes:

- `name` must use the GitHub namespace that matches the account performing
  `mcp-publisher login github`: `io.github.MistaKilla/sub-family`.
- `version` must be the exact published version, not a range or `latest`.
- `description` is limited to 100 characters by the registry schema.
- The package version must already exist on PyPI; the registry validates it.
- Keep `server.json`, `pyproject.toml`, `__version__`, and the tag in sync.

### Publish

Install the publisher (Homebrew, release archive, or build from the registry
repository), then:

```bash
mcp-publisher validate
mcp-publisher login github
mcp-publisher publish
```

`login github` uses the device flow and verifies namespace ownership. Confirm
the result:

```bash
curl "https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.MistaKilla/sub-family"
```

For later releases, update `version` in `server.json`, build and publish PyPI,
then run `mcp-publisher publish` again. The registry keeps per-version entries.

## Directory listings

These are independent directories with their own submission rules. Keep the
description and install command consistent with the README.

- **Glama** — <https://glama.ai/mcp/servers>. Glama indexes GitHub
  repositories and also accepts submissions; claim the server so the listing
  points at the canonical repository.
- **MCP.so** — <https://mcp.so>. Use the site's submit flow and provide the
  repository URL, a short description, and the `uvx sub-family-mcp` command.
- **Awesome MCP Servers** — open a pull request against
  [`punkpeye/awesome-mcp-servers`](https://github.com/punkpeye/awesome-mcp-servers)
  with one entry in the appropriate category:

  ```markdown
  - [Sub-Family](https://github.com/MistaKilla/Sub-Family-MCP) - Delegate to local and cloud LLMs over MCP with persistent sessions, parallel swarms, vision, and trust tiers.
  ```

  Follow the list's contribution guide; entries that do not match the format
  are usually closed without review.
- **Other lists** — submit only where the list is maintained and the entry adds
  value. Avoid paid "directory" sites that scrape PyPI.

After a listing goes live, verify that the install command works from a clean
machine and that the link resolves to this repository.

## Post-release verification

- [ ] `uvx sub-family-mcp --version` prints the new version from a clean cache
      (`uv cache clean sub-family-mcp` first if needed).
- [ ] `pip install sub-family-mcp` in a fresh Python 3.11, 3.12, and 3.13
      environment; run `sub-family init` in a temporary directory.
- [ ] `claude mcp add --scope user sub-family -- uvx sub-family-mcp` works and
      `list_family` returns the starter members.
- [ ] PyPI page shows the README, license, Python requirement, and project
      links.
- [ ] MCP registry search returns the new version and the package metadata.
- [ ] GitHub release notes match `CHANGELOG.md`.
- [ ] No secrets or absolute local paths were published in the sdist.

## Quality checklist

A release is ready when all of these are true:

- Tests, lint, format, and strict type checks pass in CI.
- The wheel installs without the source tree and exposes both `sub-family` and
  `sub-family-mcp`.
- The sdist contains docs and examples, and no `.env`, database, or cache files.
- README and README.ru agree on commands, tool names, and configuration fields.
- The version appears in exactly one Python source file, plus `server.json` and
  the changelog.
- `server.json` passes `mcp-publisher validate` and matches the PyPI artifact.
- The release was published through OIDC, not a stored token.
- Security-relevant changes are called out in the changelog.

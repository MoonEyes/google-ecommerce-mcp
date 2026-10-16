# Contributing

Thanks for helping. Issues and pull requests are welcome, in English or French.

## Ground rules

1. **Read-only stays read-only.** A tool must never call an endpoint that creates, updates, publishes, submits or deletes. Every endpoint a tool calls must be listed in `ALLOWED_ENDPOINTS` in `server.py` (Google services) or in the allow-list of its own module (`google_ads.py`, `meta_ads.py`, `openai_ads.py`, built on `AllowList` in `ads_common.py`); anything else is refused at runtime and fails CI. If a new tool needs a query `POST`, add it there and to `read_queries` in `tests/test_server.py::test_allow_list_has_no_write_endpoint`, and explain why in the PR.
2. **Errors are results.** Use `_call` and `@_guard` so failures come back as `{"error": ...}` objects.
3. **No hard-coded accounts.** Every id comes from an environment variable read in `config.py`.
4. **Keep answers small.** Flatten Google responses to what a model needs and cap row counts.

## Development

```bash
git clone https://github.com/MoonEyes/google-ecommerce-mcp
cd google-ecommerce-mcp
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

Tests run offline against a fake HTTP session; no Google account is needed. Every new tool needs at least one test, and `test_tools_are_registered_with_real_signatures` must be updated with the new tool count.

To try a change against real data, run `google-ecommerce-mcp setup` with your own OAuth client, then:

```bash
npx @modelcontextprotocol/inspector google-ecommerce-mcp
```

## Adding a tool

1. Write the function in `server.py` under the right section, decorated with `@mcp.tool()` then `@_guard`.
2. Its docstring is what the model reads: say what it answers, the parameter formats and one example value.
3. Document it in `docs/TOOLS.md` and the table in `README.md`.
4. Add a line to `CHANGELOG.md`.

## Adding a service to `doctor`

1. Add the service to `SERVICES` in `setup_guide.py` (ordered steps, each with the exact command and how to see it worked); its `id` becomes a valid `--only` value.
2. Add its offline check in `doctor.py` (returns a `ServiceReport`: set up, secret present, token check, status `ok` / `fix` / `not_set_up` / `optional`, and the next command). Never print or return a secret; mask ids with the existing helper.
3. Add its minimal read call to `_live_call` in `doctor.py`: call an existing read-only tool, never a new request path. Map its errors in `advice()`.
4. Keep the exit codes: `0` all configured services fine, `1` a configured service fails, `2` nothing configured.
5. Add offline tests (`tests/test_doctor.py`, `tests/test_setup_guide.py`), document the status wording in `docs/TROUBLESHOOTING.md` ("Doctor says") and list the service in `docs/SETUP.md`.

## Adding a section to the client report

1. Create `src/google_ecommerce_mcp/report/sections/<id>.py` and add `<id>` to `SECTION_IDS` in `report/sections/__init__.py`.
2. Expose `ORDER` (sort key; the defaults are in `DEFAULT_ORDER`) and `render(data, ctx)`, which returns the section body as HTML. Optionally `SUBSECTION = True` for an unnumbered block inside the previous section. The renderer adds the numbered title (`section.<id>` in `i18n.py`, French and English).
3. Read sources with `ctx.view(...)` and escape everything with `ctx.esc` or the `ctx` formatters. A missing or failed source must render `ctx.nodata` / `ctx.unavailable`, never a zero. Use `ctx.delta` for changes (it applies the same significance test as the summary). Use `accent_graphic` for graphics and `accent_text` for text colours. New texts go in `i18n.py` in both languages.
4. If the section needs a new source or part, document it in `report/contract.py` and in `docs/REPORTING.md`.
5. Add offline tests (`tests/test_report_sections.py`, with sample data from `tests/data/`): a normal case, a missing source, a failed source, and an escaping case.

## Diagrams

Diagram sources are JSON files in `docs/diagrams/`, rendered with Archify (see `docs/ARCHITECTURE.md`). Update the JSON, rerender, and commit both the JSON, the HTML and the PNG.

## Releasing (maintainers)

1. Bump the version in `pyproject.toml`, `server.json` (`version` and `packages[0].version`) and `src/google_ecommerce_mcp/__init__.py`, and add a `CHANGELOG.md` entry.
2. Commit, then `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. `.github/workflows/release.yml` checks the versions match, runs the tests, publishes to PyPI (trusted publishing) and to the official MCP Registry (GitHub OIDC). No token is stored anywhere.
4. Create the GitHub release from the tag with the changelog entry as notes.

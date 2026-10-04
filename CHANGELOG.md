# Changelog

## 0.2.0 (2026-10-04)

Read-only is now enforced, not just declared (feedback from r/mcp).

- **Runtime allow-list.** Every outgoing request is checked against `ALLOWED_ENDPOINTS` in `server.py` before it
  leaves the process; anything else returns `{"error": "blocked"}` without a network call. `readOnlyHint` stays,
  but it is only a hint a client may ignore.
- **Build gate.** `test_every_request_is_on_the_read_only_allow_list` runs every tool and fails CI on any blocked
  attempt; `test_allow_list_has_no_write_endpoint` fails if the list itself gains a write endpoint.
- **Least-privilege scopes.** `setup` now requests only read-only scopes by default. The write-capable
  `content` (Merchant Center) and `indexing` scopes need `--with-merchant` / `--with-indexing`; the installer
  adds `content` only when a Merchant Center id is given. `server_status` lists the granted scopes and flags the
  write-capable ones. **Existing users:** rerun `setup` to drop scopes you do not use.
- **Structured failures.** A missing scope returns `missing_scope` with the exact `required_scope` and the fix;
  a quota hit returns `rate_limited` with `retry_after_seconds`. Merchant pagination keeps the pages already read
  (`partial: true`), and `gtm_inventory` reports which sub-request failed instead of returning empty lists.
- **Explicit freshness.** Every result carries `fetched_at` (UTC) and `freshness` (how far behind Google's data
  is); report tools also return the `date_range` actually queried.

## 0.1.4 (2026-10-04)

- The server now reports its own version in the MCP handshake (it reported the `mcp` library version).
- Server instructions tell the assistant which tool answers which kind of question.
- Version 0.1.3 was skipped: the number was taken by a build on Glama with the same code as 0.1.2.

## 0.1.2 (2026-10-04)

- Every tool carries a title, a fuller description and the standard MCP annotations
  (`readOnlyHint`, `destructiveHint: false`, `idempotentHint`, `openWorldHint`), so clients can show it is read-only.
- Every parameter is described in the tool schema; `limit` and `max_pages` are bounded,
  `gsc_performance.dimensions` and `pagespeed.strategy` are enumerations.
- Requires `mcp>=1.10`.

## 0.1.1 (2026-10-04)

- Published on PyPI and in the official MCP Registry: install with `uvx google-ecommerce-mcp`.
- The installer now registers `uvx google-ecommerce-mcp` (PyPI) in Claude Desktop instead of a GitHub archive URL.
- Dockerfile and `glama.json` for Glama listing and checks.

## 0.1.0 (2026-10-04)

First public release.

- 12 read-only tools: GA4 (report, realtime), Search Console (performance, URL inspection, sitemaps),
  Merchant Center (data sources, product issues with pagination, MCQL reports), Tag Manager inventory,
  Indexing API status, PageSpeed Insights.
- `setup` command (OAuth consent in the browser) and `check` command.
- `install` command and one-line installers (`install.ps1`, `install.sh`): install uv if needed, ask the ids,
  authorize, register the server in Claude Desktop with a timestamped backup. Handles UTF-8 BOM configs,
  refuses to overwrite an existing entry without `--force`, never touches an unparsable config.
- OAuth token stored in the OS keyring by default, or in a file with `GOOGLE_TOKEN_FILE`.
- Offline test suite, including a guard that every outgoing call is read-only.
- Documentation: architecture, tool reference, Google Cloud setup, security model, troubleshooting,
  example prompts and client configs, Archify diagrams (architecture, tool call, setup), CI on 3 OSes.
- Release pipeline: PyPI trusted publishing and the official MCP Registry (`io.github.MoonEyes/google-ecommerce-mcp`).

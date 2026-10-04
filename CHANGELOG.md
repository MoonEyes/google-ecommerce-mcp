# Changelog

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

# Changelog

## 0.1.0 (2026-10-04)

First public release.

- 12 read-only tools: GA4 (report, realtime), Search Console (performance, URL inspection, sitemaps),
  Merchant Center (data sources, product issues with pagination, MCQL reports), Tag Manager inventory,
  Indexing API status, PageSpeed Insights.
- `setup` command (OAuth consent in the browser) and `check` command.
- OAuth token stored in the OS keyring by default, or in a file with `GOOGLE_TOKEN_FILE`.
- Offline test suite, including a guard that every outgoing call is read-only.
- Documentation: architecture, tool reference, Google Cloud setup, security model, troubleshooting,
  example prompts and client configs, Archify diagrams (architecture, tool call, setup), CI on 3 OSes.

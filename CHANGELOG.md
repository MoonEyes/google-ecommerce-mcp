# Changelog

## Unreleased

### Added

- **Paid media connectors, read-only: Google Ads, Meta Ads, ChatGPT Ads (9 tools, 22 in total).** `gads_accounts`,
  `gads_performance`, `gads_query` (Google Ads REST, `searchStream`), `meta_ad_accounts`, `meta_insights` (Graph API
  Insights), `openai_ads_account`, `openai_ads_insights`, `openai_ads_conversions` (OpenAI Ads Advertiser API), and
  `ads_overview`, which puts the three side by side. Each platform is optional and answers `not_configured` when unset.
  Guide: `docs/ADS.md`. **Tested offline only, against fake responses; not yet run against real accounts.**
- **Attribution is stated, not hidden.** Every ads result carries an `attribution` block (Meta default 7-day click +
  1-day view, ChatGPT Ads 30-day click + 1-day view by default, Google Ads per conversion action). `ads_overview` always
  returns the warning that each platform's conversions overlap and that their sum is not deduplicated. ROAS totals are
  sum of values over sum of spends, never a mean of ratios. Meta returns null (with per-window figures) when several
  attribution windows are requested, and null with a note when no purchase event is measured. Spends are added only
  when the currency is the same and known; nothing is ever converted.
- **New commands.** `setup-ads --client-secret <file> [--token-file <file>]` authorizes Google Ads with its own token.
  `set-secret meta-access-token|openai-ads-key` stores a secret in the OS keyring from a hidden prompt.
- **New settings.** `GADS_CUSTOMER_ID`, `GADS_LOGIN_CUSTOMER_ID`, `GADS_DEVELOPER_TOKEN` (optional: Google no longer
  requires it from 9 September 2026), `GADS_TOKEN_FILE`, `GADS_API_VERSION` (`v26`), `GADS_ALLOW_SENSITIVE_QUERIES`,
  `META_ACCESS_TOKEN`, `META_AD_ACCOUNT_ID`, `META_API_VERSION` (`v26.0`), `OPENAI_ADS_API_KEY`, `ADS_TRUST_ENV`.
- Added: `docs/SETUP.md` (install one service at a time, access checklist, client e-mail template, revocation), guided
  install prompts (`examples/INSTALL-PROMPT.md`, `INSTALL-PROMPT.fr.md`) and `docs/RELEASING.md` (maintainer checklist;
  no release was made for this branch, next suggested version 0.4.0).
- Added: `install` Paid media step (`--skip-ads`, `--gads-customer-id`, `--gads-login-customer-id`,
  `--meta-account-id`, `--corporate-proxy`; secrets only at hidden prompts), and `doctor [--live] [--json]
  [--only ...] [--config] [--name]` with exit codes 0, 1 and 2.
- Added: `google-ecommerce-mcp report`, a static client report generator: one self-contained HTML file (no network,
  no script, no external URL), printable to PDF, in the client's brand (`--brand-name`, `--accent`, `--logo`), French or
  English, with comparisons against the previous period and last year, polarity-aware colours, confidence levels and the
  attribution warning. Causes appear only when supplied. Package `report/`, input contract in `docs/REPORTING.md`.
- Added: `THIRD_PARTY_NOTICES.md`: parts of the report generator are ported from OpenGSC (MIT).
- Changed (`install`): every check (readable config, conflicts, separate token files) runs before any OAuth consent or secret prompt; exit codes 2, 3 and 130; clear messages for a malformed, oversized (over 5 MB), UTF-16 or read-only config; microsecond backups; Google Ads ids of exactly 10 digits, Meta ids of 3 to 20; `--corporate-proxy` alone updates an existing entry; the PageSpeed key comes from `--pagespeed-key` (not recommended), then `PAGESPEED_API_KEY`, then a hidden prompt.
- Changed (`report`): atomic write and `--output` equal to `--input` refused; hostile input exits with code 1; numbers beyond 1e15, NaN and booleans read as not provided; the summary uses the same significance test as the badge (a non-significant change is never quoted, confidence drops a level); revenue judged against the relative noise of orders; confidence capped at medium under 500 events; page URLs lose their query string and identifier parameters are masked; `accent_graphic` / `accent_text`; shares sum to 100 % (largest remainder); Content-Security-Policy meta tag; inconsistent periods give a warning; SVG logos with `&#`, `<animate>`, `<set>` or leading text are refused.
- Docs: client reporting guide
- **Docs.** New `docs/ADS.md`; README rewritten around use cases, real findings and what the server will not do;
  tool reference, security model, architecture, troubleshooting and example prompts updated for the 22 tools.

### Security

- **Google Ads token kept apart.** The `adwords` scope can write and cannot be limited, so `setup-ads` requests it
  alone, stores it in its own keyring entry (`oauth-token-ads`) or `GADS_TOKEN_FILE`, never replaces the read-only
  Google token, and refuses to overwrite a file that is not a Google Ads token. The scope never enters `setup`.
- **Read-only per platform.** One allow-list per platform (pinned host, GET plus the two read-query POSTs), checked
  before every request. Google Ads queries must pass a GAQL validator (single `SELECT`, no `;`, `--`, `/*`, `*/` or
  backslash even inside a string, no JOIN, `LIMIT` bounded to 10,001, reporting resources only, personal-data and
  billing names refused unless `GADS_ALLOW_SENSITIVE_QUERIES=1`). Meta refuses `method` and `access_token` URL
  parameters and never requests `paging.next`.
- **Transport hardening.** Redirects are never followed, answers are capped at 20 MB, secrets are masked in every
  error and log line, the ChatGPT Ads host is a constant, and the environment's proxy and CA bundle are used only with
  `ADS_TRUST_ENV=1`.
- **Remaining risks, documented:** a ChatGPT Ads key is not confirmed to be limitable to reading, Meta system user
  tokens are long-lived, and on Windows the token-file ACL restriction is best effort.

### Changed

- `server_status` / `check` report which Ads settings are present (booleans only) and the write-capable scope note;
  the server instructions mention the paid media tools and the attribution caveat.
- The command-line entry point returns an exit code (`set-secret` with an empty value exits 1).
- **Missing scopes are a list.** A `missing_scope` error now also carries `missing_scopes` (a list holding the one
  scope to grant), and a partial result from `gtm_inventory` lists every scope its failed sub-requests were missing, so
  the caller gets the partial data plus exactly what to grant. `missing_scope` and `required_scope` are unchanged. Still
  true: only the tools that make several calls (Merchant, `ga4_properties`, `gsc_performance`, `gtm_inventory`) return
  partial results; a single-call tool whose scope is missing returns the structured error alone, and `date_range` is
  still only on `ga4_report` and `gsc_performance`.
- **Installers no longer move under the user.** `install.ps1` and `install.sh` install the exact package version they
  were released with (`google-ecommerce-mcp==0.3.1`) instead of whatever PyPI serves that day, and their headers no
  longer advertise a branch URL. The README now leads with "download, read, run", gives a one-line form pinned to an
  exact commit, and publishes the SHA-256 of each script. `tests/test_installer_scripts.py` keeps all of it in step
  with `pyproject.toml`. Each release must bump the version in both scripts and refresh the README pin and checksums.
  Not covered: the uv installer the scripts call from astral.sh is still unpinned, and the server entry written into
  Claude Desktop still runs `uvx google-ecommerce-mcp` (latest) on each start.

## 0.3.1 (2026-10-05)

- An expired or revoked Google token now returns `not_authenticated` with the fix (rerun `setup`) instead of raising
  `RefreshError` out of every tool. The message explains that apps whose OAuth consent screen is in *Testing* get
  tokens that expire after 7 days. Found while checking 0.3.0 against a live account.

## 0.3.0 (2026-10-05)

Responses a model can read without guessing (feedback from r/mcp).

- **Totals next to the top N.** `ga4_report` and `ga4_realtime` return `totals` computed by GA4 over every row,
  plus `row_count` and `truncated`. `gsc_performance` returns the real property totals (same filter, no split),
  which include rows beyond `limit` and anonymized queries; if that second query fails the rows still come back
  with `partial: true`.
- **Units and definitions in the response.** A `metrics` block gives each metric's unit (count, ratio 0 to 1,
  seconds, the property currency...) and a short definition; `sessions` and `engagedSessions` are defined against
  each other. GA4 values are now numbers instead of strings. **Breaking** for code that expected strings.
- **Explicit time.** `date_range` gives calendar dates resolved in the data's timezone (the GA4 property's, Pacific
  Time for Search Console), the `requested` values when they were relative, and `data_complete`. When false,
  `settling_from` names the first day that can still change. `tzdata` is now installed on Windows for this.
- **Property discovery.** New `ga4_properties` tool (Admin API `accountSummaries`, read-only, no extra scope) lists
  every GA4 account and property the token can read. `ga4_report` and `ga4_realtime` accept a `property_id`.
  Enable the *Google Analytics Admin API* in your Cloud project to use it.
- **`api_disabled` error** names the Google Cloud API to enable instead of returning a bare 403.
- The allow-list now accepts reading a single Merchant Center product (`GET .../products/{name}`); it was refused as `blocked`.
- `setup` now always shows the Google account chooser (`prompt=consent select_account`), so the token is no longer silently issued for whichever account the browser is signed in to.

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

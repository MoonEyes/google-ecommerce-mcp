# Changelog

## Unreleased

### Fixed

- **Google Ads campaign creation is no longer refused for a missing bidding strategy.** `gads_plan_create_campaign` now sends a fixed, empty `manualCpc` (manual CPC with no bid amount and no enhanced CPC), because Google requires a bidding strategy on a new campaign. It is set by the server, never by the caller, and the write policy accepts only that one exact empty object at that one place: any other bidding field (a bid amount, `enhancedCpcEnabled`, a target CPA or ROAS) is still refused with `bidding_not_supported`. **This exemption is tested offline only; no campaign was created on a real account.**
- **`clients.json` with a repeated JSON key is refused.** Two identical keys in one object (for example two `max_daily_budget_micros` on the same client) used to let the last value win silently. It is now `registry_invalid`, and the message names the repeated key.
- **Google Ads error messages carry the field path.** An error from the API now ends with the path of the field it concerns, in square brackets (for example `[mutate_operations.campaign_operation.create.manual_cpc]`), and each per-error message is now cut at 300 characters instead of 200 (the fallback message stays cut at 200).

### Notes for contributors

- When a real `clients.json` exists on your machine, run the test suite with an isolated config folder, or the tests read your real registry. The command is in `CONTRIBUTING.md`.

## 0.5.0 (2026-10-18)

Multi-client, and controlled writing. **No write has been tried on a real account: everything below is tested offline only.** Writing is off by default.

### Added

- **Several clients in one install.** A client registry, `clients.json` (`%APPDATA%\google-ecommerce-mcp\` or `~/.config/google-ecommerce-mcp/`, or `GEM_CLIENTS_FILE`), holds each client's account ids and the names of its keyring entries, never a secret. Tools take an optional `client_id`; it is mandatory once two clients exist. `GEM_CLIENT=<id>` pins a server process to one client. Without a registry, nothing changes: one implicit client `default` built from the old environment variables and keyring entries (no migration). Guide: `docs/MULTI-CLIENT.md`.
- **Per-client secrets** in the OS keyring (`<client>/google`, `google-w`, `gads`, `meta`, `openai`), or `GEM_SECRET_<CLIENT>_<KIND>` for CI.
- **Commands.** `client add|list|show|remove|enable-write|disable-write|set-secret`, `approve <plan_id> [--client X]`, and, with the install code of this release, `setup --client X [--write]`, `setup-ads --client X`, `doctor [--client X]` and `quickstart`. Guide: `docs/QUICKSTART.md`.
- **Controlled writing, one path for everything: plan, approve, apply, journal.** A `*_plan_*` tool builds the change, checks the client's policy, snapshots the current state, runs a dry run (native when the platform has one, otherwise local), and returns a diff, the risks, a signed single-use `confirmation_token` and the exact `confirm` text. Only the four `*_apply` tools write. `rollback_plan` builds a new plan that undoes an applied one. Guide: `docs/WRITE.md`.
- **56 new tools (78 in total).**
  - Cross-cutting: `clients_list`, `client_profile`, `client_check`, `plan_get`, `plan_list`, `audit_log`, `rollback_plan`, `gads_apply`, `meta_apply`, `openai_ads_apply`, `google_apply`.
  - Google Ads: `gads_conversion_actions`; plans `gads_plan_pause_entities`, `gads_plan_budget_change`, `gads_plan_add_negatives`, `gads_plan_create_campaign` (Search, paused, own budget; see Unreleased for the bidding strategy).
  - Meta ads: `meta_campaigns`, `meta_adsets`, `meta_ads_list`; plans `meta_plan_pause`, `meta_plan_budget_change`, `meta_plan_create_campaign`, `meta_plan_create_adset`, `meta_plan_create_ad` (all created paused).
  - ChatGPT Ads: `openai_ads_campaigns`, `openai_ads_ad_groups`, `openai_ads_ads`; plans `openai_ads_plan_pause`, `openai_ads_plan_budget_change`, `openai_ads_plan_create_campaign` (paused).
  - Meta pixel, catalog, Instagram, Page: `meta_pixel_health`, `meta_dataset_quality`, `meta_catalog_diagnostics`, `meta_catalog_products`, `meta_ig_insights`, `meta_page_insights`; plan `meta_eco_plan_catalog_item_update`.
  - GA4: `ga4_custom_definitions`, `ga4_key_events`; plans `ga4_plan_key_event_create`, `ga4_plan_custom_dimension_create`.
  - Search Console: `gsc_sites`; plan `gsc_plan_sitemap_submit`.
  - Merchant Center: `merchant_products`, `merchant_account_issues`; plan `merchant_plan_product_patch` (explicit update mask).
  - Tag Manager: `gtm_workspace_status`, `gtm_get_tag`, `gtm_get_trigger`, `gtm_get_variable`, `gtm_versions`; plans `gtm_plan_workspace_create`, `gtm_plan_tag_upsert`, `gtm_plan_trigger_upsert`, `gtm_plan_variable_upsert`, `gtm_plan_quick_preview` (a non-default workspace only; nothing goes live).
- **New settings:** `GEM_WRITE` (`1` allows writing at all), `GEM_CLIENT`, `GEM_CLIENTS_FILE`, `GEM_STATE_DIR`, `GEM_WRITE_HMAC_KEY` (CI), `GEM_LIVE_TEST`, `GEM_LANG`.
- **Docs:** `docs/QUICKSTART.md`, `docs/WRITE.md`, `docs/MULTI-CLIENT.md`; `docs/SECURITY.md` rewritten; `docs/ARCHITECTURE.md`, `docs/TOOLS.md` (all 78 tools), `docs/ADS.md`, the setup guides and the README updated; `examples/multi-client.claude_desktop_config.json`; `tests/test_docs_fullcontrol.py` keeps the version and the tool list consistent across the docs.
- `THIRD_PARTY_NOTICES.md` credits the open-source projects whose ideas were reused (no code was copied from them).

### Changed

- **Positioning.** The package is no longer described as read-only. Reading is still the default and still uses read allow-lists that contain no write endpoint; for a client whose writing is turned on, the old promise "this server cannot write" no longer holds. See `docs/SECURITY.md`.
- **Tool annotations.** Reading tools and `*_plan_*` tools are annotated read-only. Only the four `*_apply` tools have `readOnlyHint: false` and `destructiveHint: true`, so you can set your MCP client's permissions per family. The tool list, parameters and annotations are frozen by a test (`tests/golden_tools.json`).
- **Google Ads:** the developer token is no longer needed (Google ended developer tokens on 9 September 2026); the docs say so, and `GADS_DEVELOPER_TOKEN` stays optional and is sent only when set.
- **Merchant Center:** the docs state that the old Content API for Shopping is shut down; only the Merchant API is used.
- The Google write scopes (GA4 edit, Tag Manager edit, Search Console, Merchant `content`) are held by a separate token (`setup --client X --write`); the read token is never used to write.

### Fixed

- **ChatGPT Ads currency.** The currency is read from the account's `currency_code` first; `openai_ads_account` no longer reports it as a dropped field and `summary()` returns it.
- **PageSpeed host.** The request now goes to `pagespeedonline.googleapis.com/pagespeedonline/v5/runPagespeed` (allow-list and tool).
- **Search Console.** The search type is sent as `type` in the request body, `searchType` being deprecated.
- **Google Ads access level.** `CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION` (and `ACTION_NOT_PERMITTED` on older versions) is reported as `project_not_approved`, with the fix: check the access level of the Google Cloud project, or use a test account.

### Security

- Writing is off by default: `GEM_WRITE=1`, plus `write.<platform>.enabled` per client, plus the account in `account_allow` and owned by the client, all re-checked at apply.
- Spending caps (`max_daily_budget_micros` in the account currency, +20 % per 24 hours, operations per plan), creations always paused, no activation, deletion, archiving or publishing, no bids.
- A field allow-list per action: any field not registered is refused; money-named fields need an explicit exemption.
- Signed confirmation tokens (HMAC-SHA256, bound to the exact plan, client and platform, expire in 15 minutes by default, single use).
- Human approval outside the chat (`google-ecommerce-mcp approve`, interactive terminal only) for plans that change spending, are irreversible, remove or archive, touch personal data or are high risk.
- A write request leaves the process only with a grant issued by the apply step for that exact method, URL and body, through a separate `WriteAllowList` with pinned hosts and no wildcard.
- A fail-closed journal (JSONL, private files): no journal, no write. Secrets and personal data are masked in it; text that comes from a platform is cleaned and marked `untrusted_text`.
- Per-client isolation: an immutable context per call, ownership check on every account id, caches, plans and journals keyed by client.
- **OAuth client file validation.** Before any consent flow or browser, every `setup`, `setup-ads` and `install` (per client and legacy, without `--client`) checks the OAuth client file: root key `installed` only (Desktop app, never `web`), `auth_uri` on `https://accounts.google.com/`, `token_uri` exactly `https://oauth2.googleapis.com/token`, `client_id` and `client_secret` present, local loopback redirects only. A hostile file (for example a `token_uri` on another host) is refused with where, why and the fix; nothing is opened nor stored. The flow is built from the validated content, never from a second read of the file. A file downloaded from Google Cloud Console is unaffected.
- **Write consent group by group.** `install --client X [--write]` offers writing one group at a time (Google properties; with `--write` also Google Ads, Meta, ChatGPT Ads), default no, each group with its own question and the typed client name, in a real terminal only; `--yes` never skips it. `GEM_WRITE=1` is added to the Claude entry only after a consent typed in that same run, and the entry is then pinned to the exact package version. `setup --client X --write` needs an explicit `--client-secret` and a real terminal.
- **Registry secret names are checked by kind on load.** The client `default` may only name the legacy keyring entry of the same kind (`google_read` -> `oauth-token`, `google_ads` -> `oauth-token-ads`, `meta` -> `meta-access-token`, `openai_ads` -> `openai-ads-key`; none for `google_write`): a mismatched pair makes the registry invalid (`registry_invalid`) instead of failing later on use.

### Known limitations

- **No write was tested on a real account.** Unconfirmed in particular: that Meta's Graph API accepts the `Authorization: Bearer` header on these write calls and honours `validate_only`; the Merchant Center update mask; that the GA4, Tag Manager, Search Console and ChatGPT Ads writes behave as documented.
- **The terminal approval does not protect against an assistant that has a shell or file access** on your machine (a pseudo-terminal, or the signing key).
- **The Google Ads token (`adwords`) and the ChatGPT Ads API key cannot be split into read and write.** Only this server's policy stands between the assistant and a change.
- The write policy is a file you, or anything running as you, can edit.
- Bids and bidding strategies are refused, which blocks some creations (for example a Meta ad set whose goal needs a bid cap). The one exception is the fixed, empty manual CPC that `gads_plan_create_campaign` sets itself (see Unreleased).
- `max_plan_budget_micros` cannot be used: the registry does not accept it. The plan-level ceiling is `max_daily_budget_micros` times `max_ops_per_plan`.
- A running server keeps the credentials it has in memory until it restarts, after `client remove` or a revocation: restart the MCP client.
- The read token of a client with Merchant Center or Indexing carries a Google write scope (`content`, `indexing`): Google has no read-only scope for them.
- A Claude entry with writing consented is pinned to its version and receives no fix until you rerun `install --client X --write` after updating.
- Activation, deletion, archiving, Tag Manager versions and publishing, conversion and audience uploads are not available.
- The paid media connectors are still tested offline only.

## 0.4.1 (2026-10-17)

### Fixed

- `ads_overview` and the report no longer compute MER when one ad platform failed: spend would be understated.
- A network error while refreshing a token now returns `{"error": "network"}` instead of an unhandled transport exception.
- PageSpeed results include `field_metrics` (LCP, INP, CLS, categories, `origin_fallback`). The CLS scale (x100) is an assumption, not yet confirmed on a live response.
- Report: a change that rounds to zero is shown as "stable"; lab values follow the report language.

### Added

- End-to-end tests (stdio server, pipeline, documentation) and an easy setup guide for the three ad platforms (`docs/SETUP-ADS.md`).
- Redrawn architecture diagram showing the paid media connectors; documentation audit corrections.

**The paid media connectors are still tested offline only, not yet against real accounts.**

## 0.4.0 (2026-10-16)

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

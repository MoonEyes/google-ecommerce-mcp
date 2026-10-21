# Troubleshooting

Start with `google-ecommerce-mcp check` (or ask the assistant to run `server_status`). It shows which variables are set and whether the token works.

## Installation and startup

**The client shows the server as failed or disconnected.**
Run the exact `command` + `args` from your client config in a terminal. A stdio MCP server waits silently for input: no output and no exit means it starts fine (stop it with Ctrl+C). An error message means the client sees the same error.

**`uvx: command not found`.**
Install [uv](https://docs.astral.sh/uv/getting-started/installation/), or use an absolute path to `uvx` in the client config (GUI apps often do not inherit your shell `PATH`).

**`ImportError: cannot import name 'FastMCP'`.**
The `mcp` package 2.x renamed it. This project pins `mcp<2`; reinstall in a clean environment (`uvx` does this automatically).

## Installer

**`irm ... | iex` is blocked by the execution policy.** Run `Set-ExecutionPolicy -Scope Process Bypass` in the same PowerShell window, then the install line again. This only affects that window.

**`uvx is not on PATH` after uv was installed.** Open a new terminal (the uv installer updates PATH for new sessions) and rerun the installer.

**`A server named 'google-ecommerce' already exists`.** You installed before. Rerun with `--force` to replace it, or `--name google-shop-2` to add a second shop.

**`... is not valid JSON`.** Your Claude Desktop config has a syntax error (often a missing comma after a manual edit). Fix it or rename it, then rerun. The installer never overwrites a file it cannot parse.

**Restore the previous Claude Desktop config.** Every change leaves `claude_desktop_config.json.bak-YYYYMMDD-HHMMSS-ffffff` next to it (the last part is microseconds). Copy it back over `claude_desktop_config.json`.

**Update to the latest version.** Run the install line again: it refreshes the package and keeps your token (answer *No* to "Authorize again?").

## Authentication

**`not_authenticated`.** No token stored for this OS user. Run `setup`. If you set `GOOGLE_TOKEN_FILE` during setup, set the same value in the client config.

**`not_authenticated` saying the token expired, or `invalid_grant` / `Token has been expired or revoked`.** If it happens every week, your OAuth consent screen is in *Testing*: Google expires those tokens after 7 days. Set it to *In production* (no verification needed for your own use), then rerun `setup`.
- Your OAuth app is External and in *Testing*: refresh tokens expire after 7 days. Publish the app (see [SETUP-GOOGLE-CLOUD.md](SETUP-GOOGLE-CLOUD.md#3-configure-the-oauth-consent-screen)) and rerun `setup`.
- You changed your Google password or revoked the app: rerun `setup`.

**`missing_scope`.** The token lacks the scope named in `required_scope`. Rerun `setup` and tick every box on the consent screen. For Merchant Center add `--with-merchant`, for `indexing_status` add `--with-indexing`: since 0.2.0 these write-capable scopes are no longer requested by default.

**`api_disabled`.** The API named in `api` is not enabled in the Google Cloud project of your OAuth client. Enable it under **APIs & Services > Library**, wait a few minutes, then retry. `ga4_properties` (0.3.0) needs the *Google Analytics Admin API*, which older setups did not enable.

**No keyring backend on a headless Linux server.**
Set `GOOGLE_TOKEN_FILE=~/.config/google-ecommerce-mcp/token.json` both for `setup` and in the client config, or install a Secret Service provider.

**Browser does not open during setup (remote machine).**
Run `setup` on a machine with a browser using `--token-file`, then copy the token file to the server and point `GOOGLE_TOKEN_FILE` at it.

## Per service

**GA4 `403 User does not have sufficient permissions`.** Wrong property id (use the numeric *Property ID*, not `G-XXXX`), or the signed-in account has no access to the property.

**`invalid_property_id`.** `property_id` takes the numeric id returned by `ga4_properties` (or `properties/123`), not the `G-XXXX` measurement id.

**`date_range.timezone` says `UTC (property timezone unknown)`.** The machine has no timezone database. On Windows it comes with the `tzdata` package, installed automatically since 0.3.0; rerun the install line.

**GA4 `400 Field xyz is not a valid dimension`.** Use API names, not UI labels: `sessionDefaultChannelGroup`, not "Default channel group". See the [API schema](https://developers.google.com/analytics/devguides/reporting/data/v1/api-schema).

**Search Console `403 User does not have sufficient permission for site`.** `GSC_SITE_URL` must match the property exactly: `sc-domain:example.com` for a domain property, or the full URL-prefix with trailing slash.

**Search Console returns no rows for recent dates.** Data lags about 2 days; the default end date already accounts for it, and `date_range.data_complete: false` flags a range that reaches into those days.

**Search Console `totals` are higher than the sum of the rows.** Expected: totals include rows beyond `limit` and the queries Google anonymizes for privacy.

**`gsc_inspect_url` 403/400.** The URL must belong to the configured property, and the daily quota is 2,000 inspections per property.

**Merchant `403 ... GCP project ... not registered`.** Register your Cloud project with Merchant Center (step 5 of the setup guide).

**Merchant `404` or empty results.** Check `MERCHANT_ACCOUNT_ID`. For a multi-client account (MCA), use the sub-account id that owns the products.

**MCQL `id must be selected`.** Queries on `product_view` must include `id` in `SELECT`.

**GTM `container_not_found`.** The signed-in account cannot see that `GTM-` container, or the id has a typo.

**`indexing_status` says no notification.** Normal for most shops: it only reports URLs submitted through the Indexing API.

**PageSpeed `Quota exceeded`.** Set `PAGESPEED_API_KEY` to your own restricted key (or, with clients, `client set-secret <client> pagespeed`). The server calls `pagespeedonline.googleapis.com`; with a key restricted in Cloud Console, enable the *PageSpeed Insights API* in the same project.

**PageSpeed `lighthouse_failed`, or `performance: null` with `lab_error`.** Lighthouse could not load or score the page (error code in `lab_error.code`, for example `NO_FCP`). Retry later or test another URL. When `field_metrics` is present it is still valid: it comes from real visits (CrUX), not from the failed lab run.

## Doctor says

`google-ecommerce-mcp doctor [--client <name>] [--live] [--json] [--only <services>] [--config <file>] [--name <server>]` prints a table. Without a client registry the columns are Service, Set up, Secret, Token, Status, Account. With a registry there is one row per client and platform (Client, Platform, Ids, Read, Write, On, Status, Account, Next, plus Live with `--live`), and `--client` limits it to one client. Then "To fix" with the exact command for each problem, "Not set up", a `Result:` line and a `Next command:`. It reads this shell's environment and the server entry of your Claude Desktop config. Exit code: `0` all fine, `1` a configured service fails, `2` nothing is configured.

Status column: **OK**, **FIX** (set up but something is wrong; the problem and the command are under "To fix"), **not set up**, **optional** (PageSpeed without a key).

**Status "not set up", exit code 2 when every service is.** The service has no id. If you do not use it, ignore it. Otherwise run the command listed under "Not set up". If you did set variables in Claude Desktop and still see this, check that the server entry is called `google-ecommerce` or pass `--name`, and `--config` if your config file is not in the standard place. The warning "Claude Desktop config not found: only this shell's environment was read" says `doctor` did not find it.

**Token "missing".** No token is stored for that service. Google services: the command is `setup` (it keeps `--with-merchant` / `--with-indexing` you already enabled). Google Ads: `setup-ads`. Meta and ChatGPT Ads: the secret column says `no`; run `google-ecommerce-mcp set-secret meta-access-token` or `set-secret openai-ads-key`. `META_ACCESS_TOKEN` / `OPENAI_ADS_API_KEY`, when set, win over the keyring.

**Token "missing scope".** The stored Google token lacks the permission of that service (Merchant Center needs the extra `content` permission). Rerun the `setup` command shown. For Google Ads, run `setup-ads`. "The Google Ads token holds other Google permissions" means a file or entry was mixed up: authorize Google Ads alone, with a token file different from `GOOGLE_TOKEN_FILE`.

**Token "unreadable".** The stored token cannot be read (corrupt file, no keyring backend). Rerun `setup` / `setup-ads`, or see "No keyring backend" under [Authentication](#authentication).

**Status FIX with `--live` (Live column shows an error code).** The configuration looks right but the small read call failed. The problem line says why (token refused or expired, API not enabled, account not visible, rate limit, network). Look the code up in [Authentication](#authentication), [Per service](#per-service) or [Paid media errors](#paid-media-errors), run the command shown, then the `doctor --only <service> --live` line printed under it. Live `not_visible` means the call worked but the configured account is not among those the credentials can read: check the id, or add the manager id for Google Ads.

**Status FIX: "must be digits" / "must look like GTM-".** The id has the wrong format (for example `G-XXXX` instead of the numeric GA4 property id). Replace it with `install --yes --skip-auth --force --<flag> <id>` as printed.

**PageSpeed shows "skipped" under `--live`.** Expected: PageSpeed needs a page address and takes about 30 seconds. Ask the assistant to run `pagespeed` on a URL.

**Warning "ADS_TRUST_ENV=1 ..." / "plain text in the Claude Desktop config".** Informational. `ADS_TRUST_ENV=1` is only for company proxies. A Meta token, ChatGPT Ads key or developer token written in the config is readable by anyone who can read that file: move it to the keyring (`set-secret ...`) and delete it from the config.

**`install` exits with code 3.** Either the Claude Desktop entry already has a different value for a variable you passed (the message names them), or the config could not be used.
For a conflict, nothing was changed and no authorization was started: rerun with `--force` to replace the values (it never deletes a variable). For a malformed, oversized or UTF-16 file, fix or move the file; `--force` does not help. A read-only file is detected only when writing, after the consent: remove the read-only attribute, close Claude Desktop and rerun (the token already stored is kept).

**`install` exits with code 2.** Refused before any authorization, for example `GADS_TOKEN_FILE` is the same file as the Google token file. Choose another file for Google Ads (or unset `GADS_TOKEN_FILE` to use the keyring).

**`install` exits with code 130.** You pressed Ctrl-C (or the input closed). It lists what was already done; rerun it, it is safe to repeat.

**doctor shows `***` for an id.** The value is malformed (a Google Ads customer id must have exactly 10 digits, a Meta ad account id 3 to 20). `doctor` then proposes an `install ... --force` command with the right flag.

**Ids appear as `123***890`.** Expected: ids are masked; no secret is ever printed (also in `--json`).

## Paid media errors

Google Ads, Meta Ads and ChatGPT Ads. Full list of codes: [ADS.md](ADS.md#structured-errors). These connectors were tested offline only, so a real-account surprise is possible: include the tool's error object in an issue.

**`not_configured` on a paid media tool.** The `detail` names the missing setting. Google Ads: `GADS_CUSTOMER_ID`. Meta: `META_ACCESS_TOKEN` (or `google-ecommerce-mcp set-secret meta-access-token`) and `META_AD_ACCOUNT_ID`. ChatGPT Ads: `OPENAI_ADS_API_KEY` (or `set-secret openai-ads-key`). Put ids in the client config `env`, secrets in the keyring.

**`project_not_approved` on Google Ads.** Your Google Cloud project does not have the access level this call needs (Google ended developer tokens on 9 September 2026; the level now belongs to the project). Use a Google Ads test account, or ask Google for a higher level for the project that owns your OAuth client. It is not a problem with your account permissions.

**`auth_failed` on Google Ads.** No Google Ads token, an expired one, or the account cannot read this customer. Run `google-ecommerce-mcp setup-ads --client-secret <file>` again. If the consent screen of your OAuth client is in *Testing*, the token expires after 7 days: publish the app. If you reach the customer through a manager account, set `GADS_LOGIN_CUSTOMER_ID`.

**`auth_failed` on Meta.** The token is invalid or expired (Graph codes 102, 190, 463, 467). Create a new system user token with `ads_read` and store it again with `set-secret meta-access-token`. Remember that `META_ACCESS_TOKEN`, when set, wins over the keyring: unset the old one.

**`auth_failed` on ChatGPT Ads.** The key is wrong or revoked. Create a new one in Ads Manager and store it with `set-secret openai-ads-key`.

**`missing_scope`.** Google Ads: run `setup-ads` and grant the Google Ads scope (`required_scope` names it). Meta: the token lacks `ads_read` or access to the ad account (`required_permission`); regenerate it and assign the ad account to the system user.

**A Google Ads write is refused with a message ending in a bracketed path.** `dry_run_failed` or a failed `gads_apply` quotes Google's own message and, when Google names it, the field it concerns in square brackets, for example `[mutate_operations.campaign_operation.create.manual_cpc]` (messages are cut at 300 characters). Read the path first: it tells you which part of the request Google rejected. Nothing was written by a dry run. Include the message in an issue if it points at a field this server builds.

**`rate_limited`.** The platform quota is hit. Wait `retry_after_seconds` if it is given, then shorten the period, lower `limit`, or ask for `level=account` instead of `ad`. Meta answers also carry `usage` with the percentages Meta reports. Nothing is retried automatically.

**`not_available`.** Google Ads: enable the *Google Ads API* in the Cloud project of your OAuth client, check the customer id (digits) and `GADS_API_VERSION` (default `v26`). Meta: check `META_AD_ACCOUNT_ID`, `META_API_VERSION` (default `v26.0`) and that the token can see the account (`meta_ad_accounts`). ChatGPT Ads: the endpoint or account is not available to this key.

**`blocked`.** The request was refused before being sent: outside the platform's allow-list, a secret or a `method` parameter in the URL, or the platform answered with a redirect (never followed). It should not happen with the shipped tools; send the `detail` in an issue.

**`network`.** The platform could not be reached. Check the connection. Behind a corporate proxy, set `ADS_TRUST_ENV=1` in the server `env` (uses `HTTPS_PROXY`, `NO_PROXY` and `REQUESTS_CA_BUNDLE`). With `ADS_TRUST_ENV=1`, a `REQUESTS_CA_BUNDLE` pointing to a missing file also gives `network`: fix the path or unset the variable. The value must be exactly `1`.

**`response_too_large`.** The answer exceeds 20 MB and was not read. Shorten the period, lower the `LIMIT` of `gads_query`, or select fewer fields.

**`too_many_rows`** (ChatGPT Ads). `openai_ads_conversions` allows 2,000 rows and has no pagination. Pass fewer `entity_ids`, a shorter period, or fewer `event_names`.

**`resource_not_allowed`** (`gads_query`). The `FROM` resource, a field prefix or a `customer.*` field is not on the reporting allow-list. Use `gads_performance`, or a listed resource ([ADS.md](ADS.md#free-gaql-queries)). Only `GADS_ALLOW_SENSITIVE_QUERIES=1` lifts the list.

**`sensitive_resource`** (`gads_query`). The query names a personal-data or billing resource (search terms, click ids, user lists, leads, calls, `payments_*`, `billing_*`...). Use aggregated fields. `GADS_ALLOW_SENSITIVE_QUERIES=1` lifts the check; personal data may then reach the conversation.

**`invalid_gaql`.** The query is not one plain `SELECT`: `;`, `--`, `/*`, `*/` or a backslash anywhere (even in a string), a second `SELECT` or `FROM`, `JOIN`, a malformed `LIMIT`, an unbalanced quote or non-ASCII characters outside strings. Rewrite it; the `fix` repeats the shape.

**`invalid_dates`.** `gads_performance` needs `start_date` and `end_date` (`YYYY-MM-DD`, start on or before end); `openai_ads_*` and `ads_overview` too. `meta_insights` takes both dates, or a `date_preset`, not both.

**Other `invalid_*`** (`invalid_customer_id`, `invalid_argument`, `invalid_entity_id`, `invalid_timezone`, `invalid_attribution`, `invalid_fields`...). A parameter was refused before any request; `detail` says which and `fix` how.

**`internal_error`.** An unexpected failure inside a connector, reported as data with secrets masked. Retry once; if it persists, open an issue with the object.

**Meta purchases, value and ROAS are `null`.** Either several `attribution_windows` were requested (their purchases overlap, so there is no single total: read `purchases_by_window`), or the account sends no purchase event (`purchase_measurement.measured` is `false`). Check the pixel or Conversions API purchase event in Events Manager. `null` is not zero.

**ChatGPT Ads values are `null`.** `null` means "not computable" (for example a ratio with no click), not zero. `spend`, `cpc` and `cpm` are in major units of the account currency.

**`ads_overview` returns `combined: null`.** The platforms that returned data do not share one known currency, so spends are not added (no conversion is ever made); `combined_reason` says which. Query each platform's own tool instead.

**The platforms' conversions do not match each other or GA4.** Expected. Each platform attributes independently with its own window and date rule, and the sum is not deduplicated. See [ADS.md](ADS.md#read-this-first-attribution).

## Several clients

**`client_required`.** Two clients or more are registered and the call gave no `client_id`. Name the client in your question, or pin a server entry with `GEM_CLIENT=<name>`. See [MULTI-CLIENT.md](MULTI-CLIENT.md#errors).

**`unknown_client` / `id_not_owned` / `pinned_client` / `registry_invalid`.** Each carries `detail` and `fix`. `client list` shows what exists; `client show <name>` shows the ids a client owns (masked). A `registry_invalid` means `clients.json` is broken (unreadable, not valid JSON, a key repeated in the same JSON object, or a value the schema rejects): the server refuses to guess, and does not fall back to environment variables. If the `detail` says `duplicate key`, a copy-paste left the same key twice in one object (for example two `max_daily_budget_micros` for a client): delete the extra one, since the last value would otherwise silently win.

**My `GA4_PROPERTY_ID` (or another id variable) is ignored.** Once a registry exists the id variables are ignored, by design. Put the id in the client: `client add <name> --replace`.

## Writing

Only if you turned writing on ([WRITE.md](WRITE.md)). Every refusal has an `error`, a `detail` and a `fix`, and means nothing was written. The full table of codes and fixes is in [WRITE.md](WRITE.md#common-refusals-and-their-fix). The three most common:

**`write_disabled`.** `GEM_WRITE=1` is missing from the server `env` of your MCP client config. Add it and restart the client.

**`approval_required`.** The plan changes spending, is high risk or touches personal data. Open a terminal and run the `approval_command` shown in the plan (`google-ecommerce-mcp approve <plan_id> --client <name>`), then apply again. The assistant cannot do this for you.

**`plan_expired`.** A plan is valid for 15 minutes by default. Ask for the plan again.

## Still stuck

Open an [issue](https://github.com/MoonEyes/google-ecommerce-mcp/issues) with the output of `google-ecommerce-mcp check` and the tool's error object. Remove ids you consider private.

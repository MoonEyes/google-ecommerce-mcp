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

**Restore the previous Claude Desktop config.** Every change leaves `claude_desktop_config.json.bak-YYYYMMDD-HHMMSS` next to it. Copy it back over `claude_desktop_config.json`.

**Update to the latest version.** Run the install line again: it refreshes the package and keeps your token (answer *No* to "Authorize again?").

## Authentication

**`not_authenticated`.** No token stored for this OS user. Run `setup`. If you set `GOOGLE_TOKEN_FILE` during setup, set the same value in the client config.

**`invalid_grant` / `Token has been expired or revoked`.**
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

**PageSpeed `Quota exceeded`.** Set `PAGESPEED_API_KEY` to your own restricted key.

## Still stuck

Open an [issue](https://github.com/MoonEyes/google-ecommerce-mcp/issues) with the output of `google-ecommerce-mcp check` and the tool's error object. Remove ids you consider private.

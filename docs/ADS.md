# Paid media connectors: Google Ads, Meta Ads, ChatGPT Ads

Easy setup guide: [docs/SETUP-ADS.md](SETUP-ADS.md)

Nine read-only tools that put your ad spend next to your GA4, Search Console and Merchant Center data. They only read reports. They cannot create, edit, pause or delete a campaign, change a budget or upload anything.

> **Status: tested offline only.** Every behaviour on this page was checked against fake responses written from the platforms' documentation. The connectors have **not been run against real Google Ads, Meta or ChatGPT Ads accounts yet**. [Known unknowns](#what-has-not-been-tested-against-real-accounts) lists what to confirm on your first real query. Compare the first figures with each platform's own interface before you rely on them.

Looking for the overall install order, the access checklist to request from a client and the revocation steps? See [SETUP.md](SETUP.md).

## Contents

- [What you get](#what-you-get)
- [Read this first: attribution](#read-this-first-attribution)
- [Prerequisites: Google Ads](#prerequisites-google-ads)
- [Prerequisites: Meta Ads](#prerequisites-meta-ads)
- [Prerequisites: ChatGPT Ads](#prerequisites-chatgpt-ads)
- [Environment variables and commands](#environment-variables-and-commands)
- [Verify with doctor](#verify-with-doctor)
- [Tool reference](#tool-reference)
- [Free GAQL queries](#free-gaql-queries)
- [Structured errors](#structured-errors)
- [Corporate proxy](#corporate-proxy)
- [Quotas, access levels and limits](#quotas-access-levels-and-limits)
- [What has not been tested against real accounts](#what-has-not-been-tested-against-real-accounts)

## What you get

| Tool | Platform | What it answers |
|---|---|---|
| `gads_accounts` | Google Ads | Which customer accounts the token can read |
| `gads_performance` | Google Ads | Spend, clicks, conversions, value, ROAS by account, campaign or ad group |
| `gads_query` | Google Ads | Any read-only GAQL `SELECT` on reporting resources |
| `meta_ad_accounts` | Meta Ads | Which ad accounts the token can read |
| `meta_insights` | Meta Ads | Spend, reach, purchases, value, ROAS by account, campaign, ad set or ad |
| `openai_ads_account` | ChatGPT Ads | Connection test: the ad account behind the key |
| `openai_ads_insights` | ChatGPT Ads | Impressions, clicks, spend, conversions, attributed sales |
| `openai_ads_conversions` | ChatGPT Ads | Click-through and view-through goal conversions per entity |
| `ads_overview` | All three | The three platforms side by side for one period |

Each platform is optional. A platform you did not configure answers `not_configured`, and `ads_overview` still returns the others.

## Read this first: attribution

Every platform decides by itself which conversions it claims, in which window, and on which date. The same order can be counted by Google Ads, Meta and ChatGPT Ads at once, and GA4 counts it a fourth time under its own rules. So:

- **The sum of the platforms' conversions is not your number of orders.** It is not deduplicated and overstates real orders. `ads_overview` repeats this warning in every answer, word for word: *"Les conversions de chaque plateforme sont attribuées indépendamment et se recoupent. La somme n'est pas dédupliquée et surestime les commandes réelles."* (In English: each platform's conversions are attributed independently and overlap; the sum is not deduplicated and overstates real orders.)
- **Windows differ.** Meta defaults to 7-day click plus 1-day view. ChatGPT Ads is requested by this server with 30-day click plus 1-day view unless you change it. Google Ads uses the setting of each conversion action (model and lookback window are chosen per action in Google Ads), and `metrics.conversions` only counts actions included in "Conversions".
- **Dates differ.** Google Ads reports a conversion on the date of the ad interaction, not the date of the purchase. Other platforms may use the conversion date (ChatGPT Ads exposes `attribution_time_basis` for this). A short period can therefore disagree even when the long-run totals look similar.
- **Consent Mode.** When visitors decline cookies, platforms and GA4 may report modelled conversions. This server cannot see your consent rate.
- **GA4 has its own limits**: thresholds on small counts, data that can still change for the last days, a different attribution model. GA4 is the better common yardstick for sessions and revenue, not a ground truth for orders.
- **What this server cannot say**: whether an ad *caused* a sale (incrementality), your real number of orders (your shop's back office is not read), or your consent rate.

Each tool returns an `attribution` block with the windows it applied, so the assistant can quote them.

## Prerequisites: Google Ads

You need a Google Ads account you can read, and a Google Cloud project with an OAuth client. About 10 minutes if you already did [the Google Cloud setup](SETUP-GOOGLE-CLOUD.md).

1. **A Google Ads customer id.** The ten digits shown at the top right of Google Ads (`123-456-7890`; dashes are accepted). If you reach the account through a manager account (MCC), note the manager's id too.
2. **Enable the Google Ads API** in the Google Cloud project of your OAuth client (APIs & Services > Library > *Google Ads API*).
3. **An OAuth client of type Desktop app.** The one you created for the other tools works. Download its JSON file.
4. **Developer token: optional.** Google announced ([developer token page](https://developers.google.com/google-ads/api/docs/api-policy/developer-token)) that developer tokens were sunset on 9 September 2026: sending one is optional and ignored by the API servers, and access is tied to the Cloud project instead. If you still have a developer token, set `GADS_DEVELOPER_TOKEN` and the server sends it; if not, leave the variable unset and nothing is sent. This server cannot see which access level or quota your Cloud project has: check Google's current documentation if a call is refused (`not_available`, `auth_failed`).
5. **Authorize, with a separate token:**

   ```bash
   google-ecommerce-mcp setup-ads --client-secret path/to/client_secret.json
   # optional: --token-file path/to/ads-token.json  (or set GADS_TOKEN_FILE)
   ```

   Your browser opens the Google consent screen, which asks for one scope only: `https://www.googleapis.com/auth/adwords`. **That scope can write**, and Google has no read-only version of it. This is why the Google Ads token is stored apart: in the OS keyring entry `oauth-token-ads`, or in `GADS_TOKEN_FILE`. It never replaces the read-only token used by the other tools (`oauth-token`), and `setup-ads` refuses to overwrite an existing file that is not a Google Ads token.
6. **Set the ids** in the server `env`: `GADS_CUSTOMER_ID` (the account to read), and `GADS_LOGIN_CUSTOMER_ID` only if you reach it through a manager account.
7. **Check:** ask the assistant to run `gads_accounts`, then `gads_performance` for last week.

If your OAuth consent screen is in *Testing*, the token expires after 7 days (see [Troubleshooting](TROUBLESHOOTING.md#authentication)).

## Prerequisites: Meta Ads

You need a Meta Business Manager (Business Portfolio) that has access to the ad account. Use a **system user**, not your personal login, so the token does not depend on a person.

1. **Business Manager.** Business Settings must list the ad account under *Accounts > Ad accounts*.
2. **A Meta app of type Business** with the **Marketing API** product added, at developers.facebook.com. Meta may require extra steps (business verification, app review) depending on your account. This guide does not cover them and they were not tested.
3. **A system user** (Business Settings > Users > System users). Give it a **read / analyst role** on the ad account, nothing more: this server only reads insights.
4. **A token with only the `ads_read` permission.** Generate it for the system user and the app, and tick `ads_read` only. Do not add `ads_management`.
5. **Store the token, never in a URL:**

   ```bash
   google-ecommerce-mcp set-secret meta-access-token     # hidden prompt, stored in the OS keyring
   ```

   Or set `META_ACCESS_TOKEN` in the server `env`; the variable wins over the keyring. The token travels only in the `Authorization: Bearer` header. The server refuses any request that would put a token or an `access_token` / `method` parameter in a URL.
6. **Set the account:** `META_AD_ACCOUNT_ID`, digits or `act_` plus digits. `meta_ad_accounts` lists what the token can see.
7. **Check:** ask for `meta_ad_accounts`, then `meta_insights` for the last 7 days.

A system user token can be long-lived. Treat it like a password and revoke it in Business Settings when you stop using it.

## Prerequisites: ChatGPT Ads

1. **An advertiser API key** from your ChatGPT Ads account in Ads Manager (the OpenAI Ads Advertiser API). One key belongs to one ad account; to read several accounts, run several server instances.
2. **Store it:**

   ```bash
   google-ecommerce-mcp set-secret openai-ads-key        # hidden prompt, stored in the OS keyring
   ```

   Or set `OPENAI_ADS_API_KEY` in the server `env`; the variable wins over the keyring.
3. **Read-only scope is not confirmed.** OpenAI's documentation does not say that a key can be limited to reading. The key may be able to change campaigns and budgets. This server only sends the read requests listed below, but a leaked key is not limited by that. Use a dedicated key, keep it out of shared configs and revoke it when you stop using the connector.
4. The API host is fixed in the code (`https://api.ads.openai.com/v1`). It cannot be changed through the environment or a tool parameter, so the key cannot be sent anywhere else.
5. **Check:** ask for `openai_ads_account`, then `openai_ads_insights` for last week.

## Environment variables and commands

| Variable | Default | Used by |
|---|---|---|
| `GADS_CUSTOMER_ID` | none | Google Ads tools: the customer id to read |
| `GADS_LOGIN_CUSTOMER_ID` | none | Manager (MCC) id; sent as `login-customer-id` only when set |
| `GADS_DEVELOPER_TOKEN` | none | Optional; sent only when set |
| `GADS_TOKEN_FILE` | keyring entry `oauth-token-ads` | Store the Google Ads token in a file instead. Must differ from `GOOGLE_TOKEN_FILE` |
| `GADS_API_VERSION` | `v26` | Google Ads REST version, format `v26` |
| `GADS_ALLOW_SENSITIVE_QUERIES` | unset | `1` lifts the GAQL resource lists in `gads_query` (personal data possible) |
| `META_ACCESS_TOKEN` | keyring entry `meta-access-token` | Meta token (`ads_read` only) |
| `META_AD_ACCOUNT_ID` | none | Ad account, digits or `act_<digits>` |
| `META_API_VERSION` | `v26.0` | Graph API version, format `v26.0` |
| `OPENAI_ADS_API_KEY` | keyring entry `openai-ads-key` | ChatGPT Ads advertiser key |
| `ADS_TRUST_ENV` | unset | `1` makes the Ads connectors use `HTTPS_PROXY`, `NO_PROXY` and `REQUESTS_CA_BUNDLE` |

Settings are read at each call, not frozen at startup.

| Command | What it does |
|---|---|
| `google-ecommerce-mcp setup-ads --client-secret <file> [--token-file <file>]` | Google consent for the `adwords` scope only; stores the token apart from the other Google token |
| `google-ecommerce-mcp set-secret meta-access-token` | Hidden prompt, stores the Meta token in the OS keyring |
| `google-ecommerce-mcp set-secret openai-ads-key` | Hidden prompt, stores the ChatGPT Ads key in the OS keyring |
| `google-ecommerce-mcp check` | Also lists which Ads settings are present (booleans only, never a value) |

The secret is read from a hidden prompt, never from the command line, so it stays out of your shell history.

## Verify with doctor

After configuring a platform, check it before asking the assistant anything:

```bash
google-ecommerce-mcp doctor --only gads --live
google-ecommerce-mcp doctor --only meta --live
google-ecommerce-mcp doctor --only openai --live
```

It prints a row per service (Service, Set up, Secret, Token, Status, Account; ids masked as `123***890`), then "To fix" with the exact command for each problem. `doctor` reads this shell's environment and the server entry of your Claude Desktop config (`--config`, `--name`). `--live` makes one minimal read call (`gads_accounts`, `meta_ad_accounts`, `openai_ads_account`). Exit code `0` all fine, `1` a configured service fails, `2` nothing configured; `--json` prints no secret and masks ids (`123***890`). `setup-ads` and `set-secret` remind you of this command when they finish. Fixes by status: [Troubleshooting, Doctor says](TROUBLESHOOTING.md#doctor-says).

## Tool reference

All tools are read-only (`readOnlyHint`), return JSON, and return errors as data (see [Structured errors](#structured-errors)). Every result carries `fetched_at` and `freshness`. Money is in the account currency and is **never converted**.

### gads_accounts

Customer accounts the Google Ads token can read directly (`customers:listAccessibleCustomers`). No parameters. Result cached 5 minutes.

Returns `customers` (`[{id}]`), `configured_customer_id`, `login_customer_id`, `cached`.

### gads_performance

Performance by account, campaign or ad group, from fixed aggregated GAQL templates, highest spend first.

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `level` | `account` \| `campaign` \| `ad_group` | `campaign` | |
| `start_date` | str | none | **Required in practice**, `YYYY-MM-DD`; empty answers `invalid_dates` |
| `end_date` | str | none | **Required in practice**, `YYYY-MM-DD` |
| `segment` | `none` \| `date` \| `device` \| `network` | `none` | Splits each row |
| `limit` | int | `50` | Rows returned, 1 to 1000 |
| `customer_id` | str | `""` | Empty uses `GADS_CUSTOMER_ID` |

Returns `rows` (spend, impressions, clicks, conversions, conversion_value, ctr, cpc, cpa, roas), `row_count`, `truncated`, `totals`, `totals_complete`, `currency`, `date_range` (account timezone, with `data_complete`), `metrics` (unit and definition of each), `attribution`. `totals_complete` is `false` when more than 10,000 rows exist: totals then cover the 10,000 highest-spend rows and `totals_note` says so. The total ROAS is the sum of values divided by the sum of spends, never the mean of row ROAS.

### gads_query

One free read-only GAQL `SELECT`, for fields the fixed templates do not cover. Rules in [Free GAQL queries](#free-gaql-queries).

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `gaql` | str | required | One `SELECT` statement |
| `customer_id` | str | `""` | Empty uses `GADS_CUSTOMER_ID` |
| `limit` | int | `100` | Rows returned, 1 to 1000 |

Returns `rows` (flat GAQL field names such as `campaign.name`, `metrics.cost`), `row_count`, `truncated`, `read_complete`, `batches`, `units_note`, `attribution`. When the server added or lowered a `LIMIT`, `query_sent` and `limit_note` show what was actually sent. Micros fields are converted to currency units: `*_micros` is renamed without the suffix (`metrics.cost_micros` becomes `metrics.cost`).

### meta_ad_accounts

Ad accounts the token can read. No parameters. Cached 5 minutes.

Returns `accounts` (`id`, `account_id`, `name`, `account_status`, `currency`, `timezone_name`, plus a readable `account_status_label`), `count`, `complete`, `configured_account_id`, `api_version`, `cached`.

### meta_insights

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `level` | `account` \| `campaign` \| `adset` \| `ad` | `account` | |
| `start_date`, `end_date` | str | `""` | `YYYY-MM-DD`, both or neither |
| `date_preset` | str | `""` | `today`, `yesterday`, `this_month`, `last_month`, `last_7d`, `last_28d`, `last_90d`... Exclusive with the dates. All three empty means `last_28d` |
| `breakdowns` | list | `[]` | `age`, `gender`, `country`, `region`, `publisher_platform`, `platform_position`, `device_platform`, `impression_device` |
| `attribution_windows` | list | `[]` | `1d_click`, `7d_click`, `28d_click`, `1d_view`, `1d_ev`. Empty means Meta's default (7-day click + 1-day view) |
| `time_increment` | `all_days` \| `1` | `all_days` | `1` gives one row per day |
| `limit` | int | `100` | Rows returned, 1 to 500 |
| `account_id` | str | `""` | Empty uses `META_AD_ACCOUNT_ID` |

Returns `rows` (spend, impressions, clicks, reach, frequency, ctr as a ratio 0 to 1, cpc, cpm, purchases, purchase_value, roas, cpa), `totals`, `totals_complete`, `currency`, `date_range`, `attribution`, `purchase_measurement` (`measured`, `note`), `rows_order` (spend descending, also for daily rows), `truncated`, `rows_omitted`.

Rules worth knowing:

- Purchases are read from the `omni_purchase` action, with `purchase` as fallback (`purchase_action_type_used` says which).
- The total ROAS is the sum of purchase values divided by the sum of spends.
- **With several attribution windows**, the windows overlap, so `totals` purchases, value and ROAS are `null`; read `purchases_by_window`, `purchase_value_by_window` and `roas_by_window`. They are never added together.
- **Without any purchase event** in the data (pixel or Conversions API not sending purchases), purchases, value and ROAS are `null` and `purchase_measurement.note` explains it. A missing measurement is not a zero.
- Meta's own `purchase_roas` is kept raw in `meta_reported_roas`.
- Pagination follows `paging.cursors.after` only (at most 5 pages of 500 rows). The `paging.next` URL Meta returns is never requested. If more rows exist, `totals_complete` is `false`.

### openai_ads_account

Connection test: the ad account behind the key. No parameters. Cached 5 minutes.

Returns `ad_account` (`id`, `name`, `status`, `currency`, `timezone`), `raw_fields_dropped` (every other field the API returned is dropped and only listed by name), `cached`.

### openai_ads_insights

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `start_date`, `end_date` | str | required | `YYYY-MM-DD`, end inclusive |
| `level` | `account` \| `campaign` \| `ad_group` \| `ad` | `account` | |
| `entity_id` | str | `""` | Required for campaign, ad_group, ad (letters, digits, `_`, `-`) |
| `timezone` | str | `""` | Empty uses the ad account timezone. Keep it empty for conversion and sales metrics |
| `time_granularity` | `none` \| `daily` \| `monthly` \| `hourly` | `none` | `hourly`: about 30 days of history, not with a segment |
| `aggregation_level` | `""` \| `ad_account` \| `campaign` \| `ad_group` \| `ad` | `""` | Row entity inside the endpoint scope |
| `fields` | list | default metrics | Canonical names (`impressions`, `clicks`, `spend`, `ctr`, `cpc`, `cpm`, `conversions`, `cpa`, `post_click_cvr`, `order_created_attributed_sales`, `order_created_roas`, `campaign.id`...). 1 to 50 |
| `segment` | `""` \| `product` \| `country` \| `device` \| `platform` | `""` | At most one |
| `filters` | list | none | `{"field", "operator", "value"}`, `IN`, `GREATER_THAN`, `LESS_THAN`; at most 10 |
| `sort` | list | none | `{"field", "direction"}`; at most 5 |
| `include_zero_impression_items` | bool | `false` | Not with a segment |
| `attribution_window_days` | `7` \| `14` \| `30` | `30` | Click window |
| `view_through_attribution_window_days` | `0` \| `1` | `1` | View window |
| `attribution_time_basis` | `""` \| `ad_event_time` \| `conversion_time` | `""` | Empty sends nothing (API default) |
| `page_size` | int | `500` | 1 to 2000 |
| `max_rows` | int | `1000` | 1 to 6000, over at most 5 pages |
| `after`, `before` | str | `""` | Cursor: `next_after` / `first_id` of a previous call |

The API's array parameters are sent in the bracket form of its documentation (`fields[]`, `time_ranges[]`, `filters[]`, `sort[]`, `segments[]`).

Returns `rows`, `has_more`, `next_after`, `stop_reason`, `complete`, `totals` (recomputed here: sums for counts and sales, ratios recomputed from the sums), `currency`, `units`, `date_range`, `attribution`, `null_means`. `spend`, `cpc` and `cpm` are decimals in **major units** of the account currency (12.50 means 12.50, not cents). `ctr` is a ratio. **`null` means "not computable", not zero** (`null_means` repeats it). The currency comes only from the data (`order_created_attributed_sales_currency`); when absent it is `null` with a note, never guessed.

### openai_ads_conversions

Goal conversions per entity, through `POST /v1/conversions/insights` (a read query).

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `start_date`, `end_date` | str | required | Whole days, 365 days at most |
| `account_timezone` | str | required | IANA name, e.g. `Europe/Paris`; the API counts whole days in it |
| `aggregation_level` | `campaign` \| `ad_group` \| `ad` | required | |
| `entity_ids` | list | none | 1 to 100 ids. Required when `group_by_entity` is true; empty when false |
| `group_by_entity` | bool | `true` | `false`: one row for the whole account |
| `time_granularity` | `none` \| `daily` | `none` | |
| `breakdown` | `""` \| `country` \| `device` | `""` | |
| `attribution_window_days` | `7` \| `14` \| `30` | `30` | |
| `view_through_attribution_window_days` | `0` \| `1` | `1` | |
| `attribution_time_basis` | str | `""` | |
| `include_attributed_events` | bool | `false` | Adds per-event aggregates. They can carry order or user identifiers: only documented aggregate fields are kept and a `warning` is added |
| `event_names` | list | none | Needs `include_attributed_events`. Stricter than the API: 20 names of 64 characters (letters, digits, `_ . : -`) |
| `include_zero_rows` | bool | `true` | |

Returns `rows`, `totals`, `currency` (the API's `account_currency`), `unix_range` (end exclusive), `attribution`, `date_range`, `null_means`. There is no pagination: more than 2000 rows answers `too_many_rows`.

### ads_overview

| Parameter | Type | Default |
|---|---|---|
| `start_date`, `end_date` | str | required, `YYYY-MM-DD` |

Calls the account-level summary of each configured platform and puts them side by side. It sends no request of its own.

Returns:

- `warning`: the attribution warning quoted [above](#read-this-first-attribution), always present;
- `platforms`: `google_ads`, `meta`, `openai_ads`, each with `status` (`ok`, `error`, `not_configured`), spend, impressions, clicks, conversions, conversion_value, `roas_reported_by_platform`, currency, attribution, date_range, totals_complete;
- `attribution_side_by_side`: the three attribution settings together;
- `combined`: spends added **only if every platform that returned data reports the same known currency** (no conversion, ever); otherwise `null` with `combined_reason`. It also carries conversions and values as `..._sum_not_deduplicated`, and a ROAS equal to total value divided by total spend, never the mean of the platforms' ROAS;
- `partial`, `not_configured`, `errors`, `platforms_without_activity`, `notes`.

A platform without activity (spend 0, nothing converted) adds zeros and does not need a currency. A failing platform gets its own error block and does not hide the others.

`ads_overview` does **not** compute a marketing efficiency ratio (revenue over total ad spend). To compare the platforms' claimed value with your revenue, ask the assistant for the GA4 figures too (`ga4_report`) and compare them yourself.

## Free GAQL queries

`gads_query` accepts one Google Ads Query Language statement and checks it **before any network call**. A refused query is never sent.

Accepted shape: `SELECT <fields> FROM <resource> [WHERE ...] [ORDER BY ...] [LIMIT n]`, starting with `SELECT` (any case, leading blanks and newlines allowed).

Refused (`invalid_gaql`):

- `;`, `--`, `/*`, `*/` and `\`, **even inside a string literal**. A harmless-looking literal can never hide a second statement or a comment;
- anything not starting with `SELECT`, empty queries, a second `SELECT` or `FROM` (no subquery), `JOIN`, a `LIMIT` that is not a whole number or appears twice;
- an unbalanced quote, a NUL or control character, and non-ASCII characters outside string literals.

The read is bounded: with no `LIMIT`, `LIMIT 10001` is added (before `PARAMETERS` if present); a larger `LIMIT` is lowered to 10001. `limit` (1 to 1000) then trims the rows returned, and `read_complete` is `false` if more than 10,000 rows matched. The answer is capped at 20 MB (`response_too_large`).

**Resource allow-list (`resource_not_allowed`).** `FROM` and every dotted name prefix in `SELECT`, `WHERE` and `ORDER BY` must be a reporting resource, or `metrics` / `segments`:

`customer`, `campaign`, `campaign_budget`, `ad_group`, `ad_group_ad`, `ad_group_criterion`, `keyword_view`, `shopping_performance_view`, `product_group_view`, `asset_group`, `asset_group_listing_group_filter`, `asset_group_asset`, `asset`, `ad_group_ad_asset_view`, `campaign_criterion`, `geographic_view`, `gender_view`, `age_range_view`, `landing_page_view`, `expanded_landing_page_view`, `display_keyword_view`, `topic_view`, `bidding_strategy`, `conversion_action`, `location_view`, `distance_view`.

`customer.*` is limited to `id`, `descriptive_name`, `currency_code`, `time_zone`, `status`, `manager`, `test_account`, `resource_name`.

**Personal-data names (`sensitive_resource`).** A second check refuses, anywhere outside a literal, names that can expose personal data or billing: `change_event`, `click_view`, `search_term_view` and the other search-term views, `user_list`, `user_*`, `customer_user_access`, `customer_client_link`, lead forms and local services leads, `call_view`, invoices, `billing_*`, `payments_*`, account budgets and links, and the placement views.

`GADS_ALLOW_SENSITIVE_QUERIES=1` lifts both lists. Do it only if you accept that personal data can appear in the conversation, and therefore go to your AI provider. The syntax rules above, the `LIMIT` bound and the read-only allow-list stay in force.

There is no `:mutate` endpoint on the allow-list, so even a crafted query cannot change anything.

## Structured errors

A failing tool returns an object with an `error` code, a `detail` (secrets masked) and a `fix`. Most carry `platform`.

| `error` | Meaning | What to do |
|---|---|---|
| `not_configured` | A required variable or secret is missing (`detail` names it) | Set it in the server `env`, or use `set-secret` / `setup-ads` |
| `auth_failed` | Credentials rejected or expired (HTTP 401, or Meta codes 102/190/463/467; a missing or unreadable Google Ads token also lands here) | Create a new Meta token, a new ChatGPT Ads key, or rerun `setup-ads` |
| `missing_scope` | The token lacks a permission; Google Ads adds `required_scope`, Meta adds `required_permission` (`ads_read`) | Rerun `setup-ads`, or give `ads_read` and the ad account to the Meta token |
| `rate_limited` | Platform quota hit. Carries `retry_after_seconds` when known and, for Meta, the parsed `usage` headers | Wait, or shorten the period and lower `limit`. No automatic retry is made |
| `not_available` | API disabled, unknown account or version (HTTP 404/501, `SERVICE_DISABLED`), or the endpoint is not offered to this account | Enable the Google Ads API in Cloud Console; check the account id and `GADS_API_VERSION` / `META_API_VERSION` |
| `blocked` | The request is outside the platform's read-only allow-list, would carry a secret or a `method` parameter in the URL, or the platform answered with a redirect (never followed) | Should not happen with these tools; report it with the `detail` |
| `network` | Transport failure, including a missing CA bundle under `ADS_TRUST_ENV=1` | Check the connection; behind a proxy set `ADS_TRUST_ENV=1` |
| `response_too_large` | The answer exceeded the 20 MB ceiling and was not loaded | Shorten the period, lower `LIMIT`, select fewer fields |
| `too_many_rows` | ChatGPT Ads answered 413: more than 2000 rows, no partial result | Fewer `entity_ids`, a shorter period, or fewer `event_names` |
| `resource_not_allowed` | `gads_query`: a resource or `customer.*` field is not on the reporting allow-list | Use an allowed resource, or `gads_performance` |
| `sensitive_resource` | `gads_query`: the query names a personal-data or billing resource | Use aggregated fields; `GADS_ALLOW_SENSITIVE_QUERIES=1` only if you accept the risk |
| `invalid_*` | A parameter was refused before any request: `invalid_gaql`, `invalid_customer_id`, `invalid_dates`, `invalid_level`, `invalid_segment`, `invalid_api_version`, `invalid_argument`, `invalid_entity_id`, `invalid_timezone`, `invalid_attribution`, `invalid_fields`, `invalid_filters`, `invalid_sort`, `invalid_cursor`, `invalid_limit`... | Read `detail` and `fix`, correct the parameter |
| `api_error` | The platform refused or failed (other 4xx, 5xx) | Read `detail`; retry later if it is a 5xx |
| `internal_error` | Unexpected failure inside a connector, reported as data with secrets masked | Retry; report it if it persists |

More fixes: [Troubleshooting](TROUBLESHOOTING.md#paid-media-errors).

## Corporate proxy

By default the Ads connectors ignore the environment: no `HTTPS_PROXY`, no `REQUESTS_CA_BUNDLE`, no `.netrc`. Behind a company proxy, set `ADS_TRUST_ENV=1` in the server `env`. The connectors then use the environment's proxy (honouring `NO_PROXY`) and CA bundle, and nothing else: `.netrc` credentials stay ignored and redirects stay refused. The value must be exactly `1`. If `REQUESTS_CA_BUNDLE` points to a missing file, you get `network` with a fix, not a crash.

## Quotas, access levels and limits

This server reads what the platforms allow; it adds its own ceilings so an answer stays small and bounded:

| Limit | Value |
|---|---|
| Rows read by one Google Ads query | 10,000 (the query asks for 10,001 to detect more) |
| Rows returned to the assistant | 1,000 (Google Ads), 500 (Meta), `max_rows` up to 6,000 (ChatGPT Ads) |
| Meta pages | 5 pages of 500 rows |
| ChatGPT Ads pages | 5 pages, `page_size` up to 2,000 |
| ChatGPT Ads conversions | 365 days, 100 `entity_ids`, 2,000 rows, no pagination |
| Response body | 20 MB |
| Discovery cache | account lists and account info, 5 minutes |

Platform quotas apply on top and are not visible to this server in advance: Google Ads access level and request quota (tied to your Cloud project and, if you use one, your developer token), Meta's business-use-case and ad-account rate limits (the `usage` fields of a `rate_limited` answer show what Meta reports), and OpenAI's limits for the Advertiser API. A quota hit is returned as `rate_limited` and is never retried automatically.

## What has not been tested against real accounts

All tests run offline against fake responses written from each platform's documentation. Nothing here has been run against a live Google Ads, Meta or ChatGPT Ads account yet. In particular, to confirm on a first real query:

- **ChatGPT Ads, `metadata.timezone` with `time_granularity=none`.** The connector requests `metadata.timezone` to report the timezone of the days. If the API does not return it in that mode, `timezone_reported` is `false` and `date_range.timezone_source` says the timezone comes from the request, or is the ad account's.
- **Meta attribution windows.** The tool offers `1d_click`, `7d_click`, `28d_click`, `1d_view` and `1d_ev`. How Meta answers each combination, including whether longer view windows such as `7d_view` and `28d_view` exist for your account, is not confirmed (the tool does not offer them).
- **OpenAI key scope.** Whether a ChatGPT Ads key can be limited to reading is not confirmed. Assume it cannot.
- **ChatGPT Ads currency.** It is taken from the data (`order_created_attributed_sales_currency`, or `account_currency` for conversions). If a response carries neither, the currency is `null`, and `ads_overview` will not add that platform's spend to the others.
- Real rate-limit headers and error bodies, the exact API versions (`v26`, `v26.0`) available to your accounts, and Meta business verification or app review requirements.

Other known limits:

- One ad account per platform per server instance (`GADS_CUSTOMER_ID`, `META_AD_ACCOUNT_ID`; a ChatGPT Ads key is one account). `customer_id` and `account_id` parameters let you query another account the token can read. Run several instances for several unrelated businesses.
- Currencies are never converted; `ads_overview` refuses to add spends across currencies.
- Platform data lags: the most recent days and conversions still inside their attribution window can change. Each answer carries `date_range.data_complete` or an equivalent settling note.
- The tools report what the platforms say. They do not read your shop's back office, so they cannot tell you real orders, margins or returns.

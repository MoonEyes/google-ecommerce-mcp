# Tool reference

Thirteen tools, all read-only. Every tool returns JSON. On failure a tool returns an object with an `error` key instead of raising:

| `error` value | Meaning | Fix |
|---|---|---|
| `not_configured` | The environment variable for this service is not set | Add it to the server `env` in your client config |
| `not_authenticated` | No token stored | Run `google-ecommerce-mcp setup --client-secret ...` |
| `network` | Google could not be reached | Check connectivity or proxy |
| `missing_scope` | The token lacks the scope this API needs; `required_scope` names it | Rerun `setup` (with `--with-merchant` or `--with-indexing` for those services) |
| `api_disabled` | The Google Cloud API named in `api` is not enabled for your OAuth client's project | Enable it in APIs & Services > Library |
| `invalid_property_id` | `property_id` is not a numeric GA4 property id | Use an id from `ga4_properties` |
| `rate_limited` | Google quota hit; `retry_after_seconds` when Google sends it | Wait, or narrow the date range or `limit` |
| `blocked` | The request is not on the read-only allow-list and was not sent | Should never happen; open an issue |
| an HTTP status (`403`, `404`, ...) | Google refused the call; `detail` holds Google's message | See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) |

Row-returning tools cap `limit` at 1,000.

Every result also carries `fetched_at` (UTC time of the read) and `freshness` (how far behind Google's data is, e.g. Search Console lags about 2 days).

Report tools (`ga4_report`, `ga4_realtime`, `gsc_performance`) also return:

- `totals` over every matching row, not just the rows returned, plus `truncated` when more rows exist;
- `metrics`: the unit (`count`, `ratio 0 to 1`, `seconds`, the property currency...) and a short definition of each metric;
- `date_range`: `start` and `end` as calendar dates in the data's `timezone` (GA4 property timezone, Pacific Time for Search Console), the `requested` values when they were relative (`7daysAgo`), and `data_complete`. When it is `false`, `settling_from` is the first day Google can still revise: do not compare those days with complete ones. When some sub-requests fail, `merchant_product_issues` and `gtm_inventory` return what they could read with `partial: true` and the failing part in `partial_error` / `partial_errors`.

## Contents

- [server_status](#server_status)
- GA4: [ga4_report](#ga4_report), [ga4_realtime](#ga4_realtime), [ga4_properties](#ga4_properties)
- Search Console: [gsc_performance](#gsc_performance), [gsc_inspect_url](#gsc_inspect_url), [gsc_sitemaps](#gsc_sitemaps)
- Merchant Center: [merchant_data_sources](#merchant_data_sources), [merchant_product_issues](#merchant_product_issues), [merchant_report_query](#merchant_report_query)
- Tag Manager: [gtm_inventory](#gtm_inventory)
- [indexing_status](#indexing_status), [pagespeed](#pagespeed)

---

## server_status

Which services are configured and whether the stored token works. No parameters. Same output as `google-ecommerce-mcp check`.

```json
{"ga4": true, "search_console": true, "merchant_center": true, "tag_manager": false,
 "pagespeed_api_key": false, "token_storage": "os-keyring", "token": "ok", "version": "0.1.0"}
```

Ask: *"Is the Google MCP server working?"*

---

## ga4_report

GA4 Data API `runReport`. Needs `GA4_PROPERTY_ID`.

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `dimensions` | list of str | `["sessionDefaultChannelGroup"]` | Any [GA4 API dimension](https://developers.google.com/analytics/devguides/reporting/data/v1/api-schema): `landingPage`, `sessionSource`, `yearMonth`, `itemName`... |
| `metrics` | list of str | `["sessions", "totalUsers"]` | `ecommercePurchases`, `purchaseRevenue`, `keyEvents`, `engagementRate`... |
| `start_date` | str | `28daysAgo` | `YYYY-MM-DD`, `NdaysAgo`, `yesterday`, `today` |
| `end_date` | str | `yesterday` | same formats |
| `limit` | int | `50` | max 1,000 |
| `channel_group` | str | `""` | exact filter on `sessionDefaultChannelGroup`, e.g. `Organic Search` |
| `property_id` | str | `""` | numeric property id from `ga4_properties`; empty uses `GA4_PROPERTY_ID` |

```json
{"rows": [{"sessionDefaultChannelGroup": "Organic Search", "sessions": 412, "purchaseRevenue": 1830.5}],
 "row_count": 6, "truncated": false, "totals": {"sessions": 1290, "purchaseRevenue": 5120.0},
 "metrics": {"sessions": {"unit": "count", "definition": "All sessions started on the site, engaged or not."},
             "purchaseRevenue": {"unit": "EUR", "definition": "Revenue from purchase events only, before refunds."}},
 "currency": "EUR", "property_id": "123456789",
 "date_range": {"start": "2026-09-07", "end": "2026-10-04", "timezone": "Europe/Paris",
                "requested": {"start": "28daysAgo", "end": "yesterday"}, "data_complete": false,
                "settling_from": "2026-10-04", "settling_note": "GA4 can still revise the last 24 to 48 hours; compare complete days only"}}
```

Ask: *"Top 10 landing pages from organic search last month, with purchases and revenue."*

---

## ga4_realtime

GA4 realtime report, last 30 minutes. Needs `GA4_PROPERTY_ID`.

| Parameter | Type | Default |
|---|---|---|
| `dimensions` | list of str | `["unifiedScreenName"]` |
| `metrics` | list of str | `["activeUsers", "screenPageViews"]` |
| `property_id` | str | `""` (configured property) |

Useful to check a tracking change: open one page in a private window and verify `screenPageViews` moves by 1, not 2.

Ask: *"I just opened the home page once. How many page views does GA4 see right now?"*

---

## ga4_properties

Every GA4 account and property the authorized Google account can read (Admin API `accountSummaries`). Lets the assistant find a property id itself instead of you pasting it, then pass it as `property_id`. Same `analytics.readonly` scope; enable the *Google Analytics Admin API* in your Cloud project.

| Parameter | Type | Default |
|---|---|---|
| `max_pages` | int | `5` (200 accounts per page) |

```json
{"properties": [{"property_id": "123456789", "property_name": "Shop FR", "property_type": "PROPERTY_TYPE_ORDINARY",
                 "account_id": "1000", "account_name": "MoonEyes", "configured": true}],
 "configured_property_id": "123456789", "truncated": false}
```

Ask: *"Which GA4 properties can you see? Compare last month's sessions of the shop and the blog."*

---

## gsc_performance

Search Console Search Analytics. Needs `GSC_SITE_URL`.

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `dimensions` | list of str | `["query"]` | `query`, `page`, `country`, `device`, `date`, `searchAppearance` |
| `start_date` | str | 30 days ago | `YYYY-MM-DD` |
| `end_date` | str | 2 days ago | Search Console data lags about 2 days |
| `limit` | int | `50` | max 1,000 |
| `page_contains` | str | `""` | keep only pages whose URL contains this text |

```json
{"rows": [{"query": "wargame terrain", "clicks": 14, "impressions": 820, "ctr": 0.0171, "position": 8.4}],
 "truncated": true, "totals": {"clicks": 230, "impressions": 15400, "ctr": 0.0149, "position": 14.2},
 "totals_note": "totals include anonymized queries, so they exceed the sum of query rows",
 "metrics": {"ctr": {"unit": "ratio 0 to 1", "definition": "clicks / impressions (0.05 means 5 %)."}, "...": "..."},
 "date_range": {"start": "2026-09-04", "end": "2026-10-02", "timezone": "America/Los_Angeles", "data_complete": true}}
```

`totals` come from a second query with the same filter and no split, so they are the real property totals.

Ask: *"Queries where we rank between 5 and 15 with more than 100 impressions: quick wins?"*

---

## gsc_inspect_url

URL Inspection API: is one URL indexed, and which canonical did Google choose. Needs `GSC_SITE_URL`; the URL must belong to that property.

| Parameter | Type | Required |
|---|---|---|
| `url` | str | yes |

Returns Google's `indexStatusResult`: `verdict`, `coverageState`, `googleCanonical`, `userCanonical`, `lastCrawlTime`, `robotsTxtState`, `pageFetchState`...

Google allows 2,000 inspections per day per property.

Ask: *"Is https://www.example.com/product/x indexed, and does Google agree with our canonical?"*

---

## gsc_sitemaps

Sitemaps declared in Search Console with last submission, last download, errors and warnings. No parameters. Needs `GSC_SITE_URL`.

Ask: *"When did Google last download our sitemap, and does it report errors?"*

---

## merchant_data_sources

Merchant API data sources (feeds): feed labels, countries, content language, fetch schedule and URL. No parameters. Needs `MERCHANT_ACCOUNT_ID`.

Ask: *"Which product feeds do we have in Merchant Center and where are they fetched from?"*

---

## merchant_product_issues

Products with item-level issues (disapprovals, demotions, warnings) per destination. Needs `MERCHANT_ACCOUNT_ID`.

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `limit` | int | `50` | products returned, max 1,000 |
| `only_with_issues` | bool | `true` | `false` lists every product |
| `max_pages` | int | `20` | 250 products per page, so 5,000 products by default |

```json
{"products": [{"offer_id": "1234", "feed_label": "FR", "title": "Ruined tower", "link": "https://...",
  "issues": [{"code": "image_link_broken", "severity": "DISAPPROVED", "description": "...",
              "attribute": "image_link", "destination": "SHOPPING_ADS"}]}],
 "scanned": 312, "truncated": false}
```

`truncated: true` means more matching products exist: raise `limit` or `max_pages`.

Ask: *"Which products are disapproved for Shopping ads, grouped by issue?"*

---

## merchant_report_query

Free Merchant Center Query Language (MCQL) report. Needs `MERCHANT_ACCOUNT_ID`.

| Parameter | Type | Default |
|---|---|---|
| `query` | str | `SELECT id, offer_id, title, aggregated_reporting_context_status FROM product_view LIMIT 50` |

Queries on `product_view` must select `id`. Examples:

```sql
SELECT offer_id, title, clicks, impressions, click_through_rate
FROM product_performance_view
WHERE date BETWEEN '2026-09-01' AND '2026-09-30'
ORDER BY clicks DESC LIMIT 20
```

```sql
SELECT id, offer_id, title, price, availability, item_issues
FROM product_view WHERE aggregated_reporting_context_status = 'NOT_ELIGIBLE_OR_DISAPPROVED'
```

See Google's [MCQL reference](https://developers.google.com/merchant/api/guides/reports/query-language) for tables and fields.

Ask: *"Our 20 products with the most free-listing clicks last month."*

---

## gtm_inventory

Tags, triggers and variables of the container's default workspace, plus the live version id. No parameters. Needs `GTM_CONTAINER_ID` (the public `GTM-XXXXXXX` id).

Built-in trigger ids are translated (`2147479553` becomes `All Pages (built-in)`).

```json
{"container": "GTM-XXXXXXX", "workspace": "Default Workspace",
 "tags": [{"name": "GA4 config", "type": "googtag", "paused": false,
           "firing_triggers": ["All Pages (built-in)"], "parameters": {"tagId": "G-XXXX"}}],
 "triggers": ["All Pages (built-in)", "purchase"], "variables": ["Page URL"],
 "live_version": {"id": "12", "name": "v12"}}
```

The workspace may hold unpublished changes; `live_version` tells you what visitors actually get.

Ask: *"List our GTM tags and their triggers. Is GA4 configured twice?"*

---

## indexing_status

Indexing API notification metadata for a URL: the last `URL_UPDATED` / `URL_DELETED` notifications Google holds. Read only: nothing is submitted.

| Parameter | Type | Required |
|---|---|---|
| `url` | str | yes |

A URL never notified returns `{"status": "no Indexing API notification has been sent for this URL"}`.

Note: Google officially supports the Indexing API only for job-posting and livestream pages.

---

## pagespeed

PageSpeed Insights (Lighthouse) performance and SEO scores plus Core Web Vitals. Uses `PAGESPEED_API_KEY` when set; no OAuth needed.

| Parameter | Type | Default |
|---|---|---|
| `url` | str | required |
| `strategy` | str | `mobile` (or `desktop`) |

```json
{"performance": 61, "seo": 92, "lcp": "3.9 s", "cls": "0.02", "tbt": "380 ms", "field_data": "AVERAGE"}
```

`field_data` is the Chrome UX Report category when Google has enough real-user data, otherwise `null`.

Ask: *"Mobile PageSpeed of our home page and three best-selling product pages."*

# Client reporting guide

How to build a monthly report for an online shop from this server's tools. **Status: delivered.** The `google-ecommerce-mcp report` command turns the tools' JSON results into one static HTML file in the client's brand, printable to PDF (see [The report file](#the-report-file)). Like the paid media connectors, it has been tested on sample data only, not on real client accounts yet.

Written by MoonEyes. Grounded on a review of the sources listed at the end (consulted 16 October 2026). Statements that come from agency blogs or secondary sources are marked **[secondary source]**. Agency cadences and habits are marked **[agency practice, not verified]**.

## Contents

- [Goal](#goal)
- [The 10 sections](#the-10-sections)
- [The 10 KPIs and their formulas](#the-10-kpis-and-their-formulas)
- [10 mistakes to avoid](#10-mistakes-to-avoid)
- [Small shops (under 500 orders a month)](#small-shops-under-500-orders-a-month)
- [Cadence](#cadence)
- [GDPR](#gdpr)
- [What the tools do not do yet](#what-the-tools-do-not-do-yet)
- [The report file](#the-report-file): [how to produce a report](#how-to-produce-a-report), [input contract](#input-contract), [reading the colours](#reading-the-colours), [brand](#the-clients-brand), [privacy](#privacy-of-the-file)
- [Sources](#sources)

## Goal

A monthly report for one shop that is:

- **read-only**: it only reads; it never changes an account, a feed or a tag;
- **aggregate**: totals and breakdowns, no individual customer, order or e-mail;
- **in the client's brand**: the client's name, logo and accent colour, not the author's;
- **decision-oriented**: every number is followed by a cause and a decision, with a stated confidence level.

The assistant pulls the figures with the tools, compares them (the tools do not compare periods by themselves), and writes the narrative. You supply what the tools cannot read: targets, costs, promotions, stock-outs, tracking changes (see [What the tools do not do yet](#what-the-tools-do-not-do-yet)).

## The 10 sections

### 1. Executive summary

Three messages, each with **the figure + the cause + the decision + a confidence level** (high, medium, low). Then targets vs. actual.

- Confidence: *high* when the figure is read from one complete source over complete days; *medium* when two sources roughly agree or part of the period is still settling; *low* when it rests on small counts, modelled data or an untested tracking setup.
- Targets come from the client. Without a target, say "no target provided" instead of inventing one.
- Tools: none of its own; it summarises sections 2 to 9.

### 2. Headline KPIs

Revenue, orders, average order value, conversion rate, new vs. returning customers, and gross margin **only if the client provides costs**. Each compared with the previous month, with the same month last year (N-1), and with the target.

- Tool: `ga4_report` (metrics such as `purchaseRevenue`, `ecommercePurchases`, `sessions`; two calls, one per period).
- The revenue of reference is the shop's back office, not GA4 (see [Small shops](#small-shops-under-500-orders-a-month)). Say which source each figure comes from.

### 3. Funnel

Sessions, `view_item`, `add_to_cart`, `begin_checkout`, `purchase`, with the drop between steps.

- Tool: `ga4_report` with `eventName` as the dimension and `eventCount` as the metric. The answer lists every event: raise `limit` and read the five events from the rows.
- Read the biggest drop first, and ask what changed on that step during the month (section 10 log).

### 4. Channels

Sessions, revenue and conversion rate per channel, and **the attribution model stated** (name the model in use for the GA4 property and say that the paid platforms use their own).

- Tool: `ga4_report` with `sessionDefaultChannelGroup` (or `sessionSourceMedium`).

### 5. Products and categories

Top and flop products by revenue and units; stock-outs that coincide with drops.

- Tool: `ga4_report` with `itemName` (and `itemCategory`) and `itemRevenue`, `itemsPurchased`, `itemsViewed`.
- Stock-outs are not in GA4: the client supplies the dates, or you read them from the back office.

### 6. Organic search (SEO)

Clicks, impressions, CTR and average position, by query and by page; queries ranking 5 to 15 with real impressions; pages with impressions and no clicks.

- Tool: `gsc_performance` (`dimensions: ["query"]`, `["page"]`, `["date"]`). Use the returned `totals`, not the sum of the rows: anonymised queries are in the total but not in the rows.
- Search Console data lags about 2 days; the answer's `date_range.data_complete` says so.

### 7. Shopping (Merchant Center)

Products disapproved or with warnings, by issue; free-listing clicks per product.

- Tools: `merchant_product_issues`, `merchant_report_query` (MCQL on `product_performance_view`).

### 8. Paid media

1. **MER (marketing efficiency ratio)** = total revenue / total ad spend, for the month. **Computed by the assistant**, with revenue from the back office or GA4 (stated), and spend as the sum of the platforms' spends **only if they share one currency**. The MCP tools do not compute it. The `report` command does, from GA4 revenue and the `ads_overview` combined spend, only in one currency; for back-office revenue, compute it yourself.
2. **One sub-section per platform**, each read on its own terms. Never add the platforms' conversions or ROAS together.

**Google Ads** (`gads_performance`): spend, clicks, CTR, CPC, conversions, conversion value, ROAS. Impression share and Shopping product reports are **not packaged**: use `gads_query` with the [templates in PROMPTS.md](../examples/PROMPTS.md#gaql-templates-for-gads_query). Attribution: set per conversion action in Google Ads; conversions are dated by the ad interaction.

**Meta Ads** (`meta_insights`): spend, reach, frequency, CPM, CPC, purchases, purchase value, ROAS, and the attribution windows used (default 7-day click + 1-day view; read `attribution`). Creative fatigue: at `level=ad` with `time_increment="1"`, look for CTR falling while frequency rises. Note: `reach` and `frequency` totals are `null` when several rows are returned (reach is not additive); read them per row. Without a purchase event the tool returns `null` with a note: not measured is not zero.

**ChatGPT Ads** (`openai_ads_insights`, `openai_ads_conversions`): spend, CTR, CPC, conversions, CPA, `post_click_cvr`, `order_created_attributed_sales`, `order_created_roas`. Attribution requested by default: 30-day click + 1-day view. `null` means "not computable", not zero. The `product.carousel_product_card_*` fields are to be verified (see [gaps](#what-the-tools-do-not-do-yet)).

**Side by side** (`ads_overview`): the three platforms in one answer with their attribution settings, and the mandatory warning: *"Les conversions de chaque plateforme sont attribuées indépendamment et se recoupent. La somme n'est pas dédupliquée et surestime les commandes réelles."* (In English: each platform's conversions are attributed independently and overlap; the sum is not deduplicated and overstates real orders.) Quote it in the report.

### 9. Speed

Core Web Vitals as **field data (CrUX), 75th percentile**, with Google's thresholds:

| Metric | Good | Poor |
|---|---|---|
| LCP | at most 2.5 s | over 4 s |
| INP | at most 200 ms | over 500 ms |
| CLS | at most 0.1 | over 0.25 |

- Tool: `pagespeed`. It returns **lab** data (`lcp`, `cls`, `tbt`, `performance`: Lighthouse, one simulated load) and, only when Google has enough real-user data for the URL or, failing that, for its origin (`field_metrics.origin_fallback: true`), **field** data: `field_metrics` (CrUX p75 of the last 28 days: `lcp_ms`, `inp_ms`, `cls`) and the overall category `field_data`. A low-traffic page often has no field data at all: `field_metrics` is then absent and only lab data can be shown. Do not mix them: report field data for Core Web Vitals, and use lab data to diagnose.

### 10. Data quality

An **event log** (promotions, stock-outs, tracking changes, campaign launches), consent and Consent Mode status, GA4 thresholds in effect, and a short glossary.

- Tools: `gtm_inventory` (tags and live version, to date tracking changes), `ga4_realtime` (check a tag fires once).
- The log is supplied by the client or the team; the tools cannot know about a promotion.

## The 10 KPIs and their formulas

| # | KPI | Formula and notes |
|---|---|---|
| 1 | Revenue | Sum of order revenue minus refunds, for the period. Say whether VAT and shipping are included. GA4 `purchaseRevenue` is before refunds |
| 2 | Orders | Count of completed orders. Reference: the back office. GA4 `ecommercePurchases` is what GA4 recorded |
| 3 | Average order value | Revenue / orders |
| 4 | Conversion rate | Orders / sessions. **State the denominator** (sessions, as here, or users) |
| 5 | Gross margin | (Revenue - cost of goods sold) / revenue. **Only with costs supplied by the client** |
| 6 | Share of returning customers | Orders from returning customers / orders. A rule of practice, not an official GA4 metric: say how "returning" is defined **[agency practice, not verified]** |
| 7 | MER | Total revenue / total ad spend. Computed by the assistant, same currency required |
| 8 | ROAS per platform | Platform-reported conversion value / that platform's spend. **Use it to steer each channel; never total it across platforms** |
| 9 | CPA / CAC | CPA = ad spend / conversions of a platform (platform-claimed). CAC = marketing cost / new customers (needs the client's costs and a definition of new) |
| 10 | Organic SEO clicks | Clicks from Search Console (`gsc_performance` totals), not the sum of query rows |

## 10 mistakes to avoid

1. **Adding up the platforms' conversions or ROAS.** Each attributes independently and they overlap; the sum overstates orders.
2. **Comparing different attribution windows.** Meta defaults to 7-day click + 1-day view, ChatGPT Ads (as this server requests it) to 30-day click + 1-day view, Google Ads uses a window per conversion action. Meta is reported to have removed the `7d_view` and `28d_view` windows from its API in January 2026 **[secondary source, not confirmed by Meta]**; this server does not offer them.
3. **Relying on GA4 attribution models that no longer exist.** GA4 retired some models; state the model actually in use **[secondary source]**.
4. **Adding up Search Console query rows.** Anonymised queries are in the total but not in the rows; use `totals`.
5. **Reading a row hidden by GA4 thresholds as zero.** A missing row is not a zero; small counts can be withheld.
6. **Vanity metrics.** Impressions or followers with no link to revenue or a decision.
7. **Numbers without a "so what".** Every figure needs a cause and a decision.
8. **No annotations.** A drop with no log of promotions, stock-outs or tracking changes cannot be explained.
9. **No year-on-year (N-1) comparison.** Month on month alone confuses seasonality with change.
10. **Lab data instead of CrUX.** Lab scores are for diagnosis; Core Web Vitals are judged on field data.

## Small shops (under 500 orders a month)

- Show **absolute values next to percentages**: "+50 %" on 4 orders is 2 orders.
- Use **rolling 3-month trends** next to the month.
- **Consent Mode modelling** in GA4 needs a minimum volume (the GA4 documentation mentions about 1,000 events per day without consent); below it, modelling may not apply, so do not assume it fills the gap.
- **Privacy thresholds** can hide small rows in GA4; say so.
- **The reference revenue is the back office.** GA4 and the platforms estimate; the shop's order system counts.
- **Incrementality is out of reach.** You cannot prove an ad caused a sale. Use MER and before/after comparisons, annotated, and call them what they are: correlations.

## Cadence

**[Agency practice, not verified.]**

| Rhythm | Reader | Content |
|---|---|---|
| Weekly, short | Marketing lead | Sections 2, 4, 8 and alerts (tracking broken, spend spike, disapproved products) |
| Monthly | Business owner | Sections 1, 2, 5, 10 (with 3, 4, 6, 7, 8, 9 as support) |
| Quarterly | Business owner | Trends, N-1, targets |

## GDPR

- **Aggregates only.** No transaction id, e-mail or name in a report. `openai_ads_conversions` can return attributed event details with identifiers; leave `include_attributed_events` off for reports.
- **Nothing goes to a third party** from this server: the report is a static local file. (The assistant's provider still receives what the tools return in the conversation; see [SECURITY.md](SECURITY.md).)
- **GA4 data retention** for event-level data is 2 or 14 months, set in the property. Check the property's setting before promising year-on-year detail.
- For the consent side (audience measurement and cookies), see the CNIL page in the sources. This guide is not legal advice.

## What the tools do not do yet

- **Impression share and Shopping product reports are not packaged.** They need free GAQL through `gads_query` ([templates](../examples/PROMPTS.md#gaql-templates-for-gads_query)); `shopping_performance_view` is on the allow-list of `gads_query`.
- **No automatic comparison with the previous period.** The assistant calls each tool twice and compares.
- **No MER from the back office.** The report computes MER from GA4 revenue only; a back-office MER is computed by the assistant from figures you give it.
- **New vs. returning customers is not packaged.** Ask `ga4_report` for the `newVsReturning` dimension and `firstTimePurchasers`.
- **Margin and targets are supplied by the user.** So are **annotations** (promotions, stock-outs, tracking changes).
- **Incrementality and consent rate are out of scope.** The report shows consent and Consent Mode only if you give them (`meta.consent`, `meta.consent_mode`) or mention them in `notes`.
- **Causes are never invented.** The report states what the figures show. A cause appears only if you supplied it in `events` or `notes`.
- **To verify:** the ChatGPT Ads `product.carousel_product_card_*` fields.
- Everything in the paid media tools is **tested offline only** ([ADS.md](ADS.md#what-has-not-been-tested-against-real-accounts)).

## The report file

`google-ecommerce-mcp report` reads one JSON file, built from the tools' results, and writes one self-contained HTML file. It makes no network call and needs no service to be configured. The page has an inline stylesheet and inline SVG charts, no script, no web font and no external URL, so it opens offline and prints cleanly (A4, one section per page). Only the client's brand is shown; there is no "made with" line. Parts of the generator are ported from [OpenGSC](https://github.com/fenjo26/OpenGSC) (MIT); see `THIRD_PARTY_NOTICES.md`.

`tests/data/sample_report_full.json` (an invented shop, `meta.sample: true`) is a complete input document you can copy as a starting point.

### How to produce a report

1. **Call the tools for each period.** The assistant runs the tools of the [10 sections](#the-10-sections) for the current month, the previous month and, if you want year-on-year, the same month last year.
2. **Save each raw output.** Keep every tool result unmodified (with its `date_range`, `attribution` and `fetched_at`). Do not retype or round figures.
3. **Assemble `report.json`.** Put the outputs under `sources` as described in [Input contract](#input-contract), add `periods`, your `goals`, the `events` log (promotions, stock-outs, tracking changes) and `notes`.
4. **Run the command:**

   ```bash
   google-ecommerce-mcp report --input report.json --output report.html --brand-name "Client name" --accent "#1F6F5B" --logo logo.png --lang en
   ```

   `--lang` is `fr` or `en` (default: `meta.lang`, else `fr`). An existing output file is kept unless you add `--force`. On an error the command prints `Report not written: ...` and exits with code 1.
5. **Open and print.** Open `report.html` in a browser, check it, then print to PDF with Ctrl+P (Cmd+P on macOS) and "Save as PDF".

### Input contract

One JSON object. Every source is optional: an absent source shows as "not provided" (`non fourni`), a source that is a tool error shows as failed, and neither is ever turned into a zero.

```json
{
  "meta": {"client": "...", "lang": "fr", "currency": "EUR", "generated_at": "YYYY-MM-DD",
           "prepared_by": "...", "sample": false,
           "brand": {"name": "...", "accent": "#RRGGBB", "logo_data_uri": "data:image/png;base64,..."},
           "ga4_attribution_model": "...", "consent": "...", "consent_mode": "...", "ga4_thresholds": "..."},
  "periods": {"current": ["YYYY-MM-DD", "YYYY-MM-DD"], "previous": ["YYYY-MM-DD", "YYYY-MM-DD"],
              "yoy": ["YYYY-MM-DD", "YYYY-MM-DD"]},
  "goals": {"revenue": 0, "orders": 0, "aov": 0, "conversion_rate": 0.0, "sessions": 0, "new_customers": 0},
  "sources": {"ga4": {}, "search_console": {}, "merchant_center": {}, "google_ads": {}, "meta_ads": {},
              "openai_ads": {}, "ads_overview": {}, "web_vitals": {}, "gtm_inventory": {}, "ga4_realtime": {}},
  "errors": [{"source": "...", "code": "...", "message": "..."}],
  "events": [{"date": "YYYY-MM-DD", "end": "YYYY-MM-DD", "label": "...", "item": "..."}],
  "notes": "operator text in a small markdown subset"
}
```

- **`meta`**: `client` and `brand` set the name; `ga4_attribution_model` is quoted in the channels section; `consent`, `consent_mode` and `ga4_thresholds` are quoted in section 10 (without them, consent is "not provided"). `sample: true` marks an invented example.
- **`periods`**: `previous` and `yoy` are optional; a missing comparison period shows "not provided", not zero.
- **`goals`**: the keys listed above, supplied by the client.
- **`events`**: `end` (a range) and `item` (the product concerned) are optional.
- **`notes`**: paragraphs, headings, `-` lists, `**bold**`, `*italic*`. Text is escaped first; a link keeps its text only.

**A *periodic* source** is either a bare tool output (taken as the current period) or `{"current": <output>, "previous": <output>, "yoy": <output>}`, each key optional.

| Source (part) | How to produce it |
|---|---|
| `ga4.kpi` | `ga4_report`, periodic. Its `totals` hold `purchaseRevenue`, `ecommercePurchases`, `sessions` (optionally `totalUsers`, `newUsers`, `firstTimePurchasers`). With `dimensions: ["date"]` its rows also feed the sparklines |
| `ga4.funnel` | `ga4_report` by `eventName` with `eventCount` |
| `ga4.channels` | `ga4_report` by `sessionDefaultChannelGroup` |
| `ga4.products` | `ga4_report` by `itemName` |
| `search_console.totals` | `gsc_performance`, periodic (any dimension; its `totals` are used; with `dimensions: ["date"]` the rows give the daily curve) |
| `search_console.queries`, `.pages` | `gsc_performance` by `query` / `page`, periodic |
| `merchant_center.product_issues` | `merchant_product_issues` |
| `merchant_center.performance` | `merchant_report_query` |
| `merchant_center.statuses` | `{"approved": n, "limited": n, "disapproved": n, "previous": {"approved": n, ...}}`, counted by the assistant from `merchant_report_query` (`aggregated_reporting_context_status`) |
| `google_ads` | `gads_performance` (level `account` or `campaign`), periodic |
| `meta_ads` | `meta_insights`, periodic |
| `openai_ads` | `openai_ads_insights`, periodic |
| `ads_overview` | `ads_overview`, periodic |
| `web_vitals.pages` | A list of `{"url": ..., "strategy": "mobile", **pagespeed output}`. The unmodified `pagespeed` output is enough: its `field_metrics` (present only when Google has field data for the URL or its origin) gives the p75 values judged against the thresholds; with `origin_fallback: true` the report says they are the whole site's. Without `field_metrics`, the page shows "no field data". Optionally add `field` (`{"lcp": seconds, "inp": milliseconds, "cls": score}`, which takes precedence) and `field_previous` for the change. `lcp`, `cls`, `tbt` and `performance` are lab values and are shown without a verdict, in the report's number format |
| `gtm_inventory`, `ga4_realtime` | The unmodified tool output; used for the data-quality checks only |

`openai_ads_conversions` is not an input source of the report. A sources block unknown to the generator is ignored and reported in `errors`.

### Reading the colours

- **Polarity.** Each metric has a direction. For revenue, orders, conversion rate or clicks, up is favourable. For cost per click, cost per acquisition, disapproved products or position, up is unfavourable. Spend, impressions, reach and frequency are neutral and never coloured as good or bad.
- **Double coding.** A change is never shown by colour alone: an arrow (▲ ▼ =), a sign and a text come with it, so it still reads in black and white print.
- **Noise.** A move under 1 % reads as stable. Larger moves are marked "not significant" (≈) when they stay within about 2 standard deviations. Counts: more than twice the square root of the sum of the two counts. Rates: about 2 standard errors of a two-proportion comparison. **Revenue** is judged on its relative change against the relative noise of the orders: 2·√(N1+N0) divided by the mean of the two order counts. **Average order value and other ratios** (ROAS, CPA, CPC, CPM, MER, spend), and rates without denominators, use a relative 5 % fallback when no order counts are used. Without order counts, a revenue move is shown as "not tested: volume unknown". This is a **statistical approximation, not a standard**: a rule of thumb to avoid colouring noise, not proof. The words "not significant" are readable by screen readers and printed.
- **Summary and notes.** The key messages use the same test as the change badge: a change that is not significant is never quoted as a fact, and the confidence badge of that message drops one level.
- **Small volumes.** With fewer than 500 orders in the period, or a base under 30, the absolute change is shown next to the percentage, for example "+27 (+17.6%)" in English ("+27 (+17,6 %)" in French).
- **Missing data.** A missing value shows "not provided" and a gap in a curve, never a zero.
- **Confidence.** Each key message carries a level (high, medium, low) with its reasons, from completeness of the days, volume and provenance (an ad platform's own claim is at most medium). Confidence is capped at "medium" under 500 events.
- **Shares.** Percentages of a total are computed with the largest-remainder method so the shown shares add up to exactly 100 %.
- **Periods.** If the periods are inconsistent, the report prints a warning and shows no "previous month" label.

### The client's brand

`--brand-name`, `--accent` (`#RRGGBB` only) and `--logo` (PNG, JPEG or SVG, 256 KB at most) set the brand; `meta.brand` (`name`, `accent`, `logo_data_uri`) does the same from the JSON, and the flags win. An invalid accent falls back to the default and the command prints a warning. Two derived colours are used: `accent_graphic` (at least 3:1 on white) for charts, borders and the initials block, and `accent_text` (at least 4.5:1 on white) for text, so a pale accent stays readable. Only the client's brand appears.

### What the report does not do

- **No invented cause.** The report states the change and the figures. A cause appears only if you provided it in `events` or `notes`.
- **Attribution warning.** The paid media section always carries the warning that each platform attributes conversions independently, that they overlap and that their sum is not deduplicated. It never adds platforms' conversions together.
- **MER** is computed from revenue and spend only when both are provided and the currency is the same; the server does not read the back office.

### Privacy of the file

- No external URL, no script, no web font: opening the file sends nothing anywhere, and a Content-Security-Policy meta tag enforces it. URLs of pages are shown as paths without their query string or fragment; identifier parameters (such as `gclid`, `order_id`) found in text are masked and reported in section 10.
- The file is written atomically (a temporary file, then a rename): an existing report stays intact if the command fails, and `--output` equal to `--input` is refused.
- Hostile input does not crash it: JSON nested more than 100 levels, a huge integer or an invalid character gives an error message and exit code 1, never a traceback. Any number beyond 1e15, NaN or a boolean where a number is expected is treated as "not provided".
- Every text from the JSON is HTML-escaped, notes included.
- Personal-data keys (such as `email`, `transaction_id`, `order_id`, `user_id`, `client_id`, `phone`, names) are dropped wherever they appear, e-mail addresses inside text are masked, and each removal is listed in section 10 (data quality).
- The logo is checked before use; a logo that fails the checks is left out with a warning (see [SECURITY.md](SECURITY.md#client-report-threat-model)). An SVG is also refused if it contains a character reference (`&#`), an `<animate>` or `<set>` element, or text before its first tag.

## Sources

All consulted 16 October 2026.

- Google Analytics: <https://support.google.com/analytics/answer/9267735>
- Google Analytics: <https://support.google.com/analytics/answer/11161109>
- Google Analytics: <https://support.google.com/analytics/answer/9383630>
- Merchant Center: <https://support.google.com/merchants/answer/160491>
- Core Web Vitals thresholds: <https://web.dev/articles/defining-core-web-vitals-thresholds>
- OpenAI Ads reporting: <https://developers.openai.com/ads/reporting>
- Google Ads developer token policy: <https://developers.google.com/google-ads/api/docs/api-policy/developer-token>
- CNIL, audience measurement tools: <https://www.cnil.fr/fr/cookies-solutions-pour-les-outils-de-mesure-daudience>

Not from these pages: the Meta window removal (secondary source), the cadences, the "returning customers" formula and the section plan itself (agency practice, not verified).

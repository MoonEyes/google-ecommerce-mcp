# Prompts that work well

Copy, adapt the dates, paste into your assistant. Each line names the tools it usually triggers.

## Weekly check (5 minutes)

> Give me this week's shop health check: sessions and revenue by channel vs. the previous week (GA4), top 10 search queries by clicks with CTR and position (Search Console), and the number of disapproved products in Merchant Center grouped by issue.

`ga4_report`, `gsc_performance`, `merchant_product_issues`

## SEO

- *"Queries with more than 100 impressions and an average position between 5 and 15 over the last 3 months. For each, the landing page."* (`gsc_performance` with `dimensions: ["query", "page"]`)
- *"Which queries of this client rank 5 to 15 with at least 200 impressions, and which pages get impressions but no clicks?"* (`gsc_opportunities`, once with `dimension: "query"` and once with `"page"`)
- *"Is /product/ruined-tower indexed? Which canonical did Google pick, and when was it last crawled?"* (`gsc_inspect_url`)
- *"Pages that get impressions but no clicks: titles to rewrite."* (`gsc_performance` with `dimensions: ["page"]`)
- *"When did Google last read our sitemaps, and are there errors?"* (`gsc_sitemaps`)

## Analytics and tracking

- *"Organic search sessions, purchases and revenue per month this year."* (`ga4_report` with `yearMonth`, `channel_group: "Organic Search"`)
- *"List the GTM tags with their triggers. Is any GA4 tag firing twice on all pages?"* (`gtm_inventory`)
- *"Are any of acme's key events inflating conversions? Check page_view, phone and e-mail clicks, and compare key events with sessions before we read any ROAS."* (`ga4_key_event_audit`)
- *"I just opened the home page once in a private window. How many page views does realtime show?"* (`ga4_realtime`)
- *"Which GA4 properties can you see? Compare last month's revenue of the shop and the blog."* (`ga4_properties`, then `ga4_report` with `property_id`)

## Merchant Center

- *"Which products are disapproved and why? Group by issue code and tell me which attribute to fix."* (`merchant_product_issues`)
- *"Top 20 products by clicks in free listings last month."* (`merchant_report_query` on `product_performance_view`)
- *"Which feeds do we have, for which countries, and where are they fetched from?"* (`merchant_data_sources`)

## Performance

- *"Mobile PageSpeed of the home page, the shop page and our best-selling product. Which Core Web Vital is the problem?"* (`pagespeed`)

## Use cases: from question to decision

These are the cases of the README, as prompts. The assistant reports; you decide, and you act in the tools themselves. Nothing here changes an account.

**A1. Traffic and sales by channel**

> Sessions, purchases and revenue by default channel group for last month vs. the month before, with the change in %. Which channel brings revenue and which only brings visits?

`ga4_report`

**A2. Organic queries one push from page one**

> Search queries over the last 3 months with more than 100 impressions and an average position between 5 and 15. For each, the landing page and the CTR. Sort by impressions.

`gsc_performance` with `dimensions: ["query", "page"]`

**A3. Disapproved products that still get visits**

> List the products disapproved in Merchant Center with the issue and the attribute to fix. Then check in Search Console which of their pages still get clicks, and rank them by clicks.

`merchant_product_issues`, `gsc_performance`

**A4. Tracking audit**

> List the GTM tags and triggers. Is the GA4 configuration tag present more than once? Is there a purchase tag, and what fires it? Does the workspace differ from the live version? Then I will open one page: tell me whether realtime counts exactly one page view.

`gtm_inventory`, `ga4_realtime`

**A5. Preparing a redesign**

> Landing pages by revenue and by organic clicks over the last 3 months: which ones must keep their URL or get a 301? For the top 5, is the page indexed and which canonical did Google pick? Give me the mobile PageSpeed of the home page and the top 3 pages as the before-baseline.

`ga4_report` with `landingPage`, `gsc_performance`, `gsc_inspect_url`, `pagespeed`

## Paid media

Needs the Google Ads, Meta Ads or ChatGPT Ads connectors ([docs/ADS.md](../docs/ADS.md)). They were tested offline only: compare the first figures with the platform's own interface.

**B1. Conversions: platforms vs. GA4**

> For last month, put Google Ads, Meta and ChatGPT Ads side by side with `ads_overview`: spend, clicks, conversions and the attribution window of each. Then get GA4 purchases for the same days. How far is each platform's conversion count from GA4? Do not add the platforms' conversions together.

`ads_overview`, `ga4_report` (`ecommercePurchases`, `purchaseRevenue`)

**B2. Reported ROAS vs. GA4 revenue**

> For last month, give the ROAS each platform reports and the value behind it, and GA4's purchase revenue for the same days. I want to compare the claimed values with the revenue GA4 saw. Say which windows each platform used, and do not compute an overall ROAS from the platforms' conversions.

`ads_overview`, `ga4_report`

**B3. Campaigns that spend without a recorded purchase**

> Over the last 28 days, which Google Ads campaigns and Meta campaigns have spend but no conversion or purchase attributed? For each, show the GA4 sessions and purchases with the same campaign name (`sessionCampaignName`). Is the tracking measuring purchases at all (`purchase_measurement`)?

`gads_performance` or `meta_insights`, `ga4_report` with `sessionCampaignName`

**Attribution questions**

- *"Why does Meta report more purchases than Google Ads for the same week? List the attribution window of each platform in the answers."* (`ads_overview`)
- *"Meta purchases for last month with the 1-day click window only, then with 7-day click and 1-day view, one figure per window."* (`meta_insights` with `attribution_windows`; the windows are never added)
- *"ChatGPT Ads conversions for last month, click-through vs. view-through, with the 7-day click window and no view-through."* (`openai_ads_conversions` with `attribution_window_days: 7`, `view_through_attribution_window_days: 0`)
- *"Which of my Google Ads conversion actions are counted in 'Conversions'?"* (`gads_query` on `conversion_action`)

**Per platform**

- *"Google Ads campaigns by spend last month, with CTR, CPC, conversions and ROAS."* (`gads_performance`)
- *"Google Ads spend by device last week."* (`gads_performance` with `segment: "device"`)
- *"Meta spend and purchases by country and by placement for the last 28 days."* (`meta_insights` with `breakdowns`)
- *"ChatGPT Ads spend, clicks and attributed sales per day for last month."* (`openai_ads_insights` with `time_granularity: "daily"`)

## Cross-service questions

- *"Products disapproved in Merchant Center that still get organic clicks in Search Console."*
- *"Landing pages with high organic traffic but a poor mobile PageSpeed score."*

These combine several tools in one answer; that is the point of having them behind one server.

## Monthly client report

One prompt for the whole report. Replace the bracketed parts, and give the annotations yourself: the tools cannot know about promotions, stock-outs or tracking changes. Method and formulas: [docs/REPORTING.md](../docs/REPORTING.md). The assistant gathers the raw tool outputs and assembles `report.json`; `google-ecommerce-mcp report` then builds the HTML file in the client's brand (input contract and steps in the guide).

> Prepare the data for the monthly report of [shop name] for [month]. Use the client's name only; no other branding. Read-only, aggregate figures only: no transaction id, e-mail or name.
>
> Method: for each source below, call the tool for the current month, the previous month and the same month last year, and keep every tool output exactly as returned (do not retype or round figures). Then assemble one `report.json` as described in docs/REPORTING.md ("Input contract"): `meta` (client name, language, currency, brand, and the GA4 attribution model, consent and Consent Mode if I give them), `periods`, `goals` ([targets, or "none"]), `sources` (the raw outputs, periodic as `{current, previous, yoy}`), `events` ([promotions, stock-outs, tracking changes with dates]) and `notes`. Add `merchant_center.statuses` (counts of approved, limited, disapproved, with the previous month) and the field p75 values (`field`: LCP in seconds, INP in ms, CLS) for each page of `web_vitals`. Do not invent a cause: only mention one if I gave it in `events` or `notes`. Then tell me the command to run: `google-ecommerce-mcp report --input report.json --output report.html --brand-name "[shop name]" --accent "[#RRGGBB]" --logo [logo file]`, and I will open the HTML and print it to PDF with Ctrl+P.
>
> Compare every figure with the previous month and with the same month last year, and with the targets I give; say when a period is incomplete (`data_complete`).
>
> The 10 sections, in order, and the sources that feed them:
> 1. Summary: 3 messages, each with the figure, the cause, the decision and a confidence level (high, medium, low).
> 2. Headline KPIs: revenue (minus refunds if I give them), orders, average order value, conversion rate (state the denominator), new vs returning customers, gross margin only if I give costs. Use `ga4_report`.
> 3. Funnel: sessions, view_item, add_to_cart, begin_checkout, purchase. Use `ga4_report`.
> 4. Channels: sessions, revenue, conversion rate per channel, and the attribution model used. Use `ga4_report`.
> 5. Products and categories: top and flop; stock-outs coinciding with drops (my list: [stock-outs]). Use `ga4_report` with `itemName`.
> 6. SEO: clicks, impressions, CTR, position, queries ranking 5 to 15. Use `gsc_performance` totals, not the sum of rows.
> 7. Shopping: disapproved products by issue and free-listing clicks. Use `merchant_product_issues`, `merchant_report_query`.
> 8. Paid media: MER = total revenue / total ad spend (you compute it, using revenue [source I give], only if all spends share one currency). Then one sub-section per configured platform (`gads_performance`, `meta_insights`, `openai_ads_insights`, `openai_ads_conversions`) with its own attribution window, then `ads_overview` side by side. Quote the attribution warning of `ads_overview`.
> 9. Speed: Core Web Vitals from field data (CrUX, 75th percentile) for the home page and the top product page; keep lab data apart. Use `pagespeed`.
> 10. Data quality: my event log: [promotions, stock-outs, tracking changes]; check tracking with `gtm_inventory`; consent and GA4 thresholds; a short glossary.
>
> Rules: never add up the platforms' conversions or ROAS. Show absolute values next to percentages. If a row is missing, say it may be hidden by thresholds, not zero. For each figure, end with a "so what". Mark anything you could not measure.

### GAQL templates for `gads_query`

Both queries use only resources on the allow-list of `gads_query` (`campaign` and `shopping_performance_view` are listed in `REPORTING_RESOURCES` in `google_ads.py`) and pass its validator. Change the dates. They are written from Google's field names and have **not been run against a real account**: if Google rejects a field, the error comes back as data.

Search impression share by campaign (ratios 0 to 1; `null` or absent values mean Google gave none):

```sql
SELECT campaign.name, campaign.advertising_channel_type, metrics.impressions,
       metrics.search_impression_share, metrics.search_budget_lost_impression_share,
       metrics.search_rank_lost_impression_share
FROM campaign
WHERE segments.date BETWEEN '2026-09-01' AND '2026-09-30' AND campaign.status = 'ENABLED'
ORDER BY metrics.impressions DESC
```

Shopping product report (spend, clicks, conversions per product):

```sql
SELECT campaign.name, segments.product_item_id, segments.product_title, segments.product_brand,
       metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions,
       metrics.conversions_value
FROM shopping_performance_view
WHERE segments.date BETWEEN '2026-09-01' AND '2026-09-30'
ORDER BY metrics.cost_micros DESC
LIMIT 100
```

`metrics.cost_micros` comes back as `metrics.cost` in currency units. Product titles can include brand names but no personal data. Conversions here are Google Ads' own, not deduplicated with other platforms.

## Speed section when Lighthouse fails

> Mobile PageSpeed of the home page and the top 3 product pages. If Lighthouse fails on a page, still give me its real-user (CrUX) values and say which lab values are missing and why.

`pagespeed`

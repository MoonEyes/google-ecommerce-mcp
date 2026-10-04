# Prompts that work well

Copy, adapt the dates, paste into your assistant. Each line names the tools it usually triggers.

## Weekly check (5 minutes)

> Give me this week's shop health check: sessions and revenue by channel vs. the previous week (GA4), top 10 search queries by clicks with CTR and position (Search Console), and the number of disapproved products in Merchant Center grouped by issue.

`ga4_report`, `gsc_performance`, `merchant_product_issues`

## SEO

- *"Queries with more than 100 impressions and an average position between 5 and 15 over the last 3 months. For each, the landing page."* (`gsc_performance` with `dimensions: ["query", "page"]`)
- *"Is /product/ruined-tower indexed? Which canonical did Google pick, and when was it last crawled?"* (`gsc_inspect_url`)
- *"Pages that get impressions but no clicks: titles to rewrite."* (`gsc_performance` with `dimensions: ["page"]`)
- *"When did Google last read our sitemaps, and are there errors?"* (`gsc_sitemaps`)

## Analytics and tracking

- *"Organic search sessions, purchases and revenue per month this year."* (`ga4_report` with `yearMonth`, `channel_group: "Organic Search"`)
- *"List the GTM tags with their triggers. Is any GA4 tag firing twice on all pages?"* (`gtm_inventory`)
- *"I just opened the home page once in a private window. How many page views does realtime show?"* (`ga4_realtime`)
- *"Which GA4 properties can you see? Compare last month's revenue of the shop and the blog."* (`ga4_properties`, then `ga4_report` with `property_id`)

## Merchant Center

- *"Which products are disapproved and why? Group by issue code and tell me which attribute to fix."* (`merchant_product_issues`)
- *"Top 20 products by clicks in free listings last month."* (`merchant_report_query` on `product_performance_view`)
- *"Which feeds do we have, for which countries, and where are they fetched from?"* (`merchant_data_sources`)

## Performance

- *"Mobile PageSpeed of the home page, the shop page and our best-selling product. Which Core Web Vital is the problem?"* (`pagespeed`)

## Cross-service questions

- *"Products disapproved in Merchant Center that still get organic clicks in Search Console."*
- *"Landing pages with high organic traffic but a poor mobile PageSpeed score."*

These combine several tools in one answer; that is the point of having them behind one server.

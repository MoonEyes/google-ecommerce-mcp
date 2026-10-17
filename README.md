# google-ecommerce-mcp

<!-- mcp-name: io.github.MoonEyes/google-ecommerce-mcp -->

One read-only MCP server for the data an online shop decides from: **GA4, Search Console, Merchant Center, Tag Manager, Indexing API, PageSpeed, and the ad platforms Google Ads, Meta Ads and ChatGPT Ads**.

Ask your AI assistant "how much organic traffic did we get this month?", "which products are disapproved in Merchant Center?", "is GA4 loaded twice on my site?" or "what did each ad platform claim last month, and what does GA4 say?" and it answers from your own accounts. It reads and reports. It never changes a campaign, a feed or a tag.

Built by [MoonEyes](https://www.mooneyeswargame.com), a small French shop selling 3D-printed tabletop terrain, because no existing MCP server covered GA4, Search Console and Merchant Center together.

> **The paid media connectors are new and tested offline only.** The Google Ads, Meta Ads and ChatGPT Ads tools were checked against fake responses written from each platform's documentation. They have not been run against real ad accounts yet: compare the first figures with each platform's own interface. Details: [docs/ADS.md](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/docs/ADS.md#what-has-not-been-tested-against-real-accounts).

![tests](https://github.com/MoonEyes/google-ecommerce-mcp/actions/workflows/tests.yml/badge.svg)
![license](https://img.shields.io/badge/license-MIT-blue)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
[![PyPI](https://img.shields.io/pypi/v/google-ecommerce-mcp)](https://pypi.org/project/google-ecommerce-mcp/)
[![MCP Registry](https://img.shields.io/badge/MCP%20Registry-io.github.MoonEyes%2Fgoogle--ecommerce--mcp-blue)](https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.MoonEyes/google-ecommerce-mcp)

![Architecture overview](https://raw.githubusercontent.com/MoonEyes/google-ecommerce-mcp/main/docs/diagrams/architecture-overview.png)

## Documentation

| Page | What is in it |
|---|---|
| [Architecture](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/docs/ARCHITECTURE.md) | Components, a tool call step by step, design choices, sequence diagrams |
| [Tool reference](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/docs/TOOLS.md) | Every tool (22): parameters, output, example questions |
| [Setup, one service at a time](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/docs/SETUP.md) | Which services you need, access checklist and e-mail template for a client, install order with `doctor` checks, revocation |
| [Client reporting](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/docs/REPORTING.md) | Monthly report plan, KPI formulas, the `report` command and its input contract |
| [Guided install prompts](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/examples/INSTALL-PROMPT.md) | A prompt to paste into Claude to be guided step by step ([French](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/examples/INSTALL-PROMPT.fr.md)) |
| [Third-party notices](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/THIRD_PARTY_NOTICES.md) | Code ported from OpenGSC (MIT) |
| [Easy setup guide for ad platforms](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/docs/SETUP-ADS.md) | Google Ads, Meta Ads, ChatGPT Ads: client e-mail, numbered steps, ids, checks, revocation |
| [Paid media guide](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/docs/ADS.md) | Google Ads, Meta Ads, ChatGPT Ads: prerequisites step by step, GAQL rules, errors, what is untested |
| [Google Cloud setup](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/docs/SETUP-GOOGLE-CLOUD.md) | OAuth client, APIs to enable, where to find each id |
| [Security model](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/docs/SECURITY.md) | Scopes, token storage, threats, how to revoke |
| [Troubleshooting](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/docs/TROUBLESHOOTING.md) | Every error we have met and its fix |
| [Example prompts](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/examples/PROMPTS.md) | Questions that work well, by use case |
| [Client configs](https://github.com/MoonEyes/google-ecommerce-mcp/tree/main/examples) | Claude Desktop (one or two shops), Claude Code |

## Why this one

- **One server for the whole picture.** Traffic and sales (GA4), organic search (Search Console), the product feed (Merchant Center), the tracking setup (Tag Manager), site speed, and what the ad platforms say they delivered. Other MCP servers cover one or two of these.
- **Read-only by design.** No tool creates, updates, publishes, pauses or deletes anything. Each platform has its own allow-list of requests, checked before anything is sent, and a test enforces it.
- **Secrets kept out of the way.** The Google token goes to your OS keyring (Windows Credential Manager, macOS Keychain or Secret Service). The Google Ads token is a separate keyring entry because its scope can write. Meta and ChatGPT Ads secrets go to the OS keyring from a hidden prompt (an environment variable also works), never on the command line or in a URL.
- **Answers a model can read correctly.** Reports return the top rows plus totals over everything, the unit and definition of each metric, dates resolved in the account's timezone with a flag when the last days can still change, and the attribution windows each ad platform used.
- **Honest about what it does not know.** Ad platforms count conversions their own way. The tools say so in every answer instead of adding up numbers that should not be added.
- **No third party.** Requests go straight from your machine to Google, Meta and OpenAI. Nothing goes through a MoonEyes server.

## What you can ask

Each case gives the question, the tools the assistant usually calls, what you learn, and the decision it supports. The decision stays yours: the server shows data and changes nothing. Ready-to-paste prompts are in [examples/PROMPTS.md](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/examples/PROMPTS.md).

**Traffic, search, feed, tracking**

1. **Where do visits and sales come from?** *"Sessions, purchases and revenue by channel, this month vs. last month."* Tools: `ga4_report`. You learn which channels bring revenue and which only bring visits. Decision: where to put the next hour or euro.
2. **Which searches are one push away from page one?** *"Queries ranking 5 to 15 with more than 100 impressions, and their landing page."* Tools: `gsc_performance`. You learn which pages already earn impressions but few clicks. Decision: which titles, descriptions and internal links to rewrite first.
3. **Which disapproved products still get visits?** *"Products disapproved in Merchant Center that still receive organic clicks."* Tools: `merchant_product_issues`, `gsc_performance`. You learn which feed errors hit pages people still visit. Decision: fix those products before the rest.
4. **Is the tracking trustworthy?** *"Is GA4 loaded twice? Does the purchase tag fire once? Is the workspace the same as the live version?"* Tools: `gtm_inventory`, `ga4_realtime`. You learn whether purchases or page views may be double counted and whether unpublished changes exist. Decision: publish, remove a duplicate tag, or leave it.
5. **Preparing a redesign.** *"Which landing pages earn money and organic clicks, which will need a 301, and what is today's speed baseline?"* Tools: `ga4_report` (dimension `landingPage`), `gsc_performance`, `gsc_inspect_url`, `pagespeed`. You learn which URLs to preserve, which redirects to plan, and the before-numbers to compare with after. Decision: the redirect map and what must not change.

**Paid media** (needs the connectors below)

6. **Do the platforms agree with GA4?** *"Compare the conversions of Google Ads, Meta and ChatGPT Ads with GA4 for last month."* Tools: `ads_overview`, `ga4_report`. You learn how far each platform's claim is from what GA4 recorded. Decision: which figure to trust less, and which attribution settings to review in that platform's interface.
7. **What does each platform's ROAS look like next to real revenue?** *"Reported ROAS per platform vs. GA4 revenue for the same days."* Tools: `ads_overview`, `ga4_report`. You learn the value each platform claims and the revenue GA4 saw. The server does not compute an overall return on ad spend (MER): you compare the two yourself. Decision: whether the claimed values are plausible together.
8. **Which campaigns spend without a recorded purchase?** *"Campaigns with spend and no purchase attributed, and what GA4 shows for the same campaign names."* Tools: `gads_performance` or `meta_insights`, `ga4_report` (dimension `sessionCampaignName`). You learn which campaigns spend while neither the platform nor GA4 shows a sale. Decision: pause, fix the tracking or wait, done by you in the ad platform.

**Why platform numbers differ.** Meta defaults to a 7-day click plus 1-day view window, ChatGPT Ads, as this server requests it by default, to 30-day click plus 1-day view, and Google Ads to the setting of each conversion action. Platforms attach a conversion to different dates, count the same order independently, and may report modelled conversions when visitors decline cookies (Consent Mode). GA4 adds its own thresholds. What the tools cannot tell you: whether an ad *caused* a sale (incrementality), your real number of orders (your shop's back office is not read), or your consent rate.

## Real findings

Findings from MoonEyes' own accounts and from one anonymised small-business account. They show the kind of problem the data revealed, not a promised result. Each states its period and how it was measured. They are single cases, not a benchmark, and a fix that coincides with a better number is a correlation, not a guarantee.

| Finding | Period | Measured by |
|---|---|---|
| Merchant Center: 2,544 listings, 112 disapproved. 224 "weight too high" refusals came from a grams-to-kilograms unit error in the feed (packs sent as about 2 tonnes), since corrected. One false "alcoholic drink" refusal was caused by a product name and went away after renaming 3 products. A duplicate feed source created by the Google for WooCommerce plugin was removed. | Audit of 1-2 June 2026 (MoonEyes) | Merchant Center product statuses and feed sources (the data `merchant_product_issues` and `merchant_data_sources` read) |
| A purchase counted twice: the same Google Ads conversion id was sent both by a WordPress snippet and by Google Tag Manager. The snippet was disabled and GTM stays the single source. The number of gtag occurrences in the served HTML went from 18 to 12 on one page and from 6 to 3 on another. | 2 June 2026 (MoonEyes) | Counting gtag occurrences in the served HTML |
| GA4 over-counted conversions: 265 conversions for 201 sessions, because phone, e-mail and contact clicks were marked as key events. | The 28 days before the 1-2 June 2026 audit (MoonEyes) | GA4 conversions and sessions |
| Search Console: 0 clicks and 708 submitted URLs for 0 indexed, the starting point of an indexing work item. | 1 June 2026 (MoonEyes) | Search Console |
| A Google Ads account showed 96 conversions at EUR 2.16 each for 10 real bookings. Real cost: EUR 207.57 / 10 = about EUR 20.76 per booking, 9.6 times the displayed figure. Cause: `page_view` and a page event were marked as key events under a target-CPA bid strategy; fixed by keeping only the booking event. Small local-services business, anonymised. | 26 August to 22 September 2026 | Google Ads console (read in the Ads interface, not with these tools) |

## Tools

22 tools, all read-only. Full parameters and outputs: [docs/TOOLS.md](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/docs/TOOLS.md).

| Tool | Service | What it answers |
|---|---|---|
| `server_status` | All | Which services are configured, does the token work |
| `ga4_report` | GA4 | Any report: channels, landing pages, purchases, revenue, by period |
| `ga4_realtime` | GA4 | Last 30 minutes, e.g. to check a page view is counted once |
| `ga4_properties` | GA4 | Every GA4 account and property you can read, to find a property id |
| `gsc_performance` | Search Console | Clicks, impressions, CTR, position by query, page, country, device, date |
| `gsc_inspect_url` | Search Console | Is this URL indexed, which canonical did Google pick, last crawl |
| `gsc_sitemaps` | Search Console | Declared sitemaps, last download, errors |
| `merchant_data_sources` | Merchant Center | Feeds, labels, countries, fetch URLs |
| `merchant_product_issues` | Merchant Center | Products with disapprovals or warnings, all pages |
| `merchant_report_query` | Merchant Center | Any Merchant Query Language report |
| `gtm_inventory` | Tag Manager | Tags, triggers, variables, live version |
| `indexing_status` | Indexing API | What Google knows about a submitted URL |
| `pagespeed` | PageSpeed Insights | Lighthouse performance and SEO scores, Core Web Vitals |
| `gads_accounts` | Google Ads | Customer accounts the token can read |
| `gads_performance` | Google Ads | Spend, clicks, conversions, value, ROAS by account, campaign or ad group |
| `gads_query` | Google Ads | One read-only GAQL `SELECT` on reporting resources |
| `meta_ad_accounts` | Meta Ads | Ad accounts the token can read |
| `meta_insights` | Meta Ads | Spend, reach, purchases, value, ROAS by level, with breakdowns |
| `openai_ads_account` | ChatGPT Ads | The ad account behind the key (connection test) |
| `openai_ads_insights` | ChatGPT Ads | Impressions, clicks, spend, conversions, attributed sales |
| `openai_ads_conversions` | ChatGPT Ads | Click-through and view-through conversions per campaign, ad group or ad |
| `ads_overview` | Google Ads, Meta, ChatGPT Ads | The three platforms side by side for one period |

## Paid media connectors

Optional. Configure one, two or all three; the others answer `not_configured`.

| Platform | Tools | You need |
|---|---|---|
| Google Ads | `gads_accounts`, `gads_performance`, `gads_query` | A customer id (`GADS_CUSTOMER_ID`), a manager id if you use one, and a **separate** authorization: `google-ecommerce-mcp setup-ads --client-secret client_secret.json`. The developer token is optional |
| Meta Ads | `meta_ad_accounts`, `meta_insights` | A system user token with only `ads_read` (`google-ecommerce-mcp set-secret meta-access-token`) and `META_AD_ACCOUNT_ID` |
| ChatGPT Ads | `openai_ads_account`, `openai_ads_insights`, `openai_ads_conversions` | An advertiser API key from Ads Manager (`google-ecommerce-mcp set-secret openai-ads-key`). One key is one account |
| All | `ads_overview` | At least one of the above |

Step by step, with the GAQL rules, every error and the known limits: [docs/ADS.md](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/docs/ADS.md).

**Attribution warning.** Each platform attributes conversions independently, with its own windows, dates and model, so the same order can be counted by several of them. The sum of the platforms' conversions is **not deduplicated and overstates real orders**. `ads_overview` repeats this in every answer. Read the figures as "what each platform claims", and compare them with GA4 and, outside this server, with your orders.

**Read-only, with one caveat.** The Google Ads permission (`adwords`) can write and Google offers no read-only version. The server only sends read requests, but the token itself could write, so it is stored apart from your other Google token. ChatGPT Ads documents no read-only key either. See [Security notes](#security-notes).

**Tested offline only.** All behaviour was checked against fake responses; real-account trials are still to do. Not yet confirmed: the timezone ChatGPT Ads returns with `time_granularity=none`, Meta's longer view windows (`7d_view`, `28d_view`; the tool does not offer them), and whether a ChatGPT Ads key can be limited to reading.

## Client reporting

`google-ecommerce-mcp report` turns the tools' JSON results into one static HTML file in your client's brand (name, logo, accent colour), printable to PDF. It needs no network and no configured service, shows comparisons with the previous month and last year, and never invents a cause: a cause appears only if you supply it.

```bash
# the assistant runs the tools for each period and saves their raw JSON; you assemble report.json from them
google-ecommerce-mcp report --input report.json --output report.html --brand-name "Client name" --accent "#1F6F5B" --logo logo.png
# open report.html in a browser, then Ctrl+P > Save as PDF
```

The plan, the input contract, the colour rules, the KPI formulas and common mistakes are in [docs/REPORTING.md](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/docs/REPORTING.md); a ready prompt is in [examples/PROMPTS.md](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/examples/PROMPTS.md#monthly-client-report). Tested on sample data only, not yet on real client accounts. Margin, targets and annotations come from you, and some figures (impression share, Shopping product reports, MER) need extra GAQL or a calculation by the assistant.

## Who is it for

- **Shop owner.** You want straight answers (what sold, what is broken, which ads spend for nothing) without opening six dashboards. Install, ask in plain language, decide yourself.
- **Agency.** You audit several shops. One instance per shop (see the two-shops client config), the same read-only checks each time, and attribution caveats written into the answers you hand to a client.
- **Developer.** You want a small, readable, tested server: plain HTTP calls, per-platform allow-lists, structured errors. Pull requests welcome (see Contributing).

## What it will not do

- **No writes, anywhere.** It cannot create, edit, pause or delete a campaign, change a budget or a bid, edit a feed or a tag, or publish anything. To act on what you learn, use the platform's own interface.
- **No causal attribution.** It reports what each platform and GA4 recorded. It cannot say whether an ad caused a sale (incrementality).
- **No real order count.** Your shop's back office (WooCommerce, Shopify...) is not read, so it cannot tell you actual orders, margins or returns. It does not know your consent rate either.
- **No deduplicated conversions, no currency conversion.** It will not merge the platforms' conversions into one "true" number, and it adds spends only when they share one currency.

## Quick install

Create your Google OAuth client first ([step 1 below](#1-google-cloud-once-about-10-minutes)), then run the installer. It installs [uv](https://docs.astral.sh/uv/) if needed, asks your ids, opens the Google consent screen and adds the server to Claude Desktop (your previous config is backed up). It installs the exact package version it was released with (`google-ecommerce-mcp==0.4.0`).

**Recommended: download, read, run.** The URLs name an exact commit, not a branch, so the script cannot change under you.

**Windows** (PowerShell):

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/MoonEyes/google-ecommerce-mcp/75c9538e21f9b24f0c739d4d78fd3f99c3e5f6d9/install.ps1 -OutFile install.ps1
Get-Content .\install.ps1            # read it
(Get-FileHash .\install.ps1 -Algorithm SHA256).Hash   # compare with the SHA-256 below
.\install.ps1
```

**macOS / Linux**:

```bash
curl -LsSf -o install.sh https://raw.githubusercontent.com/MoonEyes/google-ecommerce-mcp/75c9538e21f9b24f0c739d4d78fd3f99c3e5f6d9/install.sh
cat install.sh                       # read it
shasum -a 256 install.sh             # compare with the SHA-256 below
sh install.sh
```

SHA-256 of the pinned scripts:

- `install.ps1`: `aa5d02aaa21684d94f8717ac2f642c1d313d509eb77617f1828810f34807c68f`
- `install.sh`: `d01c0658bf5557e48afd2fd26f4abe5fe06979346919459b4525aea720f829b9`

**One line, if you accept to run it without reading** (same pinned commit; this way the SHA-256 is not checked):

```powershell
irm https://raw.githubusercontent.com/MoonEyes/google-ecommerce-mcp/75c9538e21f9b24f0c739d4d78fd3f99c3e5f6d9/install.ps1 | iex
```

```bash
curl -LsSf https://raw.githubusercontent.com/MoonEyes/google-ecommerce-mcp/75c9538e21f9b24f0c739d4d78fd3f99c3e5f6d9/install.sh | sh
```

The installer has an optional **Paid media** step (Google Ads, Meta Ads, ChatGPT Ads); skip it with `--skip-ads`. It never takes a secret as an argument: Google Ads uses the browser consent (the same OAuth client file, a separate token), and the Meta token and ChatGPT Ads key are typed at a hidden prompt into the OS keyring. The Claude Desktop config only receives non-secret ids. Non-secret options: `--gads-customer-id` (exactly 10 digits, dashes allowed), `--gads-login-customer-id`, `--meta-account-id` (3 to 20 digits), and `--corporate-proxy` (sets `ADS_TRUST_ENV=1`; it is enough on its own to add it to an existing entry). All checks run before any consent or secret prompt; a conflicting value stops with exit code 3 and changes nothing, and `--force` replaces a value but never deletes a variable. With `--yes`, ChatGPT Ads is skipped (no id to give): add it interactively (`install --skip-auth`) or with `set-secret openai-ads-key`. The PageSpeed key is read from `--pagespeed-key` (not recommended: it stays in your shell history), else `PAGESPEED_API_KEY`, else a hidden prompt.

**Guided install.** To be walked through it one step at a time, paste [examples/INSTALL-PROMPT.md](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/examples/INSTALL-PROMPT.md) (English) or [INSTALL-PROMPT.fr.md](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/examples/INSTALL-PROMPT.fr.md) (French) into Claude. Install one service at a time and the access checklist for your client are in [docs/SETUP.md](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/docs/SETUP.md).

Then quit Claude Desktop completely and reopen it. To pass options, save the script first, for example `.\install.ps1 --ga4 123456789 --gsc sc-domain:example.com --client-secret client_secret.json`. Run `google-ecommerce-mcp install --help` for every option. Maintainers: each release bumps the version in both scripts, then refreshes the commit in these URLs and the checksums (`tests/test_installer_scripts.py` fails until they match).

## Setup, step by step

If you used the quick install, you only need step 1. The steps below are the manual path.

### 1. Google Cloud (once, about 10 minutes)

Full walkthrough with every click explained: [docs/SETUP-GOOGLE-CLOUD.md](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/docs/SETUP-GOOGLE-CLOUD.md). Short version:

1. In [Google Cloud Console](https://console.cloud.google.com/), create or pick a project.
2. Enable the APIs you need: *Google Analytics Data API*, *Google Analytics Admin API*, *Google Search Console API*, *Merchant API*, *Tag Manager API*, *Web Search Indexing API*, *PageSpeed Insights API*.
3. Configure the OAuth consent screen (External, add yourself as a test user).
4. Create an OAuth client of type **Desktop app** and download its JSON file.
5. Merchant API only: [register your Cloud project](https://developers.google.com/merchant/api/guides/quickstart) with your Merchant Center account.
6. Optional: create an API key restricted to PageSpeed Insights (the anonymous quota is shared and often exhausted).

### 2. Install and authorize

```bash
# with uv (recommended): nothing to install, uvx fetches the package from PyPI
uvx google-ecommerce-mcp setup --client-secret path/to/client_secret.json --with-merchant
# read-only scopes only by default; --with-merchant / --with-indexing add the write-capable ones you need

# or with pip
pip install google-ecommerce-mcp
google-ecommerce-mcp setup --client-secret path/to/client_secret.json
```

Latest development version: `uvx --from git+https://github.com/MoonEyes/google-ecommerce-mcp google-ecommerce-mcp`.

Your browser opens the Google consent screen. The token is then stored in your OS keyring. Check everything with `google-ecommerce-mcp check`.

**Check each service with `doctor`:**

```bash
google-ecommerce-mcp doctor                          # offline: Service, Set up, Secret, Token, Status, Account
google-ecommerce-mcp doctor --only gads --live       # one service, plus one minimal read call
google-ecommerce-mcp doctor --live --json            # no secret in the output; ids masked (123***890)
```

`--only` takes `ga4,gsc,merchant,gtm,pagespeed,gads,meta,openai` (comma-separated). `doctor` reads this shell's environment and the server entry of your Claude Desktop config (`--config` and `--name` point it elsewhere). The table is followed by "To fix" (the exact command for each problem), "Not set up", a `Result:` line and a `Next command:`. Exit code 0 means all configured services work, 1 that a configured service fails, 2 that nothing is configured. `--live` does not test PageSpeed (it is shown as skipped: it needs a page address and about 30 seconds); ask the assistant to run `pagespeed` instead.

### Paid media (optional)

Set up Google Ads with `google-ecommerce-mcp setup-ads --client-secret client_secret.json` (or `install` with `--gads-customer-id`), Meta with `google-ecommerce-mcp set-secret meta-access-token` plus the ad account id (`install --yes --skip-auth --meta-account-id <id>`), ChatGPT Ads with `google-ecommerce-mcp set-secret openai-ads-key`, then check each with `doctor --only <service> --live`. Prerequisites: [docs/ADS.md](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/docs/ADS.md). Overall procedure, access checklist and an e-mail template for your client: [docs/SETUP.md](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/docs/SETUP.md).

### 3. Add it to your MCP client

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "google-ecommerce": {
      "command": "uvx",
      "args": ["google-ecommerce-mcp"],
      "env": {
        "GA4_PROPERTY_ID": "123456789",
        "GSC_SITE_URL": "sc-domain:example.com",
        "MERCHANT_ACCOUNT_ID": "1234567890",
        "GTM_CONTAINER_ID": "GTM-XXXXXXX",
        "PAGESPEED_API_KEY": ""
      }
    }
  }
}
```

**Claude Code**:

```bash
claude mcp add google-ecommerce -e GA4_PROPERTY_ID=123456789 -e GSC_SITE_URL=sc-domain:example.com \
  -e MERCHANT_ACCOUNT_ID=1234567890 -e GTM_CONTAINER_ID=GTM-XXXXXXX \
  -- uvx google-ecommerce-mcp
```

Every variable is optional: a tool for a service you did not configure simply answers `not_configured`. For the ad platforms, put ids in the config (`GADS_CUSTOMER_ID`, `META_AD_ACCOUNT_ID`) and secrets in the keyring with the commands below the table.

| Variable | Example | Used by |
|---|---|---|
| `GA4_PROPERTY_ID` | `123456789` (numeric property id) | GA4 tools |
| `GSC_SITE_URL` | `sc-domain:example.com` or `https://www.example.com/` | Search Console tools |
| `MERCHANT_ACCOUNT_ID` | `1234567890` | Merchant tools |
| `GTM_CONTAINER_ID` | `GTM-XXXXXXX` | `gtm_inventory` |
| `PAGESPEED_API_KEY` | API key | `pagespeed` |
| `GOOGLE_TOKEN_FILE` | `~/.config/google-ecommerce-mcp/token.json` | store the token in a file instead of the keyring |
| `GADS_CUSTOMER_ID` | `1234567890` | Google Ads tools |
| `GADS_LOGIN_CUSTOMER_ID` | `1234567890` | manager (MCC) id, only if you reach the account through one |
| `GADS_DEVELOPER_TOKEN` | | optional; sent only when set |
| `GADS_TOKEN_FILE` | `~/.config/google-ecommerce-mcp/ads-token.json` | store the Google Ads token in a file instead of the keyring (must differ from `GOOGLE_TOKEN_FILE`) |
| `GADS_API_VERSION` | `v26` (default) | Google Ads REST version |
| `GADS_ALLOW_SENSITIVE_QUERIES` | `1` | lifts the resource lists of `gads_query` (personal data possible) |
| `META_ACCESS_TOKEN` | | Meta token (`ads_read` only). Prefer `set-secret meta-access-token` to keep it out of the config |
| `META_AD_ACCOUNT_ID` | `act_1234567890` or `1234567890` | `meta_insights` |
| `META_API_VERSION` | `v26.0` (default) | Graph API version |
| `OPENAI_ADS_API_KEY` | | ChatGPT Ads key. Prefer `set-secret openai-ads-key` |
| `ADS_TRUST_ENV` | `1` (exactly) | use `HTTPS_PROXY`, `NO_PROXY` and `REQUESTS_CA_BUNDLE` for the Ads connectors (corporate proxy) |

Command for the client report: `google-ecommerce-mcp report --input report.json --output report.html [--lang fr|en] [--brand-name NAME] [--accent #RRGGBB] [--logo FILE] [--force]` (see [docs/REPORTING.md](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/docs/REPORTING.md)).

Commands for the paid media connectors (details in [docs/ADS.md](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/docs/ADS.md)):

```bash
google-ecommerce-mcp setup-ads --client-secret path/to/client_secret.json [--token-file path]   # Google Ads only, separate token
google-ecommerce-mcp set-secret meta-access-token      # hidden prompt, stored in the OS keyring
google-ecommerce-mcp set-secret openai-ads-key         # hidden prompt, stored in the OS keyring
```

## How a call works

![Sequence of one tool call](https://raw.githubusercontent.com/MoonEyes/google-ecommerce-mcp/main/docs/diagrams/sequence-tool-call.png)

## Security notes

Details: [docs/SECURITY.md](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/docs/SECURITY.md).

- Read-only is enforced, not just declared: every request is checked against an allow-list before it is sent, and CI fails if any tool tries an endpoint outside it. `readOnlyHint` is only a hint.
- `setup` requests read-only scopes only. Google has no read-only scope for **Merchant Center** (`content`) or the **Indexing API** (`indexing`); they are requested only with `--with-merchant` / `--with-indexing`, and `server_status` flags them when present.
- **Paid media.** Each ad platform has its own allow-list (host pinned, a few read-only endpoints, no `mutate`, `create`, `update`, `delete`). Google Ads queries go through a GAQL validator before being sent. Redirects are never followed, answers are capped at 20 MB, secrets are masked in every error, and the environment's proxy is used only if you set `ADS_TRUST_ENV=1`.
- **Google Ads scope caveat.** `setup-ads` requests the `adwords` scope, which can write and cannot be limited. It is stored in its own keyring entry (`oauth-token-ads`), apart from the read-only Google token. ChatGPT Ads documents no read-only key, and a Meta system user token is long-lived: dedicate each credential to this server and revoke it when you stop. See [docs/SECURITY.md](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/docs/SECURITY.md).
- Every result carries `fetched_at` and `freshness`; report tools return the `date_range` queried, resolved in the data's timezone, with `data_complete`.
- Revoke access at any time from your [Google account permissions](https://myaccount.google.com/permissions).

## Limitations

- The Merchant API is recent and Google keeps changing it; the older Content API for Shopping is being shut down. Open an issue if a call breaks.
- One site, Merchant account and container per server instance; GA4 tools accept any readable `property_id`. Run several instances for several shops.
- GA4 Data API quotas apply to `ga4_report`. Each ad platform has its own quotas, returned as `rate_limited`; nothing is retried automatically.
- **Paid media connectors are untested on real accounts** (offline tests only). Platform conversions are attributed independently and are not deduplicated; spends are never converted between currencies.
- Ad platforms keep changing their APIs (defaults: Google Ads `v26`, Meta `v26.0`; both are settable). Open an issue if a call breaks.
- One Google Ads customer, one Meta ad account and one ChatGPT Ads key per server instance; `customer_id` and `account_id` parameters read other accounts the credential can see.

## Contributing

See [CONTRIBUTING.md](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/CONTRIBUTING.md). Security reports: [SECURITY.md](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/SECURITY.md).

## Development

```bash
pip install -e ".[dev]"
pytest
```

Tests run offline with a fake HTTP session; no Google, Meta or OpenAI account needed.

## License

MIT, see [LICENSE](https://github.com/MoonEyes/google-ecommerce-mcp/blob/main/LICENSE).

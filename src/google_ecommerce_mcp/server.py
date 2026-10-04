"""MCP tools. Every tool is read-only: no endpoint that creates, updates, publishes or deletes is ever called."""

from __future__ import annotations

import datetime
from urllib.parse import quote

from typing import Annotated, Literal

import requests
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from .auth import NotAuthenticated, TokenProvider
from .config import NotConfigured, Settings

SETTINGS = Settings.from_env()
TOKENS = TokenProvider(SETTINGS.token_file)
HTTP = requests.Session()
TIMEOUT = 60
MAX_ROWS = 1000

mcp = FastMCP(
    "google-ecommerce-mcp",
    instructions=(
        "Read-only access to the shop's Google accounts. Start with server_status to see which services are "
        "configured. Traffic and revenue: ga4_report (ga4_realtime for the last 30 minutes). Organic search: "
        "gsc_performance, gsc_inspect_url for one URL's index state, gsc_sitemaps. Product feeds and "
        "disapprovals: merchant_product_issues, merchant_data_sources, merchant_report_query. Tracking setup: "
        "gtm_inventory. Speed: pagespeed. No tool can modify anything."
    ),
    website_url="https://github.com/MoonEyes/google-ecommerce-mcp",
)
mcp._mcp_server.version = __version__  # otherwise clients see the mcp library version

# Every tool only reads from Google: declared to MCP clients through the standard annotations.
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)


def tool(title: str):
    return mcp.tool(title=title, annotations=READ_ONLY)


# ------------------------------------------------------------------ helpers
def _require(value: str | None, variable: str) -> str:
    if not value:
        raise NotConfigured(variable)
    return value


def _call(method: str, url: str, **kwargs) -> dict:
    try:
        response = HTTP.request(method, url, headers=TOKENS.headers(), timeout=TIMEOUT, **kwargs)
    except NotAuthenticated as exc:
        return {"error": "not_authenticated", "detail": str(exc)}
    except requests.RequestException as exc:
        return {"error": "network", "detail": str(exc)}
    try:
        data = response.json()
    except ValueError:
        data = {"text": response.text[:500]}
    if not response.ok:
        detail = data.get("error", data) if isinstance(data, dict) else data
        return {"error": response.status_code, "detail": detail}
    return data


def _guard(fn):
    """Turn configuration errors into a readable tool result instead of a crash."""

    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except NotConfigured as exc:
            return {"error": "not_configured", "detail": str(exc)}

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    wrapper.__annotations__ = fn.__annotations__
    wrapper.__wrapped__ = fn
    return wrapper


def _days_ago(n: int) -> str:
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()


def _cap(limit: int) -> int:
    return max(1, min(int(limit), MAX_ROWS))


def _ga4_rows(resp: dict, limit: int) -> dict:
    if "error" in resp:
        return resp
    dims = [d["name"] for d in resp.get("dimensionHeaders", [])]
    mets = [m["name"] for m in resp.get("metricHeaders", [])]
    rows = []
    for row in resp.get("rows", [])[:limit]:
        item = dict(zip(dims, (v["value"] for v in row.get("dimensionValues", []))))
        item.update(zip(mets, (v["value"] for v in row.get("metricValues", []))))
        rows.append(item)
    return {"rows": rows, "row_count": resp.get("rowCount", len(rows))}


def check() -> dict:
    """Configuration status plus a live token test (used by `google-ecommerce-mcp check`)."""
    status = SETTINGS.status()
    try:
        TOKENS.headers()
        status["token"] = "ok"
    except NotAuthenticated as exc:
        status["token"] = str(exc)
    except Exception as exc:  # refresh failures, revoked tokens
        status["token"] = f"error: {exc}"
    status["version"] = __version__
    return status


# ------------------------------------------------------------------ server info
@tool("Server status")
def server_status() -> dict:
    """Which Google services are configured for this server, and whether the stored OAuth token works.
    Call it first when another tool answers not_configured or not_authenticated. Returns one boolean per
    service, the token storage (os-keyring or file), "token": "ok" or the error, and the package version."""
    return check()


# ------------------------------------------------------------------ GA4
@tool("GA4 report")
@_guard
def ga4_report(
    dimensions: Annotated[list[str], Field(description="GA4 API dimension names, e.g. sessionDefaultChannelGroup, "
                                           "landingPage, sessionSource, yearMonth, itemName")] = ["sessionDefaultChannelGroup"],
    metrics: Annotated[list[str], Field(description="GA4 API metric names, e.g. sessions, totalUsers, "
                                        "ecommercePurchases, purchaseRevenue, keyEvents")] = ["sessions", "totalUsers"],
    start_date: Annotated[str, Field(description="YYYY-MM-DD, NdaysAgo, yesterday or today")] = "28daysAgo",
    end_date: Annotated[str, Field(description="YYYY-MM-DD, NdaysAgo, yesterday or today")] = "yesterday",
    limit: Annotated[int, Field(description="Maximum rows returned (1 to 1000)", ge=1, le=1000)] = 50,
    channel_group: Annotated[str, Field(description="Optional exact filter on sessionDefaultChannelGroup, "
                                        "e.g. 'Organic Search'; empty for all channels")] = "",
) -> dict:
    """Run a Google Analytics 4 report (Data API runReport) on the configured property: traffic, conversions or
    revenue split by any dimensions over a date range. Returns {"rows": [{dimension: value, metric: value}],
    "row_count": total}. Use ga4_realtime for the last 30 minutes."""
    prop = _require(SETTINGS.ga4_property_id, "GA4_PROPERTY_ID")
    limit = _cap(limit)
    body = {"dateRanges": [{"startDate": start_date, "endDate": end_date}],
            "dimensions": [{"name": d} for d in dimensions], "metrics": [{"name": m} for m in metrics], "limit": limit}
    if channel_group:
        body["dimensionFilter"] = {"filter": {"fieldName": "sessionDefaultChannelGroup",
                                              "stringFilter": {"value": channel_group}}}
    return _ga4_rows(_call("POST", f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport", json=body), limit)


@tool("GA4 realtime")
@_guard
def ga4_realtime(
    dimensions: Annotated[list[str], Field(description="GA4 realtime dimensions, e.g. unifiedScreenName, "
                                           "country, deviceCategory")] = ["unifiedScreenName"],
    metrics: Annotated[list[str], Field(description="GA4 realtime metrics, e.g. activeUsers, screenPageViews, "
                                        "eventCount")] = ["activeUsers", "screenPageViews"],
) -> dict:
    """GA4 realtime report for the last 30 minutes. Useful to check a tracking change, for example that one page
    view is counted once and not twice. Returns {"rows": [...], "row_count": n}."""
    prop = _require(SETTINGS.ga4_property_id, "GA4_PROPERTY_ID")
    body = {"dimensions": [{"name": d} for d in dimensions], "metrics": [{"name": m} for m in metrics]}
    return _ga4_rows(_call("POST", f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runRealtimeReport", json=body), 100)


# ------------------------------------------------------------------ Search Console
def _site() -> str:
    return quote(_require(SETTINGS.gsc_site_url, "GSC_SITE_URL"), safe="")


@tool("Search Console performance")
@_guard
def gsc_performance(
    dimensions: Annotated[list[Literal["query", "page", "country", "device", "date", "searchAppearance"]],
                          Field(description="How to split the results")] = ["query"],
    start_date: Annotated[str, Field(description="YYYY-MM-DD; empty means 30 days ago")] = "",
    end_date: Annotated[str, Field(description="YYYY-MM-DD; empty means 2 days ago (Search Console data lag)")] = "",
    limit: Annotated[int, Field(description="Maximum rows returned (1 to 1000)", ge=1, le=1000)] = 50,
    page_contains: Annotated[str, Field(description="Optional: keep only pages whose URL contains this text")] = "",
) -> dict:
    """Google Search Console search performance for the configured property: clicks, impressions, CTR and average
    position, split by query, page, country, device or date. Returns {"rows": [{<dimensions>, clicks, impressions,
    ctr, position}]}."""
    limit = _cap(limit)
    body = {"startDate": start_date or _days_ago(30), "endDate": end_date or _days_ago(2),
            "dimensions": dimensions, "rowLimit": limit}
    if page_contains:
        body["dimensionFilterGroups"] = [{"filters": [{"dimension": "page", "operator": "contains",
                                                       "expression": page_contains}]}]
    resp = _call("POST", f"https://www.googleapis.com/webmasters/v3/sites/{_site()}/searchAnalytics/query", json=body)
    if "error" in resp:
        return resp
    return {"rows": [dict(zip(dimensions, r["keys"]), clicks=r["clicks"], impressions=r["impressions"],
                          ctr=round(r["ctr"], 4), position=round(r["position"], 1)) for r in resp.get("rows", [])]}


@tool("Search Console URL inspection")
@_guard
def gsc_inspect_url(
    url: Annotated[str, Field(description="Full URL to inspect; must belong to the configured Search Console property")],
) -> dict:
    """Google index status of one URL (URL Inspection API): verdict, coverage state, user and Google-selected
    canonical, robots.txt state, page fetch state and last crawl time. Quota: 2,000 inspections per day."""
    site = _require(SETTINGS.gsc_site_url, "GSC_SITE_URL")
    resp = _call("POST", "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect",
                 json={"inspectionUrl": url, "siteUrl": site})
    return resp if "error" in resp else resp.get("inspectionResult", {}).get("indexStatusResult", resp)


@tool("Search Console sitemaps")
@_guard
def gsc_sitemaps() -> dict:
    """Sitemaps declared in Search Console, with last submission, last download, errors and warnings."""
    return _call("GET", f"https://www.googleapis.com/webmasters/v3/sites/{_site()}/sitemaps")


# ------------------------------------------------------------------ Merchant Center (Merchant API v1)
MERCHANT = "https://merchantapi.googleapis.com"


@tool("Merchant Center data sources")
@_guard
def merchant_data_sources() -> dict:
    """Merchant Center data sources (feeds): labels, countries, languages, fetch URLs."""
    acc = _require(SETTINGS.merchant_account_id, "MERCHANT_ACCOUNT_ID")
    return _call("GET", f"{MERCHANT}/datasources/v1/accounts/{acc}/dataSources")


@tool("Merchant Center product issues")
@_guard
def merchant_product_issues(
    limit: Annotated[int, Field(description="Maximum products returned (1 to 1000)", ge=1, le=1000)] = 50,
    only_with_issues: Annotated[bool, Field(description="True: only products with at least one issue; "
                                            "False: every product")] = True,
    max_pages: Annotated[int, Field(description="Pages of 250 products to scan at most", ge=1, le=200)] = 20,
) -> dict:
    """Merchant Center products with their item-level issues (disapprovals, demotions, warnings) per destination,
    with the attribute to fix. Scans every page of products up to max_pages. Returns {"products": [{offer_id,
    feed_label, title, link, issues: [{code, severity, description, attribute, destination}]}], "scanned": n,
    "truncated": bool}."""
    acc = _require(SETTINGS.merchant_account_id, "MERCHANT_ACCOUNT_ID")
    limit = _cap(limit)
    out, token, pages, scanned = [], None, 0, 0
    while pages < max(1, max_pages):
        params = {"pageSize": 250}
        if token:
            params["pageToken"] = token
        resp = _call("GET", f"{MERCHANT}/products/v1/accounts/{acc}/products", params=params)
        if "error" in resp:
            return resp
        pages += 1
        for product in resp.get("products", []):
            scanned += 1
            issues = [{"code": i.get("code"), "severity": i.get("severity"), "description": i.get("description"),
                       "attribute": i.get("attribute"), "destination": i.get("reportingContext")}
                      for i in product.get("productStatus", {}).get("itemLevelIssues", [])]
            if only_with_issues and not issues:
                continue
            attrs = product.get("productAttributes", product.get("attributes", {}))
            out.append({"offer_id": product.get("offerId"), "feed_label": product.get("feedLabel"),
                        "title": attrs.get("title"), "link": attrs.get("link"), "issues": issues})
            if len(out) >= limit:
                return {"products": out, "scanned": scanned, "truncated": True}
        token = resp.get("nextPageToken")
        if not token:
            break
    return {"products": out, "scanned": scanned, "truncated": bool(token)}


@tool("Merchant Center report query")
@_guard
def merchant_report_query(
    query: Annotated[str, Field(description="Merchant Center Query Language (MCQL) statement. Tables include "
                                "product_view (must select id) and product_performance_view (clicks, impressions "
                                "by date or offer)")] =
        "SELECT id, offer_id, title, aggregated_reporting_context_status FROM product_view LIMIT 50",
) -> dict:
    """Run a Merchant Center report (Merchant API reports:search) with an MCQL query, for example product status
    or free-listing performance. Returns Google's raw result rows."""
    acc = _require(SETTINGS.merchant_account_id, "MERCHANT_ACCOUNT_ID")
    return _call("POST", f"{MERCHANT}/reports/v1/accounts/{acc}/reports:search", json={"query": query})


# ------------------------------------------------------------------ Tag Manager (read only)
GTM_API = "https://tagmanager.googleapis.com/tagmanager/v2"
BUILT_IN_TRIGGERS = {"2147479553": "All Pages (built-in)", "2147479573": "Initialization - All Pages (built-in)",
                     "2147479572": "Consent Initialization - All Pages (built-in)"}


@tool("Tag Manager inventory")
@_guard
def gtm_inventory() -> dict:
    """Tags (type, paused, firing triggers, parameters), triggers and variables of the configured Google Tag Manager
    container's default workspace, plus the id of the live published version. Built-in trigger ids are translated,
    e.g. All Pages. Useful to spot a tag configured twice."""
    public_id = _require(SETTINGS.gtm_container_id, "GTM_CONTAINER_ID")
    accounts = _call("GET", f"{GTM_API}/accounts")
    if "error" in accounts:
        return accounts
    for account in accounts.get("account", []):
        for container in _call("GET", f"{GTM_API}/{account['path']}/containers").get("container", []):
            if container.get("publicId") != public_id:
                continue
            workspaces = _call("GET", f"{GTM_API}/{container['path']}/workspaces").get("workspace", [])
            if not workspaces:
                return {"error": "no_workspace"}
            base = f"{GTM_API}/{workspaces[0]['path']}"
            triggers = dict(BUILT_IN_TRIGGERS)
            triggers.update({t["triggerId"]: t["name"] for t in _call("GET", f"{base}/triggers").get("trigger", [])})
            tags = [{"name": t["name"], "type": t["type"], "paused": t.get("paused", False),
                     "firing_triggers": [triggers.get(i, i) for i in t.get("firingTriggerId", [])],
                     "parameters": {p["key"]: p.get("value") for p in t.get("parameter", []) if "value" in p}}
                    for t in _call("GET", f"{base}/tags").get("tag", [])]
            live = _call("GET", f"{GTM_API}/{container['path']}/versions:live")
            return {"container": public_id, "workspace": workspaces[0].get("name"), "tags": tags,
                    "triggers": sorted(set(triggers.values())),
                    "variables": [v["name"] for v in _call("GET", f"{base}/variables").get("variable", [])],
                    "live_version": {"id": live.get("containerVersionId"), "name": live.get("name")}}
    return {"error": "container_not_found", "detail": f"{public_id} is not visible to the authorized Google account"}


# ------------------------------------------------------------------ Indexing API + PageSpeed
@tool("Indexing API status")
def indexing_status(
    url: Annotated[str, Field(description="Full URL to look up")],
) -> dict:
    """Latest Indexing API notifications (URL_UPDATED / URL_DELETED) Google holds for a URL. Read only: nothing is
    submitted. Most URLs were never notified, which is reported as such."""
    resp = _call("GET", "https://indexing.googleapis.com/v3/urlNotifications/metadata", params={"url": url})
    if resp.get("error") == 404:
        return {"status": "no Indexing API notification has been sent for this URL"}
    return resp


@tool("PageSpeed Insights")
def pagespeed(
    url: Annotated[str, Field(description="Full public URL to test")],
    strategy: Annotated[Literal["mobile", "desktop"], Field(description="Device profile Lighthouse emulates")] = "mobile",
) -> dict:
    """Lighthouse performance and SEO scores (0 to 100) plus LCP, CLS and TBT for a URL, and the Chrome UX Report
    field category when Google has real-user data. Set PAGESPEED_API_KEY to avoid the shared anonymous quota."""
    params = {"url": url, "strategy": strategy, "category": ["performance", "seo"]}
    if SETTINGS.pagespeed_api_key:
        params["key"] = SETTINGS.pagespeed_api_key
    try:
        data = HTTP.get("https://www.googleapis.com/pagespeedonline/v5/runPagespeed", params=params, timeout=120).json()
    except (requests.RequestException, ValueError) as exc:
        return {"error": "network", "detail": str(exc)}
    if "error" in data:
        message = data["error"].get("message", "")
        hint = "set PAGESPEED_API_KEY (Google Cloud API key with the PageSpeed Insights API enabled)" if "Quota" in message else ""
        return {"error": message, "hint": hint}
    lh, audits = data["lighthouseResult"], data["lighthouseResult"]["audits"]
    return {"performance": round(lh["categories"]["performance"]["score"] * 100),
            "seo": round(lh["categories"]["seo"]["score"] * 100),
            "lcp": audits["largest-contentful-paint"]["displayValue"],
            "cls": audits["cumulative-layout-shift"]["displayValue"],
            "tbt": audits["total-blocking-time"]["displayValue"],
            "field_data": data.get("loadingExperience", {}).get("overall_category")}

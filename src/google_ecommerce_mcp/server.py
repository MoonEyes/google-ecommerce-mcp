"""MCP tools. Every tool is read-only: no endpoint that creates, updates, publishes or deletes is ever called."""

from __future__ import annotations

import datetime
from urllib.parse import quote

import requests
from mcp.server.fastmcp import FastMCP

from . import __version__
from .auth import NotAuthenticated, TokenProvider
from .config import NotConfigured, Settings

SETTINGS = Settings.from_env()
TOKENS = TokenProvider(SETTINGS.token_file)
HTTP = requests.Session()
TIMEOUT = 60
MAX_ROWS = 1000

mcp = FastMCP("google-ecommerce-mcp")


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
@mcp.tool()
def server_status() -> dict:
    """Which Google services are configured for this server, and whether the stored token works."""
    return check()


# ------------------------------------------------------------------ GA4
@mcp.tool()
@_guard
def ga4_report(dimensions: list[str] = ["sessionDefaultChannelGroup"], metrics: list[str] = ["sessions", "totalUsers"],
               start_date: str = "28daysAgo", end_date: str = "yesterday", limit: int = 50,
               channel_group: str = "") -> dict:
    """GA4 report (Data API runReport). Any standard dimensions/metrics, e.g. landingPage, sessionSource, yearMonth;
    sessions, ecommercePurchases, purchaseRevenue, keyEvents. Dates: YYYY-MM-DD or relative (28daysAgo, yesterday).
    channel_group filters on sessionDefaultChannelGroup (e.g. 'Organic Search')."""
    prop = _require(SETTINGS.ga4_property_id, "GA4_PROPERTY_ID")
    limit = _cap(limit)
    body = {"dateRanges": [{"startDate": start_date, "endDate": end_date}],
            "dimensions": [{"name": d} for d in dimensions], "metrics": [{"name": m} for m in metrics], "limit": limit}
    if channel_group:
        body["dimensionFilter"] = {"filter": {"fieldName": "sessionDefaultChannelGroup",
                                              "stringFilter": {"value": channel_group}}}
    return _ga4_rows(_call("POST", f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport", json=body), limit)


@mcp.tool()
@_guard
def ga4_realtime(dimensions: list[str] = ["unifiedScreenName"], metrics: list[str] = ["activeUsers", "screenPageViews"]) -> dict:
    """GA4 realtime report (last 30 minutes). Handy to check that one page view is counted once, not twice."""
    prop = _require(SETTINGS.ga4_property_id, "GA4_PROPERTY_ID")
    body = {"dimensions": [{"name": d} for d in dimensions], "metrics": [{"name": m} for m in metrics]}
    return _ga4_rows(_call("POST", f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runRealtimeReport", json=body), 100)


# ------------------------------------------------------------------ Search Console
def _site() -> str:
    return quote(_require(SETTINGS.gsc_site_url, "GSC_SITE_URL"), safe="")


@mcp.tool()
@_guard
def gsc_performance(dimensions: list[str] = ["query"], start_date: str = "", end_date: str = "", limit: int = 50,
                    page_contains: str = "") -> dict:
    """Search Console clicks, impressions, CTR and average position. dimensions: query, page, country, device, date.
    Dates YYYY-MM-DD; default is the last 28 days ending 2 days ago (Search Console data lag)."""
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


@mcp.tool()
@_guard
def gsc_inspect_url(url: str) -> dict:
    """Google index status of one URL: verdict, coverage state, Google-selected canonical, last crawl time."""
    site = _require(SETTINGS.gsc_site_url, "GSC_SITE_URL")
    resp = _call("POST", "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect",
                 json={"inspectionUrl": url, "siteUrl": site})
    return resp if "error" in resp else resp.get("inspectionResult", {}).get("indexStatusResult", resp)


@mcp.tool()
@_guard
def gsc_sitemaps() -> dict:
    """Sitemaps declared in Search Console, with last submission, last download, errors and warnings."""
    return _call("GET", f"https://www.googleapis.com/webmasters/v3/sites/{_site()}/sitemaps")


# ------------------------------------------------------------------ Merchant Center (Merchant API v1)
MERCHANT = "https://merchantapi.googleapis.com"


@mcp.tool()
@_guard
def merchant_data_sources() -> dict:
    """Merchant Center data sources (feeds): labels, countries, languages, fetch URLs."""
    acc = _require(SETTINGS.merchant_account_id, "MERCHANT_ACCOUNT_ID")
    return _call("GET", f"{MERCHANT}/datasources/v1/accounts/{acc}/dataSources")


@mcp.tool()
@_guard
def merchant_product_issues(limit: int = 50, only_with_issues: bool = True, max_pages: int = 20) -> dict:
    """Merchant Center products with their item-level issues (disapprovals, warnings) per destination.
    Walks through all pages (250 products each, up to max_pages)."""
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


@mcp.tool()
@_guard
def merchant_report_query(query: str = "SELECT id, offer_id, title, aggregated_reporting_context_status FROM product_view LIMIT 50") -> dict:
    """Merchant Center Query Language (MCQL) report search, e.g. on product_view or product_performance_view.
    product_view queries must select the id field."""
    acc = _require(SETTINGS.merchant_account_id, "MERCHANT_ACCOUNT_ID")
    return _call("POST", f"{MERCHANT}/reports/v1/accounts/{acc}/reports:search", json={"query": query})


# ------------------------------------------------------------------ Tag Manager (read only)
GTM_API = "https://tagmanager.googleapis.com/tagmanager/v2"
BUILT_IN_TRIGGERS = {"2147479553": "All Pages (built-in)", "2147479573": "Initialization - All Pages (built-in)",
                     "2147479572": "Consent Initialization - All Pages (built-in)"}


@mcp.tool()
@_guard
def gtm_inventory() -> dict:
    """Tags, triggers and variables of the container's default workspace, plus the live (published) version."""
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
@mcp.tool()
def indexing_status(url: str) -> dict:
    """Latest Indexing API notifications Google holds for a URL (read only, nothing is submitted)."""
    resp = _call("GET", "https://indexing.googleapis.com/v3/urlNotifications/metadata", params={"url": url})
    if resp.get("error") == 404:
        return {"status": "no Indexing API notification has been sent for this URL"}
    return resp


@mcp.tool()
def pagespeed(url: str, strategy: str = "mobile") -> dict:
    """Lighthouse performance and SEO scores plus Core Web Vitals for a URL. Set PAGESPEED_API_KEY to avoid the
    shared anonymous quota. strategy: mobile or desktop."""
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

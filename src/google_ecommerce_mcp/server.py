"""MCP tools. Every tool is read-only: no endpoint that creates, updates, publishes or deletes is ever called.

The guarantee does not rest on the readOnlyHint annotation (a client may ignore it): every outgoing request is
checked against ALLOWED_ENDPOINTS before it leaves the process, and anything else is refused without a network
call. tests/test_server.py fails the build if a tool needs an endpoint outside that list."""

from __future__ import annotations

import datetime
import re
from urllib.parse import quote, urlsplit

from typing import Annotated, Literal

import requests
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from .auth import NotAuthenticated, TokenProvider
from . import metrics
from .config import API_BY_HOST, SCOPE_BY_HOST, WRITE_CAPABLE_SCOPES, NotConfigured, Settings

SETTINGS = Settings.from_env()
TOKENS = TokenProvider(SETTINGS.token_file)
HTTP = requests.Session()
TIMEOUT = 60
MAX_ROWS = 1000

mcp = FastMCP(
    "google-ecommerce-mcp",
    instructions=(
        "Read-only access to the shop's Google accounts. Start with server_status to see which services are "
        "configured. Traffic and revenue: ga4_report (ga4_realtime for the last 30 minutes); ga4_properties lists "
        "the GA4 properties the account can read, to find a property id. Organic search: "
        "gsc_performance, gsc_inspect_url for one URL's index state, gsc_sitemaps. Product feeds and "
        "disapprovals: merchant_product_issues, merchant_data_sources, merchant_report_query. Tracking setup: "
        "gtm_inventory. Speed: pagespeed. No tool can modify anything. Report results carry totals, the unit and "
        "definition of each metric, and a date_range with its timezone and data_complete: when data_complete is "
        "false, the days from settling_from can still change, so do not compare them with complete days."
    ),
    website_url="https://github.com/MoonEyes/google-ecommerce-mcp",
)
mcp._mcp_server.version = __version__  # otherwise clients see the mcp library version

# Every tool only reads from Google: declared to MCP clients through the standard annotations.
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)


def tool(title: str):
    return mcp.tool(title=title, annotations=READ_ONLY)


# ------------------------------------------------------------------ allow-list (the real read-only guarantee)
# Every request this server may send. POST appears only on query endpoints that read data.
# Adding a tool that needs another endpoint means adding it here, where review can see it.
ALLOWED_ENDPOINTS = [
    ("POST", r"https://analyticsdata\.googleapis\.com/v1beta/properties/[^/]+:runReport"),
    ("POST", r"https://analyticsdata\.googleapis\.com/v1beta/properties/[^/]+:runRealtimeReport"),
    ("GET", r"https://analyticsadmin\.googleapis\.com/v1beta/accountSummaries"),
    ("POST", r"https://www\.googleapis\.com/webmasters/v3/sites/[^/]+/searchAnalytics/query"),
    ("GET", r"https://www\.googleapis\.com/webmasters/v3/sites/[^/]+/sitemaps"),
    ("POST", r"https://searchconsole\.googleapis\.com/v1/urlInspection/index:inspect"),
    ("GET", r"https://merchantapi\.googleapis\.com/datasources/v1/accounts/[^/]+/dataSources"),
    ("GET", r"https://merchantapi\.googleapis\.com/products/v1/accounts/[^/]+/products"),
    ("GET", r"https://merchantapi\.googleapis\.com/products/v1/accounts/[^/]+/products/[^/]+"),
    ("POST", r"https://merchantapi\.googleapis\.com/reports/v1/accounts/[^/]+/reports:search"),
    ("GET", r"https://tagmanager\.googleapis\.com/tagmanager/v2/accounts"),
    ("GET", r"https://tagmanager\.googleapis\.com/tagmanager/v2/accounts/[^/]+/containers"),
    ("GET", r"https://tagmanager\.googleapis\.com/tagmanager/v2/accounts/[^/]+/containers/[^/]+/workspaces"),
    ("GET", r"https://tagmanager\.googleapis\.com/tagmanager/v2/accounts/[^/]+/containers/[^/]+/workspaces/[^/]+/(tags|triggers|variables)"),
    ("GET", r"https://tagmanager\.googleapis\.com/tagmanager/v2/accounts/[^/]+/containers/[^/]+/versions:live"),
    ("GET", r"https://indexing\.googleapis\.com/v3/urlNotifications/metadata"),
    ("GET", r"https://www\.googleapis\.com/pagespeedonline/v5/runPagespeed"),
]
_ALLOWED = [(m, re.compile(pattern + r"$")) for m, pattern in ALLOWED_ENDPOINTS]


def is_allowed(method: str, url: str) -> bool:
    parts = urlsplit(url)
    bare = f"{parts.scheme}://{parts.netloc}{parts.path}"
    return any(method.upper() == m and rx.match(bare) for m, rx in _ALLOWED)


class Blocked(Exception):
    """Raised when code tries to send a request outside ALLOWED_ENDPOINTS."""


BLOCKED_ATTEMPTS: list[tuple[str, str]] = []  # kept so the test suite can fail the build on any attempt


def _send(method: str, url: str, **kwargs):
    if not is_allowed(method, url):
        BLOCKED_ATTEMPTS.append((method, url))
        raise Blocked(f"{method} {url} is not on the read-only allow-list; request not sent")
    return HTTP.request(method, url, **kwargs)


# ------------------------------------------------------------------ helpers
def _require(value: str | None, variable: str) -> str:
    if not value:
        raise NotConfigured(variable)
    return value


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _scope_for(url: str) -> str | None:
    return next((scope for host, scope in SCOPE_BY_HOST.items() if host in url), None)


def _api_for(url: str) -> str:
    return next((api for host, api in API_BY_HOST.items() if host in url), "the API this tool calls")


def _api_error(response, data, url: str) -> dict:
    """Structured error: the caller learns what is missing and what to do, not just a status code."""
    detail = data.get("error", data) if isinstance(data, dict) else data
    text = str(detail)
    if response.status_code == 403 and ("SCOPE_INSUFFICIENT" in text or "insufficient authentication scopes" in text):
        scope = _scope_for(url)
        return {"error": "missing_scope", "required_scope": scope,
                "fix": "rerun `google-ecommerce-mcp setup` and grant this scope"
                       + (" (add --with-merchant or --with-indexing)" if scope in WRITE_CAPABLE_SCOPES else ""),
                "detail": detail}
    if response.status_code == 403 and ("SERVICE_DISABLED" in text or "has not been used in project" in text
                                        or "it is disabled" in text):
        return {"error": "api_disabled", "api": _api_for(url),
                "fix": f"enable {_api_for(url)} in Google Cloud Console (APIs & Services > Library) for the project "
                       "of your OAuth client, wait a few minutes, then retry",
                "detail": detail}
    if response.status_code == 429:
        headers = getattr(response, "headers", None) or {}
        return {"error": "rate_limited", "retry_after_seconds": headers.get("Retry-After"),
                "fix": "wait and retry, or narrow the request (shorter date range, smaller limit)", "detail": detail}
    return {"error": response.status_code, "detail": detail}


def _call(method: str, url: str, **kwargs) -> dict:
    try:
        response = _send(method, url, headers=TOKENS.headers(), timeout=TIMEOUT, **kwargs)
    except Blocked as exc:
        return {"error": "blocked", "detail": str(exc)}
    except NotAuthenticated as exc:
        return {"error": "not_authenticated", "detail": str(exc),
                "fix": "rerun `google-ecommerce-mcp setup --client-secret <file>` (or the install line)"}
    except requests.RequestException as exc:
        return {"error": "network", "detail": str(exc)}
    try:
        data = response.json()
    except ValueError:
        data = {"text": response.text[:500]}
    if not response.ok:
        return _api_error(response, data, url)
    return data


def _guard(freshness: str):
    """Turn configuration errors into a readable result, and stamp every result with when it was read and how
    fresh the underlying Google data is, so the assistant never has to guess."""

    def decorate(fn):
        def wrapper(*args, **kwargs):
            try:
                result = fn(*args, **kwargs)
            except NotConfigured as exc:
                result = {"error": "not_configured", "detail": str(exc)}
            if isinstance(result, dict):
                result.setdefault("fetched_at", _now())
                result.setdefault("freshness", freshness)
            return result

        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        wrapper.__annotations__ = fn.__annotations__
        wrapper.__wrapped__ = fn
        return wrapper

    return decorate


def _cap(limit: int) -> int:
    return max(1, min(int(limit), MAX_ROWS))


GA4_SETTLING_NOTE = "GA4 can still revise the last 24 to 48 hours; compare complete days only"
GSC_SETTLING_NOTE = "Search Console fills the last 2 days late; recent days may be missing or partial"


def _ga4_rows(resp: dict, limit: int, date_range: dict | None = None) -> dict:
    """Flatten a GA4 response: numbers as numbers, totals, unit and definition per metric, timezone, currency."""
    if "error" in resp:
        return resp
    dims = [d["name"] for d in resp.get("dimensionHeaders", [])]
    headers = resp.get("metricHeaders", [])
    mets = [m["name"] for m in headers]
    types = [m.get("type", "") for m in headers]
    meta = resp.get("metadata", {})

    def values(row: dict) -> dict:
        return {name: metrics.to_number(v.get("value"), kind)
                for name, kind, v in zip(mets, types, row.get("metricValues", []))}

    rows = []
    for row in resp.get("rows", [])[:limit]:
        item = dict(zip(dims, (v["value"] for v in row.get("dimensionValues", []))))
        item.update(values(row))
        rows.append(item)
    row_count = resp.get("rowCount", len(rows))
    out = {"rows": rows, "row_count": row_count, "truncated": row_count > len(rows)}
    if resp.get("totals"):
        out["totals"] = values(resp["totals"][0])
    out["metrics"] = metrics.ga4_metric_info(headers, meta.get("currencyCode"))
    if meta.get("currencyCode"):
        out["currency"] = meta["currencyCode"]
    if date_range:
        out["date_range"] = date_range
    return out


def metrics_date_range(start: str, end: str, timezone: str | None, note: str) -> dict:
    return metrics.date_range_info(start, end, timezone, settle_days=2, note=note)


def _ga4_property(property_id: str) -> str:
    """The property to query: the one passed (from ga4_properties) or GA4_PROPERTY_ID."""
    prop = (property_id or "").strip().removeprefix("properties/")
    if prop:
        if not prop.isdigit():
            raise ValueError(f"property_id must be the numeric GA4 property id, got {property_id!r}")
        return prop
    return _require(SETTINGS.ga4_property_id, "GA4_PROPERTY_ID")


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
    try:
        granted = TOKENS.granted_scopes()
    except Exception:
        granted = []
    status["scopes"] = granted
    status["write_capable_scopes"] = sorted(set(granted) & WRITE_CAPABLE_SCOPES)
    status["version"] = __version__
    return status


# ------------------------------------------------------------------ server info
@tool("Server status")
@_guard("live: token tested now")
def server_status() -> dict:
    """Which Google services are configured for this server, and whether the stored OAuth token works.
    Call it first when another tool answers not_configured or not_authenticated. Returns one boolean per
    service, the token storage (os-keyring or file), "token": "ok" or the error, and the package version."""
    return check()


# ------------------------------------------------------------------ GA4
PROPERTY_ID = Annotated[str, Field(description="Numeric GA4 property id, as listed by ga4_properties; "
                                            "empty uses the configured GA4_PROPERTY_ID")]


@tool("GA4 report")
@_guard("GA4 processed data; the last 24 to 48 hours can still change")
def ga4_report(
    dimensions: Annotated[list[str], Field(description="GA4 API dimension names, e.g. sessionDefaultChannelGroup, "
                                           "landingPage, sessionSource, yearMonth, itemName")] = ["sessionDefaultChannelGroup"],
    metrics: Annotated[list[str], Field(description="GA4 API metric names, e.g. sessions, totalUsers, "
                                        "ecommercePurchases, purchaseRevenue, keyEvents")] = ["sessions", "totalUsers"],
    start_date: Annotated[str, Field(description="YYYY-MM-DD, NdaysAgo, yesterday or today")] = "28daysAgo",
    end_date: Annotated[str, Field(description="YYYY-MM-DD, NdaysAgo, yesterday or today")] = "yesterday",
    limit: Annotated[int, Field(description="Maximum rows returned (1 to 1000); totals always cover every row",
                                ge=1, le=1000)] = 50,
    channel_group: Annotated[str, Field(description="Optional exact filter on sessionDefaultChannelGroup, "
                                        "e.g. 'Organic Search'; empty for all channels")] = "",
    property_id: PROPERTY_ID = "",
) -> dict:
    """Run a Google Analytics 4 report (Data API runReport): traffic, conversions or revenue split by any dimensions
    over a date range. Returns the top rows ({"rows": [{dimension: value, metric: number}]}), "totals" over every
    row, "row_count" and "truncated", the unit and definition of each metric under "metrics", the currency, and a
    "date_range" resolved to calendar dates in the property's timezone with "data_complete". Use ga4_realtime for the
    last 30 minutes."""
    try:
        prop = _ga4_property(property_id)
    except ValueError as exc:
        return {"error": "invalid_property_id", "detail": str(exc)}
    limit = _cap(limit)
    body = {"dateRanges": [{"startDate": start_date, "endDate": end_date}],
            "dimensions": [{"name": d} for d in dimensions], "metrics": [{"name": m} for m in metrics],
            "limit": limit, "metricAggregations": ["TOTAL"]}
    if channel_group:
        body["dimensionFilter"] = {"filter": {"fieldName": "sessionDefaultChannelGroup",
                                              "stringFilter": {"value": channel_group}}}
    resp = _call("POST", f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport", json=body)
    if "error" in resp:
        return resp
    timezone = resp.get("metadata", {}).get("timeZone")
    out = _ga4_rows(resp, limit, metrics_date_range(start_date, end_date, timezone, GA4_SETTLING_NOTE))
    out["property_id"] = prop
    return out


@tool("GA4 realtime")
@_guard("realtime: last 30 minutes")
def ga4_realtime(
    dimensions: Annotated[list[str], Field(description="GA4 realtime dimensions, e.g. unifiedScreenName, "
                                           "country, deviceCategory")] = ["unifiedScreenName"],
    metrics: Annotated[list[str], Field(description="GA4 realtime metrics, e.g. activeUsers, screenPageViews, "
                                        "eventCount")] = ["activeUsers", "screenPageViews"],
    property_id: PROPERTY_ID = "",
) -> dict:
    """GA4 realtime report for the last 30 minutes. Useful to check a tracking change, for example that one page
    view is counted once and not twice. Returns {"rows": [...], "row_count": n, "totals": {...}, "metrics": {...}}."""
    try:
        prop = _ga4_property(property_id)
    except ValueError as exc:
        return {"error": "invalid_property_id", "detail": str(exc)}
    body = {"dimensions": [{"name": d} for d in dimensions], "metrics": [{"name": m} for m in metrics],
            "metricAggregations": ["TOTAL"]}
    out = _ga4_rows(_call("POST", f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runRealtimeReport",
                          json=body), 100, {"start": "30 minutes ago", "end": "now", "data_complete": False,
                                            "settling_note": "realtime counts keep moving"})
    if "error" not in out:
        out["property_id"] = prop
    return out


@tool("GA4 properties")
@_guard("current state")
def ga4_properties(
    max_pages: Annotated[int, Field(description="Pages of 200 accounts to read at most", ge=1, le=20)] = 5,
) -> dict:
    """GA4 accounts and properties the authorized Google account can read (Admin API accountSummaries), so the
    assistant can find a property id itself and pass it as property_id to ga4_report or ga4_realtime. Returns
    {"properties": [{property_id, property_name, property_type, account_id, account_name, configured}],
    "configured_property_id"}. Needs the Google Analytics Admin API enabled; no extra OAuth scope."""
    url = "https://analyticsadmin.googleapis.com/v1beta/accountSummaries"
    found, token, pages = [], None, 0
    while pages < max_pages:
        params = {"pageSize": 200}
        if token:
            params["pageToken"] = token
        resp = _call("GET", url, params=params)
        if "error" in resp:
            if not pages:
                return resp
            return {"properties": found, "configured_property_id": SETTINGS.ga4_property_id,
                    "truncated": True, "partial": True, "partial_error": resp}
        pages += 1
        for account in resp.get("accountSummaries", []):
            account_id = account.get("account", "").removeprefix("accounts/")
            for prop in account.get("propertySummaries", []):
                prop_id = prop.get("property", "").removeprefix("properties/")
                found.append({"property_id": prop_id, "property_name": prop.get("displayName"),
                              "property_type": prop.get("propertyType"), "account_id": account_id,
                              "account_name": account.get("displayName"),
                              "configured": prop_id == SETTINGS.ga4_property_id})
        token = resp.get("nextPageToken")
        if not token:
            break
    return {"properties": found, "configured_property_id": SETTINGS.ga4_property_id, "truncated": bool(token)}


# ------------------------------------------------------------------ Search Console
def _site() -> str:
    return quote(_require(SETTINGS.gsc_site_url, "GSC_SITE_URL"), safe="")


@tool("Search Console performance")
@_guard("Search Console data lags about 2 days")
def gsc_performance(
    dimensions: Annotated[list[Literal["query", "page", "country", "device", "date", "searchAppearance"]],
                          Field(description="How to split the results")] = ["query"],
    start_date: Annotated[str, Field(description="YYYY-MM-DD; empty means 30 days ago (Pacific Time)")] = "",
    end_date: Annotated[str, Field(description="YYYY-MM-DD; empty means 2 days ago (Search Console data lag)")] = "",
    limit: Annotated[int, Field(description="Maximum rows returned (1 to 1000); totals always cover the whole "
                                "property", ge=1, le=1000)] = 50,
    page_contains: Annotated[str, Field(description="Optional: keep only pages whose URL contains this text")] = "",
) -> dict:
    """Google Search Console search performance for the configured property: clicks, impressions, CTR and average
    position, split by query, page, country, device or date. Returns the top rows, "totals" for the whole period
    (same filter, no split), "truncated", the unit and definition of each metric under "metrics", and a
    "date_range" in Pacific Time (Search Console's timezone) with "data_complete"."""
    limit = _cap(limit)
    today, _ = metrics.today_in(metrics.GSC_TIMEZONE)
    start = start_date or (today - datetime.timedelta(days=30)).isoformat()
    end = end_date or (today - datetime.timedelta(days=2)).isoformat()
    body = {"startDate": start, "endDate": end, "dimensions": dimensions, "rowLimit": limit}
    if page_contains:
        body["dimensionFilterGroups"] = [{"filters": [{"dimension": "page", "operator": "contains",
                                                       "expression": page_contains}]}]
    url = f"https://www.googleapis.com/webmasters/v3/sites/{_site()}/searchAnalytics/query"
    resp = _call("POST", url, json=body)
    if "error" in resp:
        return resp
    rows = [dict(zip(dimensions, r["keys"]), clicks=r["clicks"], impressions=r["impressions"],
                 ctr=round(r["ctr"], 4), position=round(r["position"], 1)) for r in resp.get("rows", [])]
    out = {"rows": rows, "truncated": len(rows) >= limit}
    # Totals come from the same query without dimensions: summing the rows would miss the rows beyond the limit and
    # the queries Google anonymizes, so row sums are always lower than the real total.
    totals = _call("POST", url, json={k: v for k, v in body.items() if k not in ("dimensions", "rowLimit")})
    if "error" in totals:
        out.update(partial=True, partial_error={"step": "totals", **totals})
    else:
        t = (totals.get("rows") or [{"clicks": 0, "impressions": 0, "ctr": 0, "position": 0}])[0]
        out["totals"] = {"clicks": t["clicks"], "impressions": t["impressions"], "ctr": round(t["ctr"], 4),
                         "position": round(t["position"], 1)}
        if "query" in dimensions:
            out["totals_note"] = "totals include anonymized queries, so they exceed the sum of query rows"
    out["metrics"] = metrics.GSC_METRICS
    out["date_range"] = metrics_date_range(start, end, metrics.GSC_TIMEZONE, GSC_SETTLING_NOTE)
    return out


@tool("Search Console URL inspection")
@_guard("Google's current index state; see lastCrawlTime for the crawl date")
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
@_guard("current state")
def gsc_sitemaps() -> dict:
    """Sitemaps declared in Search Console, with last submission, last download, errors and warnings."""
    return _call("GET", f"https://www.googleapis.com/webmasters/v3/sites/{_site()}/sitemaps")


# ------------------------------------------------------------------ Merchant Center (Merchant API v1)
MERCHANT = "https://merchantapi.googleapis.com"


@tool("Merchant Center data sources")
@_guard("current state")
def merchant_data_sources() -> dict:
    """Merchant Center data sources (feeds): labels, countries, languages, fetch URLs."""
    acc = _require(SETTINGS.merchant_account_id, "MERCHANT_ACCOUNT_ID")
    return _call("GET", f"{MERCHANT}/datasources/v1/accounts/{acc}/dataSources")


@tool("Merchant Center product issues")
@_guard("current state; issues update a few hours after a feed fetch")
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
            if not pages:
                return resp
            # Keep what was already scanned: a quota hit on page 7 should not throw away pages 1 to 6.
            return {"products": out, "scanned": scanned, "truncated": True, "partial": True, "partial_error": resp}
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
@_guard("performance tables lag about 1 day; product_view is current")
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
@_guard("current workspace state, which may differ from the live version")
def gtm_inventory() -> dict:
    """Tags (type, paused, firing triggers, parameters), triggers and variables of the configured Google Tag Manager
    container's default workspace, plus the id of the live published version. Built-in trigger ids are translated,
    e.g. All Pages. Useful to spot a tag configured twice."""
    public_id = _require(SETTINGS.gtm_container_id, "GTM_CONTAINER_ID")
    accounts = _call("GET", f"{GTM_API}/accounts")
    if "error" in accounts:
        return accounts
    errors: list[dict] = []

    def fetch(step: str, url: str, key: str) -> list:
        """One sub-request; on failure record which part is missing instead of returning an empty list silently."""
        resp = _call("GET", url)
        if "error" in resp:
            errors.append({"step": step, **resp})
            return []
        return resp.get(key, [])

    for account in accounts.get("account", []):
        for container in fetch(f"containers of {account['path']}", f"{GTM_API}/{account['path']}/containers", "container"):
            if container.get("publicId") != public_id:
                continue
            workspaces = fetch("workspaces", f"{GTM_API}/{container['path']}/workspaces", "workspace")
            if not workspaces:
                return {"error": "no_workspace", "partial_errors": errors}
            base = f"{GTM_API}/{workspaces[0]['path']}"
            triggers = dict(BUILT_IN_TRIGGERS)
            triggers.update({t["triggerId"]: t["name"] for t in fetch("triggers", f"{base}/triggers", "trigger")})
            tags = [{"name": t["name"], "type": t["type"], "paused": t.get("paused", False),
                     "firing_triggers": [triggers.get(i, i) for i in t.get("firingTriggerId", [])],
                     "parameters": {p["key"]: p.get("value") for p in t.get("parameter", []) if "value" in p}}
                    for t in fetch("tags", f"{base}/tags", "tag")]
            variables = [v["name"] for v in fetch("variables", f"{base}/variables", "variable")]
            live = _call("GET", f"{GTM_API}/{container['path']}/versions:live")
            if "error" in live:
                errors.append({"step": "live version", **live})
                live = {}
            result = {"container": public_id, "workspace": workspaces[0].get("name"), "tags": tags,
                      "triggers": sorted(set(triggers.values())), "variables": variables,
                      "live_version": {"id": live.get("containerVersionId"), "name": live.get("name")}}
            if errors:
                result.update(partial=True, partial_errors=errors)
            return result
    if errors:
        return {"error": "container_not_found", "detail": f"{public_id} not found; some accounts could not be read",
                "partial_errors": errors}
    return {"error": "container_not_found", "detail": f"{public_id} is not visible to the authorized Google account"}


# ------------------------------------------------------------------ Indexing API + PageSpeed
@tool("Indexing API status")
@_guard("current state")
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
@_guard("lab test run now; field_data covers the last 28 days")
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
        data = _send("GET", "https://www.googleapis.com/pagespeedonline/v5/runPagespeed", params=params, timeout=120).json()
    except Blocked as exc:
        return {"error": "blocked", "detail": str(exc)}
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

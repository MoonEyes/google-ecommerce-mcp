"""End to end: real tool code on a simulated upstream, then the real `report` command on the raw outputs.

Chain proved here: upstream HTTP answer (shaped like each API's documentation) -> the REAL tool functions (server,
google_ads, meta_ads, openai_ads, ads_overview) -> their raw outputs, unmodified -> report.json assembled exactly as
docs/REPORTING.md "How to produce a report" describes -> `google-ecommerce-mcp report` (__main__.main) -> HTML whose
figures are checked against the simulated upstream.

The upstream is simulated at the lowest level available without touching the tested code: every HTTP client
(server.HTTP, and the requests.Session of each AdsClient) is a REAL requests.Session whose transport adapter is
the `Upstream` below (tests/ads_fakes.real_session_with + a routing FakeAdapter). Nothing opens a socket; the
autouse `no_network` fixture of ads_fakes fails the test on any other request. OAuth tokens are real token files
(google.oauth2 Credentials with a far expiry, so no refresh is attempted), the keyring is the in-memory
`no_keyring`, every secret is an obviously fake value assembled at run time.

Upstream shapes (documentation of each API):
- GA4 Data API runReport / runRealtimeReport: dimensionHeaders, metricHeaders (typed), rows of string values,
  totals, rowCount, metadata.currencyCode / timeZone; the `date` dimension is YYYYMMDD.
- Search Console searchAnalytics.query: rows [{keys, clicks, impressions, ctr, position}]; no keys without dimension.
- Merchant API v1: products.list {products: [{offerId, feedLabel, productAttributes, productStatus.itemLevelIssues}],
  nextPageToken}; reports:search {results: [{productPerformanceView | productView: {...}}]}, int64 as strings.
- Tag Manager v2: account / container / workspace / tag / trigger / variable lists, versions:live.
- PageSpeed v5: lighthouseResult (categories scores 0..1, audits displayValue), loadingExperience.
- Google Ads searchStream: a JSON ARRAY of batches {results, fieldMask, requestId}, camelCase, int64 (costMicros,
  impressions, clicks) as strings, conversions / conversionsValue as numbers.
- Meta Insights: {data: [{spend, impressions, ... as strings, actions, action_values, purchase_roas,
  account_currency, date_start, date_stop}], paging: {cursors: {before, after}}} (no paging.next on the last page).
- OpenAI Ads insights: {object: "list", data, count, first_id, last_id, has_more}.
"""

from __future__ import annotations

import datetime as dt
import html as html_lib
import importlib
import io
import json
import re
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import requests

from ads_fakes import (GOOGLE_ACCESS_PREFIX, GOOGLE_REFRESH_PREFIX, META_TOKEN_PREFIX, OPENAI_KEY_PREFIX,
                       FakeAdapter, no_keyring, no_network, real_session_with)  # noqa: F401  (fixtures)
from google_ecommerce_mcp import ads_overview as overview_mod
from google_ecommerce_mcp import google_ads, meta_ads, openai_ads
from google_ecommerce_mcp.__main__ import main
from google_ecommerce_mcp.report.i18n import fmt_int, fmt_money, fmt_number, tr

CURRENT = ("2026-09-01", "2026-09-30")
PREVIOUS = ("2026-08-01", "2026-08-31")
YOY = ("2025-09-01", "2025-09-30")
PERIODS = {"current": CURRENT, "previous": PREVIOUS, "yoy": YOY}
# Activity level of each period in the simulated accounts (the figures below are derived from it).
FACTOR = {CURRENT: 1.0, PREVIOUS: 0.9, YOY: 0.8}

GA4_PROPERTY = "424242"
GSC_SITE = "sc-domain:shop.example"
MERCHANT_ID = "5550001"
GTM_ID = "GTM-ABC1234"
GADS_CUSTOMER = "1234567890"
META_ACCOUNT = "777000111"
SHOP_URL = "https://shop.example/"


def days(period):
    start, end = (dt.date.fromisoformat(d) for d in period)
    return [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]


def scaled(value, period):
    return int(round(value * FACTOR[period]))


# ====================================================================================== simulated upstream data
def ga4_daily(period):
    """[(YYYYMMDD, sessions, orders, revenue)] of one period."""
    out = []
    for day in days(period):
        sessions = scaled(300 + (day.day % 7) * 5, period)
        orders = scaled(5 + day.day % 4, period)
        out.append((day.strftime("%Y%m%d"), sessions, orders, round(orders * 145.6, 2)))
    return out


def ga4_kpi_totals(period):
    rows = ga4_daily(period)
    return {"sessions": sum(r[1] for r in rows), "ecommercePurchases": sum(r[2] for r in rows),
            "purchaseRevenue": round(sum(r[3] for r in rows), 2)}


CHANNEL_WEIGHTS = [("Organic Search", 0.38), ("Paid Search", 0.22), ("Direct", 0.17), ("Paid Social", 0.12),
                   ("Email", 0.07), ("Referral", 0.04)]
PRODUCTS = [("Chaussure Trail", 61, 89.9, 940), ("Sac Rando 30L", 38, 74.5, 610), ("Gourde Inox", 52, 19.9, 820),
            ("Veste Pluie", 17, 129.0, 450), ("Bonnet Laine", 25, 24.0, 300), ("Chaussettes Merinos", 44, 14.5, 380),
            ("Lampe Frontale", 12, 39.0, 260)]
QUERIES = [("chaussure trail", 410, 5200, 4.1), ("sac rando 30l", 260, 3900, 6.3), ("gourde inox", 180, 4100, 8.2),
           ("veste pluie homme", 95, 2600, 11.4), ("shop example", 520, 900, 1.1)]
PAGES = [("https://shop.example/p/chaussure-trail", 520), ("https://shop.example/", 610),
         ("https://shop.example/p/sac-rando-30l?gclid=TEST", 300)]


def split(total, weights, digits=0):
    """Split total by weights; the last share takes the rounding remainder (shares sum exactly to total)."""
    parts = [round(total * w, digits) for _, w in weights[:-1]]
    parts.append(round(total - sum(parts), digits))
    return [int(p) if digits == 0 else p for p in parts]


def ga4_report_body(dims, mets, period):
    """(rows as {name: number}, ordered) for a runReport request."""
    if dims == ["date"]:
        return [{"date": d, "sessions": s, "ecommercePurchases": o, "purchaseRevenue": r}
                for d, s, o, r in ga4_daily(period)]
    totals = ga4_kpi_totals(period)
    if dims == ["sessionDefaultChannelGroup"]:
        sessions = split(totals["sessions"], CHANNEL_WEIGHTS)
        orders = split(totals["ecommercePurchases"], CHANNEL_WEIGHTS)
        revenue = split(totals["purchaseRevenue"], CHANNEL_WEIGHTS, 2)
        return [{"sessionDefaultChannelGroup": n, "sessions": s, "ecommercePurchases": o, "purchaseRevenue": r}
                for (n, _), s, o, r in zip(CHANNEL_WEIGHTS, sessions, orders, revenue)]
    if dims == ["eventName"]:
        t = totals
        counts = {"page_view": t["sessions"] * 4, "session_start": t["sessions"], "user_engagement":
                  t["sessions"] * 2, "view_item": int(t["sessions"] * 0.55), "scroll": int(t["sessions"] * 0.8),
                  "add_to_cart": int(t["sessions"] * 0.09), "begin_checkout": int(t["sessions"] * 0.04),
                  "first_visit": int(t["sessions"] * 0.6), "purchase": t["ecommercePurchases"]}
        return [{"eventName": k, "eventCount": v} for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]
    if dims == ["itemName"]:
        return [{"itemName": n, "itemsPurchased": scaled(q, period), "itemRevenue": round(scaled(q, period) * p, 2),
                 "itemsViewed": scaled(v, period)} for n, q, p, v in PRODUCTS]
    raise AssertionError(f"unexpected GA4 request {dims} {mets}")


GA4_TYPES = {"sessions": "TYPE_INTEGER", "ecommercePurchases": "TYPE_INTEGER", "purchaseRevenue": "TYPE_CURRENCY",
             "eventCount": "TYPE_INTEGER", "itemsPurchased": "TYPE_INTEGER", "itemRevenue": "TYPE_CURRENCY",
             "itemsViewed": "TYPE_INTEGER", "activeUsers": "TYPE_INTEGER", "screenPageViews": "TYPE_INTEGER"}


def ga4_value(v):
    return str(v)


def ga4_payload(dims, mets, rows, realtime=False):
    """The runReport JSON (strings everywhere, totals over every row), as the GA4 Data API sends it."""
    def metric_values(row):
        return [{"value": ga4_value(row[m])} for m in mets]

    totals = {m: (round(sum(r[m] for r in rows), 2) if GA4_TYPES[m] == "TYPE_CURRENCY" else sum(r[m] for r in rows))
              for m in mets}
    payload = {"dimensionHeaders": [{"name": d} for d in dims],
               "metricHeaders": [{"name": m, "type": GA4_TYPES[m]} for m in mets],
               "rows": [{"dimensionValues": [{"value": r[d]} for d in dims], "metricValues": metric_values(r)}
                        for r in rows],
               "totals": [{"dimensionValues": [{"value": "RESERVED_TOTAL"}] * len(dims),
                           "metricValues": metric_values(totals)}],
               "rowCount": len(rows),
               "kind": "analyticsData#runRealtimeReport" if realtime else "analyticsData#runReport"}
    if not realtime:
        payload["metadata"] = {"currencyCode": "EUR", "timeZone": "Europe/Paris"}
    return payload


def gsc_rows(dims, period):
    if dims == []:
        clicks = sum(r["clicks"] for r in gsc_rows(["date"], period))
        impressions = sum(r["impressions"] for r in gsc_rows(["date"], period))
        return [{"clicks": clicks, "impressions": impressions, "ctr": clicks / impressions, "position": 7.4}]
    if dims == ["date"]:
        return [{"keys": [d.isoformat()], "clicks": scaled(60 + d.day % 5 * 4, period),
                 "impressions": scaled(1500 + d.day * 10, period), "ctr": 0.04, "position": 7.4}
                for d in days(period)]
    if dims == ["query"]:
        return [{"keys": [q], "clicks": scaled(c, period), "impressions": scaled(i, period),
                 "ctr": scaled(c, period) / scaled(i, period), "position": p} for q, c, i, p in QUERIES]
    if dims == ["page"]:
        return [{"keys": [u], "clicks": scaled(c, period), "impressions": scaled(c * 12, period),
                 "ctr": 1 / 12, "position": 6.0} for u, c in PAGES]
    raise AssertionError(f"unexpected GSC dimensions {dims}")


def gsc_totals(period):
    return gsc_rows([], period)[0]


def merchant_product(offer, title, issues):
    return {"name": f"accounts/{MERCHANT_ID}/products/online~fr~FR~{offer}", "offerId": offer,
            "contentLanguage": "fr", "feedLabel": "FR", "dataSource": f"accounts/{MERCHANT_ID}/dataSources/1",
            "productAttributes": {"title": title, "link": f"https://shop.example/p/{offer.lower()}",
                                  "price": {"amountMicros": "89900000", "currencyCode": "EUR"}},
            "productStatus": {"destinationStatuses": [{"reportingContext": "SHOPPING_ADS"}],
                              "itemLevelIssues": issues}}


def issue(code, severity, description, attribute):
    return {"code": code, "severity": severity, "resolution": "merchant_action", "attribute": attribute,
            "reportingContext": "SHOPPING_ADS", "description": description, "detail": description,
            "documentation": "https://support.google.com/merchants/answer/6324350", "applicableCountries": ["FR"]}


MERCHANT_PAGES = {
    None: {"products": [
        merchant_product("SKU1", "Chaussure Trail", [issue("image_too_small", "DISAPPROVED", "Image trop petite",
                                                            "image link")]),
        merchant_product("SKU2", "Sac Rando 30L", []),
        merchant_product("SKU3", "Gourde Inox", [issue("missing_gtin", "DEMOTED", "GTIN manquant", "gtin")]),
    ], "nextPageToken": "page-2"},
    "page-2": {"products": [
        merchant_product("SKU4", "Veste Pluie", [issue("image_too_small", "DISAPPROVED", "Image trop petite",
                                                        "image link")]),
        merchant_product("SKU5", "Bonnet Laine", []),
    ]},
}
DISAPPROVED_PRODUCTS = 2
PRODUCT_VIEW = [("SKU1", "NOT_ELIGIBLE_OR_DISAPPROVED"), ("SKU2", "ELIGIBLE"), ("SKU3", "ELIGIBLE_LIMITED"),
                ("SKU4", "NOT_ELIGIBLE_OR_DISAPPROVED"), ("SKU5", "ELIGIBLE")]
PERFORMANCE = [("SKU1", "Chaussure Trail", "84", "2300"), ("SKU2", "Sac Rando 30L", "51", "1900"),
               ("SKU3", "Gourde Inox", "33", "1500")]

GTM = {
    "/accounts$": {"account": [{"path": "accounts/6001", "accountId": "6001", "name": "Shop"}]},
    "/accounts/6001/containers$": {"container": [{"path": "accounts/6001/containers/7001", "accountId": "6001",
                                                  "containerId": "7001", "name": "shop.example",
                                                  "publicId": GTM_ID}]},
    "/containers/7001/workspaces$": {"workspace": [{"path": "accounts/6001/containers/7001/workspaces/3",
                                                    "workspaceId": "3", "name": "Default Workspace"}]},
    "/workspaces/3/tags$": {"tag": [
        {"name": "Google tag", "type": "googtag", "tagId": "1", "firingTriggerId": ["2147479553"],
         "parameter": [{"type": "template", "key": "tagId", "value": "G-ABC123"}]},
        {"name": "GA4 - purchase", "type": "gaawe", "tagId": "2", "firingTriggerId": ["12"],
         "parameter": [{"type": "template", "key": "eventName", "value": "purchase"},
                       {"type": "template", "key": "measurementIdOverride", "value": "G-ABC123"}]}]},
    "/workspaces/3/triggers$": {"trigger": [{"triggerId": "12", "name": "CE - purchase", "type": "customEvent"}]},
    "/workspaces/3/variables$": {"variable": [{"name": "DLV - ecommerce.value", "type": "v"}]},
    "/versions:live$": {"containerVersionId": "42", "name": "v42 - consent update"},
}

PAGESPEED = {"lighthouseResult": {
    "categories": {"performance": {"score": 0.62}, "seo": {"score": 0.92}},
    "audits": {"largest-contentful-paint": {"displayValue": "3.1 s", "numericValue": 3100.2},
               "cumulative-layout-shift": {"displayValue": "0.08", "numericValue": 0.08},
               "total-blocking-time": {"displayValue": "240 ms", "numericValue": 240}}},
    "loadingExperience": {"metrics": {"LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 2300, "category": "FAST"},
                                      "INTERACTION_TO_NEXT_PAINT": {"percentile": 180, "category": "FAST"},
                                      "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 5, "category": "FAST"}},
                          "overall_category": "FAST"}}

CAMPAIGNS = [("111", "Search - Marque", "SEARCH", 312_450_000, 9800, 610, 21.0, 2520.75),
             ("222", "PMax - Catalogue", "PERFORMANCE_MAX", 898_120_000, 61000, 1150, 33.5, 4105.2)]


def gads_campaigns(period):
    """[(id, name, type, cost_micros, impressions, clicks, conversions, value)] scaled for the period."""
    f = FACTOR[period]
    return [(i, n, t, int(c * f), int(im * f), int(cl * f), round(cv * f, 2), round(v * f, 2))
            for i, n, t, c, im, cl, cv, v in CAMPAIGNS]


def gads_spend(period):
    return round(sum(c[3] for c in gads_campaigns(period)) / 1_000_000, 6)


def gads_stream(query):
    """searchStream answer for the gads_performance templates and the account info query."""
    customer = {"resourceName": f"customers/{GADS_CUSTOMER}", "id": GADS_CUSTOMER, "descriptiveName": "Shop",
                "currencyCode": "EUR", "timeZone": "Europe/Paris"}
    if "FROM customer LIMIT 1" in query:
        return [{"results": [{"customer": customer}], "fieldMask": "customer.currencyCode,customer.timeZone"}]
    found = re.search(r"BETWEEN '(\d{4}-\d{2}-\d{2})' AND '(\d{4}-\d{2}-\d{2})'", query)
    period = (found.group(1), found.group(2))
    rows = gads_campaigns(period)

    def metrics(cost, imp, clicks, conv, value):
        return {"costMicros": str(cost), "impressions": str(imp), "clicks": str(clicks), "conversions": conv,
                "conversionsValue": value}

    if " FROM customer " in query:
        totals = [sum(r[k] for r in rows) for k in (3, 4, 5)] + [round(sum(r[6] for r in rows), 2),
                                                                  round(sum(r[7] for r in rows), 2)]
        results = [{"customer": customer, "metrics": metrics(*totals)}]
        return [{"results": results, "fieldMask": "customer.id,metrics.costMicros", "requestId": "req-acc"}]
    assert " FROM campaign " in query, query
    batches = []
    for i, n, t, cost, imp, clicks, conv, value in rows:  # one batch per campaign: searchStream may split
        batches.append({"results": [{"customer": customer,
                                     "campaign": {"resourceName": f"customers/{GADS_CUSTOMER}/campaigns/{i}", "id": i,
                                                  "name": n, "status": "ENABLED", "advertisingChannelType": t},
                                     "metrics": metrics(cost, imp, clicks, conv, value)}],
                        "fieldMask": "campaign.id,campaign.name,metrics.costMicros", "requestId": f"req-{i}"})
    return batches


def meta_row(period):
    f = FACTOR[period]
    spend, impressions, reach, clicks = round(812.4 * f, 2), int(60500 * f), int(21000 * f), int(1820 * f)
    purchases, value = int(31 * f), round(3720.5 * f, 2)
    return {"account_id": META_ACCOUNT, "account_name": "Shop", "spend": f"{spend:.2f}",
            "impressions": str(impressions), "clicks": str(clicks), "reach": str(reach),
            "frequency": f"{impressions / reach:.6f}", "cpc": f"{spend / clicks:.6f}",
            "cpm": f"{spend / impressions * 1000:.6f}",
            "actions": [{"action_type": "link_click", "value": str(int(clicks * 0.8))},
                        {"action_type": "omni_purchase", "value": str(purchases)},
                        {"action_type": "purchase", "value": str(purchases)}],
            "action_values": [{"action_type": "omni_purchase", "value": f"{value:.2f}"},
                              {"action_type": "purchase", "value": f"{value:.2f}"}],
            "purchase_roas": [{"action_type": "omni_purchase", "value": f"{value / spend:.6f}"}],
            "account_currency": "EUR", "date_start": period[0], "date_stop": period[1]}


def openai_row(period):
    f = FACTOR[period]
    spend, clicks, impressions, conversions, sales = round(450.0 * f, 2), int(600 * f), int(30000 * f), \
        int(12 * f), round(1380.0 * f, 2)
    return {"id": f"row_{period[0]}", "impressions": impressions, "clicks": clicks, "spend": spend,
            "ctr": round(clicks / impressions, 6), "cpc": round(spend / clicks, 6),
            "cpm": round(spend / impressions * 1000, 6), "conversions": conversions,
            "cpa": round(spend / conversions, 6), "post_click_cvr": round(conversions / clicks, 6),
            "order_created_attributed_sales": sales, "order_created_attributed_sales_currency": "EUR",
            "order_created_roas": round(sales / spend, 6), "timezone": "Europe/Paris"}


# ====================================================================================== the transport
class Upstream(FakeAdapter):
    """One routing transport for every simulated API. `fail` maps a host fragment to an (status, payload) answer
    that replaces the normal one (to simulate one source failing). Records (method, url) of every request."""

    def __init__(self, fail=None):
        super().__init__()
        self.fail = fail or {}
        self.seen: list[tuple[str, str]] = []

    def send(self, request, **kwargs):
        url = request.url
        self.seen.append((request.method, url))
        body = request.body
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        payload_in = json.loads(body) if body else None
        for fragment, answer in self.fail.items():
            if fragment in url:
                self.status, payload = answer
                break
        else:
            self.status, payload = 200, self.route(request.method, url, payload_in)
        self.headers = {"Content-Type": "application/json"}
        self.body = json.dumps(payload).encode("utf-8")
        return super().send(request, **kwargs)

    def route(self, method, url, body):
        parts = urlsplit(url)
        path, query = unquote(parts.path), parse_qs(parts.query)
        host = parts.netloc
        if host == "analyticsdata.googleapis.com":
            dims, mets = [d["name"] for d in body["dimensions"]], [m["name"] for m in body["metrics"]]
            if path.endswith(":runRealtimeReport"):
                rows = [{"unifiedScreenName": n, "activeUsers": a, "screenPageViews": v}
                        for n, a, v in (("Accueil", 9, 14), ("Chaussure Trail", 4, 6), ("Panier", 2, 3))]
                return ga4_payload(dims, mets, rows, realtime=True)
            rng = body["dateRanges"][0]
            return ga4_payload(dims, mets, ga4_report_body(dims, mets, (rng["startDate"], rng["endDate"])))
        if host == "www.googleapis.com" and "/webmasters/v3/sites/" in path:
            assert GSC_SITE in path, path
            rows = gsc_rows(body.get("dimensions", []), (body["startDate"], body["endDate"]))
            limit = body.get("rowLimit")
            return {"rows": rows[:limit] if limit else rows, "responseAggregationType": "byProperty"}
        if host == "www.googleapis.com" and path.endswith("/runPagespeed"):
            return PAGESPEED
        if host == "merchantapi.googleapis.com":
            assert f"/accounts/{MERCHANT_ID}/" in path, path
            if path.endswith("/products"):
                return MERCHANT_PAGES[(query.get("pageToken") or [None])[0]]
            if path.endswith("/reports:search"):
                if "product_performance_view" in body["query"]:
                    return {"results": [{"productPerformanceView": {"offerId": o, "title": t, "clicks": c,
                                                                    "impressions": i}} for o, t, c, i in PERFORMANCE]}
                return {"results": [{"productView": {"id": f"online~fr~FR~{o}", "offerId": o,
                                                     "aggregatedReportingContextStatus": s}} for o, s in PRODUCT_VIEW]}
        if host == "tagmanager.googleapis.com":
            for pattern, payload in GTM.items():
                if re.search(pattern, path):
                    return payload
        if host == "googleads.googleapis.com":
            assert method == "POST" and path.endswith(f"/customers/{GADS_CUSTOMER}/googleAds:searchStream"), path
            return gads_stream(body["query"])
        if host == "graph.facebook.com":
            if path.endswith(f"/act_{META_ACCOUNT}"):
                return {"id": f"act_{META_ACCOUNT}", "currency": "EUR", "timezone_name": "Europe/Paris"}
            if path.endswith(f"/act_{META_ACCOUNT}/insights"):
                rng = json.loads(query["time_range"][0])
                return {"data": [meta_row((rng["since"], rng["until"]))],
                        "paging": {"cursors": {"before": "MAZDZD", "after": "MAZDZD"}}}
        if host == "api.ads.openai.com":
            if path == "/v1/ad_account/insights":
                rng = json.loads(query["time_ranges[]"][0])
                row = openai_row((rng["since"], rng["until"]))
                return {"object": "list", "data": [row], "count": 1, "first_id": row["id"], "last_id": row["id"],
                        "has_more": False}
            if path == "/v1/ad_account":
                return {"id": "adacct_1", "name": "Shop", "status": "active", "currency": "EUR"}
        raise AssertionError(f"unexpected upstream request {method} {url}")


# ====================================================================================== fixtures
def _token_file(path, scopes):
    """A real authorized-user token file whose access token is valid until 2099: no refresh is attempted."""
    path.write_text(json.dumps({
        "token": GOOGLE_ACCESS_PREFIX + "e2e-access-token-not-real", "refresh_token": GOOGLE_REFRESH_PREFIX
        + "e2e-refresh-token-not-real", "client_id": "e2e.apps.googleusercontent.com",
        "client_secret": "e2e-client-secret-not-real", "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": scopes, "expiry": "2099-01-01T00:00:00Z"}), encoding="utf-8")
    return str(path)


@pytest.fixture
def stack(tmp_path, monkeypatch, no_keyring):
    """Every service configured with fake ids and secrets, every HTTP client wired to one Upstream."""
    env = {"GA4_PROPERTY_ID": GA4_PROPERTY, "GSC_SITE_URL": GSC_SITE, "MERCHANT_ACCOUNT_ID": MERCHANT_ID,
           "GTM_CONTAINER_ID": GTM_ID,
           "GOOGLE_TOKEN_FILE": _token_file(tmp_path / "google-token.json",
                                            ["https://www.googleapis.com/auth/analytics.readonly"]),
           "GADS_TOKEN_FILE": _token_file(tmp_path / "gads-token.json", ["https://www.googleapis.com/auth/adwords"]),
           "GADS_CUSTOMER_ID": GADS_CUSTOMER, "META_AD_ACCOUNT_ID": f"act_{META_ACCOUNT}",
           "META_ACCESS_TOKEN": META_TOKEN_PREFIX + "E2Efakemetatoken000000",
           "OPENAI_ADS_API_KEY": OPENAI_KEY_PREFIX + "e2e-fake-openai-key-0000"}
    for key in ("PAGESPEED_API_KEY", "GADS_LOGIN_CUSTOMER_ID", "GADS_DEVELOPER_TOKEN", "GADS_API_VERSION",
                "META_API_VERSION", "ADS_TRUST_ENV", "GADS_ALLOW_SENSITIVE_QUERIES"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import google_ecommerce_mcp.server as server

    server = importlib.reload(server)  # SETTINGS and TOKENS are read at import
    for client in (google_ads.CLIENT, meta_ads.CLIENT, openai_ads.CLIENT):
        client.cache.clear()
        client.BLOCKED_ATTEMPTS.clear()
    server.BLOCKED_ATTEMPTS.clear()
    google_ads.TOKENS._creds = None

    def wire(fail=None):
        upstream = Upstream(fail)
        real_session_with(monkeypatch, server.HTTP, upstream)
        for client in (google_ads.CLIENT, meta_ads.CLIENT, openai_ads.CLIENT):
            real_session_with(monkeypatch, client.session, upstream)
        return upstream

    yield server, wire
    monkeypatch.undo()
    importlib.reload(server)  # leave the module as the environment outside this test configures it


# ====================================================================================== the assistant's part
def collect(server):
    """Run the tools of the 10 sections for each period, exactly as docs/REPORTING.md lists them."""
    out: dict = {"ga4": {"kpi": {}, "funnel": {}, "channels": {}, "products": {}},
                 "search_console": {"totals": {}, "queries": {}, "pages": {}},
                 "google_ads": {}, "meta_ads": {}, "openai_ads": {}, "ads_overview": {}}
    for name, (start, end) in PERIODS.items():
        ga4 = out["ga4"]
        ga4["kpi"][name] = server.ga4_report(dimensions=["date"], metrics=["sessions", "ecommercePurchases",
                                                                           "purchaseRevenue"],
                                             start_date=start, end_date=end, limit=100)
        if name != "yoy":
            ga4["funnel"][name] = server.ga4_report(dimensions=["eventName"], metrics=["eventCount"],
                                                    start_date=start, end_date=end, limit=100)
            ga4["channels"][name] = server.ga4_report(dimensions=["sessionDefaultChannelGroup"],
                                                      metrics=["sessions", "ecommercePurchases", "purchaseRevenue"],
                                                      start_date=start, end_date=end)
            ga4["products"][name] = server.ga4_report(dimensions=["itemName"],
                                                      metrics=["itemRevenue", "itemsPurchased", "itemsViewed"],
                                                      start_date=start, end_date=end)
        gsc = out["search_console"]
        gsc["totals"][name] = server.gsc_performance(dimensions=["date"], start_date=start, end_date=end, limit=100)
        if name != "yoy":
            gsc["queries"][name] = server.gsc_performance(dimensions=["query"], start_date=start, end_date=end)
            gsc["pages"][name] = server.gsc_performance(dimensions=["page"], start_date=start, end_date=end)
        out["google_ads"][name] = google_ads.gads_performance(level="campaign", start_date=start, end_date=end)
        out["meta_ads"][name] = meta_ads.meta_insights(level="account", start_date=start, end_date=end)
        out["openai_ads"][name] = openai_ads.openai_ads_insights(start_date=start, end_date=end)
        out["ads_overview"][name] = overview_mod.ads_overview(start_date=start, end_date=end)
    out["product_issues"] = server.merchant_product_issues()
    out["performance"] = server.merchant_report_query(
        "SELECT offer_id, title, clicks, impressions FROM product_performance_view "
        f"WHERE date BETWEEN '{CURRENT[0]}' AND '{CURRENT[1]}'")
    out["product_view"] = server.merchant_report_query()  # default: product_view with the reporting status
    out["pagespeed"] = server.pagespeed(SHOP_URL, strategy="mobile")
    out["gtm_inventory"] = server.gtm_inventory()
    out["ga4_realtime"] = server.ga4_realtime()
    return out


def count_statuses(product_view):
    """What the assistant does for merchant_center.statuses: count aggregated_reporting_context_status."""
    mapping = {"ELIGIBLE": "approved", "ELIGIBLE_LIMITED": "limited", "NOT_ELIGIBLE_OR_DISAPPROVED": "disapproved"}
    counts = {"approved": 0, "limited": 0, "disapproved": 0}
    for result in product_view["results"]:
        status = result["productView"].get("aggregatedReportingContextStatus")
        if status in mapping:
            counts[mapping[status]] += 1
    return counts


def assemble(raw, lang="fr"):
    """report.json as docs/REPORTING.md "How to produce a report" / "Input contract" describe it, with the tool
    outputs placed UNMODIFIED (no retyping, no rounding)."""
    return {
        "meta": {"client": "Boutique E2E", "lang": lang, "currency": "EUR", "generated_at": "2026-10-05",
                 "prepared_by": "Agence E2E", "sample": False,
                 "brand": {"name": "Boutique E2E", "accent": "#1F6F5B"},
                 "ga4_attribution_model": "Data-driven", "consent": "bandeau CMP v2",
                 "consent_mode": "advanced"},
        "periods": {"current": list(CURRENT), "previous": list(PREVIOUS), "yoy": list(YOY)},
        "goals": {"revenue": 25000, "orders": 175, "aov": 150, "conversion_rate": 0.02},
        "sources": {
            "ga4": raw["ga4"],
            "search_console": raw["search_console"],
            "merchant_center": {"product_issues": raw["product_issues"], "performance": raw["performance"],
                                "statuses": count_statuses(raw["product_view"])},
            "google_ads": raw["google_ads"],
            "meta_ads": raw["meta_ads"],
            "openai_ads": raw["openai_ads"],
            "ads_overview": raw["ads_overview"],
            "web_vitals": {"pages": [{"url": SHOP_URL, "strategy": "mobile", **raw["pagespeed"]}]},
            "gtm_inventory": raw["gtm_inventory"],
            "ga4_realtime": raw["ga4_realtime"],
        },
        "errors": [],
        "events": [{"date": "2026-09-12", "end": "2026-09-14", "label": "Promotion -15 % trail"},
                   {"date": "2026-09-20", "label": "Rupture Veste Pluie", "item": "Veste Pluie"}],
        "notes": "**Contexte** : mois de rentrée.",
    }


def run_report(tmp_path, document, lang="fr", name="report"):
    source = tmp_path / f"{name}.json"
    target = tmp_path / f"{name}.html"
    source.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    code = main(["report", "--input", str(source), "--output", str(target), "--lang", lang])
    return code, target.read_text(encoding="utf-8") if target.exists() else ""


def section(page, sid):
    """Inner HTML of <section id="s-<sid>">, unescaped to plain characters for value checks."""
    match = re.search(rf'<section [^>]*id="s-{sid}"[^>]*>(.*?)</section>', page, re.S)
    assert match, f"section {sid} missing"
    return html_lib.unescape(match.group(1))


def text_of(fragment):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment))


ALL_SECTIONS = ("kpi", "funnel", "channels", "products", "seo", "merchant", "ads", "cwv", "data_quality")


def assert_no_section_failed(page, lang):
    plain = html_lib.unescape(page)
    for key in ("nodata.section_error", "nodata.section_missing"):
        marker = tr(lang, key, error="").split("(")[0].strip()
        assert marker not in plain, f"{key} shown: {marker}"
    assert not re.search(r'class="[^"]*section-error', page)


def assert_self_contained(page):
    assert "<script" not in page.lower()
    for attr in re.findall(r'(?:src|href|action|xlink:href)\s*=\s*"([^"]*)"', page, re.I):
        assert attr.startswith(("data:", "#")), attr
    assert not re.search(r"https?://", page), re.search(r".{60}https?://.{60}", page, re.S)
    assert "url(" not in page.replace("url(#", "")
    assert "@import" not in page


# ====================================================================================== tests
def test_tools_to_report_full_chain(stack, tmp_path, capsys):
    server, wire = stack
    upstream = wire()
    raw = collect(server)

    # every tool answered with data, no error anywhere in the raw outputs
    def errors_in(node, path="raw"):
        if isinstance(node, dict):
            if "error" in node:
                yield path, node
            for key, value in node.items():
                if key in ("platforms",):  # ads_overview blocks are checked below
                    continue
                yield from errors_in(value, f"{path}.{key}")
    found = list(errors_in(raw))
    assert found == [], found[:3]
    for name in PERIODS:
        blocks = raw["ads_overview"][name]["platforms"]
        assert {k: b["status"] for k, b in blocks.items()} == {"google_ads": "ok", "meta": "ok", "openai_ads": "ok"}
    # the read-only allow-lists were never hit, and only the simulated upstream was reached
    assert server.BLOCKED_ATTEMPTS == [] and google_ads.CLIENT.BLOCKED_ATTEMPTS == []
    assert meta_ads.CLIENT.BLOCKED_ATTEMPTS == [] and openai_ads.CLIENT.BLOCKED_ATTEMPTS == []
    hosts = {urlsplit(u).netloc for _, u in upstream.seen}
    assert hosts == {"analyticsdata.googleapis.com", "www.googleapis.com", "merchantapi.googleapis.com",
                     "tagmanager.googleapis.com", "googleads.googleapis.com", "graph.facebook.com",
                     "api.ads.openai.com"}, hosts
    assert sum("/products" in u and "merchantapi" in u for _, u in upstream.seen) == 2  # both pages scanned

    for lang in ("fr", "en"):
        document = assemble(raw, lang)
        snapshot = json.dumps(document["sources"], sort_keys=True)
        code, page = run_report(tmp_path, document, lang, name=f"report-{lang}")
        assert json.dumps(document["sources"], sort_keys=True) == snapshot  # outputs left untouched
        out = capsys.readouterr()
        assert code in (None, 0), out.err
        assert "Report written to" in out.out and out.err == "", out
        assert_no_section_failed(page, lang)
        assert_self_contained(page)
        plain = html_lib.unescape(page)
        for source in ("ga4", "search_console", "merchant_center", "google_ads", "meta_ads", "openai_ads",
                       "web_vitals"):
            name = tr(lang, f"source.{source}")
            assert tr(lang, "nodata.source_missing", source=name) not in plain, source
            assert tr(lang, "nodata.source_error", source=name, code="")[:-3] not in plain, source

        # 2. KPI: revenue and orders = GA4 totals of the simulated property
        kpi = ga4_kpi_totals(CURRENT)
        s_kpi = section(page, "kpi")
        assert fmt_money(kpi["purchaseRevenue"], "EUR", lang) in s_kpi
        assert fmt_int(kpi["ecommercePurchases"], lang) in s_kpi
        assert fmt_int(kpi["sessions"], lang) in s_kpi
        assert fmt_money(ga4_kpi_totals(PREVIOUS)["purchaseRevenue"], "EUR", lang) in s_kpi
        assert fmt_money(ga4_kpi_totals(YOY)["purchaseRevenue"], "EUR", lang) in s_kpi
        assert "<svg" in s_kpi  # the daily rows (GA4 YYYYMMDD) feed the sparklines

        # 3-5. funnel, channels, products read the raw GA4 rows
        assert fmt_int(kpi["ecommercePurchases"], lang) in section(page, "funnel")
        s_channels = section(page, "channels")
        assert "Organic Search" in s_channels and "Data-driven" in s_channels
        s_products = section(page, "products")
        assert "Chaussure Trail" in s_products and "Rupture Veste Pluie" in s_products

        # 6. SEO: Search Console totals (not the sum of the query rows), queries and pages
        s_seo = section(page, "seo")
        assert fmt_int(gsc_totals(CURRENT)["clicks"], lang) in s_seo
        assert fmt_int(gsc_totals(PREVIOUS)["clicks"], lang) in s_seo
        assert "chaussure trail" in s_seo and "/p/chaussure-trail" in s_seo
        assert "gclid" not in s_seo  # page URLs shown as paths, without their query string

        # 7. Merchant: refused listings = the simulated disapprovals
        s_merchant = section(page, "merchant")
        expected_head = tr(lang, "merchant.head_of", n=fmt_int(DISAPPROVED_PRODUCTS, lang), total=fmt_int(5, lang),
                           share=re.sub(r"\s", " ", "40,0 %" if lang == "fr" else "40.0%"))
        assert re.sub(r"\s", " ", expected_head) in re.sub(r"\s", " ", s_merchant), s_merchant[:400]
        assert "Image trop petite" in s_merchant and "Chaussure Trail" in s_merchant  # issue + free listings

        # 8. Paid media: each platform's spend, MER, the attribution warning
        s_ads = section(page, "ads")
        gads = gads_spend(CURRENT)
        meta = float(meta_row(CURRENT)["spend"])
        openai = openai_row(CURRENT)["spend"]
        for spend in (gads, meta, openai):
            assert fmt_money(spend, "EUR", lang) in s_ads, (spend, text_of(s_ads)[:600])
        combined = round(gads + meta + openai, 6)
        assert raw["ads_overview"]["current"]["combined"]["spend"] == pytest.approx(combined)
        assert fmt_money(combined, "EUR", lang) in s_ads
        mer = kpi["purchaseRevenue"] / combined
        assert fmt_number(mer, 1, lang) in s_ads
        assert overview_mod.WARNING in s_ads  # the tool's own mandatory warning, quoted
        assert fmt_int(int(31 * FACTOR[CURRENT]), lang) in s_ads  # Meta purchases (omni_purchase)

        # 9. Speed: field p75 from the raw pagespeed output (field_metrics), lab values in the report's format
        s_cwv = section(page, "cwv")
        assert raw["pagespeed"]["field_data"] == "FAST"  # unchanged key, same type as before
        nbsp = " "
        lab_lcp = "3,1" + nbsp + "s" if lang == "fr" else "3.1" + nbsp + "s"
        assert lab_lcp in s_cwv and "240" + nbsp + "ms" in s_cwv
        assert ("3.1 s" not in s_cwv) if lang == "fr" else True
        for pattern in ((r"2,3\ss", r"180\sms", r"0,05") if lang == "fr" else (r"2\.3\ss", r"180\sms", r"0\.05")):
            assert re.search(pattern, s_cwv), (pattern, text_of(s_cwv)[:600])
        assert tr(lang, "cwv.head_good", total=fmt_int(3, lang)) in s_cwv
        assert tr(lang, "cwv.no_field") not in s_cwv

        # 10. Data quality: GTM inventory and realtime read, every source OK, consent quoted
        s_dq = section(page, "data_quality")
        assert "42" in s_dq  # live version id
        assert tr(lang, "dq.gtm_purchase_one") in s_dq
        assert "advanced" in s_dq
        assert tr(lang, "dq.sources_ok", n=fmt_int(10, lang)) in s_dq, text_of(s_dq)[:800]
        # no key of a real tool output is mistaken for personal data; only the gclid planted in a page URL is
        assert "personal data key removed" not in s_dq
        assert "2 identifying URL parameter value(s) masked" in s_dq

        # 1. Summary cites the same figures
        s_sum = section(page, "summary")
        assert fmt_money(kpi["purchaseRevenue"], "EUR", lang) in s_sum
        assert fmt_number(mer, 1, lang) in s_sum


def test_one_failing_source_gives_one_readable_block(stack, tmp_path, capsys):
    """Search Console answers 403 SERVICE_DISABLED: gsc_performance returns its structured api_disabled error, the
    report shows a readable block for Search Console only, and every other section still renders its figures."""
    server, wire = stack
    disabled = (403, {"error": {"code": 403, "status": "PERMISSION_DENIED",
                                "message": "Google Search Console API has not been used in project 123 before or it "
                                           "is disabled.",
                                "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                                             "reason": "SERVICE_DISABLED"}]}})
    wire({"/webmasters/v3/": disabled})
    raw = collect(server)
    for part in ("totals", "queries", "pages"):
        for name, output in raw["search_console"][part].items():
            assert output["error"] == "api_disabled", (part, name, output)

    document = assemble(raw, "fr")
    code, page = run_report(tmp_path, document, "fr")
    out = capsys.readouterr()
    assert code in (None, 0), out.err
    assert_no_section_failed(page, "fr")
    assert_self_contained(page)
    s_seo = section(page, "seo")
    assert tr("fr", "nodata.source_error", source=tr("fr", "source.search_console"), code="api_disabled") in s_seo
    assert "<svg" not in s_seo  # no chart, no zero drawn for the failed source
    assert re.search(r">\s*0\s*<", s_seo) is None
    # the rest is intact
    assert fmt_money(ga4_kpi_totals(CURRENT)["purchaseRevenue"], "EUR", "fr") in section(page, "kpi")
    assert fmt_money(gads_spend(CURRENT), "EUR", "fr") in section(page, "ads")
    s_dq = section(page, "data_quality")
    assert "api_disabled" in s_dq and tr("fr", "source.search_console") in s_dq
    for sid in ("kpi", "funnel", "channels", "products", "merchant", "ads", "cwv"):
        assert "Source en erreur" not in section(page, sid), sid


def test_one_failing_ad_platform_keeps_the_others(stack, tmp_path, capsys):
    """Meta answers 401 (expired token): meta_insights and the Meta block of ads_overview are auth_failed; the
    report shows that platform as unavailable, the two others with their figures."""
    server, wire = stack
    wire({"graph.facebook.com": (401, {"error": {"message": "Error validating access token: Session has expired",
                                                 "type": "OAuthException", "code": 190, "error_subcode": 463,
                                                 "fbtrace_id": "AbCdEf"}})})
    raw = collect(server)
    assert raw["meta_ads"]["current"]["error"] == "auth_failed"
    assert raw["ads_overview"]["current"]["platforms"]["meta"]["status"] == "error"
    assert raw["ads_overview"]["current"]["errors"] == ["meta"]

    code, page = run_report(tmp_path, assemble(raw, "fr"), "fr")
    assert code in (None, 0), capsys.readouterr().err
    assert_no_section_failed(page, "fr")
    s_ads = section(page, "ads")
    assert tr("fr", "ads.platform_error", code="auth_failed") in s_ads
    assert fmt_money(gads_spend(CURRENT), "EUR", "fr") in s_ads
    assert fmt_money(openai_row(CURRENT)["spend"], "EUR", "fr") in s_ads
    assert overview_mod.WARNING in s_ads
    # Bug found by this test (fixed in report/summary.py mer()): with Meta failing, combined.spend only holds
    # Google Ads + ChatGPT Ads, and the report used to print it as THE MER (overstated) in sections 1 and 8.
    partial_spend = gads_spend(CURRENT) + openai_row(CURRENT)["spend"]
    wrong_mer = fmt_number(ga4_kpi_totals(CURRENT)["purchaseRevenue"] / partial_spend, 1, "fr")
    assert wrong_mer not in section(page, "summary") and f">{wrong_mer}<" not in s_ads.replace(" ", "")
    assert tr("fr", "ads.mer_none", reason=tr("fr", "ads.reason.platform_failed", platforms="Meta Ads")) in s_ads
    assert tr("fr", "ads.spend_incomplete", platforms="Meta Ads") in s_ads
    assert tr("fr", "msg.ads.title", spend=fmt_money(partial_spend, "EUR", "fr")) not in section(page, "summary")


def test_pagespeed_field_data_reaches_the_report(stack):
    """Was a known gap (pagespeed kept only loadingExperience.overall_category): the CrUX p75 values now travel in
    the additive `field_metrics` key and the report reads them from the raw output."""
    from google_ecommerce_mcp.report.sections.cwv import field_values

    server, wire = stack
    wire()
    output = server.pagespeed(SHOP_URL)
    # the simulated upstream HAS field data: LCP p75 2300 ms, INP 180 ms, CLS 5 (the API sends CLS x 100)
    assert output["field_data"] == "FAST"
    assert output["field_metrics"] == {"lcp_ms": 2300, "inp_ms": 180, "cls": 0.05, "lcp_category": "FAST",
                                       "inp_category": "FAST", "cls_category": "FAST", "origin_fallback": False}
    assert field_values({"url": SHOP_URL, "strategy": "mobile", **output}) == pytest.approx(
        {"lcp": 2.3, "inp": 180.0, "cls": 0.05})


def test_pagespeed_without_field_data_has_no_field_metrics(stack, monkeypatch, tmp_path, capsys):
    """No CrUX data for the URL nor its origin: no field_metrics key at all (never zeros), lab only in the report;
    with origin_fallback the report says the values are the whole site's."""
    server, wire = stack
    wire()
    lab_only = {"lighthouseResult": PAGESPEED["lighthouseResult"], "loadingExperience": {"id": SHOP_URL}}
    monkeypatch.setitem(globals(), "PAGESPEED", lab_only)
    output = server.pagespeed(SHOP_URL)
    assert "field_metrics" not in output and output["field_data"] is None

    origin = {"lighthouseResult": PAGESPEED["lighthouseResult"],
              "loadingExperience": {**lab_only["loadingExperience"], "origin_fallback": True, "metrics": {
                  "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 28, "category": "SLOW"}},
                  "overall_category": "SLOW"}}
    monkeypatch.setitem(globals(), "PAGESPEED", origin)
    fallback = server.pagespeed(SHOP_URL)
    assert fallback["field_metrics"] == {"cls": 0.28, "cls_category": "SLOW", "origin_fallback": True}

    document = {"meta": {"lang": "fr"}, "sources": {"web_vitals": {"pages": [
        {"url": SHOP_URL, "strategy": "mobile", **output},
        {"url": SHOP_URL + "p/x", "strategy": "mobile", **fallback}]}}}
    code, page = run_report(tmp_path, document, "fr")
    assert code in (None, 0), capsys.readouterr().err
    s_cwv = section(page, "cwv")
    assert tr("fr", "cwv.no_field") in s_cwv          # first page: lab only
    assert "0,28" in s_cwv and tr("fr", "cwv.origin_fallback") in s_cwv


def _overview_with(monkeypatch, statuses):
    """A REAL ads_overview output where each platform's summary answers ok, not_configured or an error."""
    def summary_for(platform, status):
        def summary(start_date, end_date):
            if status == "ok":
                return {"platform": platform, "spend": 100.0, "impressions": 1000, "clicks": 10, "conversions": 1.0,
                        "conversion_value": 300.0, "roas": 3.0, "currency": "EUR", "attribution": None,
                        "date_range": {"start": start_date, "end": end_date}, "totals_complete": True}
            if status == "not_configured":
                raise overview_mod.NotConfigured("X")
            return {"platform": platform, "error": "auth_failed", "detail": "expired"}
        return summary

    for (name, module), status in zip(overview_mod.PLATFORMS, statuses):
        monkeypatch.setattr(module, "summary", summary_for(name, status))
    return overview_mod.ads_overview(*CURRENT)


@pytest.mark.parametrize("statuses,expected_mer", [
    (("ok", "ok", "ok"), 1000 / 300),
    (("ok", "not_configured", "ok"), 1000 / 200),   # a platform the shop does not use is not a failure
    (("ok", "error", "ok"), None),                  # Meta failed: its spend is unknown, no MER
])
def test_mer_needs_every_used_platform(monkeypatch, statuses, expected_mer):
    from google_ecommerce_mcp.report.contract import sanitize
    from google_ecommerce_mcp.report.summary import failed_platforms, mer

    overview = _overview_with(monkeypatch, statuses)
    data = sanitize({"sources": {"ads_overview": overview,
                                 "ga4": {"kpi": {"totals": {"purchaseRevenue": 1000.0, "ecommercePurchases": 10,
                                                            "sessions": 500}, "currency": "EUR"}}}})
    result = mer(data)
    if expected_mer is None:
        assert result.value is None and result.reason == "ads.reason.platform_failed"
        assert result.reason_args == {"platforms": ("meta_ads",)} == {"platforms": failed_platforms(overview)}
    else:
        assert result.value == pytest.approx(expected_mer) and failed_platforms(overview) == ()

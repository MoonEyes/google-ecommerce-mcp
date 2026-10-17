"""Offline tests: Google APIs are replaced by a fake HTTP session, no credentials needed."""

import asyncio
import importlib

import pytest


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None):
        self.headers = headers or {}
        self.status_code = status
        self.ok = status < 400
        self._payload = payload if payload is not None else {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


class FakeSession:
    """Answers requests from a list of (url_fragment, response) rules and records every call."""

    def __init__(self, rules):
        self.rules = rules
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        for fragment, response in self.rules:
            if fragment in url:
                return response(kwargs) if callable(response) else response
        return FakeResponse(404, {"error": {"message": "unexpected url " + url}})

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)


@pytest.fixture
def server(monkeypatch, tmp_path):
    # No client registry (legacy mode) whatever exists on the machine running the tests.
    monkeypatch.setenv("GEM_CLIENTS_FILE", str(tmp_path / "no-registry.json"))
    monkeypatch.delenv("GEM_CLIENT", raising=False)
    for key, value in {"GA4_PROPERTY_ID": "123", "GSC_SITE_URL": "sc-domain:example.com",
                       "MERCHANT_ACCOUNT_ID": "999", "GTM_CONTAINER_ID": "GTM-TEST"}.items():
        monkeypatch.setenv(key, value)
    import google_ecommerce_mcp.server as module

    module = importlib.reload(module)
    monkeypatch.setattr(module.TOKENS, "headers", lambda: {"Authorization": "Bearer test"})
    return module


def use(server, monkeypatch, rules):
    session = FakeSession(rules)
    monkeypatch.setattr(server, "HTTP", session)
    return session


def call_every_tool(server):
    server.ga4_report(); server.ga4_realtime(); server.ga4_properties(); server.gsc_performance(); server.gsc_inspect_url("https://example.com/")
    server.gsc_sitemaps(); server.merchant_data_sources(); server.merchant_product_issues(); server.merchant_report_query()
    server.gtm_inventory(); server.indexing_status("https://example.com/"); server.pagespeed("https://example.com/")


GTM_RULES = [
    ("/workspaces/1/tags", FakeResponse(200, {"tag": [{"name": "GA4", "type": "googtag", "firingTriggerId": ["2147479553"]}]})),
    ("/workspaces/1/triggers", FakeResponse(200, {})),
    ("/workspaces/1/variables", FakeResponse(200, {})),
    ("/workspaces", FakeResponse(200, {"workspace": [{"path": "accounts/1/containers/2/workspaces/1", "name": "Default"}]})),
    ("versions:live", FakeResponse(200, {"containerVersionId": "7", "name": "v7"})),
    ("/containers", FakeResponse(200, {"container": [{"publicId": "GTM-TEST", "path": "accounts/1/containers/2"}]})),
    ("/accounts", FakeResponse(200, {"account": [{"path": "accounts/1"}]})),
]


def test_every_request_is_on_the_read_only_allow_list(server, monkeypatch):
    """Build gate: a tool that reaches any endpoint outside ALLOWED_ENDPOINTS fails CI.
    readOnlyHint is only a hint a client may ignore; this test and the runtime allow-list are the guarantee."""
    session = use(server, monkeypatch, GTM_RULES + [("", FakeResponse(200, {"lighthouseResult": {
        "categories": {"performance": {"score": 1}, "seo": {"score": 1}},
        "audits": {k: {"displayValue": "0"} for k in ("largest-contentful-paint", "cumulative-layout-shift",
                                                       "total-blocking-time")}}}))])
    server.BLOCKED_ATTEMPTS.clear()
    call_every_tool(server)
    assert server.BLOCKED_ATTEMPTS == [], "a tool tried a request outside the read-only allow-list"
    assert len(session.calls) >= 15
    for method, url, _ in session.calls:
        assert server.is_allowed(method, url), (method, url)
        assert method in ("GET", "POST"), (method, url)


def test_allow_list_has_no_write_endpoint(server):
    """POST is allowed only on endpoints that run a read query; no PUT, PATCH or DELETE at all."""
    read_queries = (":runReport", ":runRealtimeReport", "/searchAnalytics/query", "index:inspect", "reports:search")
    for method, pattern in server.ALLOWED_ENDPOINTS:
        assert method in ("GET", "POST"), pattern
        if method == "POST":
            assert pattern.replace("\\", "").endswith(read_queries), pattern
        for word in ("publish", "create", "update", "delete", "insert", "fetchNow", "publish", "urlNotifications:"):
            assert word not in pattern, pattern


@pytest.mark.parametrize("method,url", [
    ("POST", "https://tagmanager.googleapis.com/tagmanager/v2/accounts/1/containers/2/versions/7:publish"),
    ("POST", "https://indexing.googleapis.com/v3/urlNotifications:publish"),
    ("DELETE", "https://merchantapi.googleapis.com/products/v1/accounts/999/products/online~fr~A"),
    ("PATCH", "https://merchantapi.googleapis.com/products/v1/accounts/999/products"),
    ("POST", "https://merchantapi.googleapis.com/products/v1/accounts/999/products"),
    ("GET", "https://evil.example.com/analyticsdata.googleapis.com/v1beta/properties/1:runReport"),
])
def test_write_requests_are_refused_before_any_network_call(server, monkeypatch, method, url):
    session = use(server, monkeypatch, [("", FakeResponse(200, {}))])
    result = server._call(method, url, json={})
    assert result["error"] == "blocked"
    assert session.calls == []


def test_single_product_read_is_allowed(server):
    """Reading one Merchant product is a read; it must not be blocked (found while fixing a live feed, 05/10/2026)."""
    assert server.is_allowed("GET", "https://merchantapi.googleapis.com/products/v1/accounts/1/products/fr~FR~5163")
    assert not server.is_allowed("DELETE", "https://merchantapi.googleapis.com/products/v1/accounts/1/products/fr~FR~5163")


def test_default_scopes_are_read_only():
    from google_ecommerce_mcp.config import WRITE_CAPABLE_SCOPES, scopes_for

    assert not set(scopes_for()) & WRITE_CAPABLE_SCOPES
    assert all(s.endswith(".readonly") for s in scopes_for())
    assert set(scopes_for(merchant=True)) & WRITE_CAPABLE_SCOPES == {"https://www.googleapis.com/auth/content"}
    assert set(scopes_for(indexing=True)) & WRITE_CAPABLE_SCOPES == {"https://www.googleapis.com/auth/indexing"}


def test_server_status_flags_write_capable_scopes(server, monkeypatch):
    monkeypatch.setattr(server.TOKENS, "granted_scopes", lambda: [
        "https://www.googleapis.com/auth/analytics.readonly", "https://www.googleapis.com/auth/content"])
    status = server.server_status()
    assert status["write_capable_scopes"] == ["https://www.googleapis.com/auth/content"]


def test_ga4_report_flattens_rows(server, monkeypatch):
    payload = {"dimensionHeaders": [{"name": "sessionDefaultChannelGroup"}],
               "metricHeaders": [{"name": "sessions", "type": "TYPE_INTEGER"}],
               "rows": [{"dimensionValues": [{"value": "Organic Search"}], "metricValues": [{"value": "99"}]}], "rowCount": 1}
    use(server, monkeypatch, [(":runReport", FakeResponse(200, payload))])
    result = server.ga4_report()
    assert result["rows"] == [{"sessionDefaultChannelGroup": "Organic Search", "sessions": 99}] and result["row_count"] == 1
    assert result["truncated"] is False


def test_results_state_date_range_and_freshness(server, monkeypatch):
    use(server, monkeypatch, [(":runReport", FakeResponse(200, {})), ("searchAnalytics", FakeResponse(200, {}))])
    ga4 = server.ga4_report(start_date="7daysAgo", end_date="yesterday")
    assert ga4["date_range"]["requested"] == {"start": "7daysAgo", "end": "yesterday"}
    assert len(ga4["date_range"]["start"]) == 10 and ga4["date_range"]["data_complete"] is False
    gsc = server.gsc_performance()
    assert len(gsc["date_range"]["start"]) == 10 and len(gsc["date_range"]["end"]) == 10
    for result in (ga4, gsc, server.merchant_data_sources()):
        assert result["fetched_at"].endswith("+00:00") and result["freshness"]


def test_missing_scope_names_the_scope_to_grant(server, monkeypatch):
    error = {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "Request had insufficient authentication scopes.",
                       "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}}
    use(server, monkeypatch, [("/products", FakeResponse(403, error))])
    result = server.merchant_product_issues()
    assert result["error"] == "missing_scope"
    assert result["required_scope"] == "https://www.googleapis.com/auth/content" and "--with-merchant" in result["fix"]
    assert result["missing_scopes"] == ["https://www.googleapis.com/auth/content"]


def test_rate_limit_is_structured(server, monkeypatch):
    use(server, monkeypatch, [(":runReport", FakeResponse(429, {"error": {"message": "quota"}}, {"Retry-After": "30"}))])
    result = server.ga4_report()
    assert result["error"] == "rate_limited" and result["retry_after_seconds"] == "30"


def test_merchant_keeps_scanned_pages_when_a_later_page_fails(server, monkeypatch):
    def page(kwargs):
        if kwargs["params"].get("pageToken") == "p2":
            return FakeResponse(429, {"error": {"message": "quota"}})
        return FakeResponse(200, {"products": [{"offerId": "A", "productStatus": {"itemLevelIssues": [{"code": "x"}]}}],
                                  "nextPageToken": "p2"})

    use(server, monkeypatch, [("/products", page)])
    result = server.merchant_product_issues()
    assert result["partial"] is True and [p["offer_id"] for p in result["products"]] == ["A"]
    assert result["partial_error"]["error"] == "rate_limited"


def test_gtm_reports_which_part_failed(server, monkeypatch):
    rules = [r for r in GTM_RULES if r[0] != "/workspaces/1/variables"]
    use(server, monkeypatch, [("/workspaces/1/variables", FakeResponse(403, {"error": {"message": "denied"}}))] + rules)
    result = server.gtm_inventory()
    assert result["tags"] and result["partial"] is True
    assert result["partial_errors"][0]["step"] == "variables"


def test_gtm_partial_result_lists_every_missing_scope(server, monkeypatch):
    """A scope error inside a partial result must name the scope to grant, like a single-call tool does, so the
    caller gets a partial result plus exactly what is missing instead of having to parse each partial error."""
    scope_error = {"error": {"code": 403, "status": "PERMISSION_DENIED",
                             "message": "Request had insufficient authentication scopes."}}
    rules = [r for r in GTM_RULES if r[0] != "/workspaces/1/variables"]
    use(server, monkeypatch, [("/workspaces/1/variables", FakeResponse(403, scope_error))] + rules)
    result = server.gtm_inventory()
    assert result["partial"] is True and result["tags"]
    assert result["missing_scopes"] == [p["required_scope"] for p in result["partial_errors"]
                                        if p.get("error") == "missing_scope"]
    assert result["missing_scopes"] and all(s.startswith("https://www.googleapis.com/auth/") for s in result["missing_scopes"])


def test_missing_configuration_is_reported(server, monkeypatch):
    monkeypatch.setattr(server, "SETTINGS", server.Settings(None, None, None, None, None, None))
    result = server.ga4_report()
    assert result["error"] == "not_configured" and "GA4_PROPERTY_ID" in result["detail"]


def test_api_errors_are_returned_not_raised(server, monkeypatch):
    use(server, monkeypatch, [("searchAnalytics", FakeResponse(403, {"error": {"message": "forbidden"}}))])
    assert server.gsc_performance()["error"] == 403


def test_merchant_pagination_and_filter(server, monkeypatch):
    def page(kwargs):
        if kwargs["params"].get("pageToken") == "p2":
            return FakeResponse(200, {"products": [{"offerId": "B", "productStatus": {"itemLevelIssues": [{"code": "x"}]}}]})
        return FakeResponse(200, {"products": [{"offerId": "A", "productStatus": {}}], "nextPageToken": "p2"})

    use(server, monkeypatch, [("/products", page)])
    result = server.merchant_product_issues()
    assert [p["offer_id"] for p in result["products"]] == ["B"] and result["scanned"] == 2 and not result["truncated"]


def test_indexing_404_means_no_notification(server, monkeypatch):
    use(server, monkeypatch, [("urlNotifications", FakeResponse(404, {"error": {}}))])
    assert "no Indexing API notification" in server.indexing_status("https://example.com/")["status"]


def test_gtm_maps_built_in_triggers(server, monkeypatch):
    use(server, monkeypatch, GTM_RULES)
    result = server.gtm_inventory()
    assert result["tags"][0]["firing_triggers"] == ["All Pages (built-in)"] and result["live_version"]["id"] == "7"
    assert "partial" not in result


def test_tools_are_registered_with_real_signatures(server):
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    # 13 Google tools + 9 paid-media tools + 3 client tools, plus the write tools of modules already installed
    # (the exact frozen list lives in tests/golden_tools.json).
    assert len(tools) >= 25
    for name in ("server_status", "ga4_report", "pagespeed", "ads_overview", "clients_list", "client_profile",
                 "client_check"):
        assert name in tools, name
    assert "property_id" in tools["ga4_report"].inputSchema["properties"]
    assert "channel_group" in tools["ga4_report"].inputSchema["properties"]
    assert tools["gsc_inspect_url"].inputSchema["required"] == ["url"]


def test_every_tool_is_declared_read_only_and_documented(server):
    """Every tool except the *_apply ones (the only tools that write, through write_core) is read-only."""
    for t in asyncio.run(server.mcp.list_tools()):
        if t.name.endswith("_apply"):
            assert t.annotations.readOnlyHint is False and t.annotations.destructiveHint is True, t.name
        else:
            assert t.annotations.readOnlyHint is True and t.annotations.destructiveHint is False, t.name
        assert t.title and len(t.description) > 60, t.name
        for name, prop in t.inputSchema.get("properties", {}).items():
            assert prop.get("description"), f"{t.name}.{name} has no description"


def test_server_reports_its_own_version(server):
    from google_ecommerce_mcp import __version__

    options = server.mcp._mcp_server.create_initialization_options()
    assert options.server_version == __version__ and options.instructions


# ------------------------------------------------------------------ 0.3.0: totals, units, time, discovery
GA4_PAYLOAD = {
    "dimensionHeaders": [{"name": "landingPage"}],
    "metricHeaders": [{"name": "sessions", "type": "TYPE_INTEGER"}, {"name": "engagementRate", "type": "TYPE_FLOAT"},
                      {"name": "purchaseRevenue", "type": "TYPE_CURRENCY"}],
    "rows": [{"dimensionValues": [{"value": "/"}], "metricValues": [{"value": "10"}, {"value": "0.5"}, {"value": "12.5"}]},
             {"dimensionValues": [{"value": "/shop"}], "metricValues": [{"value": "9"}, {"value": "0.25"}, {"value": "0"}]}],
    "totals": [{"dimensionValues": [{"value": "RESERVED_TOTAL"}],
                "metricValues": [{"value": "120"}, {"value": "0.4"}, {"value": "300.75"}]}],
    "rowCount": 37,
    "metadata": {"currencyCode": "EUR", "timeZone": "Europe/Paris"},
}


def test_ga4_returns_totals_numbers_units_and_definitions(server, monkeypatch):
    session = use(server, monkeypatch, [(":runReport", FakeResponse(200, GA4_PAYLOAD))])
    result = server.ga4_report(metrics=["sessions", "engagementRate", "purchaseRevenue"], limit=2)
    assert session.calls[0][2]["json"]["metricAggregations"] == ["TOTAL"]
    assert result["rows"][1] == {"landingPage": "/shop", "sessions": 9, "engagementRate": 0.25, "purchaseRevenue": 0.0}
    assert result["totals"] == {"sessions": 120, "engagementRate": 0.4, "purchaseRevenue": 300.75}
    assert result["truncated"] is True and result["row_count"] == 37
    assert result["metrics"]["sessions"]["unit"] == "count"
    assert result["metrics"]["engagementRate"]["unit"] == "ratio 0 to 1"
    assert result["metrics"]["purchaseRevenue"]["unit"] == "EUR" and result["currency"] == "EUR"
    assert "engaged" in result["metrics"]["sessions"]["definition"].lower()


def test_ga4_dates_are_resolved_in_the_property_timezone(server, monkeypatch):
    use(server, monkeypatch, [(":runReport", FakeResponse(200, GA4_PAYLOAD))])
    complete = server.ga4_report(start_date="2026-01-01", end_date="2026-01-31")
    assert complete["date_range"]["timezone"] == "Europe/Paris"
    assert complete["date_range"]["data_complete"] is True and "settling_from" not in complete["date_range"]
    assert "requested" not in complete["date_range"]
    recent = server.ga4_report(start_date="7daysAgo", end_date="today")
    from google_ecommerce_mcp.metrics import today_in
    import datetime
    today, _ = today_in("Europe/Paris")
    assert recent["date_range"]["end"] == today.isoformat()
    assert recent["date_range"]["settling_from"] == (today - datetime.timedelta(days=1)).isoformat()
    assert recent["date_range"]["settling_note"]


def test_unknown_timezone_falls_back_to_utc_and_says_so():
    from google_ecommerce_mcp.metrics import date_range_info

    info = date_range_info("2026-01-01", "2026-01-02", "Not/AZone", 2, "note")
    assert info["timezone"].startswith("UTC") and info["data_complete"] is True


def test_ga4_property_id_override_and_validation(server, monkeypatch):
    session = use(server, monkeypatch, [(":runReport", FakeResponse(200, GA4_PAYLOAD))])
    assert server.ga4_report(property_id="properties/456")["property_id"] == "456"
    assert "/properties/456:runReport" in session.calls[0][1]
    bad = server.ga4_report(property_id="456:runReport/../x")
    assert bad["error"] == "invalid_property_id" and len(session.calls) == 1
    monkeypatch.setattr(server, "SETTINGS", server.Settings(None, None, None, None, None, None))
    assert server.ga4_report(property_id="789")["property_id"] == "789"


def test_ga4_properties_lists_every_account_and_marks_the_configured_one(server, monkeypatch):
    def page(kwargs):
        if kwargs["params"].get("pageToken") == "n2":
            return FakeResponse(200, {"accountSummaries": [{"account": "accounts/2", "displayName": "Other",
                                                             "propertySummaries": [{"property": "properties/77",
                                                                                    "displayName": "Blog"}]}]})
        return FakeResponse(200, {"accountSummaries": [{"account": "accounts/1", "displayName": "Shop", "propertySummaries": [
            {"property": "properties/123", "displayName": "Shop FR", "propertyType": "PROPERTY_TYPE_ORDINARY"}]}],
            "nextPageToken": "n2"})

    use(server, monkeypatch, [("accountSummaries", page)])
    result = server.ga4_properties()
    assert [p["property_id"] for p in result["properties"]] == ["123", "77"]
    assert result["properties"][0]["configured"] is True and result["properties"][1]["configured"] is False
    assert result["properties"][1]["account_name"] == "Other" and result["truncated"] is False
    assert result["configured_property_id"] == "123"


def test_ga4_properties_keeps_first_pages_on_later_failure(server, monkeypatch):
    def page(kwargs):
        if kwargs["params"].get("pageToken"):
            return FakeResponse(429, {"error": {"message": "quota"}})
        return FakeResponse(200, {"accountSummaries": [{"account": "accounts/1", "propertySummaries": [
            {"property": "properties/1"}]}], "nextPageToken": "n2"})

    use(server, monkeypatch, [("accountSummaries", page)])
    result = server.ga4_properties()
    assert result["partial"] is True and len(result["properties"]) == 1


def test_admin_api_disabled_tells_which_api_to_enable(server, monkeypatch):
    error = {"error": {"code": 403, "message": "Google Analytics Admin API has not been used in project 1 before or it "
                                               "is disabled.", "status": "PERMISSION_DENIED",
                       "details": [{"reason": "SERVICE_DISABLED"}]}}
    use(server, monkeypatch, [("accountSummaries", FakeResponse(403, error))])
    result = server.ga4_properties()
    assert result["error"] == "api_disabled" and result["api"] == "Google Analytics Admin API"


def test_gsc_returns_true_totals_units_and_pacific_dates(server, monkeypatch):
    def answer(kwargs):
        if "dimensions" in kwargs["json"]:
            return FakeResponse(200, {"rows": [{"keys": ["terrain"], "clicks": 3, "impressions": 100, "ctr": 0.03,
                                                "position": 7.26}]})
        return FakeResponse(200, {"rows": [{"clicks": 40, "impressions": 2000, "ctr": 0.02, "position": 12.04}]})

    session = use(server, monkeypatch, [("searchAnalytics", answer)])
    result = server.gsc_performance(limit=1, page_contains="/shop")
    totals_body = session.calls[1][2]["json"]
    assert "dimensions" not in totals_body and "rowLimit" not in totals_body
    assert totals_body["dimensionFilterGroups"] == session.calls[0][2]["json"]["dimensionFilterGroups"]
    assert result["totals"] == {"clicks": 40, "impressions": 2000, "ctr": 0.02, "position": 12.0}
    assert result["truncated"] is True and "anonymized" in result["totals_note"]
    assert result["metrics"]["ctr"]["unit"] == "ratio 0 to 1" and "lower is better" in result["metrics"]["position"]["definition"]
    assert result["date_range"]["timezone"] == "America/Los_Angeles" and result["date_range"]["data_complete"] is True


def test_gsc_totals_failure_is_partial_not_fatal(server, monkeypatch):
    def answer(kwargs):
        if "dimensions" in kwargs["json"]:
            return FakeResponse(200, {"rows": []})
        return FakeResponse(429, {"error": {"message": "quota"}})

    use(server, monkeypatch, [("searchAnalytics", answer)])
    result = server.gsc_performance()
    assert result["rows"] == [] and result["partial"] is True and result["partial_error"]["step"] == "totals"


def test_expired_token_is_reported_not_raised(server, monkeypatch):
    """Found live on 05/10/2026: a revoked refresh token raised RefreshError out of every tool."""
    from google.auth.exceptions import RefreshError
    from google_ecommerce_mcp.auth import TokenProvider

    provider = TokenProvider(None)

    class Expired:
        valid = False

        def refresh(self, request):
            raise RefreshError("invalid_grant: Token has been expired or revoked.")

    provider._creds = Expired()
    monkeypatch.setattr(server, "TOKENS", provider)
    session = use(server, monkeypatch, [("", FakeResponse(200, {}))])
    result = server.ga4_report()
    assert result["error"] == "not_authenticated" and "expired" in result["detail"] and "7 days" in result["detail"]
    assert "setup" in result["fix"] and session.calls == [] and provider._creds is None


# ------------------------------------------------------------------ 0.5.0: multi-client, PageSpeed host, GSC body
import json as _json  # noqa: E402
from urllib.parse import quote as _quote  # noqa: E402

PAGESPEED_OK = {"lighthouseResult": {
    "categories": {"performance": {"score": 1}, "seo": {"score": 1}},
    "audits": {k: {"displayValue": "0"} for k in ("largest-contentful-paint", "cumulative-layout-shift",
                                                   "total-blocking-time")}}}


def test_pagespeed_uses_the_documented_host(server, monkeypatch):
    session = use(server, monkeypatch, [("runPagespeed", FakeResponse(200, PAGESPEED_OK))])
    assert server.pagespeed("https://example.com/")["performance"] == 100
    method, url, kwargs = session.calls[0]
    assert url == "https://pagespeedonline.googleapis.com/pagespeedonline/v5/runPagespeed"
    assert kwargs["params"]["strategy"] == "mobile" and "emulatedFormFactor" not in kwargs["params"]
    assert server.is_allowed("GET", url)
    assert not server.is_allowed("GET", "https://www.googleapis.com/pagespeedonline/v5/runPagespeed")
    from google_ecommerce_mcp.config import API_BY_HOST

    assert server._api_for(url) == "PageSpeed Insights API" and "pagespeedonline.googleapis.com" in API_BY_HOST


def test_gsc_site_is_percent_encoded_in_the_path_and_raw_in_bodies(server, monkeypatch):
    session = use(server, monkeypatch, [("searchAnalytics", FakeResponse(200, {})), ("sitemaps", FakeResponse(200, {})),
                                        ("index:inspect", FakeResponse(200, {}))])
    server.gsc_performance()
    server.gsc_sitemaps()
    server.gsc_inspect_url("https://example.com/a")
    assert "/sites/sc-domain%3Aexample.com/searchAnalytics/query" in session.calls[0][1]
    assert session.calls[2][1].endswith("/sites/sc-domain%3Aexample.com/sitemaps")
    assert session.calls[3][2]["json"]["siteUrl"] == "sc-domain:example.com"
    assert server.site_path("https://www.example.com/") == "https%3A%2F%2Fwww.example.com%2F"
    for method, url, _ in session.calls:
        assert server.is_allowed(method, url), url


def test_gsc_search_type_is_sent_as_type_never_search_type(server, monkeypatch):
    session = use(server, monkeypatch, [("searchAnalytics", FakeResponse(200, {}))])
    server.gsc_performance()
    assert "type" not in session.calls[0][2]["json"] and "searchType" not in session.calls[0][2]["json"]
    server.gsc_performance(search_type="image")
    for _, _, kwargs in session.calls[2:]:  # rows query and totals query
        assert kwargs["json"]["type"] == "image" and "searchType" not in kwargs["json"]


def test_gsc_site_url_parameter_in_legacy_mode(server, monkeypatch):
    session = use(server, monkeypatch, [("searchAnalytics", FakeResponse(200, {}))])
    result = server.gsc_performance(site_url="https://shop.example/")
    assert "/sites/https%3A%2F%2Fshop.example%2F/" in session.calls[0][1]
    assert result["site_url"] == "https://shop.example/"


# ---- legacy compatibility
def test_legacy_results_carry_no_client_id_and_accept_default(server, monkeypatch):
    use(server, monkeypatch, [(":runReport", FakeResponse(200, GA4_PAYLOAD))])
    plain = server.ga4_report()
    named = server.ga4_report(client_id="default")
    assert "client_id" not in plain and "client_id" not in named
    assert {k: v for k, v in plain.items() if k != "fetched_at"} == {k: v for k, v in named.items() if k != "fetched_at"}


def test_legacy_unknown_client_is_refused_without_a_request(server, monkeypatch):
    session = use(server, monkeypatch, [("", FakeResponse(200, {}))])
    result = server.ga4_report(client_id="acme")
    assert result["error"] == "unknown_client" and result["fix"] and result["detail"]
    assert result["client_id"] == "acme" and session.calls == []


def test_legacy_without_the_clients_module(server, monkeypatch):
    """When clients.py is absent the server still works exactly as before; an explicit unknown id is refused."""
    monkeypatch.setattr(server, "_clients_module", lambda: None)
    session = use(server, monkeypatch, [(":runReport", FakeResponse(200, GA4_PAYLOAD))])
    assert server.ga4_report()["property_id"] == "123" and "client_id" not in server.ga4_report(client_id="default")
    refused = server.gsc_sitemaps(client_id="acme")
    assert refused["error"] == "unknown_client" and "fix" in refused
    assert all("sitemaps" not in url for _, url, _ in session.calls)


def test_check_keeps_its_legacy_keys(server):
    status = server.check()
    assert set(status) >= {"ga4", "search_console", "merchant_center", "tag_manager", "pagespeed_api_key",
                           "token_storage", "token", "scopes", "write_capable_scopes", "ads", "version"}
    assert "client_id" not in status


# ---- two registered clients, isolated
CLIENTS = {
    "acme": {"ga4": "111111", "gsc": "sc-domain:acme.fr", "merchant": "313131", "gtm": "GTM-ACME11",
             "gtm_account": "7001", "token": "tok-acme-AAA"},
    "beta": {"ga4": "222222", "gsc": "sc-domain:beta.fr", "merchant": "424242", "gtm": "GTM-BETA22",
             "gtm_account": "7002", "token": "tok-beta-BBB"},
}


def _registry(tmp_path, monkeypatch, names=("acme", "beta")):
    pytest.importorskip("google_ecommerce_mcp.clients")
    clients = {}
    for name in names:
        c = CLIENTS[name]
        clients[name] = {"label": name.title(), "sandbox": True, "accounts": {
            "ga4": {"property_id": c["ga4"]}, "gsc": {"site_url": c["gsc"]},
            "merchant": {"account_id": c["merchant"]},
            "gtm": {"account_id": c["gtm_account"], "public_id": c["gtm"]}}}
        monkeypatch.setenv(f"GEM_SECRET_{name.upper()}_GOOGLE", _json.dumps(
            {"token": c["token"], "refresh_token": "r-" + name, "client_id": "cid", "client_secret": "cs",
             "expiry": "2099-01-01T00:00:00Z"}))

    def no_refresh(self, request):  # never reach oauth2.googleapis.com from a test
        raise AssertionError("token refresh attempted in an offline test")

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", no_refresh)
    path = tmp_path / "clients.json"
    path.write_text(_json.dumps({"version": 1, "clients": clients}), encoding="utf-8")
    monkeypatch.setenv("GEM_CLIENTS_FILE", str(path))
    return path


def _client_rules(name):
    c = CLIENTS[name]
    return [
        ("/workspaces/1/tags", FakeResponse(200, {"tag": [{"name": "GA4", "type": "googtag"}]})),
        ("/workspaces/1/triggers", FakeResponse(200, {})),
        ("/workspaces/1/variables", FakeResponse(200, {})),
        ("/workspaces", FakeResponse(200, {"workspace": [{"path": f"accounts/{c['gtm_account']}/containers/5/workspaces/1",
                                                          "name": "Default"}]})),
        ("versions:live", FakeResponse(200, {"containerVersionId": "3"})),
        ("/containers", FakeResponse(200, {"container": [{"publicId": c["gtm"], "containerId": "5",
                                                          "path": f"accounts/{c['gtm_account']}/containers/5"}]})),
        ("/accounts", FakeResponse(200, {"account": [
            {"accountId": CLIENTS[o]["gtm_account"], "path": f"accounts/{CLIENTS[o]['gtm_account']}"}
            for o in CLIENTS]})),
        ("runPagespeed", FakeResponse(200, PAGESPEED_OK)),
        ("", FakeResponse(200, {})),
    ]


def _call_every_google_tool(server, client_id):
    c = CLIENTS[client_id]
    return [
        server.server_status(client_id=client_id),
        server.ga4_report(client_id=client_id), server.ga4_realtime(client_id=client_id),
        server.ga4_report(property_id=c["ga4"], client_id=client_id),
        server.ga4_properties(client_id=client_id),
        server.gsc_performance(client_id=client_id), server.gsc_sitemaps(client_id=client_id),
        server.gsc_performance(site_url=c["gsc"], client_id=client_id),
        server.gsc_inspect_url(f"https://{client_id}.fr/x", client_id=client_id),
        server.merchant_data_sources(client_id=client_id), server.merchant_product_issues(client_id=client_id),
        server.merchant_report_query(client_id=client_id), server.gtm_inventory(client_id=client_id),
        server.indexing_status("https://example.org/", client_id=client_id),
        server.pagespeed("https://example.org/", client_id=client_id),
    ]


@pytest.mark.parametrize("me,other", [("acme", "beta"), ("beta", "acme")])
def test_two_clients_never_share_ids_nor_tokens(server, monkeypatch, tmp_path, me, other):
    """Isolation matrix: every request made for one client carries only that client's ids and Bearer token."""
    _registry(tmp_path, monkeypatch)
    session = use(server, monkeypatch, _client_rules(me))
    results = _call_every_google_tool(server, me)
    assert len(session.calls) >= 15
    mine, theirs = CLIENTS[me], CLIENTS[other]
    for method, url, kwargs in session.calls:
        assert server.is_allowed(method, url), (method, url)
        sent = url + " " + _json.dumps({k: v for k, v in kwargs.items() if k != "timeout"}, default=str)
        for value in (theirs["ga4"], theirs["merchant"], theirs["gtm"], theirs["token"], other + ".fr",
                      "example.com", "GTM-TEST", "/properties/123:", "Bearer test"):
            assert value not in sent, (value, method, url)
        if "runPagespeed" not in url:
            assert kwargs["headers"]["Authorization"] == f"Bearer {mine['token']}", url
        else:
            assert "headers" not in kwargs  # PageSpeed never receives an OAuth token
    urls = " ".join(url for _, url, _ in session.calls)
    assert f"/properties/{mine['ga4']}:runReport" in urls and f"/accounts/{mine['merchant']}/" in urls
    assert _quote(mine["gsc"], safe="") in urls
    # the containers of the other client's GTM account are never even listed
    assert f"accounts/{theirs['gtm_account']}/containers" not in urls
    for result in results:
        assert result.get("client_id") == me, result
        assert "error" not in result, result


def test_a_client_cannot_use_another_clients_ids(server, monkeypatch, tmp_path):
    _registry(tmp_path, monkeypatch)
    session = use(server, monkeypatch, _client_rules("acme"))
    for result in (server.ga4_report(property_id=CLIENTS["beta"]["ga4"], client_id="acme"),
                   server.ga4_realtime(property_id=CLIENTS["beta"]["ga4"], client_id="acme"),
                   server.gsc_performance(site_url=CLIENTS["beta"]["gsc"], client_id="acme"),
                   server.gsc_sitemaps(site_url="sc-domain:beta.fr", client_id="acme"),
                   server.gsc_inspect_url("https://beta.fr/", site_url="sc-domain:beta.fr", client_id="acme")):
        assert result["error"] == "id_not_owned" and result["fix"] and result["detail"], result
        assert result["client_id"] == "acme"
        assert "222222" not in result["detail"]  # the refused id is masked
    assert session.calls == []


def test_registry_requires_client_id_with_two_clients(server, monkeypatch, tmp_path):
    _registry(tmp_path, monkeypatch)
    session = use(server, monkeypatch, [("", FakeResponse(200, {}))])
    for result in (server.ga4_report(), server.gsc_sitemaps(), server.server_status()):
        assert result["error"] == "client_required" and "client_id" in result["fix"]
        assert result["fetched_at"]
    assert server.ga4_report(client_id="nobody")["error"] == "unknown_client"
    assert session.calls == []


def test_registry_ignores_the_environment_ids(server, monkeypatch, tmp_path):
    """With a registry the GA4_PROPERTY_ID... of the environment are never used (no mixing)."""
    _registry(tmp_path, monkeypatch, names=("acme",))
    session = use(server, monkeypatch, [(":runReport", FakeResponse(200, GA4_PAYLOAD))])
    result = server.ga4_report()  # one client: the implicit default
    assert result["client_id"] == "acme" and result["property_id"] == "111111"
    assert "/properties/111111:" in session.calls[0][1]


def test_gsc_is_multi_site_within_the_client(server, monkeypatch, tmp_path):
    path = _registry(tmp_path, monkeypatch, names=("acme",))
    data = _json.loads(path.read_text(encoding="utf-8"))
    data["clients"]["acme"]["accounts"]["gsc"]["site_url"] = "https://blog.acme.fr/"
    path.write_text(_json.dumps(data), encoding="utf-8")
    session = use(server, monkeypatch, [("sitemaps", FakeResponse(200, {}))])
    assert "error" not in server.gsc_sitemaps(site_url="https://blog.acme.fr/", client_id="acme")
    assert session.calls[0][1].endswith("/sites/https%3A%2F%2Fblog.acme.fr%2F/sitemaps")
    assert server.gsc_sitemaps(site_url="sc-domain:acme.fr", client_id="acme")["error"] == "id_not_owned"


def test_token_providers_are_cached_per_client(server, monkeypatch, tmp_path):
    _registry(tmp_path, monkeypatch)
    a = server.resolve_scope("acme").tokens
    b = server.resolve_scope("beta").tokens
    assert a is server.resolve_scope("acme").tokens and a is not b
    assert a.headers()["Authorization"] == "Bearer tok-acme-AAA"
    assert b.headers()["Authorization"] == "Bearer tok-beta-BBB"
    assert set(server._CLIENT_TOKENS) == {"acme", "beta"}


def test_client_without_token_is_not_authenticated(server, monkeypatch, tmp_path):
    _registry(tmp_path, monkeypatch)
    monkeypatch.delenv("GEM_SECRET_BETA_GOOGLE")
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda *a: None)
    session = use(server, monkeypatch, [("", FakeResponse(200, {}))])
    result = server.merchant_data_sources(client_id="beta")
    assert result["error"] == "not_authenticated" and "beta" in result["detail"] and session.calls == []


def _run_tool(server, name, args):
    result = asyncio.run(server.mcp.call_tool(name, args))
    content = result[0] if isinstance(result, tuple) else result
    return _json.loads(content[0].text)


def test_client_tools(server, monkeypatch, tmp_path):
    _registry(tmp_path, monkeypatch)
    listed = _run_tool(server, "clients_list", {})
    assert [c["client_id"] for c in listed["clients"]] == ["acme", "beta"] and listed["client_id_required"] is True
    profile = _run_tool(server, "client_profile", {"client_id": "acme"})
    assert profile["client_id"] == "acme" and profile["accounts"]["ga4"]["property_id"] == "111111"
    text = _json.dumps(profile) + _json.dumps(listed)
    assert "tok-acme" not in text and "tok-beta" not in text and "222222" not in _json.dumps(profile)
    checked = _run_tool(server, "client_check", {"client_id": "beta"})
    assert checked["client_id"] == "beta" and checked["token"] == "ok" and checked["secrets"]["google"] is True
    assert "tok-beta" not in _json.dumps(checked)
    assert _run_tool(server, "client_profile", {})["error"] == "client_required"


def test_client_tools_in_legacy_mode(server):
    from google_ecommerce_mcp import registry

    listed = registry.clients_list()
    assert listed["count"] == 1 and listed["clients"][0]["client_id"] == "default"
    profile = registry.client_profile()
    assert profile["client_id"] == "default" and profile["legacy"] is True
    assert profile["accounts"]["ga4"]["property_id"] == "123"

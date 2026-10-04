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
def server(monkeypatch):
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
    assert len(tools) == 13
    assert "property_id" in tools["ga4_report"].inputSchema["properties"]
    assert "channel_group" in tools["ga4_report"].inputSchema["properties"]
    assert tools["gsc_inspect_url"].inputSchema["required"] == ["url"]


def test_every_tool_is_declared_read_only_and_documented(server):
    for t in asyncio.run(server.mcp.list_tools()):
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

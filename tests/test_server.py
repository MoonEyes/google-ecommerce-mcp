"""Offline tests: Google APIs are replaced by a fake HTTP session, no credentials needed."""

import asyncio
import importlib

import pytest


class FakeResponse:
    def __init__(self, status=200, payload=None):
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


def test_every_call_is_read_only(server, monkeypatch):
    """POST is only used for query endpoints (reports, inspection), never for create/update/publish."""
    session = use(server, monkeypatch, [("", FakeResponse(200, {}))])
    server.ga4_report(); server.ga4_realtime(); server.gsc_performance(); server.gsc_inspect_url("https://example.com/")
    server.gsc_sitemaps(); server.merchant_data_sources(); server.merchant_product_issues(); server.merchant_report_query()
    server.gtm_inventory(); server.indexing_status("https://example.com/")
    allowed_posts = (":runReport", ":runRealtimeReport", "/searchAnalytics/query", "index:inspect", "reports:search")
    for method, url, _ in session.calls:
        assert method == "GET" or url.endswith(allowed_posts), (method, url)
        assert not any(word in url for word in (":publish", ":create_version", "fetchNow", "/delete"))


def test_ga4_report_flattens_rows(server, monkeypatch):
    payload = {"dimensionHeaders": [{"name": "sessionDefaultChannelGroup"}], "metricHeaders": [{"name": "sessions"}],
               "rows": [{"dimensionValues": [{"value": "Organic Search"}], "metricValues": [{"value": "99"}]}], "rowCount": 1}
    use(server, monkeypatch, [(":runReport", FakeResponse(200, payload))])
    assert server.ga4_report() == {"rows": [{"sessionDefaultChannelGroup": "Organic Search", "sessions": "99"}], "row_count": 1}


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
    use(server, monkeypatch, [
        ("/workspaces/1/tags", FakeResponse(200, {"tag": [{"name": "GA4", "type": "googtag", "firingTriggerId": ["2147479553"]}]})),
        ("/workspaces/1/triggers", FakeResponse(200, {})),
        ("/workspaces/1/variables", FakeResponse(200, {})),
        ("/workspaces", FakeResponse(200, {"workspace": [{"path": "accounts/1/containers/2/workspaces/1", "name": "Default"}]})),
        ("versions:live", FakeResponse(200, {"containerVersionId": "7", "name": "v7"})),
        ("/containers", FakeResponse(200, {"container": [{"publicId": "GTM-TEST", "path": "accounts/1/containers/2"}]})),
        ("/accounts", FakeResponse(200, {"account": [{"path": "accounts/1"}]})),
    ])
    result = server.gtm_inventory()
    assert result["tags"][0]["firing_triggers"] == ["All Pages (built-in)"] and result["live_version"]["id"] == "7"


def test_tools_are_registered_with_real_signatures(server):
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert len(tools) == 12
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

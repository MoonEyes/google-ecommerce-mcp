"""Offline tests of the Google Ads connector: allow-list, GAQL guard, headers, flattening, totals, errors, secrets.

Zero network (no_network, autouse) and no OS keyring (no_keyring, autouse via clean_env): the Google Ads token
provider is replaced by a fake, so a real token on the machine never changes a result."""

import ast
import asyncio
import inspect
import json
import logging
from pathlib import Path

import pytest
import requests

from ads_fakes import FakeResponse, FakeSession, no_keyring, no_network  # noqa: F401  (fixtures)
from google_ecommerce_mcp import google_ads
from google_ecommerce_mcp.ads_config import AdsSettings
from google_ecommerce_mcp.auth import NotAuthenticated

ENV = ["GADS_CUSTOMER_ID", "GADS_LOGIN_CUSTOMER_ID", "GADS_DEVELOPER_TOKEN", "GADS_TOKEN_FILE", "GADS_API_VERSION",
       "META_ACCESS_TOKEN", "META_AD_ACCOUNT_ID", "OPENAI_ADS_API_KEY", "GOOGLE_TOKEN_FILE"]
ACCESS = "ya29." + "a" * 40
DEV_TOKEN = "devtoken-" + "d" * 20
ATTRIBUTION = {"basis": "conversion action settings", "window": "per conversion action",
               "note": google_ads.ATTRIBUTION_NOTE}
BASE = "https://googleads.googleapis.com/v26"
STREAM_URL = f"{BASE}/customers/1234567890/googleAds:searchStream"
LIST_URL = f"{BASE}/customers:listAccessibleCustomers"


class FakeTokens:
    def __init__(self, error=None, token=ACCESS):
        self.error = error
        self.token = token

    def headers(self):
        if self.error:
            raise self.error
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, no_keyring):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GADS_CUSTOMER_ID", "123-456-7890")
    monkeypatch.setattr(google_ads, "TOKENS", FakeTokens())
    google_ads.CLIENT.BLOCKED_ATTEMPTS.clear()
    google_ads.CLIENT.cache.clear()
    google_ads._CURRENT_BEARER.clear()
    yield
    google_ads._CURRENT_BEARER.clear()
    google_ads.CLIENT.BLOCKED_ATTEMPTS.clear()
    google_ads.CLIENT.cache.clear()


def use(rules):
    session = FakeSession(rules)
    google_ads.CLIENT.session = session
    return session


def campaign(cid, name, cost=None, impressions=None, clicks=None, conversions=None, value=None, **segments):
    metrics = {k: v for k, v in (("costMicros", cost), ("impressions", impressions), ("clicks", clicks),
                                 ("conversions", conversions), ("conversionsValue", value)) if v is not None}
    row = {"customer": {"resourceName": "customers/1234567890", "currencyCode": "EUR", "timeZone": "Europe/Paris"},
           "campaign": {"resourceName": f"customers/1234567890/campaigns/{cid}", "id": cid, "name": name,
                        "status": "ENABLED", "advertisingChannelType": "SEARCH"},
           "metrics": metrics}
    if segments:
        row["segments"] = segments
    return row


# Realistic searchStream answer: a JSON array of batches, int64 as strings, zero fields omitted.
STREAM = [
    {"results": [campaign("111", "Brand", "12500000", "1000", "50", 5.0, 250.0)],
     "fieldMask": "campaign.id,campaign.name,metrics.costMicros", "requestId": "req-1"},
    {"results": [campaign("222", "Shopping", "2500000", "200")], "fieldMask": "campaign.id", "requestId": "req-1"},
]
ACCOUNTS = {"resourceNames": ["customers/1234567890", "customers/9876543210"]}


def ok_rules(stream=STREAM):
    return [("GET", "customers:listAccessibleCustomers", FakeResponse(200, ACCOUNTS)),
            ("POST", ":searchStream", FakeResponse(200, stream))]


TOOL_ARGS = {
    "gads_accounts": {},
    "gads_performance": {"level": "campaign", "start_date": "2026-01-01", "end_date": "2026-01-31",
                         "segment": "date"},
    "gads_query": {"gaql": "SELECT campaign.id, metrics.cost_micros FROM campaign"},
}


# ------------------------------------------------------------------ 1-3: allow-list
def test_every_request_is_on_the_read_only_allow_list():
    session = use(ok_rules())
    assert {fn.__name__ for _, fn in google_ads.TOOLS} == set(TOOL_ARGS)
    for _, fn in google_ads.TOOLS:
        assert "error" not in fn(**TOOL_ARGS[fn.__name__]), fn.__name__
    assert google_ads.summary("2026-01-01", "2026-01-31")["spend"] == 15.0
    assert google_ads.CLIENT.BLOCKED_ATTEMPTS == []
    assert len(session.calls) == 4
    for method, url, kwargs in session.calls:
        assert google_ads.ALLOW_LIST.is_allowed(method, url, kwargs.get("json")), (method, url)
        assert url.startswith("https://googleads.googleapis.com/v26/")


def test_allow_list_has_no_write_endpoint():
    patterns = google_ads.ALLOW_LIST.patterns
    assert google_ads.ALLOW_LIST.hosts == {"googleads.googleapis.com"}
    assert {m for m, _ in patterns} <= {"GET", "POST"}
    assert [p for m, p in patterns if m == "POST"] == [
        r"https://googleads\.googleapis\.com/v[0-9]{1,3}/customers/[0-9]+/googleAds:searchStream"]
    for _, pattern in patterns:
        for word in ("mutate", "create", "update", "delete", "pause", "upload", "budget", "publish"):
            assert word not in pattern.lower()
    assert google_ads.ALLOW_LIST.is_allowed("POST", STREAM_URL, None) is False  # body validator required


@pytest.mark.parametrize("method,url", [
    ("POST", f"{BASE}/customers/1/campaigns:mutate"),
    ("POST", f"{BASE}/customers/1/googleAds:mutate"),
    ("POST", f"{BASE}/customers/1/campaignBudgets:mutate"),
    ("POST", f"{BASE}/customers/1/googleAds:search"),
    ("DELETE", f"{BASE}/customers/1/googleAds:searchStream"),
    ("GET", f"{BASE}/customers/1/googleAds:searchStream"),
    ("POST", "https://evil/googleads.googleapis.com/v26/customers/1/googleAds:searchStream"),
    ("POST", "https://googleads.googleapis.com.evil.com/v26/customers/1/googleAds:searchStream"),
    ("GET", "https://evil/googleads.googleapis.com/v26/customers:listAccessibleCustomers"),
    ("GET", "http://googleads.googleapis.com/v26/customers:listAccessibleCustomers"),
    ("POST", f"{BASE}/customers/1/../2/googleAds:searchStream"),
    ("POST", f"{BASE}/customers/abc/googleAds:searchStream"),
])
def test_refused_without_network(method, url):
    session = use([("", FakeResponse(200, {}))])
    result = google_ads.CLIENT.call(method, url, json={"query": "SELECT campaign.id FROM campaign LIMIT 1"})
    assert result["error"] == "blocked" and session.calls == []
    assert google_ads.CLIENT.BLOCKED_ATTEMPTS == [(method, url)]


@pytest.mark.parametrize("body", [{"query": "UPDATE campaign"}, {"query": "SELECT a FROM b; SELECT c"},
                                  {"query": "SELECT a FROM b LIMIT 1", "operations": []}, None,
                                  "SELECT a FROM b LIMIT 1", {"query": "SELECT a FROM b"},
                                  {"query": "SELECT a FROM b LIMIT 20000"},
                                  {"query": "SELECT campaign.id FROM campaign"},
                                  {"query": "SELECT campaign.id FROM campaign LIMIT 10002"},
                                  {"query": "SELECT campaign.id FROM campaign LIMIT 1", "pageSize": 5},
                                  {"query": "SELECT click_view.gclid FROM click_view LIMIT 1"}])
def test_allow_list_validator_refuses_bad_bodies_on_the_search_route(body):
    session = use([("", FakeResponse(200, []))])
    assert google_ads.CLIENT.call("POST", STREAM_URL, json=body)["error"] == "blocked"
    assert session.calls == []


def test_module_never_uses_requests_directly():
    tree = ast.parse(Path(inspect.getfile(google_ads)).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(a.name.split(".")[0] != "requests" for a in node.names)
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "requests"
        if isinstance(node, ast.Name):
            assert node.id != "requests"


# ------------------------------------------------------------------ 4: GAQL guard
@pytest.mark.parametrize("gaql", [
    "select campaign.id from campaign",
    "\n  SELECT\n    campaign.id,\n    metrics.clicks\n  FROM campaign\n  WHERE segments.date DURING LAST_7_DAYS\n",
    "SeLeCt\tad_group.id FROM ad_group LIMIT 5",
])
def test_gaql_accepted(gaql):
    session = use(ok_rules())
    assert google_ads.is_safe_gaql(gaql)
    result = google_ads.gads_query(gaql)
    assert "error" not in result and len(session.calls) == 1
    assert session.calls[0][2]["json"] == {"query": google_ads.check_gaql(gaql)}


@pytest.mark.parametrize("gaql", [
    "", "   ", "UPDATE campaign SET campaign.status = PAUSED", "SELECT 1; SELECT 2", "-- x\nSELECT a FROM b",
    "SELECT a FROM b -- trailing", "SELECT a /* c */ FROM b", "SELECT a FROM b */", "SELECTa FROM b",
    "DELETE FROM campaign", " ;SELECT a FROM b", "mutate", "WITH x AS (SELECT 1) SELECT 2",
])
def test_gaql_refused_without_network(gaql):
    session = use(ok_rules())
    assert not google_ads.is_safe_gaql(gaql)
    result = google_ads.gads_query(gaql)
    assert result["error"] == "invalid_gaql" and session.calls == [] and result["fix"]


def test_gaql_guard_refuses_non_strings():
    assert not google_ads.is_safe_gaql(None) and not google_ads.is_safe_gaql(["SELECT a FROM b"])


@pytest.mark.parametrize("kwargs,code", [
    ({"segment": "date FROM campaign; --"}, "invalid_segment"),
    ({"segment": "segments.date, campaign.id"}, "invalid_segment"),
    ({"level": "campaign WHERE 1=1"}, "invalid_level"),
    ({"start_date": "2026-01-01' OR '1'='1"}, "invalid_dates"),
    ({"end_date": "2026-01-31'; --"}, "invalid_dates"),
    ({"start_date": "LAST_30_DAYS"}, "invalid_dates"),
    ({"start_date": "2026-02-30"}, "invalid_dates"),
    ({"start_date": "2026-02-01", "end_date": "2026-01-01"}, "invalid_dates"),
    ({"customer_id": "1/../2"}, "invalid_customer_id"),
    ({"customer_id": "123 OR 1"}, "invalid_customer_id"),
])
def test_performance_injection_refused_without_network(kwargs, code):
    session = use(ok_rules())
    args = {"level": "campaign", "start_date": "2026-01-01", "end_date": "2026-01-31", **kwargs}
    result = google_ads.gads_performance(**args)
    assert result["error"] == code and session.calls == []


@pytest.mark.parametrize("level", list(google_ads.LEVELS))
@pytest.mark.parametrize("segment", list(google_ads.SEGMENTS))
def test_performance_templates_are_safe_gaql(level, segment):
    query = google_ads.build_performance_query(level, "2026-01-01", "2026-01-31", segment)
    assert google_ads.is_safe_gaql(query)
    assert "customer.currency_code" in query and "customer.time_zone" in query
    assert "WHERE segments.date BETWEEN '2026-01-01' AND '2026-01-31'" in query
    assert query.endswith("ORDER BY metrics.cost_micros DESC LIMIT 10001")


# ------------------------------------------------------------------ headers and configuration
def test_optional_headers_absent_when_unset():
    session = use(ok_rules())
    google_ads.gads_accounts()
    headers = session.calls[0][2]["headers"]
    assert headers["Authorization"] == f"Bearer {ACCESS}"
    assert "developer-token" not in headers and "login-customer-id" not in headers


def test_optional_headers_sent_when_set(monkeypatch):
    monkeypatch.setenv("GADS_DEVELOPER_TOKEN", DEV_TOKEN)
    monkeypatch.setenv("GADS_LOGIN_CUSTOMER_ID", "111-222-3333")
    session = use(ok_rules())
    google_ads.gads_performance(start_date="2026-01-01", end_date="2026-01-31")
    method, url, kwargs = session.calls[0]
    assert kwargs["headers"]["developer-token"] == DEV_TOKEN
    assert kwargs["headers"]["login-customer-id"] == "1112223333"
    assert DEV_TOKEN not in url and ACCESS not in url and url == STREAM_URL


def test_api_version_from_settings_and_validated(monkeypatch):
    monkeypatch.setenv("GADS_API_VERSION", "v27")
    session = use(ok_rules())
    google_ads.gads_accounts()
    assert session.calls[0][1] == "https://googleads.googleapis.com/v27/customers:listAccessibleCustomers"
    monkeypatch.setenv("GADS_API_VERSION", "v26/customers/1/campaigns:mutate?")
    session = use(ok_rules())
    for _, fn in google_ads.TOOLS:
        assert fn(**TOOL_ARGS[fn.__name__])["error"] == "invalid_api_version"
    assert session.calls == []


def test_customer_id_parameter_overrides_and_is_normalized():
    session = use(ok_rules())
    result = google_ads.gads_query("SELECT customer.id FROM customer", customer_id="987-654-3210")
    assert result["customer_id"] == "9876543210"
    assert session.calls[0][1] == f"{BASE}/customers/9876543210/googleAds:searchStream"


def test_invalid_login_customer_id_refused_without_network(monkeypatch):
    monkeypatch.setenv("GADS_LOGIN_CUSTOMER_ID", "abc")
    session = use(ok_rules())
    assert google_ads.gads_accounts()["error"] == "invalid_customer_id" and session.calls == []


# ------------------------------------------------------------------ 6: realistic responses
def test_accounts_and_discovery_cache():
    session = use(ok_rules())
    first = google_ads.gads_accounts()
    assert first["customers"] == [{"id": "1234567890"}, {"id": "9876543210"}]
    assert first["configured_customer_id"] == "1234567890" and first["login_customer_id"] is None
    assert first["cached"] is False and first["fetched_at"] and first["freshness"]
    assert google_ads.gads_accounts()["cached"] is True and len(session.calls) == 1


def test_performance_flattens_batches_converts_micros_and_pools_roas():
    use(ok_rules())
    result = google_ads.gads_performance(level="campaign", start_date="2026-01-01", end_date="2026-01-31")
    assert result["row_count"] == 2 and result["currency"] == "EUR" and result["totals_complete"] is True
    brand, shopping = result["rows"]
    assert brand == {"campaign_id": "111", "campaign_name": "Brand", "campaign_status": "ENABLED",
                     "channel_type": "SEARCH", "spend": 12.5, "impressions": 1000, "clicks": 50, "conversions": 5.0,
                     "conversion_value": 250.0, "ctr": 0.05, "cpc": 0.25, "cpa": 2.5, "roas": 20.0}
    # Omitted fields are zeros; nullable ratios are None, never a division error.
    assert shopping["clicks"] == 0 and shopping["conversion_value"] == 0.0
    assert shopping["cpc"] is None and shopping["cpa"] is None and shopping["roas"] == 0.0
    totals = result["totals"]
    assert totals["spend"] == 15.0 and totals["impressions"] == 1200 and totals["clicks"] == 50
    assert totals["roas"] == round(250 / 15, 6) and totals["roas"] != (20.0 + 0.0) / 2  # pooled, not mean
    assert result["date_range"]["timezone"] == "Europe/Paris" and result["date_range"]["data_complete"] is True
    assert result["attribution"] == ATTRIBUTION
    assert result["metrics"]["spend"]["unit"] == "EUR" and result["metrics"]["ctr"]["unit"] == "ratio 0..1"
    assert set(result["metrics"]) >= {"spend", "impressions", "clicks", "conversions", "conversion_value", "roas"}


def test_performance_segment_and_limit():
    stream = [{"results": [campaign("111", "Brand", "3000000", "10", "1", device="MOBILE"),
                           campaign("111", "Brand", "1000000", "5", "1", device="DESKTOP")]}]
    use(ok_rules(stream))
    result = google_ads.gads_performance(level="campaign", start_date="2026-01-01", end_date="2026-01-31",
                                         segment="device", limit=1)
    assert [r["device"] for r in result["rows"]] == ["MOBILE"] and result["truncated"] is True
    assert result["totals"]["spend"] == 4.0  # totals cover every row, not only the returned ones


ACCOUNT_INFO = [{"results": [{"customer": {"resourceName": "customers/1234567890", "currencyCode": "EUR",
                                           "timeZone": "Europe/Paris"}}]}]


def by_query(report, info=FakeResponse(200, ACCOUNT_INFO)):
    """searchStream answer chosen by the query: the account info lookup, or the report."""
    return lambda kwargs: info if kwargs["json"]["query"] == google_ads.ACCOUNT_INFO_QUERY else report


@pytest.mark.parametrize("stream", [[], [{"fieldMask": "campaign.id", "requestId": "r"}]])
def test_empty_stream_still_reports_currency_and_timezone(stream):
    session = use([(":searchStream", by_query(FakeResponse(200, stream)))])
    result = google_ads.gads_performance(start_date="2026-01-01", end_date="2026-01-31")
    assert result["rows"] == [] and result["totals"]["spend"] == 0.0 and result["totals"]["roas"] is None
    assert result["currency"] == "EUR" and result["date_range"]["timezone"] == "Europe/Paris" and result["note"]
    assert "timezone_note" not in result and len(session.calls) == 2
    assert google_ads.summary("2026-01-01", "2026-01-31")["currency"] == "EUR"
    assert len(session.calls) == 3  # account info cached 300 s: only the report is sent again


def test_empty_stream_with_failed_lookup_says_utc():
    failed = FakeResponse(429, [{"error": {"status": "RESOURCE_EXHAUSTED"}}])
    use([(":searchStream", by_query(FakeResponse(200, []), failed))])
    result = google_ads.gads_performance(start_date="2026-01-01", end_date="2026-01-31")
    assert result["currency"] is None and result["date_range"]["timezone"].startswith("UTC")
    assert "UTC" in result["timezone_note"] and result["account_info_error"]["error"] == "rate_limited"


def test_totals_complete_false_beyond_ten_thousand_rows():
    many = [{"results": [campaign(str(i), "c", "1000000", "1") for i in range(5001)]},
            {"results": [campaign(str(i), "c", "1000000", "1") for i in range(5000)]}]
    use(ok_rules(many))
    result = google_ads.gads_performance(start_date="2026-01-01", end_date="2026-01-31", limit=10)
    assert result["totals_complete"] is False and result["row_count"] == 10_000 and result["totals_note"]
    assert result["totals"]["spend"] == 10_000.0 and len(result["rows"]) == 10


def test_gads_query_flattens_to_gaql_names():
    use(ok_rules())
    result = google_ads.gads_query("SELECT campaign.id, metrics.cost_micros FROM campaign", limit=1)
    row = result["rows"][0]
    assert row["campaign.id"] == "111" and row["campaign.advertising_channel_type"] == "SEARCH"
    assert row["metrics.cost"] == 12.5 and "metrics.cost_micros" not in row
    assert row["metrics.impressions"] == 1000 and row["metrics.conversions_value"] == 250.0
    assert result["row_count"] == 2 and result["truncated"] is True and result["batches"] == 2
    assert result["currency"] == "EUR"


def test_summary_is_normalized():
    use(ok_rules())
    s = google_ads.summary("2026-01-01", "2026-01-31")
    assert {"spend", "impressions", "clicks", "conversions", "conversion_value", "roas", "currency",
            "attribution", "date_range"} <= set(s)
    assert s["platform"] == "google_ads" and s["spend"] == 15.0 and s["conversion_value"] == 250.0
    assert s["roas"] == round(250 / 15, 6) and s["currency"] == "EUR"
    assert s["attribution"] == ATTRIBUTION and isinstance(s["attribution"]["note"], str)


def test_summary_returns_errors_as_data(monkeypatch):
    monkeypatch.delenv("GADS_CUSTOMER_ID")
    s = google_ads.summary("2026-01-01", "2026-01-31")
    assert s["platform"] == "google_ads" and s["error"] == "not_configured"


# ------------------------------------------------------------------ 7: one test per error code
def test_error_not_configured(monkeypatch):
    monkeypatch.delenv("GADS_CUSTOMER_ID")
    session = use(ok_rules())
    for name in ("gads_performance", "gads_query"):
        result = getattr(google_ads, name)(**TOOL_ARGS[name])
        assert result["error"] == "not_configured" and "GADS_CUSTOMER_ID" in result["detail"]
    assert session.calls == []


def test_error_auth_failed_from_api():
    use([(":searchStream", FakeResponse(401, [{"error": {"code": 401, "message": "Request is missing required "
                                                         "authentication credential.", "status": "UNAUTHENTICATED"}}]))])
    result = google_ads.gads_performance(start_date="2026-01-01", end_date="2026-01-31")
    assert result["error"] == "auth_failed" and "setup-ads" in result["fix"] and result["http_status"] == 401


def test_error_auth_failed_without_token_and_without_network(monkeypatch):
    monkeypatch.setattr(google_ads, "TOKENS", FakeTokens(NotAuthenticated("No Google Ads token found. Run "
                                                                           "setup-ads.")))
    session = use(ok_rules())
    assert google_ads.gads_accounts()["error"] == "auth_failed" and session.calls == []


def test_error_missing_scope():
    body = {"error": {"code": 403, "message": "Request had insufficient authentication scopes.",
                      "status": "PERMISSION_DENIED", "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}}
    use([("listAccessibleCustomers", FakeResponse(403, body))])
    result = google_ads.gads_accounts()
    assert result["error"] == "missing_scope"
    assert result["missing_scopes"] == ["https://www.googleapis.com/auth/adwords"]


def test_error_rate_limited():
    body = [{"error": {"code": 429, "message": "Resource has been exhausted (e.g. check quota).",
                       "status": "RESOURCE_EXHAUSTED"}}]
    use([(":searchStream", FakeResponse(429, body, {"Retry-After": "30"}))])
    result = google_ads.gads_query("SELECT campaign.id FROM campaign")
    assert result["error"] == "rate_limited" and result["retry_after_seconds"] == 30


@pytest.mark.parametrize("status,body", [
    (403, {"error": {"code": 403, "message": "Google Ads API has not been used in project 123 before or it is "
                                             "disabled.", "status": "PERMISSION_DENIED",
                     "details": [{"reason": "SERVICE_DISABLED"}]}}),
    (404, ValueError("html 404 page")),
])
def test_error_not_available(status, body):
    use([(":searchStream", FakeResponse(status, body, text="<html>Not Found</html>"))])
    result = google_ads.gads_performance(start_date="2026-01-01", end_date="2026-01-31")
    assert result["error"] == "not_available" and result["fix"]


def test_error_network_is_data():
    use([("", requests.ConnectionError(f"connection reset (Bearer {ACCESS})"))])
    result = google_ads.gads_accounts()
    assert result["error"] == "network" and ACCESS not in json.dumps(result)


# ------------------------------------------------------------------ 5: secrets
def test_error_body_echoing_secrets_is_redacted(monkeypatch, caplog):
    monkeypatch.setenv("GADS_DEVELOPER_TOKEN", DEV_TOKEN)
    echo = [{"error": {"code": 400, "status": "INVALID_ARGUMENT",
                       "message": f"bad developer token {DEV_TOKEN} with Bearer {ACCESS} and {ACCESS}"}}]
    use([(":searchStream", FakeResponse(400, echo, text=json.dumps(echo)))])
    with caplog.at_level(logging.DEBUG):
        result = google_ads.gads_performance(start_date="2026-01-01", end_date="2026-01-31")
        google_ads.CLIENT.call("POST", f"{BASE}/customers/1/googleAds:mutate",
                               headers={"Authorization": f"Bearer {ACCESS}", "developer-token": DEV_TOKEN})
    dumped = json.dumps(result)
    assert result["error"] == "api_error" and DEV_TOKEN not in dumped and ACCESS not in dumped
    assert "[REDACTED]" in dumped
    assert DEV_TOKEN not in caplog.text and ACCESS not in caplog.text
    assert DEV_TOKEN not in str(google_ads.CLIENT.BLOCKED_ATTEMPTS)


def test_settings_repr_masks_developer_token(monkeypatch):
    monkeypatch.setenv("GADS_DEVELOPER_TOKEN", DEV_TOKEN)
    settings = AdsSettings.from_env(keyring_fallback=False)
    assert settings.gads_developer_token == DEV_TOKEN and DEV_TOKEN not in repr(settings)


def test_module_reads_no_platform_secret_from_the_keyring(no_keyring, monkeypatch):
    import keyring

    no_keyring[("google-ecommerce-mcp", "meta-access-token")] = "EAAB" + "k" * 40
    calls = []
    monkeypatch.setattr(keyring, "get_password", lambda s, u: calls.append(u) or no_keyring.get_password(s, u))
    use(ok_rules())
    google_ads.gads_accounts()
    assert google_ads._secrets() == [ACCESS] and calls == []


# ------------------------------------------------------------------ registration
def test_register_declares_read_only_documented_tools():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test-google-ads")
    assert google_ads.register(mcp) == ["gads_accounts", "gads_performance", "gads_query"]
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert set(tools) == {"gads_accounts", "gads_performance", "gads_query"}
    for t in tools.values():
        assert t.title and t.annotations.readOnlyHint is True and t.annotations.destructiveHint is False
        assert len(t.description) > 60
        assert all(p.get("description") for p in t.inputSchema.get("properties", {}).values())
    perf = tools["gads_performance"].inputSchema["properties"]
    assert perf["level"]["enum"] == ["account", "campaign", "ad_group"]
    assert perf["segment"]["enum"] == ["none", "date", "device", "network"]
    assert perf["limit"]["maximum"] == 1000 and tools["gads_query"].inputSchema["required"] == ["gaql"]


# ------------------------------------------------------------------ P3/P4: personal data, read bound, micros
SENSITIVE = sorted(google_ads.SENSITIVE_RESOURCES) + ["user_interest", "user_personalization_lists",
                                                     "customer_manager_link", "customer_manager_link_x"]


@pytest.mark.parametrize("resource", SENSITIVE)
def test_sensitive_resource_refused_without_network(resource):
    session = use(ok_rules())
    result = google_ads.gads_query(f"SELECT {resource}.resource_name FROM {resource}")
    assert result["error"] == "sensitive_resource" and resource in result["detail"]
    assert "GADS_ALLOW_SENSITIVE_QUERIES=1" in result["fix"] and session.calls == []


@pytest.mark.parametrize("gaql", [
    "select click_view.gclid from click_view where segments.date = '2026-01-01'",
    "SELECT\n  change_event.user_email\nFrOm\n\tCHANGE_EVENT\nWHERE change_event.change_date_time > '2026-01-01'",
    "  SeLeCt search_term_view.search_term   FROM    Search_Term_View   LIMIT 5",
    "SELECT campaign.id, user_list.name FROM campaign",  # sensitive resource reached through a field
])
def test_sensitive_from_is_parsed_case_and_space_insensitively(gaql):
    session = use(ok_rules())
    assert google_ads.gads_query(gaql)["error"] == "sensitive_resource" and session.calls == []


def test_sensitive_resource_allowed_with_explicit_option(monkeypatch):
    monkeypatch.setenv("GADS_ALLOW_SENSITIVE_QUERIES", "1")
    session = use(ok_rules())
    result = google_ads.gads_query("SELECT click_view.gclid FROM click_view WHERE segments.date = '2026-01-01'")
    assert "error" not in result and len(session.calls) == 1
    assert google_ads.CLIENT.BLOCKED_ATTEMPTS == []


def test_lookalike_names_and_literals_are_not_sensitive():
    session = use(ok_rules())
    for gaql in ("SELECT campaign.id FROM campaign WHERE campaign.name = 'x FROM click_view'",
                 "SELECT campaign.id FROM campaign WHERE campaign.name LIKE '%user_list%'",
                 "SELECT customer.id, metrics.unique_users FROM customer"):
        assert "error" not in google_ads.gads_query(gaql), gaql
    assert len(session.calls) == 3


def test_gads_performance_is_not_restricted():
    session = use(ok_rules())
    for level in google_ads.LEVELS:
        assert "error" not in google_ads.gads_performance(level=level, start_date="2026-01-01",
                                                          end_date="2026-01-31")
    assert len(session.calls) == 3


@pytest.mark.parametrize("gaql,sent", [
    ("SELECT campaign.id FROM campaign", "SELECT campaign.id FROM campaign LIMIT 10001"),
    ("SELECT campaign.id FROM campaign\n", "SELECT campaign.id FROM campaign LIMIT 10001"),
    ("SELECT campaign.id FROM campaign LIMIT 5", "SELECT campaign.id FROM campaign LIMIT 5"),
    ("SELECT campaign.id FROM campaign limit 50000", "SELECT campaign.id FROM campaign limit 10001"),
    ("SELECT campaign.id FROM campaign WHERE campaign.name = 'LIMIT 5'",
     "SELECT campaign.id FROM campaign WHERE campaign.name = 'LIMIT 5' LIMIT 10001"),
    ("SELECT campaign.id FROM campaign PARAMETERS include_drafts=true",
     "SELECT campaign.id FROM campaign LIMIT 10001 PARAMETERS include_drafts=true"),
    ("SELECT campaign.id FROM campaign LIMIT 20000 PARAMETERS include_drafts=true",
     "SELECT campaign.id FROM campaign LIMIT 10001 PARAMETERS include_drafts=true"),
])
def test_gads_query_read_is_bounded(gaql, sent):
    session = use(ok_rules())
    result = google_ads.gads_query(gaql)
    assert session.calls[0][2]["json"] == {"query": sent}
    assert ("limit_note" in result) is (sent != gaql)


@pytest.mark.parametrize("gaql", [
    "SELECT campaign.id FROM campaign LIMIT 5 LIMIT 6", "SELECT campaign.id FROM campaign LIMIT abc",
    "SELECT campaign.id FROM campaign LIMIT", "SELECT campaign.id FROM campaign WHERE campaign.name = 'x",
    "SELECT FROM campaign", "SELECT campaign.id, 'x' FROM campaign", "\u017felect campaign.id FROM campaign",
    "SELECT campaign.id FR\u00d8M campaign",
])
def test_gads_query_malformed_refused_without_network(gaql):
    session = use(ok_rules())
    assert google_ads.gads_query(gaql)["error"] == "invalid_gaql" and session.calls == []


def test_gads_query_reports_incomplete_read():
    many = [{"results": [campaign(str(i), "c", "1000000", "1") for i in range(10_001)]}]
    use(ok_rules(many))
    result = google_ads.gads_query("SELECT campaign.id FROM campaign", limit=3)
    assert result["read_complete"] is False and result["row_count"] == 10_000 and result["read_note"]


def test_micros_fields_without_suffix_are_converted():
    row = campaign("111", "Brand", "12500000", "1000", "50", 5.0, 250.0)
    row["metrics"].update({"averageCpc": 1500000.0, "averageCpm": 3000000.0, "costPerConversion": 2500000.0,
                           "costPerAllConversions": "500000", "averageTargetCpaMicros": "4000000"})
    use(ok_rules([{"results": [row]}]))
    flat = google_ads.gads_query("SELECT metrics.average_cpc FROM campaign")["rows"][0]
    assert flat["metrics.average_cpc"] == 1.5 and flat["metrics.average_cpm"] == 3.0
    assert flat["metrics.cost_per_conversion"] == 2.5 and flat["metrics.cost_per_all_conversions"] == 0.5
    assert flat["metrics.average_target_cpa"] == 4.0 and flat["metrics.cost"] == 12.5
    assert "average_cpc" in google_ads.gads_query("SELECT metrics.clicks FROM campaign")["units_note"]


# ------------------------------------------------------------------ never crash
def test_corrupt_token_is_auth_failed_not_a_crash(monkeypatch):
    monkeypatch.setattr(google_ads, "TOKENS", FakeTokens(json.JSONDecodeError("Expecting value", "x", 0)))
    session = use(ok_rules())
    for _, fn in google_ads.TOOLS:
        result = fn(**TOOL_ARGS[fn.__name__])
        assert result["error"] == "auth_failed" and "setup-ads" in result["detail"]
    s = google_ads.summary("2026-01-01", "2026-01-31")
    assert s["error"] == "auth_failed" and s["platform"] == "google_ads" and session.calls == []


def test_unexpected_exception_is_internal_error_with_secrets_masked(monkeypatch):
    monkeypatch.setenv("GADS_DEVELOPER_TOKEN", DEV_TOKEN)

    def boom(*args, **kwargs):
        raise RuntimeError(f"keyring backend failed with {DEV_TOKEN}")

    monkeypatch.setattr(google_ads, "_search", boom)
    monkeypatch.setattr(google_ads.CLIENT, "call", boom)
    for _, fn in google_ads.TOOLS:
        result = fn(**TOOL_ARGS[fn.__name__])
        assert result["error"] == "internal_error" and DEV_TOKEN not in json.dumps(result) and result["fetched_at"]
    s = google_ads.summary("2026-01-01", "2026-01-31")
    assert s["error"] == "internal_error" and DEV_TOKEN not in json.dumps(s)


def test_summary_never_raises_even_if_the_tool_does(monkeypatch):
    def boom(**kwargs):
        raise KeyError("totals")

    monkeypatch.setattr(google_ads, "gads_performance", boom)
    s = google_ads.summary("2026-01-01", "2026-01-31")
    assert s == {"platform": "google_ads", "error": "internal_error", "detail": "KeyError: 'totals'",
                 "fix": s["fix"]}


def test_byte_ceiling_is_passed_to_the_client(monkeypatch):
    seen = {}

    def call(method, url, *, headers=None, params=None, json=None, timeout=None, cache=False, max_bytes=None):
        seen["max_bytes"] = max_bytes
        return {"items": []}

    monkeypatch.setattr(google_ads.CLIENT, "call", call)
    google_ads.gads_query("SELECT campaign.id FROM campaign")
    assert seen["max_bytes"] == google_ads.MAX_RESPONSE_BYTES


# ------------------------------------------------------------------ wave 2: reporting allow-list, scanner, bytes
@pytest.mark.parametrize("resource", [
    "lead_form_submission_data", "local_services_lead", "local_services_lead_conversation",
    "smart_campaign_search_term_view", "paid_organic_search_term_view", "call_view", "access_invitation",
    "invoice", "billing_setup", "payments_account", "account_budget", "customer_client", "account_link",
    "third_party_app_analytics_link", "batch_job", "detail_placement_view", "group_placement_view",
    "change_status", "local_services_verification_artifact",
])
def test_holes_of_the_old_deny_list_are_refused(resource):
    session = use(ok_rules())
    result = google_ads.gads_query(f"SELECT {resource}.resource_name FROM {resource}")
    assert result["error"] in {"sensitive_resource", "resource_not_allowed"} and session.calls == []
    assert "GADS_ALLOW_SENSITIVE_QUERIES=1" in result["fix"]


@pytest.mark.parametrize("resource", ["ad_schedule_view", "hotel_performance_view", "label", "feed_item",
                                      "experiment", "offline_user_data_job"])
def test_any_resource_off_the_reporting_list_is_refused(resource):
    session = use(ok_rules())
    result = google_ads.gads_query(f"SELECT {resource}.resource_name FROM {resource}")
    assert result["error"] in {"resource_not_allowed", "sensitive_resource"} and session.calls == []


def test_resource_not_allowed_error_shape():
    use(ok_rules())
    result = google_ads.gads_query("SELECT label.name FROM label")
    assert result["error"] == "resource_not_allowed" and "label" in result["detail"]
    assert result["fix"] == "GADS_ALLOW_SENSITIVE_QUERIES=1 pour lever (données personnelles possibles)"


@pytest.mark.parametrize("resource", sorted(google_ads.REPORTING_RESOURCES))
def test_every_reporting_resource_is_accepted(resource):
    session = use(ok_rules())
    assert "error" not in google_ads.gads_query(f"SELECT {resource}.resource_name, metrics.clicks FROM {resource}")
    assert len(session.calls) == 1


def test_field_prefix_outside_the_list_is_refused():
    session = use(ok_rules())
    assert google_ads.gads_query("SELECT label.name FROM campaign")["error"] == "resource_not_allowed"
    assert session.calls == []


def test_switch_lifts_allow_list_and_deny_list(monkeypatch):
    monkeypatch.setenv("GADS_ALLOW_SENSITIVE_QUERIES", "1")
    session = use(ok_rules())
    for gaql in ("SELECT label.name FROM label", "SELECT call_view.caller_area_code FROM call_view"):
        assert "error" not in google_ads.gads_query(gaql)
    assert len(session.calls) == 2


@pytest.mark.parametrize("gaql", [
    "SELECT campaign.id FROM campaign WHERE campaign.id IN (SELECT ad_group.id FROM ad_group)",
    "SELECT campaign.id FROM campaign FROM ad_group",
    "SELECT campaign.id FROM campaign JOIN ad_group",
    "SELECT campaign.id FROM campaign LİMIT 5",
    "SELECT campaign.id FROM campaign\x00",
    "SELECT campaign.id FROM campaign WHERE campaign.name = 'a\x00b'",
    "SELECT campaign.id FROM campaign\x0b",
    "SELECT campaign.id FROM campaign WHERE campaign.name = 'x' AND campaign.id > 1",
])
def test_structural_tricks_refused_without_network(gaql):
    session = use(ok_rules())
    assert google_ads.gads_query(gaql)["error"] == "invalid_gaql" and session.calls == []


@pytest.mark.parametrize("gaql", [
    "SELECT campaign.id FROM campaign WHERE campaign.resource_name = customer_user_access",
    "SELECT campaign.id FROM campaign ORDER BY click_view.gclid",
    "SELECT campaign.id FROM campaign PARAMETERS user_list_x=1",
])
def test_sensitive_names_anywhere_outside_literals_are_refused(gaql):
    session = use(ok_rules())
    assert google_ads.gads_query(gaql)["error"] == "sensitive_resource" and session.calls == []


def test_non_ascii_inside_a_literal_is_allowed():
    session = use(ok_rules())
    assert "error" not in google_ads.gads_query("SELECT campaign.id FROM campaign WHERE campaign.name = 'Été'")
    assert len(session.calls) == 1


def test_opaque_bearer_token_is_masked_in_internal_errors(monkeypatch):
    opaque = "opaqueAccess" + "Q" * 30  # no ya29. prefix: only the exact-value mask can catch it
    monkeypatch.setattr(google_ads, "TOKENS", FakeTokens(token=opaque))
    use(ok_rules())
    assert "error" not in google_ads.gads_accounts()

    def boom(*args, **kwargs):
        raise RuntimeError(f"backend dump: {opaque}")

    monkeypatch.setattr(google_ads, "_search", boom)
    for result in (google_ads.gads_query("SELECT campaign.id FROM campaign"),
                   google_ads.summary("2026-01-01", "2026-01-31")):
        assert result["error"] == "internal_error" and opaque not in json.dumps(result)
        assert "[REDACTED]" in result["detail"]


def test_response_too_large_is_reported_as_incomplete_read():
    huge = FakeResponse(200, [], headers={"Content-Length": str(google_ads.MAX_RESPONSE_BYTES + 1)})
    use([(":searchStream", huge)])
    for result in (google_ads.gads_query("SELECT campaign.id FROM campaign"),
                   google_ads.gads_performance(start_date="2026-01-01", end_date="2026-01-31")):
        assert result["error"] == "response_too_large" and result["read_complete"] is False
        assert "20 MB" in result["read_note"]
    assert google_ads.MAX_RESPONSE_BYTES == 20 * 1024 * 1024


def test_rows_without_cost_have_zero_spend_not_none():
    use(ok_rules([{"results": [campaign("333", "No cost"), campaign("444", "Free", impressions="10")]}]))
    rows = google_ads.gads_performance(start_date="2026-01-01", end_date="2026-01-31")["rows"]
    for row in rows:
        assert row["spend"] == 0.0 and row["spend"] is not None and isinstance(row["spend"], float)
        assert row["cpc"] is None and row["roas"] is None


# ------------------------------------------------------------------ wave 3: billing, every prefix, backslash
def test_validator_accepts_a_bounded_reporting_query_and_nothing_looser():
    ok = {"query": "SELECT campaign.id FROM campaign LIMIT 10001"}
    assert google_ads.ALLOW_LIST.is_allowed("POST", STREAM_URL, ok)
    assert google_ads.ALLOW_LIST.is_allowed("POST", STREAM_URL, {**ok, "summaryRowSetting": "NO_SUMMARY_ROW"})
    for body in ({"query": "SELECT campaign.id FROM campaign"},
                 {"query": "SELECT campaign.id FROM campaign LIMIT 10002"},
                 {**ok, "pageSize": 5}):
        assert not google_ads.ALLOW_LIST.is_allowed("POST", STREAM_URL, body), body


@pytest.mark.parametrize("gaql", [
    "SELECT customer.payments_account_info.payments_account_id FROM customer",
    "SELECT customer.payments_account_info.payments_profile_id FROM customer",
    "SELECT campaign.id FROM campaign WHERE campaign.id = 1 ORDER BY billing_setup_x.id",
    "SELECT campaign.id FROM campaign WHERE payments_profile.id = 1",
])
def test_billing_names_are_refused_anywhere(gaql):
    session = use(ok_rules())
    assert google_ads.gads_query(gaql)["error"] == "sensitive_resource" and session.calls == []


@pytest.mark.parametrize("gaql", [
    "SELECT customer.conversion_tracking_setting.conversion_tracking_id FROM customer",
    "SELECT customer.remarketing_setting.google_global_site_tag FROM customer",
    "SELECT campaign.id FROM campaign WHERE customer.tracking_url_template != ''",
    "SELECT campaign.id FROM campaign ORDER BY customer.optimization_score",
])
def test_customer_is_limited_to_identity_fields(gaql):
    session = use(ok_rules())
    result = google_ads.gads_query(gaql)
    assert result["error"] == "resource_not_allowed" and "customer." in result["detail"] and session.calls == []


def test_customer_identity_fields_are_accepted():
    session = use(ok_rules())
    fields = ", ".join(f"customer.{f}" for f in sorted(google_ads.CUSTOMER_FIELDS))
    assert "error" not in google_ads.gads_query(f"SELECT {fields} FROM customer")
    assert len(session.calls) == 1


def test_switch_lifts_customer_and_billing_checks(monkeypatch):
    monkeypatch.setenv("GADS_ALLOW_SENSITIVE_QUERIES", "1")
    session = use(ok_rules())
    for gaql in ("SELECT customer.payments_account_info.payments_account_id FROM customer",
                 "SELECT customer.conversion_tracking_setting.conversion_tracking_id FROM customer"):
        assert "error" not in google_ads.gads_query(gaql)
    assert len(session.calls) == 2


@pytest.mark.parametrize("gaql", [
    "SELECT campaign.id FROM campaign WHERE feed.id = 1",
    "SELECT campaign.id FROM campaign ORDER BY label.name",
    "SELECT metrics.clicks FROM carrier_constant",
    "SELECT segments.date, metrics.clicks FROM ad_schedule_view",
])
def test_every_prefix_and_bare_from_must_be_on_the_reporting_list(gaql):
    session = use(ok_rules())
    assert google_ads.gads_query(gaql)["error"] == "resource_not_allowed" and session.calls == []
    with pytest.raises(google_ads.InvalidInput) as raised:
        google_ads.check_gaql(gaql)
    assert raised.value.code == "resource_not_allowed"


def test_literals_and_numbers_do_not_count_as_prefixes():
    session = use(ok_rules())
    for gaql in ("SELECT campaign.id FROM campaign WHERE campaign.name = 'feed.id x.y'",
                 "SELECT campaign.id FROM campaign WHERE metrics.ctr > 0.5",
                 "SELECT campaign.network_settings.target_search FROM campaign"):
        assert "error" not in google_ads.gads_query(gaql), gaql
    assert len(session.calls) == 3


@pytest.mark.parametrize("gaql", [
    "SELECT campaign.id FROM campaign WHERE campaign.name = 'a\\'' OR click_view.gclid = 'b' OR "
    "campaign.name = 'c\\''",
    "SELECT campaign.id FROM campaign WHERE campaign.name = 'a\\b'",
    "SELECT campaign.id FROM campaign \\",
])
def test_backslash_is_refused(gaql):
    session = use(ok_rules())
    assert "\\" in gaql and not google_ads.is_safe_gaql(gaql)
    assert google_ads.gads_query(gaql)["error"] == "invalid_gaql" and session.calls == []


def test_unclosed_block_comment_alone_is_refused():
    assert not google_ads.is_safe_gaql("SELECT campaign.id FROM campaign /* x")
    assert not google_ads.is_safe_gaql("SELECT campaign.id FROM campaign */")


@pytest.mark.parametrize("start,end", [("", ""), ("", "2026-01-31"), ("2026-01-01", ""), ("  ", "2026-01-31")])
def test_performance_dates_are_required(start, end):
    session = use(ok_rules())
    result = google_ads.gads_performance(start_date=start, end_date=end)
    assert result["error"] == "invalid_dates" and "required" in result["detail"] and session.calls == []


def test_response_ceiling_is_the_shared_one():
    from google_ecommerce_mcp import ads_common

    assert google_ads.MAX_RESPONSE_BYTES is ads_common.MAX_RESPONSE_BYTES
    assert not hasattr(google_ads, "is_sensitive_resource") and not hasattr(google_ads, "SENSITIVE_PREFIXES")

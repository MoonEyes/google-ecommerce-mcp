"""Offline tests of the Meta Ads connector: allow-list, no write, no token in URLs, pagination by cursor, purchases
from actions/action_values, ratios, pooled ROAS and every error code. Nothing leaves the process."""

import ast
import asyncio
import inspect
import json
import logging
from pathlib import Path

import pytest

from ads_fakes import FakeResponse, FakeSession, no_keyring, no_network  # noqa: F401  (fixtures)
from google_ecommerce_mcp import meta_ads
from google_ecommerce_mcp.ads_config import AdsSettings

# Fake token built at run time (no realistic secret literal in the source for scanners).
TOKEN = "".join(["E", "A", "A", "T"]) + "z" * 44
ACCOUNT = "1234567890"
V = "https://graph.facebook.com/v26.0"
ENV = ["GADS_CUSTOMER_ID", "GADS_LOGIN_CUSTOMER_ID", "GADS_DEVELOPER_TOKEN", "GADS_TOKEN_FILE", "GADS_API_VERSION",
       "META_ACCESS_TOKEN", "META_AD_ACCOUNT_ID", "META_API_VERSION", "OPENAI_ADS_API_KEY"]

ACCOUNT_INFO = {"currency": "EUR", "timezone_name": "Europe/Paris", "id": f"act_{ACCOUNT}"}
ACCOUNTS_PAGE = {"data": [{"id": f"act_{ACCOUNT}", "account_id": ACCOUNT, "name": "Shop", "account_status": 1,
                           "currency": "EUR", "timezone_name": "Europe/Paris"}],
                 "paging": {"cursors": {"before": "QVFI", "after": "QVFJ"}}}


def insight(campaign, spend, impressions, clicks, purchases=None, value=None, kind="omni_purchase", **extra):
    row = {"campaign_id": campaign, "campaign_name": f"Campaign {campaign}", "spend": str(spend),
           "impressions": str(impressions), "clicks": str(clicks), "reach": str(impressions // 2),
           "frequency": "2.0", "cpc": "0.5", "cpm": "10.1", "account_currency": "EUR",
           "date_start": "2026-09-01", "date_stop": "2026-09-30"}
    if purchases is not None:
        row["actions"] = [{"action_type": "link_click", "value": str(clicks)},
                          {"action_type": kind, "value": str(purchases), "7d_click": str(purchases)}]
        row["action_values"] = [{"action_type": kind, "value": str(value)}]
        row["purchase_roas"] = [{"action_type": kind, "value": str(round(value / spend, 3))}]
    row.update(extra)
    return row


@pytest.fixture(autouse=True)
def env(monkeypatch, no_keyring, tmp_path):
    """Clean environment and in-memory keyring: a real token on this machine never reaches the tests. No client
    registry (legacy mode), whatever exists on this machine."""
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEM_CLIENTS_FILE", str(tmp_path / "no-registry.json"))
    monkeypatch.delenv("GEM_CLIENT", raising=False)
    monkeypatch.setenv("META_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("META_AD_ACCOUNT_ID", f"act_{ACCOUNT}")
    meta_ads.CLIENT.BLOCKED_ATTEMPTS.clear()
    meta_ads.CLIENT.cache.clear()
    yield no_keyring
    meta_ads.CLIENT.BLOCKED_ATTEMPTS.clear()
    meta_ads.CLIENT.cache.clear()


def fake(*rules):
    session = FakeSession(list(rules) + [(f"/act_{ACCOUNT}", FakeResponse(200, ACCOUNT_INFO))])
    meta_ads.CLIENT.session = session
    return session


def insights_calls(session):
    return [c for c in session.calls if c[1].endswith("/insights")]


# ------------------------------------------------------------------ 1-3: allow-list, no write, refusal offline
def test_every_request_is_on_the_read_only_allow_list():
    session = fake(("/me/adaccounts", FakeResponse(200, ACCOUNTS_PAGE)),
                   ("/insights", FakeResponse(200, {"data": [insight("1", 10, 100, 5, 1, 30)]})))
    assert "error" not in meta_ads.meta_ad_accounts()
    for level in ("account", "campaign", "adset", "ad"):
        assert "error" not in meta_ads.meta_insights(level=level, start_date="2026-09-01", end_date="2026-09-30",
                                                     breakdowns=["age", "country"],
                                                     attribution_windows=["7d_click", "1d_view"])
    assert "error" not in meta_ads.meta_insights(date_preset="last_7d", time_increment="1")
    assert "error" not in meta_ads.summary("2026-09-01", "2026-09-30")
    assert meta_ads.CLIENT.BLOCKED_ATTEMPTS == []
    assert session.calls
    for method, url, kwargs in session.calls:
        assert method == "GET" and meta_ads.CLIENT.is_allowed(method, url), url
        assert TOKEN not in url and TOKEN not in json.dumps(kwargs.get("params") or {})
        assert "access_token" not in json.dumps(kwargs.get("params") or {})
        assert kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_allow_list_has_no_write_endpoint():
    assert meta_ads.ALLOW_LIST.hosts == {"graph.facebook.com"}
    forbidden = ("mutate", "create", "update", "delete", "pause", "upload", "budget", "publish")
    for method, pattern in meta_ads.ALLOWED_ENDPOINTS:
        assert method == "GET", pattern
        assert pattern.startswith(r"https://graph\.facebook\.com/")
        assert not any(word in pattern.lower() for word in forbidden), pattern
        assert not any(seg in ("campaigns", "adsets", "ads", "adcreatives") for seg in pattern.split("/")), pattern
    assert len(meta_ads.ALLOWED_ENDPOINTS) == 3


@pytest.mark.parametrize("method,url", [
    ("POST", f"{V}/act_1/campaigns"),
    ("POST", f"{V}/act_1/insights"),
    ("DELETE", f"{V}/act_1"),
    ("DELETE", f"{V}/123456"),
    ("GET", f"{V}/act_1/campaigns"),
    ("GET", f"{V}/act_1/adsets"),
    ("GET", "https://graph.facebook.com.evil.com/v26.0/me/adaccounts"),
    ("GET", "https://evil.com/graph.facebook.com/v26.0/me/adaccounts"),
    ("GET", "http://graph.facebook.com/v26.0/me/adaccounts"),
    ("GET", f"{V}/act_1/insights/../campaigns"),
    ("GET", "https://graph.facebook.com/me/adaccounts"),
])
def test_refused_without_network_call(method, url):
    session = fake(("", FakeResponse(200, {})))
    result = meta_ads.CLIENT.call(method, url, headers={"Authorization": f"Bearer {TOKEN}"}, json={})
    assert result["error"] == "blocked" and session.calls == []
    assert TOKEN not in json.dumps(result)


def test_token_in_url_is_refused_without_network_call():
    session = fake(("", FakeResponse(200, {})))
    for url, params in ((f"{V}/me/adaccounts?access_token={TOKEN}", None),
                        (f"{V}/me/adaccounts", {"access_token": TOKEN})):
        result = meta_ads.CLIENT.call("GET", url, params=params, headers={"Authorization": f"Bearer {TOKEN}"})
        assert result["error"] == "blocked" and TOKEN not in json.dumps(result)
    assert session.calls == []


def test_module_never_imports_requests():
    tree = ast.parse(Path(inspect.getsourcefile(meta_ads)).read_text(encoding="utf-8"))
    names = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert not any(name.split(".")[0] in ("requests", "urllib3", "httpx") for name in names)


def test_api_version_comes_from_settings(monkeypatch):
    monkeypatch.setenv("META_API_VERSION", "v27.0")
    session = fake(("/insights", FakeResponse(200, {"data": []})))
    meta_ads.meta_insights(start_date="2026-09-01", end_date="2026-09-02")
    assert all(url.startswith("https://graph.facebook.com/v27.0/") for _, url, _ in session.calls)
    monkeypatch.setenv("META_API_VERSION", "v27.0/../act_1/campaigns")
    session = fake(("", FakeResponse(200, {})))
    bad = meta_ads.meta_insights(start_date="2026-09-01", end_date="2026-09-02")
    assert bad["error"] == "invalid_api_version" and "META_API_VERSION" in bad["detail"] and session.calls == []


# ------------------------------------------------------------------ arguments
@pytest.mark.parametrize("kwargs", [
    {"start_date": "2026-09-01", "end_date": "2026-09-30", "date_preset": "last_7d"},
    {"start_date": "2026-09-01"},
    {"start_date": "2026-09-01'", "end_date": "2026-09-30"},
    {"start_date": "2026-09-30", "end_date": "2026-09-01"},
    {"start_date": "2026-09-01", "end_date": "2026-09-02\",\"x\":\"y"},
    {"breakdowns": ["age", "action_type"]},
    {"breakdowns": ["age&access_token=x"]},
    {"attribution_windows": ["7d_view"]},
    {"level": "business"},
    {"time_increment": "7"},
    {"date_preset": "lifetime"},
    {"account_id": "act_12/campaigns"},
    {"account_id": "me"},
])
def test_invalid_arguments_are_refused_without_network_call(kwargs):
    session = fake(("", FakeResponse(200, {"data": []})))
    result = meta_ads.meta_insights(**kwargs)
    assert result["error"] == "invalid_argument", result
    assert session.calls == []


def test_time_range_preset_and_breakdowns_are_sent_as_parameters():
    session = fake(("/insights", FakeResponse(200, {"data": []})), ("/act_42", FakeResponse(200, ACCOUNT_INFO)))
    meta_ads.meta_insights(level="adset", start_date="2026-09-01", end_date="2026-09-30",
                           breakdowns=["publisher_platform"], attribution_windows=["1d_click"], account_id="42")
    _, url, kwargs = insights_calls(session)[0]
    params = kwargs["params"]
    assert url == f"{V}/act_42/insights"
    assert json.loads(params["time_range"]) == {"since": "2026-09-01", "until": "2026-09-30"}
    assert "date_preset" not in params and params["level"] == "adset" and params["breakdowns"] == "publisher_platform"
    assert json.loads(params["action_attribution_windows"]) == ["1d_click"]
    assert {"spend", "actions", "action_values", "purchase_roas", "adset_id"} <= set(params["fields"].split(","))
    assert "ctr" not in params["fields"].split(",")

    session = fake(("/insights", FakeResponse(200, {"data": []})))
    out = meta_ads.meta_insights()
    params = insights_calls(session)[0][2]["params"]
    assert params["date_preset"] == "last_28d" and "time_range" not in params
    assert out["date_range"]["date_preset_defaulted"] is True


# ------------------------------------------------------------------ 6: realistic responses
def test_purchases_from_actions_ratios_and_attribution():
    rows = [insight("1", 100, 10000, 250, 4, 400), insight("2", 50.5, 2000, 0, 1, 20, kind="purchase"),
            insight("3", 0, 0, 0)]
    fake(("/insights", FakeResponse(200, {"data": rows})))
    out = meta_ads.meta_insights(level="campaign", start_date="2026-09-01", end_date="2026-09-30")
    first, second, third = out["rows"]
    assert first["campaign_id"] == "1" and first["purchases"] == 4 and first["purchase_value"] == 400
    assert first["purchase_action_type_used"] == "omni_purchase" and first["roas"] == 4.0
    assert first["ctr"] == 0.025 and 0 <= first["ctr"] <= 1  # ratio, not Meta's percent
    assert first["cpa"] == 25.0 and first["meta_reported_roas"] == [{"action_type": "omni_purchase", "value": "4.0"}]
    assert first["actions"]["link_click"] == 250
    assert second["purchase_action_type_used"] == "purchase" and second["purchases"] == 1
    assert second["ctr"] == 0.0 and second["roas"] == round(20 / 50.5, 6)
    # spend present (0) and no purchase action: zero purchases, not unknown; ratios on a zero base stay null
    assert third["purchases"] == 0.0 and third["purchase_value"] == 0.0
    assert third["roas"] is None and third["ctr"] is None and third["cpa"] is None
    assert out["purchase_action_type_used"] == ["omni_purchase", "purchase"]
    assert out["currency"] == "EUR" and out["date_range"]["timezone"] == "Europe/Paris"
    assert out["attribution"] == {"windows": ["7d_click", "1d_view"], "default_used": True, "single_total": True,
                                  "note": out["attribution"]["note"]}
    assert "7-day click" in out["attribution"]["note"]
    assert out["metrics"]["ctr"]["unit"] == "ratio 0..1"
    assert out["totals"]["impressions"] == 12000 and out["totals"]["clicks"] == 250
    assert out["totals"]["ctr"] == round(250 / 12000, 6) and out["totals"]["reach"] is None
    assert out["freshness"] and out["fetched_at"]


def test_omni_purchase_wins_over_purchase():
    row = insight("1", 10, 100, 1, 3, 90)
    row["actions"].append({"action_type": "purchase", "value": "5"})
    row["action_values"].append({"action_type": "purchase", "value": "150"})
    fake(("/insights", FakeResponse(200, {"data": [row]})))
    out = meta_ads.meta_insights(start_date="2026-09-01", end_date="2026-09-30")
    assert out["rows"][0]["purchases"] == 3 and out["rows"][0]["purchase_value"] == 90
    assert out["purchase_action_type_used"] == "omni_purchase"


def test_total_roas_is_sum_of_values_over_sum_of_spends_not_mean():
    rows = [insight("1", 100, 1000, 10, 10, 1000), insight("2", 10, 100, 1, 1, 10)]
    fake(("/insights", FakeResponse(200, {"data": rows})))
    out = meta_ads.meta_insights(level="campaign", start_date="2026-09-01", end_date="2026-09-30")
    mean_of_roas = (10.0 + 1.0) / 2
    assert out["totals"]["roas"] == round(1010 / 110, 6) != mean_of_roas
    assert out["totals"]["spend"] == 110 and out["totals"]["purchase_value"] == 1010
    assert out["totals"]["cpa"] == 10.0


def test_pagination_uses_cursors_after_and_never_paging_next():
    trap = "https://graph.facebook.com/v26.0/act_1/campaigns?method=post"
    pages = {
        None: {"data": [insight("1", 30, 100, 1)], "paging": {"cursors": {"before": "B0", "after": "C1"},
                                                                "next": trap}},
        "C1": {"data": [insight("2", 20, 100, 1)], "paging": {"cursors": {"before": "C1", "after": "C2"},
                                                                "next": trap}},
        "C2": {"data": [insight("3", 10, 100, 1)], "paging": {"cursors": {"before": "C2", "after": "C3"}}},
    }
    session = fake(("/insights", lambda kwargs: FakeResponse(200, pages[kwargs["params"].get("after")])))
    out = meta_ads.meta_insights(level="campaign", start_date="2026-09-01", end_date="2026-09-30", limit=2)
    calls = insights_calls(session)
    assert [c[2]["params"].get("after") for c in calls] == [None, "C1", "C2"]
    assert all(trap not in url and "campaigns" not in url for _, url, _ in session.calls)
    assert out["pages_read"] == 3 and out["totals_complete"] is True and out["row_count"] == 2
    assert [r["campaign_id"] for r in out["rows"]] == ["1", "2"]          # highest spend first, trimmed to limit
    assert out["totals"]["spend"] == 60 and out["rows_read"] == 3        # totals cover every row read


def test_pagination_stops_at_five_pages_and_says_totals_are_partial():
    def endless(kwargs):
        n = int((kwargs["params"].get("after") or "P0")[1:]) + 1
        return FakeResponse(200, {"data": [insight(str(n), 1, 10, 1)],
                                  "paging": {"cursors": {"after": f"P{n}"}, "next": "https://graph.facebook.com/x"}})

    session = fake(("/insights", endless))
    out = meta_ads.meta_insights(level="campaign", start_date="2026-09-01", end_date="2026-09-30")
    assert len(insights_calls(session)) == meta_ads.MAX_PAGES == 5
    assert out["totals_complete"] is False and out["totals_note"] and out["rows_read"] == 5


def test_ad_accounts_listing_and_cache():
    session = fake(("/me/adaccounts", FakeResponse(200, ACCOUNTS_PAGE)))
    out = meta_ads.meta_ad_accounts()
    assert out["accounts"][0] == {"id": f"act_{ACCOUNT}", "account_id": ACCOUNT, "name": "Shop", "account_status": 1,
                                  "account_status_label": "ACTIVE", "currency": "EUR",
                                  "timezone_name": "Europe/Paris"}
    assert out["configured_account_id"] == ACCOUNT and out["complete"] is True and out["cached"] is False
    params = session.calls[0][2]["params"]
    assert set(params["fields"].split(",")) >= {"id", "name", "account_status", "currency", "timezone_name"}
    assert meta_ads.meta_ad_accounts()["cached"] is True and len(session.calls) == 1


def test_summary_is_normalised_for_the_overview():
    fake(("/insights", FakeResponse(200, {"data": [{**insight("1", 200, 5000, 100, 8, 640), "account_id": ACCOUNT}]})))
    s = meta_ads.summary("2026-09-01", "2026-09-30")
    assert s["platform"] == "meta"
    assert {k: s[k] for k in ("spend", "impressions", "clicks", "conversions", "conversion_value", "roas",
                              "currency")} == {"spend": 200, "impressions": 5000, "clicks": 100, "conversions": 8,
                                               "conversion_value": 640, "roas": 3.2, "currency": "EUR"}
    assert s["attribution"]["windows"] == ["7d_click", "1d_view"]
    assert s["date_range"]["start"] == "2026-09-01" and s["date_range"]["timezone"] == "Europe/Paris"


def test_summary_without_configuration(monkeypatch):
    monkeypatch.delenv("META_ACCESS_TOKEN")
    session = fake()
    s = meta_ads.summary("2026-09-01", "2026-09-30")
    assert s["error"] == "not_configured" and s["platform"] == "meta" and session.calls == []


# ------------------------------------------------------------------ 5: secrets
def test_error_echoing_the_token_never_shows_it(caplog):
    echo = {"error": {"message": f"Invalid OAuth access token {TOKEN}", "type": "OAuthException", "code": 190,
                      "fbtrace_id": "AbC"}}
    fake(("/insights", FakeResponse(400, echo, text=json.dumps(echo))))
    with caplog.at_level(logging.DEBUG):
        out = meta_ads.meta_insights(start_date="2026-09-01", end_date="2026-09-30")
        meta_ads.CLIENT.call("POST", f"{V}/act_1/campaigns", headers={"Authorization": f"Bearer {TOKEN}"})
    assert out["error"] == "auth_failed"
    assert TOKEN not in json.dumps(out) and TOKEN not in caplog.text
    assert TOKEN not in repr(AdsSettings.from_env(keyring_fallback=False))
    assert TOKEN not in str(meta_ads.CLIENT.BLOCKED_ATTEMPTS)


# ------------------------------------------------------------------ 7: one test per error code
def test_not_configured_without_token(monkeypatch):
    monkeypatch.delenv("META_ACCESS_TOKEN")
    session = fake()
    for tool in (meta_ads.meta_ad_accounts, meta_ads.meta_insights):
        out = tool()
        assert out["error"] == "not_configured" and "META_ACCESS_TOKEN" in out["detail"]
    assert session.calls == []


def test_not_configured_without_account(monkeypatch):
    monkeypatch.delenv("META_AD_ACCOUNT_ID")
    session = fake()
    out = meta_ads.meta_insights(start_date="2026-09-01", end_date="2026-09-30")
    assert out["error"] == "not_configured" and "META_AD_ACCOUNT_ID" in out["detail"] and session.calls == []


def test_token_from_keyring_is_used_when_env_is_empty(monkeypatch, env):
    from google_ecommerce_mcp.config import KEYRING_SERVICE

    monkeypatch.delenv("META_ACCESS_TOKEN")
    env[(KEYRING_SERVICE, "meta-access-token")] = TOKEN
    session = fake(("/me/adaccounts", FakeResponse(200, ACCOUNTS_PAGE)))
    assert "error" not in meta_ads.meta_ad_accounts()
    assert session.calls[0][2]["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_auth_failed_code_190():
    fake(("/insights", FakeResponse(400, {"error": {"message": "Error validating access token: Session has expired",
                                                    "type": "OAuthException", "code": 190, "error_subcode": 463}})))
    out = meta_ads.meta_insights(start_date="2026-09-01", end_date="2026-09-30")
    assert out["error"] == "auth_failed" and out["http_status"] == 400 and "META_ACCESS_TOKEN" in out["fix"]


def test_missing_scope():
    fake(("/me/adaccounts", FakeResponse(403, {"error": {"message": "(#200) Requires ads_read permission",
                                                         "type": "OAuthException", "code": 200}})))
    out = meta_ads.meta_ad_accounts()
    assert out["error"] == "missing_scope" and out["required_permission"] == "ads_read"


def test_rate_limited_code_17_with_usage_headers():
    headers = {"X-Business-Use-Case-Usage": json.dumps({ACCOUNT: [{"type": "ads_insights", "call_count": 100,
                                                                   "total_cputime": 40, "total_time": 60,
                                                                   "estimated_time_to_regain_access": 2}]}),
               "X-Ad-Account-Usage": json.dumps({"acc_id_util_pct": 100, "reset_time_duration": 90})}
    fake(("/insights", FakeResponse(400, {"error": {"message": "User request limit reached", "code": 17,
                                                    "error_subcode": 2446079}}, headers)))
    out = meta_ads.meta_insights(start_date="2026-09-01", end_date="2026-09-30")
    # max(reset_time_duration=90 s, estimated_time_to_regain_access=2 min)
    assert out["error"] == "rate_limited" and out["retry_after_seconds"] == 120
    assert out["usage"]["business_use_case"][ACCOUNT][0]["call_count"] == 100
    assert out["usage"]["ad_account"]["acc_id_util_pct"] == 100


def test_not_available():
    fake((f"/act_{ACCOUNT}", FakeResponse(400, {"error": {"message": "Unsupported get request. Object with ID "
                                                          "'act_1234567890' does not exist", "code": 100,
                                                          "error_subcode": 33}})))
    out = meta_ads.meta_insights(start_date="2026-09-01", end_date="2026-09-30")
    assert out["error"] == "not_available" and "META_AD_ACCOUNT_ID" in out["fix"]


def test_network_error_is_data():
    import requests

    fake(("/me/adaccounts", requests.ConnectionError("connection reset")))
    assert meta_ads.meta_ad_accounts()["error"] == "network"


# ------------------------------------------------------------------ registration
def test_register_declares_read_only_documented_tools():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test-meta")
    names = ["meta_ad_accounts", "meta_insights", "meta_campaigns", "meta_adsets", "meta_ads_list"]
    assert meta_ads.register(mcp) == names
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert set(tools) == set(names)
    for t in tools.values():
        assert t.title and len(t.description) > 60
        assert t.annotations.readOnlyHint is True and t.annotations.destructiveHint is False
        for name, prop in t.inputSchema.get("properties", {}).items():
            assert prop.get("description"), f"{t.name}.{name}"
    props = tools["meta_insights"].inputSchema["properties"]
    assert set(props) == {"level", "start_date", "end_date", "date_preset", "breakdowns", "attribution_windows",
                          "time_increment", "limit", "account_id", "client_id"}
    for t in tools.values():
        assert "client_id" in t.inputSchema["properties"], t.name
        assert "client_id" not in t.inputSchema.get("required", []), t.name
    assert props["limit"]["maximum"] == 500
    assert "7d_view" not in json.dumps(props["attribution_windows"])


def test_tool_call_through_mcp_runs_offline():
    from mcp.server.fastmcp import FastMCP

    fake(("/insights", FakeResponse(200, {"data": [insight("1", 10, 100, 5, 1, 30)]})))
    mcp = FastMCP("test-meta")
    meta_ads.register(mcp)
    result = asyncio.run(mcp.call_tool("meta_insights", {"start_date": "2026-09-01", "end_date": "2026-09-30",
                                                         "breakdowns": ["age"]}))
    assert "omni_purchase" in str(result)


# ------------------------------------------------------------------ P3/P4 review fixes
def test_rows_without_purchases_count_as_zero_in_pooled_roas():
    """A (spend 100, value 300) + B (spend 100, no action_values) = 300 / 200 = 1.5, not 3.0."""
    a = insight("A", 100, 1000, 10, 3, 300)
    b = insight("B", 100, 1000, 10)  # no actions / action_values at all: Meta means zero purchases
    fake(("/insights", FakeResponse(200, {"data": [a, b]})))
    out = meta_ads.meta_insights(level="campaign", start_date="2026-09-01", end_date="2026-09-30")
    row_b = next(r for r in out["rows"] if r["campaign_id"] == "B")
    assert row_b["purchases"] == 0.0 and row_b["purchase_value"] == 0.0 and row_b["roas"] == 0.0
    assert row_b["cpa"] is None
    assert out["totals"]["roas"] == 1.5 and out["totals"]["purchase_value"] == 300 and out["totals"]["spend"] == 200

    meta_ads.CLIENT.cache.clear()
    fake(("/insights", FakeResponse(200, {"data": [a, b]})))
    s = meta_ads.summary("2026-09-01", "2026-09-30")
    assert s["roas"] == 1.5 and s["conversions"] == 3 and s["conversion_value"] == 300 and s["spend"] == 200


def test_empty_action_values_with_spend_is_zero_value():
    row = insight("1", 50, 500, 5, 2, 80)
    row["action_values"] = []
    fake(("/insights", FakeResponse(200, {"data": [row]})))
    out = meta_ads.meta_insights(start_date="2026-09-01", end_date="2026-09-30")
    assert out["rows"][0]["purchases"] == 2 and out["rows"][0]["purchase_value"] == 0.0
    assert out["rows"][0]["roas"] == 0.0 and out["totals"]["roas"] == 0.0


def windowed(campaign, spend):
    row = insight(campaign, spend, 1000, 10)
    row["actions"] = [{"action_type": "omni_purchase", "value": "9", "1d_click": "3", "7d_click": "5",
                       "1d_view": "4"}]
    row["action_values"] = [{"action_type": "omni_purchase", "value": "900", "1d_click": "300", "7d_click": "500",
                             "1d_view": "400"}]
    return row


def test_several_attribution_windows_are_reported_per_window_never_added():
    session = fake(("/insights", FakeResponse(200, {"data": [windowed("1", 100), insight("2", 100, 1000, 10)]})))
    out = meta_ads.meta_insights(level="campaign", start_date="2026-09-01", end_date="2026-09-30",
                                 attribution_windows=["1d_click", "7d_click"])
    params = insights_calls(session)[0][2]["params"]
    assert json.loads(params["action_attribution_windows"]) == ["1d_click", "7d_click"]
    first = next(r for r in out["rows"] if r["campaign_id"] == "1")
    assert first["purchases"] is None and first["purchase_value"] is None and first["roas"] is None
    assert first["purchases_by_window"] == {"1d_click": 3, "7d_click": 5}           # not 3 + 5, not "value" 9
    assert first["purchase_value_by_window"] == {"1d_click": 300, "7d_click": 500}
    assert first["roas_by_window"] == {"1d_click": 3.0, "7d_click": 5.0}
    second = next(r for r in out["rows"] if r["campaign_id"] == "2")
    assert second["purchases_by_window"] == {"1d_click": 0.0, "7d_click": 0.0}
    totals = out["totals"]
    assert totals["purchases"] is None and totals["purchase_value"] is None and totals["roas"] is None
    assert totals["purchases_by_window"] == {"1d_click": 3, "7d_click": 5}
    assert totals["roas_by_window"] == {"1d_click": 1.5, "7d_click": 2.5}            # pooled over both rows
    assert out["attribution"]["single_total"] is False and "never added" in out["attribution"]["note"]


def test_single_attribution_window_reads_its_own_key():
    fake(("/insights", FakeResponse(200, {"data": [windowed("1", 100)]})))
    out = meta_ads.meta_insights(start_date="2026-09-01", end_date="2026-09-30", attribution_windows=["7d_click"])
    assert out["rows"][0]["purchases"] == 5 and out["rows"][0]["purchase_value"] == 500
    assert out["totals"]["roas"] == 5.0 and out["attribution"]["windows"] == ["7d_click"]
    assert out["attribution"]["single_total"] is True and "7d_click" in out["attribution"]["note"]


def test_token_travels_only_in_the_authorization_header():
    pages = {None: {"data": [insight("1", 10, 100, 1, 1, 20)], "paging": {"cursors": {"after": "N1"},
                                                                           "next": "https://graph.facebook.com/n"}},
             "N1": {"data": [insight("2", 5, 100, 1)], "paging": {"cursors": {"after": "N2"}}}}
    session = fake(("/me/adaccounts", FakeResponse(200, ACCOUNTS_PAGE)),
                   ("/insights", lambda kwargs: FakeResponse(200, pages[kwargs["params"].get("after")])))
    meta_ads.meta_ad_accounts()
    meta_ads.meta_insights(level="ad", start_date="2026-09-01", end_date="2026-09-30", breakdowns=["age"],
                           attribution_windows=["7d_click"])
    meta_ads.summary("2026-09-01", "2026-09-30")
    assert len(session.calls) >= 5
    for method, url, kwargs in session.calls:
        headers = kwargs.get("headers") or {}
        assert set(headers) == {"Authorization"} and headers["Authorization"] == f"Bearer {TOKEN}"
        others = {k: v for k, v in kwargs.items() if k != "headers"}
        assert TOKEN not in url and TOKEN not in json.dumps(others, default=str), (url, others)
        assert "access_token" not in url and "access_token" not in json.dumps(others, default=str)
        assert TOKEN[4:20] not in url  # not even a fragment of it


def test_settling_days_with_a_fixed_today(monkeypatch):
    import datetime

    from google_ecommerce_mcp import metrics

    monkeypatch.setattr(metrics, "today_in", lambda tz: (datetime.date(2026, 10, 16), tz or "UTC"))

    def period(end):
        meta_ads.CLIENT.cache.clear()
        fake(("/insights", FakeResponse(200, {"data": []})))
        return meta_ads.meta_insights(start_date="2026-10-01", end_date=end)["date_range"]

    recent = period("2026-10-14")  # today - 2: inside the 3 settling days (14, 15, 16)
    assert recent["data_complete"] is False and recent["settling_from"] == "2026-10-14"
    assert recent["timezone"] == "Europe/Paris" and "compare complete days" in recent["settling_note"]
    assert period("2026-10-13")["data_complete"] is True
    monkeypatch.setattr(meta_ads, "SETTLE_DAYS", 0)  # with 0 settling days the same range would look final
    assert period("2026-10-14")["data_complete"] is True


def test_keyring_is_read_once_per_tool_call(monkeypatch, env):
    import keyring

    from google_ecommerce_mcp.config import KEYRING_SERVICE

    monkeypatch.delenv("META_ACCESS_TOKEN")
    env[(KEYRING_SERVICE, "meta-access-token")] = TOKEN
    reads = []

    def counting(service, user):
        reads.append(user)
        return env.get_password(service, user)

    monkeypatch.setattr(keyring, "get_password", counting)
    pages = {None: {"data": [insight("1", 1, 10, 1)], "paging": {"cursors": {"after": "K1"}, "next": "x"}},
             "K1": {"data": [insight("2", 1, 10, 1)], "paging": {"cursors": {"after": "K2"}, "next": "x"}},
             "K2": {"data": [insight("3", 1, 10, 1)], "paging": {"cursors": {}}}}
    session = fake(("/insights", lambda kwargs: FakeResponse(200, pages[kwargs["params"].get("after")])))
    out = meta_ads.meta_insights(level="campaign", start_date="2026-09-01", end_date="2026-09-30")
    assert out["pages_read"] == 3 and len(session.calls) == 4
    assert reads.count("meta-access-token") == 1


def test_broken_keyring_gives_a_structured_error(monkeypatch):
    import keyring

    def broken(service, user):
        raise RuntimeError("no keyring backend")

    def locked():
        raise OSError("keychain locked")

    monkeypatch.delenv("META_ACCESS_TOKEN")
    monkeypatch.setattr(keyring, "get_password", broken)
    session = fake()
    assert meta_ads.meta_insights()["error"] == "not_configured"
    monkeypatch.setattr(meta_ads, "_settings", locked)
    for tool in (meta_ads.meta_ad_accounts, meta_ads.meta_insights):
        out = tool()
        assert out["error"] == "not_configured" and "OSError" in out["detail"] and out["fix"]
    assert session.calls == []


def test_summary_never_raises(monkeypatch):
    def explode(**kwargs):
        raise KeyError(f"boom {TOKEN}")

    monkeypatch.setattr(meta_ads, "meta_insights", explode)
    s = meta_ads.summary("2026-09-01", "2026-09-30")
    assert s["error"] == "internal_error" and s["platform"] == "meta" and TOKEN not in json.dumps(s)


def test_only_ascii_digits_are_accepted():
    arabic = "١٢٣"  # Arabic-Indic 123: matched by \d, refused by [0-9]
    assert not meta_ads.CLIENT.is_allowed("GET", f"{V}/act_{arabic}/insights")
    assert not meta_ads.CLIENT.is_allowed("GET", "https://graph.facebook.com/v٢٦.0/me/adaccounts")
    session = fake(("", FakeResponse(200, {})))
    assert meta_ads.meta_insights(account_id=f"act_{arabic}")["error"] == "invalid_argument"
    assert session.calls == []


def test_tool_defaults_are_immutable():
    for name, param in inspect.signature(meta_ads.meta_insights).parameters.items():
        assert not isinstance(param.default, (list, dict, set)), name


# ------------------------------------------------------------------ second P3/P4 wave
def test_empty_result_is_no_activity_with_zero_conversions():
    fake(("/insights", FakeResponse(200, {"data": []})))
    out = meta_ads.meta_insights(start_date="2026-09-01", end_date="2026-09-30")
    assert out["rows"] == [] and out["totals"]["no_activity"] is True
    assert out["totals"]["purchases"] == 0.0 and out["totals"]["purchase_value"] == 0.0
    assert out["totals"]["roas"] is None and out["totals"]["spend"] == 0.0
    assert out["purchase_measurement"]["note"] is None

    meta_ads.CLIENT.cache.clear()
    fake(("/insights", FakeResponse(200, {"data": []})))
    s = meta_ads.summary("2026-09-01", "2026-09-30")
    assert s["spend"] == 0.0 and s["conversions"] == 0.0 and s["conversion_value"] == 0.0
    assert s["roas"] is None and s["no_activity"] is True


def test_spend_without_any_purchase_entry_is_unknown_not_zero():
    rows = [insight("1", 100, 1000, 10), insight("2", 40, 500, 4)]  # no actions/action_values anywhere
    fake(("/insights", FakeResponse(200, {"data": rows})))
    out = meta_ads.meta_insights(level="campaign", start_date="2026-09-01", end_date="2026-09-30")
    for row in out["rows"]:
        assert row["purchases"] is None and row["purchase_value"] is None and row["roas"] is None
    totals = out["totals"]
    assert totals["purchases"] is None and totals["purchase_value"] is None and totals["roas"] is None
    assert totals["spend"] == 140 and totals["no_activity"] is False
    assert out["purchase_measurement"]["measured"] is False
    note = out["purchase_measurement"]["note"]
    assert "pixel/CAPI" in note and "non mesurable" in note

    meta_ads.CLIENT.cache.clear()
    fake(("/insights", FakeResponse(200, {"data": rows})))
    s = meta_ads.summary("2026-09-01", "2026-09-30")
    assert s["conversions"] is None and s["conversion_value"] is None and s["roas"] is None
    assert s["purchase_measurement"]["measured"] is False and s["no_activity"] is False


def test_requested_window_absent_from_entry_is_zero_not_value():
    row = insight("1", 100, 1000, 10)
    row["actions"] = [{"action_type": "omni_purchase", "value": "9", "7d_click": "9"}]
    row["action_values"] = [{"action_type": "omni_purchase", "value": "900", "7d_click": "900"}]
    fake(("/insights", FakeResponse(200, {"data": [row]})))
    out = meta_ads.meta_insights(start_date="2026-09-01", end_date="2026-09-30", attribution_windows=("1d_view",))
    assert out["rows"][0]["purchases"] == 0.0 and out["rows"][0]["purchase_value"] == 0.0
    assert out["totals"]["roas"] == 0.0 and out["purchase_measurement"]["measured"] is True


def test_daily_rows_keep_the_most_expensive_days_when_trimmed():
    days = [insight("1", spend, 100, 1, date_start=f"2026-09-0{d}", date_stop=f"2026-09-0{d}")
            for d, spend in ((1, 5), (2, 10), (3, 50))]  # Meta answers in chronological order
    fake(("/insights", FakeResponse(200, {"data": days})))
    out = meta_ads.meta_insights(start_date="2026-09-01", end_date="2026-09-03", time_increment="1", limit=2)
    assert [r["date_start"] for r in out["rows"]] == ["2026-09-03", "2026-09-02"]
    assert out["truncated"] is True and out["rows_omitted"] == 1 and out["totals"]["spend"] == 65
    assert out["rows_order"].startswith("spend descending")
    full = meta_ads.meta_insights(start_date="2026-09-01", end_date="2026-09-03", time_increment="1")
    assert full["truncated"] is False and full["rows_omitted"] == 0


def test_account_info_is_fetched_once_for_two_reports():
    session = fake(("/insights", FakeResponse(200, {"data": []})))
    meta_ads.meta_insights(start_date="2026-09-01", end_date="2026-09-30")
    meta_ads.meta_insights(level="campaign", date_preset="last_7d")
    info_calls = [url for _, url, _ in session.calls if url.endswith(f"/act_{ACCOUNT}")]
    assert len(info_calls) == 1 and len(insights_calls(session)) == 2


# ------------------------------------------------------------------ final wave
def test_row_without_spend_stays_unknown_even_when_purchases_are_measured():
    measured = insight("1", 100, 1000, 10, 2, 200)
    no_spend = insight("2", 0, 50, 1)
    del no_spend["spend"]
    fake(("/insights", FakeResponse(200, {"data": [measured, no_spend]})))
    out = meta_ads.meta_insights(level="campaign", start_date="2026-09-01", end_date="2026-09-30")
    row = next(r for r in out["rows"] if r["campaign_id"] == "2")
    assert row["spend"] is None
    assert row["purchases"] is None and row["purchase_value"] is None and row["roas"] is None
    assert out["purchase_measurement"]["measured"] is True and out["totals"]["roas"] == 2.0


@pytest.mark.parametrize("tool", ["meta_ad_accounts", "meta_insights"])
def test_unexpected_exception_becomes_internal_error_without_secret(monkeypatch, tool):
    def explode(*args, **kwargs):
        raise RuntimeError(f"boom with Bearer {TOKEN}")

    session = fake()
    monkeypatch.setattr(meta_ads, "_paged", explode)
    out = getattr(meta_ads, tool)()
    assert out["error"] == "internal_error" and out["platform"] == "meta" and "RuntimeError" in out["detail"]
    assert TOKEN not in json.dumps(out) and out["fix"] and out["fetched_at"]
    assert session.calls == [] or all(TOKEN not in url for _, url, _ in session.calls)


def test_os_error_from_the_transport_is_contained():
    """E.g. REQUESTS_CA_BUNDLE pointing to a missing file: OSError is not a RequestException."""
    fake(("/me/adaccounts", OSError("Could not find a suitable TLS CA certificate bundle")))
    out = meta_ads.meta_ad_accounts()  # never raises: network (AdsClient) or internal_error (_contained)
    assert out["error"] in ("network", "internal_error") and out["platform"] == "meta" and out["detail"]


def test_invalid_api_version_is_contained_and_matches_google_code(monkeypatch):
    monkeypatch.setenv("META_API_VERSION", "26")
    session = fake(("", FakeResponse(200, {})))
    for tool in (meta_ads.meta_ad_accounts, meta_ads.meta_insights):
        out = tool()
        assert out["error"] == "invalid_api_version" and "v26.0" in out["fix"]
    assert meta_ads.summary("2026-09-01", "2026-09-30")["error"] == "invalid_api_version"
    assert session.calls == []


def test_contained_tools_keep_their_signature():
    params = inspect.signature(meta_ads.meta_insights).parameters
    assert list(params)[:3] == ["level", "start_date", "end_date"] and "account_id" in params

"""Offline tests of the OpenAI Ads (ChatGPT ads) read-only connector."""

import ast
import asyncio
import datetime
import inspect
import json
import logging
import re
from zoneinfo import ZoneInfo

import pytest

from ads_fakes import FakeResponse, FakeSession, no_keyring, no_network  # noqa: F401  (fixtures)
from google_ecommerce_mcp import openai_ads
from google_ecommerce_mcp.ads_config import AdsSettings

# Built at run time: no literal secret in the repository.
KEY = "sk-" + "test" + "A" * 30
BASE = "https://api.ads.openai.com/v1"
START, END = "2026-09-01", "2026-09-07"
CONV_ARGS = {"account_timezone": "Europe/Paris", "aggregation_level": "campaign", "entity_ids": ["cmpn_1"]}


@pytest.fixture(autouse=True)
def _isolated(no_keyring, monkeypatch, tmp_path):  # noqa: F811  (fixture)
    """No real keyring, no real key nor client registry from the machine, a fresh fake session and cache for
    every test (legacy mode: GEM_CLIENTS_FILE names a file that does not exist)."""
    for name in ("OPENAI_ADS_API_KEY", "META_ACCESS_TOKEN", "GADS_DEVELOPER_TOKEN", "GEM_CLIENT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEM_CLIENTS_FILE", str(tmp_path / "no-registry.json"))
    monkeypatch.setenv("OPENAI_ADS_API_KEY", KEY)
    monkeypatch.setattr(openai_ads.CLIENT, "session", FakeSession())
    monkeypatch.setattr(openai_ads.CLIENT, "BLOCKED_ATTEMPTS", [])
    openai_ads.CLIENT.cache.clear()
    yield
    openai_ads.CLIENT.cache.clear()


def use(rules):
    session = FakeSession(rules)
    openai_ads.CLIENT.session = session
    return session


ROW = {"id": "start=1:end=2:entity_id=cmpn_1", "campaign_id": "cmpn_1", "campaign_name": "Rentrée",
       "readable_time": "2026-09-01", "timezone": "Europe/Paris", "impressions": 1000, "clicks": 40, "spend": 12.5,
       "ctr": 0.04, "cpc": 0.3125, "cpm": 12.5, "conversions": 4, "cpa": 3.125, "post_click_cvr": 0.075,
       "order_created_attributed_sales": "50.0", "order_created_attributed_sales_currency": "EUR",
       "order_created_roas": 4.0}
NULL_ROW = {"campaign_id": "cmpn_2", "timezone": "Europe/Paris", "impressions": 10, "clicks": 0, "spend": 0,
            "ctr": 0.0, "cpc": None, "cpm": 0, "conversions": 0, "cpa": None, "post_click_cvr": None,
            "order_created_attributed_sales": None, "order_created_attributed_sales_currency": "EUR",
            "order_created_roas": None}


def page(rows, has_more=False, first_id="r1", last_id="r9"):
    return {"object": "list", "data": rows, "count": len(rows), "first_id": first_id, "last_id": last_id,
            "has_more": has_more}


EVENT = {"entity_id": "cmpn_1", "event_name": "order_created", "event_kind": "standard",
         "attributed_event_count": 2, "click_through_conversions": 2, "view_through_conversions": 0,
         "attributed_event_value_amount": 100, "attributed_event_value_count": 2,
         "attributed_event_value_currency": "EUR", "conversion_event_setting_breakdowns": [],
         "order_id": "ord_secret_42", "user_email": "someone@example.com"}
CONV = {"object": "list", "count": 2, "account_currency": "EUR",
        "data": [{"entity_id": "cmpn_1", "conversions": 5, "click_through_conversions": 4,
                  "view_through_conversions": 1, "order_created_attributed_sales": "80.5",
                  "order_created_attributed_sales_currency": "EUR"},
                 {"entity_id": "cmpn_2", "conversions": 0, "click_through_conversions": 0,
                  "view_through_conversions": 0, "order_created_attributed_sales": None,
                  "order_created_attributed_sales_currency": "EUR"}]}


def catch_all(kwargs=None):
    payload = CONV if (kwargs or {}).get("json") else {**page([dict(ROW)]), "id": "acct_1"}
    return FakeResponse(200, payload)


def run_every_tool():
    openai_ads.openai_ads_account()
    for level, entity in (("account", ""), ("campaign", "cmpn_1"), ("ad_group", "ag-2"), ("ad", "ad_3")):
        openai_ads.openai_ads_insights(START, END, level=level, entity_id=entity)
    openai_ads.openai_ads_insights(START, END, timezone="Europe/Paris", time_granularity="daily", segment="device",
                                   aggregation_level="campaign", after="start=1:end=2:sort_values=126",
                                   sort=[{"field": "spend", "direction": "desc"}, {"field": "campaign.id"}],
                                   filters=[{"field": "campaign.id", "operator": "IN", "value": ["cmpn_1"]},
                                            {"field": "ad.clicks", "operator": "GREATER_THAN", "value": 10}],
                                   attribution_window_days=7, view_through_attribution_window_days=0,
                                   attribution_time_basis="conversion_time")
    openai_ads.openai_ads_insights(START, END, before="r1", include_zero_impression_items=True)
    openai_ads.openai_ads_conversions(START, END, "Europe/Paris", "campaign", entity_ids=["cmpn_1"],
                                      breakdown="country", include_attributed_events=True,
                                      event_names=["order_created"], include_zero_rows=False,
                                      attribution_time_basis="ad_event_time")
    openai_ads.openai_ads_conversions(START, END, "UTC", "ad", group_by_entity=False)
    openai_ads.summary(START, END)


# ------------------------------------------------------------------ 1-3 allow-list, no write, refusals
def test_every_request_is_on_the_read_only_allow_list():
    session = use([("", catch_all)])
    run_every_tool()
    assert openai_ads.CLIENT.BLOCKED_ATTEMPTS == []
    assert len(session.calls) == 10
    for method, url, kwargs in session.calls:
        assert openai_ads.ALLOWED_ENDPOINTS.is_allowed(method, url, kwargs.get("json")), (method, url)
        assert url.startswith(BASE + "/")
        assert kwargs["headers"]["Authorization"] == f"Bearer {KEY}"
        assert KEY not in url and KEY not in json.dumps(kwargs.get("params") or {})


def test_allow_list_has_no_write_endpoint():
    patterns = openai_ads.ALLOWED_ENDPOINTS.patterns
    assert {m for m, _ in patterns} <= {"GET", "POST"}
    assert [p for m, p in patterns if m == "POST"] == [r"https://api\.ads\.openai\.com/v1/conversions/insights"]
    for _, pattern in patterns:
        for word in ("mutate", "create", "update", "delete", "pause", "upload", "budget", "publish"):
            assert word not in pattern.lower()
    assert openai_ads.ALLOWED_ENDPOINTS.hosts == {"api.ads.openai.com"}


VALID_RANGE = json.dumps({"type": "unix_range", "start": "1788213600", "end": "1788818400"})
VALID_BODY = {"aggregation_level": "campaign", "time_ranges": [VALID_RANGE], "entity_ids": ["cmpn_1"]}


@pytest.mark.parametrize("method,url,body", [
    ("POST", f"{BASE}/campaigns", {"name": "x"}),
    ("PATCH", f"{BASE}/ads/x", {"status": "paused"}),
    ("DELETE", f"{BASE}/campaigns/1", None),
    ("POST", f"{BASE}/ad_account/insights", {}),
    ("POST", f"{BASE}/campaigns/cmpn_1/pause", None),
    ("POST", f"{BASE}/campaigns/cmpn_1/activate", None),
    ("POST", f"{BASE}/campaigns/cmpn_1", {"status": "paused"}),
    ("GET", f"{BASE}/campaigns/cmpn_1/budget", None),
    ("GET", f"{BASE}/ad_account/spend_limit_windows", None),
    ("GET", f"{BASE}/custom_audiences", None),
    ("GET", f"{BASE}/campaigns/1/insights/../budget", None),
    ("GET", "https://api.ads.openai.com.evil.com/v1/ad_account", None),
    ("GET", "https://evil.example/api.ads.openai.com/v1/ad_account", None),
    ("GET", "http://api.ads.openai.com/v1/ad_account", None),
    ("POST", f"{BASE}/conversions/insights", None),
    ("POST", f"{BASE}/conversions/insights", {**VALID_BODY, "filters": []}),
    ("POST", f"{BASE}/conversions/insights", {**VALID_BODY, "action": "delete"}),
    ("POST", f"{BASE}/conversions/insights", {"time_ranges": [VALID_RANGE], "entity_ids": ["cmpn_1"]}),
    ("POST", f"{BASE}/conversions/insights", {**VALID_BODY, "aggregation_level": "ad_account"}),
    ("POST", f"{BASE}/conversions/insights", {**VALID_BODY, "entity_ids": []}),
    ("POST", f"{BASE}/conversions/insights", {"aggregation_level": "campaign", "time_ranges": [VALID_RANGE]}),
    ("POST", f"{BASE}/conversions/insights", {**VALID_BODY, "group_by_entity": False}),
    ("POST", f"{BASE}/conversions/insights", {**VALID_BODY, "group_by_entity": "no"}),
    ("POST", f"{BASE}/conversions/insights", {**VALID_BODY, "event_names": ["order_created"]}),
    ("POST", f"{BASE}/conversions/insights", {**VALID_BODY, "time_ranges": ["not json"]}),
    ("POST", f"{BASE}/conversions/insights",
     {**VALID_BODY, "time_ranges": [json.dumps({"type": "unix_range", "start": "1", "end": str(1 + 400 * 86400)})]}),
    ("POST", f"{BASE}/conversions/insights", {**VALID_BODY, "entity_ids": ["../x"]}),
    ("POST", f"{BASE}/conversions/insights", {**VALID_BODY, "breakdown": "product"}),
    ("POST", f"{BASE}/conversions/insights", {**VALID_BODY, "attribution_window_days": 90}),
])
def test_writes_foreign_hosts_and_bad_bodies_are_refused_without_network(method, url, body):
    session = use([("", catch_all)])
    result = openai_ads.CLIENT.call(method, url, json=body, headers={"Authorization": f"Bearer {KEY}"})
    assert result["error"] == "blocked"
    assert session.calls == []
    assert openai_ads.CLIENT.BLOCKED_ATTEMPTS and KEY not in str(openai_ads.CLIENT.BLOCKED_ATTEMPTS)


@pytest.mark.parametrize("body", [
    VALID_BODY,
    {"aggregation_level": "ad", "time_granularity": "daily", "time_ranges": [VALID_RANGE], "entity_ids": ["ad_1"],
     "group_by_entity": True, "breakdown": "device", "attribution_window_days": 14,
     "view_through_attribution_window_days": 0, "attribution_time_basis": "conversion_time",
     "include": ["attributed_events"], "event_names": ["order_created"], "include_zero_rows": True},
    {"aggregation_level": "campaign", "time_ranges": [VALID_RANGE], "group_by_entity": False},
])
def test_conversions_validator_accepts_the_documented_bodies(body):
    assert openai_ads.ALLOWED_ENDPOINTS.is_allowed("POST", f"{BASE}/conversions/insights", body)


def test_host_is_fixed_and_not_configurable(monkeypatch):
    for name in ("OPENAI_ADS_BASE_URL", "OPENAI_ADS_HOST", "OPENAI_BASE_URL"):
        monkeypatch.setenv(name, "https://evil.example/v1")
    session = use([("", catch_all)])
    openai_ads.openai_ads_account()
    assert session.calls[0][1] == f"{BASE}/ad_account"
    source = inspect.getsource(openai_ads)
    assert "getenv" not in source and "os.environ" not in source
    assert openai_ads.BASE_URL == BASE


def test_module_never_uses_requests_directly():
    tree = ast.parse(inspect.getsource(openai_ads))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(a.name.split(".")[0] != "requests" for a in node.names)
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "requests"
        if isinstance(node, ast.Name):
            assert node.id != "requests"


def test_key_is_read_once_per_tool_call_not_per_page(monkeypatch):
    reads = []
    real = AdsSettings.from_env

    def counting(*args, **kwargs):
        reads.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(openai_ads.AdsSettings, "from_env", counting)

    def answer(kwargs):
        index = {None: 0, "L0": 1, "L1": 2}[kwargs["params"].get("after")]
        return FakeResponse(200, page([dict(ROW)], has_more=index < 2, last_id=f"L{index}"))

    session = use([("/ad_account/insights", answer)])
    openai_ads.openai_ads_insights(START, END, page_size=1, max_rows=10)
    assert len(session.calls) == 3 and len(reads) == 1


def test_tool_defaults_are_not_mutable_lists():
    for fn in (openai_ads.openai_ads_insights, openai_ads.openai_ads_conversions):
        for param in inspect.signature(fn).parameters.values():
            assert not isinstance(param.default, (list, dict)), (fn.__name__, param.name)


# ------------------------------------------------------------------ input validation
@pytest.mark.parametrize("level,entity_id", [
    ("campaign", ""), ("campaign", "../ad_account"), ("ad", "a/b"), ("ad_group", "x?y=1"), ("ad", "a b"),
    ("campaign", "%2e%2e"), ("ad", "x" * 129), ("account", "cmpn_1"), ("campaign", "id;drop"),
    ("campaign", "cmpn_١٢"),  # non-ASCII digits
])
def test_invalid_entity_id_is_refused_without_network(level, entity_id):
    session = use([("", catch_all)])
    result = openai_ads.openai_ads_insights(START, END, level=level, entity_id=entity_id)
    assert result["error"] == "invalid_entity_id" and session.calls == []


@pytest.mark.parametrize("kwargs,code", [
    ({"start_date": "2026-02-30"}, "invalid_dates"),
    ({"start_date": "2026-03-01", "end_date": "2026-02-01"}, "invalid_dates"),
    ({"timezone": "Europe/Paris\"}"}, "invalid_timezone"),
    ({"time_granularity": "weekly"}, "invalid_time_granularity"),
    ({"time_granularity": "hourly", "segment": "device"}, "invalid_time_granularity"),
    ({"segment": "age"}, "invalid_segment"),
    ({"segment": "product", "include_zero_impression_items": True}, "invalid_argument"),
    ({"aggregation_level": "account"}, "invalid_aggregation_level"),
    ({"fields": []}, "invalid_fields"),
    ({"fields": ["spend", "Bad-Name"]}, "invalid_fields"),
    ({"filters": [{"field": "campaign.id", "operator": "IN"}]}, "invalid_filters"),
    ({"filters": [{"field": "campaign.id", "operator": "in", "value": ["x"]}]}, "invalid_filters"),
    ({"filters": [{"field": "campaign.id", "operator": "EQUALS", "value": ["x"]}]}, "invalid_filters"),
    ({"filters": [{"field": "campaign.id", "operator": "IN", "value": "x"}]}, "invalid_filters"),
    ({"filters": [{"field": "campaign.id", "operator": "IN", "value": []}]}, "invalid_filters"),
    ({"filters": [{"field": "ad.clicks", "operator": "GREATER_THAN", "value": ["10"]}]}, "invalid_filters"),
    ({"filters": [{"field": "ad.clicks", "operator": "LESS_THAN", "value": True}]}, "invalid_filters"),
    ({"filters": [{"field": "x y", "operator": "IN", "value": ["1"]}]}, "invalid_filters"),
    ({"sort": [{"field": "spend desc"}]}, "invalid_sort"),
    ({"sort": [{"field": "spend", "direction": "down"}]}, "invalid_sort"),
    ({"sort": [{"field": "spend", "order": "asc"}]}, "invalid_sort"),
    ({"attribution_window_days": 28}, "invalid_attribution"),
    ({"view_through_attribution_window_days": 7}, "invalid_attribution"),
    ({"attribution_time_basis": "click_time"}, "invalid_attribution"),
    ({"page_size": 0}, "invalid_limit"),
    ({"page_size": 2001}, "invalid_limit"),
    ({"max_rows": 6001}, "invalid_limit"),
    ({"after": "a", "before": "b"}, "invalid_cursor"),
    ({"after": "line\nbreak"}, "invalid_cursor"),
])
def test_invalid_insights_parameters_are_refused_without_network(kwargs, code):
    session = use([("", catch_all)])
    args = {"start_date": START, "end_date": END, **kwargs}
    assert openai_ads.openai_ads_insights(**args)["error"] == code
    assert session.calls == []


@pytest.mark.parametrize("kwargs,code", [
    ({"start_date": "2026-13-01"}, "invalid_dates"),
    ({"start_date": "2025-01-01", "end_date": "2026-01-01"}, "invalid_dates"),  # 366 days
    ({"account_timezone": ""}, "invalid_timezone"),
    ({"account_timezone": "Mars/Olympus_Mons"}, "invalid_timezone"),
    ({"account_timezone": "Europe/Paris;x"}, "invalid_timezone"),
    ({"aggregation_level": "ad_account"}, "invalid_argument"),
    ({"entity_ids": None}, "invalid_argument"),
    ({"entity_ids": []}, "invalid_argument"),
    ({"group_by_entity": False}, "invalid_argument"),  # entity_ids given with group_by_entity false
    ({"entity_ids": ["cmp/1"]}, "invalid_entity_id"),
    ({"event_names": ["order_created"]}, "invalid_argument"),  # without include_attributed_events
    ({"event_names": ["purchase now"], "include_attributed_events": True}, "invalid_event_names"),
    ({"event_names": ["e"] * 21, "include_attributed_events": True}, "invalid_event_names"),
    ({"attribution_window_days": 1}, "invalid_attribution"),
])
def test_invalid_conversions_parameters_are_refused_without_network(kwargs, code):
    session = use([("", catch_all)])
    args = {"start_date": START, "end_date": END, **CONV_ARGS, **kwargs}
    assert openai_ads.openai_ads_conversions(**args)["error"] == code
    assert session.calls == []


def test_conversions_has_no_filters_and_requires_level():
    params = inspect.signature(openai_ads.openai_ads_conversions).parameters
    assert "filters" not in params and "filters" not in openai_ads._BODY_KEYS
    assert params["aggregation_level"].default is inspect.Parameter.empty
    assert params["account_timezone"].default is inspect.Parameter.empty


# ------------------------------------------------------------------ outputs
def test_account_keeps_only_whitelisted_fields_and_is_cached():
    raw = {"id": "acct_1", "name": "Shop", "status": "active", "currency_code": "EUR", "timezone": "Europe/Paris",
           "billing_email": "owner@example.com", "owner": {"name": "Jane"}}
    session = use([("GET", "/v1/ad_account", FakeResponse(200, raw))])
    first = openai_ads.openai_ads_account()
    second = openai_ads.openai_ads_account()
    assert first["ad_account"] == {"id": "acct_1", "name": "Shop", "status": "active", "currency_code": "EUR",
                                   "timezone": "Europe/Paris"}
    assert first["raw_fields_dropped"] == ["billing_email", "owner"]
    assert "owner@example.com" not in json.dumps(first)
    assert first["cached"] is False and second["cached"] is True
    assert first["fetched_at"] and len(session.calls) == 1


def test_insights_sends_documented_bracket_parameters():
    session = use([("/ad_account/insights", FakeResponse(200, page([dict(ROW)])))])
    openai_ads.openai_ads_insights(START, END, timezone="Europe/Paris", time_granularity="daily", segment="country",
                                   aggregation_level="campaign",
                                   sort=[{"field": "spend", "direction": "asc"}, {"field": "campaign.id"}],
                                   filters=[{"field": "campaign.id", "operator": "IN", "value": ["x"]},
                                            {"field": "ad.clicks", "operator": "GREATER_THAN", "value": 10}],
                                   include_zero_impression_items=False, attribution_window_days=14,
                                   view_through_attribution_window_days=0, attribution_time_basis="conversion_time",
                                   page_size=100)
    params = session.calls[0][2]["params"]
    assert [json.loads(t) for t in params["time_ranges[]"]] == [
        {"type": "date_range", "since": START, "until": END, "timezone": "Europe/Paris"}]
    assert params["fields[]"] == openai_ads.DEFAULT_FIELDS and "metadata.timezone" in params["fields[]"]
    assert params["segments[]"] == ["country"]
    assert [json.loads(f) for f in params["filters[]"]] == [
        {"field": "campaign.id", "operator": "IN", "value": ["x"]},
        {"field": "ad.clicks", "operator": "GREATER_THAN", "value": 10}]
    assert [json.loads(s) for s in params["sort[]"]] == [{"field": "spend", "direction": "asc"},
                                                         {"field": "campaign.id", "direction": "desc"}]
    assert params["time_granularity"] == "daily" and params["aggregation_level"] == "campaign"
    assert params["attribution_window_days"] == 14 and params["view_through_attribution_window_days"] == 0
    assert params["attribution_time_basis"] == "conversion_time" and params["limit"] == 100
    for unbracketed in ("time_ranges", "fields", "segments", "filters", "sort", "includes", "after", "before"):
        assert unbracketed not in params


def test_insights_defaults_leave_optional_parameters_out():
    session = use([("/ad_account/insights", FakeResponse(200, page([dict(ROW)])))])
    out = openai_ads.openai_ads_insights(START, END, include_zero_impression_items=True)
    params = session.calls[0][2]["params"]
    assert json.loads(params["time_ranges[]"][0]) == {"type": "date_range", "since": START, "until": END}
    assert params["includes[]"] == ["zero_impression_items"]
    for key in ("segments[]", "filters[]", "sort[]", "aggregation_level", "attribution_time_basis"):
        assert key not in params
    assert out["attribution"]["click_window_days"] == 30 and out["attribution"]["view_window_days"] == 1
    assert out["attribution"]["time_basis"] == "ad_event_time"


def test_timezone_comes_from_metadata_timezone_of_the_rows():
    use([("/ad_account/insights", FakeResponse(200, page([dict(ROW, timezone="America/New_York")])))])
    out = openai_ads.openai_ads_insights(START, END)
    assert out["timezone_reported"] is True and out["date_range"]["timezone"] == "America/New_York"
    assert out["date_range"]["timezone_source"].startswith("api")


def test_timezone_unknown_only_when_the_field_is_absent():
    row = {k: v for k, v in ROW.items() if k != "timezone"}
    use([("/ad_account/insights", FakeResponse(200, page([row])))])
    out = openai_ads.openai_ads_insights(START, END)
    assert out["timezone_reported"] is False and out["date_range"]["timezone"].startswith("ad account timezone")
    use([("/ad_account/insights", FakeResponse(200, page([row])))])
    sent = openai_ads.openai_ads_insights(START, END, timezone="Europe/Paris")
    assert sent["timezone_reported"] is False and sent["date_range"]["timezone"] == "Europe/Paris"
    assert sent["date_range"]["timezone_source"].startswith("request")


def test_insights_keeps_null_values_and_states_units_and_currency():
    use([("/ad_account/insights", FakeResponse(200, page([dict(ROW), dict(NULL_ROW)])))])
    out = openai_ads.openai_ads_insights(START, END)
    assert out["rows"][1]["cpc"] is None and "order_created_roas" in out["rows"][1]  # null kept, not dropped
    assert out["rows"][0]["campaign_id"] == "cmpn_1"  # flat keys as returned
    assert out["null_means"] == "non calculable"
    assert "spend_unit_unverified" not in out and "major units" in out["units"]
    assert out["metrics"]["ctr"]["unit"].startswith("ratio") and "major units" in out["metrics"]["spend"]["unit"]
    assert out["currency"] == "EUR" and "currency_note" not in out
    assert out["date_range"]["start"] == START and out["level"] == "account"
    totals = out["totals"]
    assert totals["spend"] == 12.5 and totals["impressions"] == 1010 and totals["clicks"] == 40
    assert totals["order_created_attributed_sales"] == 50.0 and totals["order_created_roas"] == 4.0
    assert totals["ctr"] == round(40 / 1010, 6) and totals["cpc"] == 0.3125 and totals["post_click_cvr"] is None


def test_currency_is_none_with_a_note_when_absent_or_mixed():
    row = {k: v for k, v in ROW.items() if k != "order_created_attributed_sales_currency"}
    use([("/ad_account/insights", FakeResponse(200, page([row])))])
    out = openai_ads.openai_ads_insights(START, END, fields=["spend", "clicks"])
    assert out["currency"] is None and "never guessed" in out["currency_note"]
    use([("/ad_account/insights", FakeResponse(200, page([dict(ROW),
                                                          dict(ROW, order_created_attributed_sales_currency="USD")])))])
    mixed = openai_ads.openai_ads_insights(START, END)
    assert mixed["currency"] is None and "EUR" in mixed["currency_note"] and "USD" in mixed["currency_note"]


def test_settle_days_is_two():
    today = datetime.datetime.now(ZoneInfo("Europe/Paris")).date()
    yesterday = (today - datetime.timedelta(days=1)).isoformat()
    two_days_ago = (today - datetime.timedelta(days=2)).isoformat()
    use([("/ad_account/insights", FakeResponse(200, page([dict(ROW)])))])
    out = openai_ads.openai_ads_insights(two_days_ago, yesterday)
    assert out["date_range"]["data_complete"] is False and out["date_range"]["settling_from"] == yesterday
    assert "spend" in out["date_range"]["settling_note"]


def test_entity_level_path():
    session = use([("/campaigns/cmpn_1/insights", FakeResponse(200, page([dict(ROW)])))])
    out = openai_ads.openai_ads_insights(START, END, level="campaign", entity_id="cmpn_1")
    assert out["row_count"] == 1 and out["entity_id"] == "cmpn_1"
    assert session.calls[0][1] == f"{BASE}/campaigns/cmpn_1/insights"


def test_total_roas_is_pooled_not_the_mean_of_row_roas():
    rows = [dict(ROW, spend=100, order_created_attributed_sales=1000, order_created_roas=10.0),
            dict(ROW, spend=10, order_created_attributed_sales=10, order_created_roas=1.0)]
    use([("/ad_account/insights", FakeResponse(200, page(rows)))])
    roas = openai_ads.openai_ads_insights(START, END)["totals"]["order_created_roas"]
    assert roas == round(1010 / 110, 6) and roas != (10.0 + 1.0) / 2


DOC_CURSORS = ["start=1777075200:end=1777161600:entity_id=v2ad_account_id%3Dadacct_123%7Cproduct_feed_id%3Dfeed_1",
               "start=1777075200:end=1777161600:entity_id=v2ad_account_id%3D%3Cnull%3E%7Citem_id%3Dsku_2",
               "start=1:end=2:entity_id=ad_501:sort=clicks.desc:sort_values=126",
               "with a space"]


def test_documented_cursor_shapes_are_followed_and_accepted():
    def answer(kwargs):
        after = kwargs["params"].get("after")
        index = 0 if after is None else DOC_CURSORS.index(after) + 1
        return FakeResponse(200, page([dict(ROW, impressions=index)], has_more=index < 4,
                                      last_id=DOC_CURSORS[index] if index < 4 else "end"))

    session = use([("/ad_account/insights", answer)])
    out = openai_ads.openai_ads_insights(START, END, page_size=1, max_rows=10)
    assert [c[2]["params"].get("after") for c in session.calls] == [None] + DOC_CURSORS
    assert out["complete"] is True and out["row_count"] == 5
    for cursor in DOC_CURSORS:
        session = use([("/ad_account/insights", FakeResponse(200, page([dict(ROW)])))])
        assert "error" not in openai_ads.openai_ads_insights(START, END, after=cursor)
        assert session.calls[0][2]["params"]["after"] == cursor


def test_pagination_page_size_and_max_rows_are_separate():
    def answer(kwargs):
        after = kwargs["params"].get("after")
        index = 0 if after is None else int(after[1:]) + 1
        size = kwargs["params"]["limit"]
        return FakeResponse(200, page([dict(ROW, impressions=index)] * size, has_more=True, first_id=f"F{index}",
                                      last_id=f"L{index}"))

    session = use([("/ad_account/insights", answer)])
    out = openai_ads.openai_ads_insights(START, END, page_size=2, max_rows=5)
    assert [c[2]["params"]["limit"] for c in session.calls] == [2, 2, 1]  # last page asks only what is needed
    assert out["row_count"] == 5 and out["pages_read"] == 3 and out["stop_reason"] == "max_rows"
    assert out["has_more"] is True and out["truncated"] is True and out["next_after"] == "L2"
    assert out["complete"] is False and out["totals_complete"] is False and out["first_id"] == "F0"
    assert [c[2]["params"].get("after") for c in session.calls] == [None, "L0", "L1"]


def test_pagination_stops_at_max_pages():
    def answer(kwargs):
        after = kwargs["params"].get("after")
        index = 0 if after is None else int(after[1:]) + 1
        return FakeResponse(200, page([dict(ROW)], has_more=True, last_id=f"L{index}"))

    session = use([("/ad_account/insights", answer)])
    out = openai_ads.openai_ads_insights(START, END, page_size=1, max_rows=6000)
    assert len(session.calls) == openai_ads.MAX_PAGES and out["stop_reason"] == "max_pages"
    assert out["next_after"] == f"L{openai_ads.MAX_PAGES - 1}" and out["totals_complete"] is False


def test_single_page_when_everything_fits():
    session = use([("/ad_account/insights", FakeResponse(200, page([dict(ROW)] * 3, has_more=False)))])
    out = openai_ads.openai_ads_insights(START, END)
    assert out["row_count"] == 3 and out["truncated"] is False and out["complete"] is True
    assert out["next_after"] is None and len(session.calls) == 1
    assert session.calls[0][2]["params"]["limit"] == 500


def test_resumed_read_is_never_complete():
    use([("/ad_account/insights", FakeResponse(200, page([dict(ROW)], has_more=False)))])
    out = openai_ads.openai_ads_insights(START, END, after="L7")
    assert out["has_more"] is False and out["complete"] is False and out["totals_complete"] is False


@pytest.mark.parametrize("second_page", [
    page([], has_more=True, last_id="L1"),            # empty page that claims more
    page([dict(ROW)], has_more=True, last_id="L0"),   # same last_id as the cursor sent
])
def test_page_without_progress_stops(second_page):
    def answer(kwargs):
        if kwargs["params"].get("after") is None:
            return FakeResponse(200, page([dict(ROW)], has_more=True, last_id="L0"))
        return FakeResponse(200, second_page)

    session = use([("/ad_account/insights", answer)])
    out = openai_ads.openai_ads_insights(START, END, page_size=1, max_rows=100)
    assert len(session.calls) == 2 and out["stop_reason"] == "no_progress"
    assert out["page_error"]["error"] == "api_error" and out["next_after"] is None and out["complete"] is False


def test_empty_first_page_with_has_more_stops():
    session = use([("/ad_account/insights", FakeResponse(200, page([], has_more=True, last_id="L0")))])
    out = openai_ads.openai_ads_insights(START, END)
    assert len(session.calls) == 1 and out["stop_reason"] == "no_progress" and out["row_count"] == 0


def test_more_rows_than_limit_do_not_skip_rows():
    rows = [dict(ROW, id=f"row{i}", impressions=i) for i in range(6)]

    def answer(kwargs):
        after = kwargs["params"].get("after")
        start = 0 if after is None else int(after[3:]) + 1
        # a misbehaving API: always 3 rows whatever the limit, last_id = last row sent
        sent = rows[start:start + 3]
        return FakeResponse(200, page(sent, has_more=start + 3 < len(rows), last_id=sent[-1]["id"]))

    session = use([("/ad_account/insights", answer)])
    out = openai_ads.openai_ads_insights(START, END, page_size=2, max_rows=100)
    assert [r["impressions"] for r in out["rows"]] == [0, 1, 2, 3, 4, 5]  # nothing skipped, nothing doubled
    assert [c[2]["params"].get("after") for c in session.calls] == [None, "row1", "row3"]
    assert out["complete"] is True


def test_more_rows_than_limit_continue_after_the_last_row_kept():
    rows = [dict(ROW, id=f"row{i}") for i in range(3)]
    use([("/ad_account/insights", FakeResponse(200, page(rows, has_more=False, last_id="row2")))])
    out = openai_ads.openai_ads_insights(START, END, page_size=2, max_rows=2)
    assert out["row_count"] == 2 and out["next_after"] == "row1" and out["has_more"] is True
    assert out["stop_reason"] == "max_rows"


def test_more_rows_than_limit_without_row_ids_stops_with_an_error():
    use([("/ad_account/insights", FakeResponse(200, page([dict(ROW, id=None)] * 3, has_more=False)))])
    out = openai_ads.openai_ads_insights(START, END, page_size=2, max_rows=10)
    assert out["row_count"] == 2 and out["stop_reason"] == "error" and out["next_after"] is None
    assert out["complete"] is False


@pytest.mark.parametrize("operator,value", [("IN", ["cmpn_1"]), ("GREATER_THAN", 10), ("LESS_THAN", 2.5)])
def test_each_documented_filter_operator_is_sent(operator, value):
    field = "campaign.id" if operator == "IN" else "ad.clicks"
    session = use([("/ad_account/insights", FakeResponse(200, page([dict(ROW)])))])
    out = openai_ads.openai_ads_insights(START, END, filters=[{"field": field, "operator": operator, "value": value}])
    assert "error" not in out
    assert [json.loads(f) for f in session.calls[0][2]["params"]["filters[]"]] == [
        {"field": field, "operator": operator, "value": value}]


def test_before_reads_one_page_backwards():
    session = use([("/ad_account/insights", FakeResponse(200, page([dict(ROW)], has_more=True)))])
    out = openai_ads.openai_ads_insights(START, END, before="F5")
    assert len(session.calls) == 1 and session.calls[0][2]["params"]["before"] == "F5"
    assert "after" not in session.calls[0][2]["params"]
    assert out["next_after"] is None and out["first_id"] == "r1" and out["complete"] is False


def test_has_more_without_last_id_stops_paging():
    session = use([("/ad_account/insights", FakeResponse(200, page([dict(ROW)], has_more=True, last_id=None)))])
    out = openai_ads.openai_ads_insights(START, END)
    assert len(session.calls) == 1 and out["page_error"]["error"] == "api_error" and out["complete"] is False


def test_error_on_a_later_page_keeps_rows_and_reports_it():
    def answer(kwargs):
        if kwargs["params"].get("after"):
            return FakeResponse(429, {"error": {"message": "slow down"}}, {"Retry-After": "9"})
        return FakeResponse(200, page([dict(ROW)], has_more=True, last_id="L0"))

    session = use([("/ad_account/insights", answer)])
    out = openai_ads.openai_ads_insights(START, END)
    assert out["row_count"] == 1 and out["page_error"]["error"] == "rate_limited"
    assert out["page_error"]["retry_after_seconds"] == 9
    assert out["complete"] is False and len(session.calls) == 2  # no automatic retry


def test_conversions_posts_whole_days_as_unix_range_end_exclusive():
    session = use([("POST", "/conversions/insights", FakeResponse(200, CONV))])
    out = openai_ads.openai_ads_conversions(START, END, "Europe/Paris", "campaign", entity_ids=["cmpn_1", "cmpn_2"],
                                            breakdown="device")
    method, url, kwargs = session.calls[0]
    assert method == "POST" and url == f"{BASE}/conversions/insights"
    body = kwargs["json"]
    paris = ZoneInfo("Europe/Paris")
    expected_start = int(datetime.datetime(2026, 9, 1, tzinfo=paris).timestamp())
    expected_end = int(datetime.datetime(2026, 9, 8, tzinfo=paris).timestamp())
    assert json.loads(body["time_ranges"][0]) == {"type": "unix_range", "start": str(expected_start),
                                                  "end": str(expected_end)}
    assert expected_end - expected_start == 7 * 86400
    assert body["entity_ids"] == ["cmpn_1", "cmpn_2"] and body["breakdown"] == "device"
    assert body["aggregation_level"] == "campaign" and body["group_by_entity"] is True
    assert body["include_zero_rows"] is True and "include" not in body and "event_names" not in body
    assert set(body) <= openai_ads._BODY_KEYS and "filters" not in body and "cursor" not in body
    assert out["currency"] == "EUR" and out["count"] == 2 and out["row_count"] == 2
    assert out["rows"][1]["order_created_attributed_sales"] is None and out["null_means"] == "non calculable"
    assert out["totals"] == {"conversions": 5.0, "click_through_conversions": 4.0, "view_through_conversions": 1.0,
                             "order_created_attributed_sales": 80.5}
    assert out["unix_range"]["end_exclusive"] is True and out["date_range"]["timezone"] == "Europe/Paris"
    assert out["attribution"]["click_window_days"] == 30 and "warning" not in out


def test_conversions_whole_account_with_group_by_entity_false():
    session = use([("POST", "/conversions/insights", FakeResponse(200, CONV))])
    out = openai_ads.openai_ads_conversions(START, END, "Europe/Paris", "ad", group_by_entity=False)
    body = session.calls[0][2]["json"]
    assert body["group_by_entity"] is False and "entity_ids" not in body and body["aggregation_level"] == "ad"
    assert "error" not in out


def test_attributed_events_keep_only_aggregate_fields_with_a_warning():
    payload = {**CONV, "data": [dict(CONV["data"][0], attributed_events=[dict(EVENT)], raw_debug="x")]}
    session = use([("POST", "/conversions/insights", FakeResponse(200, payload))])
    out = openai_ads.openai_ads_conversions(START, END, **CONV_ARGS, include_attributed_events=True,
                                            event_names=["order_created"])
    body = session.calls[0][2]["json"]
    assert body["include"] == ["attributed_events"] and body["event_names"] == ["order_created"]
    event = out["rows"][0]["attributed_events"][0]
    assert set(event) == set(openai_ads.EVENT_FIELDS)
    text = json.dumps(out)
    assert "ord_secret_42" not in text and "someone@example.com" not in text
    assert {"order_id", "user_email", "raw_debug", "conversion_event_setting_breakdowns",
            "entity_id"} <= set(out["raw_fields_dropped"])
    assert "identifiers" in out["warning"]
    assert "identifiers" in openai_ads.openai_ads_conversions.__doc__


def test_conversions_dst_week_and_utc():
    session = use([("POST", "/conversions/insights", FakeResponse(200, CONV))])
    openai_ads.openai_ads_conversions("2026-10-19", "2026-10-25", **CONV_ARGS)  # DST ends on 25 Oct
    rng = json.loads(session.calls[0][2]["json"]["time_ranges"][0])
    assert int(rng["end"]) - int(rng["start"]) == 7 * 86400 + 3600
    openai_ads.openai_ads_conversions(START, START, **{**CONV_ARGS, "account_timezone": "UTC"})
    rng = json.loads(session.calls[1][2]["json"]["time_ranges"][0])
    assert rng["start"] == str(int(datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc).timestamp()))
    assert int(rng["end"]) - int(rng["start"]) == 86400


def test_conversions_currency_none_when_absent():
    use([("POST", "/conversions/insights", FakeResponse(200, {"object": "list", "data": [], "count": 0}))])
    out = openai_ads.openai_ads_conversions(START, END, **CONV_ARGS)
    assert out["currency"] is None and "never guessed" in out["currency_note"] and out["rows"] == []


def test_conversions_413_is_too_many_rows():
    session = use([("/conversions/insights", FakeResponse(413, {"error": {"message": "more than 2000 rows"}}))])
    out = openai_ads.openai_ads_conversions(START, END, **CONV_ARGS)
    assert out["error"] == "too_many_rows" and out["fix"] == "réduire la période ou ajouter des filtres"
    assert len(session.calls) == 1


def test_429_with_retry_after_is_rate_limited_without_retry():
    session = use([("/ad_account/insights", FakeResponse(429, {"error": {"message": "rate limit"}},
                                                         {"Retry-After": "30"}))])
    out = openai_ads.openai_ads_insights(START, END)
    assert out["error"] == "rate_limited" and out["retry_after_seconds"] == 30
    assert len(session.calls) == 1


# ------------------------------------------------------------------ one test per error code
def test_error_not_configured(monkeypatch):
    monkeypatch.delenv("OPENAI_ADS_API_KEY")
    session = use([("", catch_all)])
    for result in (openai_ads.openai_ads_account(), openai_ads.openai_ads_insights(START, END),
                   openai_ads.openai_ads_conversions(START, END, **CONV_ARGS), openai_ads.summary(START, END)):
        assert result["error"] == "not_configured" and "OPENAI_ADS_API_KEY" in result["detail"]
    assert session.calls == []


def test_key_from_keyring_is_used_when_env_is_empty(monkeypatch, no_keyring):  # noqa: F811
    monkeypatch.delenv("OPENAI_ADS_API_KEY")
    no_keyring.set_password("google-ecommerce-mcp", "openai-ads-key", KEY)
    session = use([("", catch_all)])
    assert "error" not in openai_ads.openai_ads_account()
    assert session.calls[0][2]["headers"]["Authorization"] == f"Bearer {KEY}"


def test_error_auth_failed():
    use([("", FakeResponse(401, {"error": {"message": "Incorrect API key provided", "type": "invalid_request"}}))])
    out = openai_ads.openai_ads_insights(START, END)
    assert out["error"] == "auth_failed" and "OPENAI_ADS_API_KEY" in out["fix"]


def test_error_missing_scope():
    use([("", FakeResponse(403, {"error": {"message": "API key lacks the required scope ads.read"}}))])
    assert openai_ads.openai_ads_account()["error"] == "missing_scope"


def test_error_rate_limited():
    use([("", FakeResponse(429, {"error": {"message": "too many requests"}}, {"retry-after": "5"}))])
    out = openai_ads.openai_ads_account()
    assert out["error"] == "rate_limited" and out["retry_after_seconds"] == 5


def test_error_not_available():
    use([("", FakeResponse(404, {"error": {"message": "Ads API not enabled for this organization"}}))])
    out = openai_ads.openai_ads_insights(START, END, level="ad", entity_id="ad_9")
    assert out["error"] == "not_available" and out["fix"]


# ------------------------------------------------------------------ secrets
def test_error_echoing_the_key_never_shows_it(caplog):
    echo = {"error": {"message": f"Incorrect API key provided: {KEY}", "authorization": f"Bearer {KEY}"}}
    use([("", FakeResponse(401, echo, text=json.dumps(echo)))])
    with caplog.at_level(logging.DEBUG):
        results = [openai_ads.openai_ads_account(), openai_ads.openai_ads_insights(START, END),
                   openai_ads.openai_ads_conversions(START, END, **CONV_ARGS), openai_ads.summary(START, END)]
        openai_ads.CLIENT.call("PATCH", f"{BASE}/ads/x", headers={"Authorization": f"Bearer {KEY}"})
    for result in results:
        assert result["error"] == "auth_failed"
        assert KEY not in json.dumps(result) and "[REDACTED]" in json.dumps(result)
    assert KEY not in caplog.text


def test_unusual_key_shape_echoed_is_masked_through_the_header(monkeypatch):
    plain = "plainKeyWithoutPrefix123456"
    monkeypatch.setenv("OPENAI_ADS_API_KEY", plain)
    use([("", FakeResponse(500, {"error": {"message": f"key {plain} broken"}}))])
    out = openai_ads.openai_ads_insights(START, END)
    assert out["error"] == "api_error" and plain not in json.dumps(out)


def test_settings_repr_masks_the_key():
    settings = AdsSettings.from_env(keyring_fallback=False)
    assert settings.openai_ads_api_key == KEY and KEY not in repr(settings)


def test_no_literal_secret_in_this_module():
    source = inspect.getsource(openai_ads)
    assert not re.search(r"sk-[A-Za-z0-9_-]{16,}", source)


# ------------------------------------------------------------------ summary for ads_overview
def test_summary_is_normalised():
    session = use([("/ad_account/insights", FakeResponse(200, page([dict(ROW), dict(NULL_ROW)])))])
    out = openai_ads.summary(START, END)
    for key in ("spend", "impressions", "clicks", "conversions", "conversion_value", "roas", "currency",
                "attribution", "date_range"):
        assert key in out
    assert out["spend"] == 12.5 and out["conversion_value"] == 50.0 and out["roas"] == 4.0
    assert out["currency"] == "EUR" and out["timezone_reported"] is True and "major units" in out["units"]
    assert out["date_range"]["timezone"] == "Europe/Paris" and "spend_unit_unverified" not in out
    assert out["platform"] == "openai_ads" and out["totals_complete"] is True
    params = session.calls[0][2]["params"]
    assert params["time_granularity"] == "none" and params["aggregation_level"] == "ad_account"
    assert "order_created_attributed_sales_currency" in params["fields[]"]
    assert "metadata.timezone" in params["fields[]"] and params["limit"] == 2000
    assert "timezone" not in json.loads(params["time_ranges[]"][0])  # account timezone for conversion metrics
    assert len(session.calls) == 1  # no extra call to guess the currency


@pytest.mark.parametrize("account", [{"id": "acct_1", "currency_code": "EUR"}, {"id": "acct_1", "currency": "EUR"},
                                     {"id": "acct_1", "account_currency": "EUR"}])
def test_summary_without_activity_gives_zeros_and_the_account_currency(account):
    session = use([("/ad_account/insights", FakeResponse(200, page([]))),
                   ("GET", "/v1/ad_account", FakeResponse(200, account))])
    out = openai_ads.summary(START, END)
    assert (out["spend"], out["impressions"], out["clicks"], out["conversions"]) == (0.0, 0.0, 0.0, 0.0)
    assert out["conversion_value"] == 0.0 and out["roas"] is None and out["no_activity"] is True
    assert out["currency"] == "EUR" and out["currency_source"] == "ad_account" and "currency_note" not in out
    assert [c[1] for c in session.calls] == [f"{BASE}/ad_account/insights", f"{BASE}/ad_account"]
    openai_ads.summary(START, END)
    assert len(session.calls) == 3  # the ad account lookup is cached 300 s


def test_summary_without_activity_and_no_account_currency_never_guesses():
    use([("/ad_account/insights", FakeResponse(200, page([]))),
         ("GET", "/v1/ad_account", FakeResponse(200, {"id": "acct_1", "name": "Shop"}))])
    out = openai_ads.summary(START, END)
    assert out["spend"] == 0.0 and out["no_activity"] is True
    assert out["currency"] is None and "never guessed" in out["currency_note"] and "currency_error" not in out


@pytest.mark.parametrize("failure", [FakeResponse(500, {"error": {"message": "down"}}),
                                     FakeResponse(429, {"error": {"message": "slow"}}, {"Retry-After": "4"})])
def test_summary_account_lookup_failure_only_adds_a_note(failure):
    use([("/ad_account/insights", FakeResponse(200, page([]))), ("GET", "/v1/ad_account", failure)])
    out = openai_ads.summary(START, END)
    assert "error" not in out and out["spend"] == 0.0 and out["currency"] is None
    assert out["currency_error"]["error"] in ("api_error", "rate_limited") and "lookup failed" in out["currency_note"]


def test_summary_account_lookup_crash_only_adds_a_note(monkeypatch):
    use([("/ad_account/insights", FakeResponse(200, page([])))])
    real_call = openai_ads.CLIENT.call

    def call(method, url, **kwargs):
        if url.endswith("/ad_account"):
            raise RuntimeError("boom")
        return real_call(method, url, **kwargs)

    monkeypatch.setattr(openai_ads.CLIENT, "call", call)
    out = openai_ads.summary(START, END)
    assert "error" not in out and out["currency"] is None and out["currency_error"]["error"] == "internal_error"


def test_summary_mixed_currencies_are_not_resolved_by_the_account():
    rows = [dict(ROW), dict(ROW, order_created_attributed_sales_currency="USD")]
    session = use([("/ad_account/insights", FakeResponse(200, page(rows)))])
    out = openai_ads.summary(START, END)
    assert out["currency"] is None and "several currencies" in out["currency_note"] and len(session.calls) == 1


def test_summary_with_null_spend_keeps_null():
    row = {k: v for k, v in NULL_ROW.items() if k != "order_created_attributed_sales_currency"}
    use([("/ad_account/insights", FakeResponse(200, page([dict(row, spend=None)])))])
    out = openai_ads.summary(START, END)
    assert out["currency"] is None and out["spend"] is None and out["roas"] is None


def test_summary_errors_are_data_and_it_never_raises(monkeypatch):
    use([("", FakeResponse(429, {"error": {"message": "slow"}}, {"Retry-After": "3"}))])
    assert openai_ads.summary(START, END)["error"] == "rate_limited"
    assert openai_ads.summary("bad", END)["error"] == "invalid_dates"

    def boom(*args, **kwargs):
        raise RuntimeError(f"unexpected with {KEY}")

    monkeypatch.setattr(openai_ads, "_insights", boom)
    out = openai_ads.summary(START, END)
    assert out["error"] == "internal_error" and KEY not in json.dumps(out)


def test_summary_in_ads_overview_adds_zero_spend(monkeypatch):
    from google_ecommerce_mcp import ads_overview

    use([("/ad_account/insights", FakeResponse(200, page([]))),
         ("GET", "/v1/ad_account", FakeResponse(200, {"currency": "EUR"}))])
    block = ads_overview._block("openai_ads", openai_ads.summary(START, END))
    assert block["status"] == "ok" and block["spend"] == 0.0 and block["currency"] == "EUR"


# ------------------------------------------------------------------ registration
def test_register_declares_read_only_documented_tools():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test-openai-ads")
    names = openai_ads.register(mcp)
    assert names == ["openai_ads_account", "openai_ads_insights", "openai_ads_conversions", "openai_ads_campaigns",
                     "openai_ads_ad_groups", "openai_ads_ads"]
    tools = asyncio.run(mcp.list_tools())
    assert len(tools) == 6
    for tool in tools:
        assert tool.title and tool.annotations.readOnlyHint is True and tool.annotations.destructiveHint is False
        assert len(tool.description) > 60
        assert all(p.get("description") for p in tool.inputSchema.get("properties", {}).values())
    by_name = {t.name: t for t in tools}
    assert set(by_name["openai_ads_insights"].inputSchema["properties"]) == {
        "start_date", "end_date", "level", "entity_id", "timezone", "time_granularity", "aggregation_level",
        "fields", "segment", "filters", "sort", "include_zero_impression_items", "attribution_window_days",
        "view_through_attribution_window_days", "attribution_time_basis", "page_size", "max_rows", "after",
        "before", "client_id"}
    for tool in tools:  # client_id everywhere, optional
        assert "client_id" in tool.inputSchema["properties"], tool.name
        assert "client_id" not in tool.inputSchema.get("required", []), tool.name
    assert by_name["openai_ads_ad_groups"].inputSchema["required"] == ["campaign_id"]
    assert by_name["openai_ads_ads"].inputSchema["required"] == ["ad_group_id"]
    conversions = by_name["openai_ads_conversions"].inputSchema
    assert "filters" not in conversions["properties"] and "group_by_entity" in conversions["properties"]
    assert conversions["required"] == ["start_date", "end_date", "account_timezone", "aggregation_level"]


def test_registered_tool_runs_through_mcp():
    from mcp.server.fastmcp import FastMCP

    use([("/campaigns/cmpn_1/insights", FakeResponse(200, page([dict(ROW)])))])
    mcp = FastMCP("test-openai-ads")
    openai_ads.register(mcp)
    result = asyncio.run(mcp.call_tool("openai_ads_insights", {
        "start_date": START, "end_date": END, "level": "campaign", "entity_id": "cmpn_1",
        "attribution_window_days": 7,
        "filters": [{"field": "campaign.id", "operator": "IN", "value": ["cmpn_1"]}]}))
    assert "order_created_attributed_sales" in str(result) and KEY not in str(result)


# ------------------------------------------------------------------ final wave: currency lookup, containment
def test_summary_rows_without_currency_take_the_account_currency_once():
    row = {k: v for k, v in ROW.items() if k != "order_created_attributed_sales_currency"}
    session = use([("/ad_account/insights", FakeResponse(200, page([row]))),
                   ("GET", "/v1/ad_account", FakeResponse(200, {"id": "acct_1", "currency": "EUR"}))])
    out = openai_ads.summary(START, END)
    assert out["spend"] == 12.5 and "no_activity" not in out
    assert out["currency"] == "EUR" and out["currency_source"] == "ad_account" and "currency_note" not in out
    openai_ads.summary(START, END)
    account_calls = [c for c in session.calls if c[1] == f"{BASE}/ad_account"]
    assert len(account_calls) == 1 and len(session.calls) == 3  # 2 insights reads, 1 cached account lookup


TOOL_CALLS = {
    "openai_ads_account": lambda: openai_ads.openai_ads_account(),
    "openai_ads_insights": lambda: openai_ads.openai_ads_insights(START, END),
    "openai_ads_conversions": lambda: openai_ads.openai_ads_conversions(START, END, **CONV_ARGS),
}


@pytest.mark.parametrize("name", sorted(TOOL_CALLS))
def test_unexpected_exception_is_a_masked_internal_error(name, monkeypatch):
    def explode(*args, **kwargs):
        raise OSError(f"Could not find a suitable TLS CA certificate bundle (Bearer {KEY}) {KEY}")

    monkeypatch.setattr(openai_ads.CLIENT, "call", explode)
    out = TOOL_CALLS[name]()
    assert out["error"] == "internal_error" and out["platform"] == "openai_ads" and out["fix"]
    assert "OSError" in out["detail"] and KEY not in json.dumps(out) and out["fetched_at"]


@pytest.mark.parametrize("name", sorted(TOOL_CALLS))
def test_invalid_input_raised_inside_a_tool_is_a_structured_error(name, monkeypatch):
    def refuse(*args, **kwargs):
        raise openai_ads.InvalidInput("invalid_argument", "refused before sending")

    monkeypatch.setattr(openai_ads.CLIENT, "call", refuse)
    out = TOOL_CALLS[name]()
    assert out == {"error": "invalid_argument", "platform": "openai_ads", "detail": "refused before sending",
                   "fetched_at": out["fetched_at"], "freshness": out["freshness"]}


def test_not_configured_still_reaches_guard_through_containment(monkeypatch):
    monkeypatch.delenv("OPENAI_ADS_API_KEY")
    for call in TOOL_CALLS.values():
        assert call()["error"] == "not_configured"


def test_contained_tools_keep_their_signature():
    assert "start_date" in inspect.signature(openai_ads.openai_ads_insights).parameters
    assert openai_ads.openai_ads_conversions.__name__ == "openai_ads_conversions"


@pytest.mark.parametrize("bad", [None, ["not", "a", "dict"], "text"])
def test_summary_non_dict_result_is_an_internal_error(bad, monkeypatch):
    monkeypatch.setattr(openai_ads, "_summary", lambda start, end, ctx=None: bad)
    out = openai_ads.summary(START, END)
    assert out["error"] == "internal_error" and type(bad).__name__ in out["detail"]


def test_summary_invalid_input_is_structured(monkeypatch):
    def refuse(start, end, ctx=None):
        raise openai_ads.InvalidInput("invalid_dates", "nope")

    monkeypatch.setattr(openai_ads, "_summary", refuse)
    assert openai_ads.summary(START, END)["error"] == "invalid_dates"


# ------------------------------------------------------------------ B3: currency_code (PLAN section 5 point 1)
def test_account_currency_code_is_kept_not_dropped():
    raw = {"id": "adacct_123", "name": "Acme LLC", "legal_name": "Acme LLC", "status": "active", "timezone": "UTC",
           "currency_code": "USD", "review": {"status": "approved"}}
    use([("GET", "/v1/ad_account", FakeResponse(200, raw))])
    out = openai_ads.openai_ads_account()
    assert out["ad_account"]["currency_code"] == "USD"
    assert "currency_code" not in out["raw_fields_dropped"] and out["raw_fields_dropped"] == ["legal_name", "review"]


def test_summary_takes_the_documented_currency_code_of_the_account():
    row = {k: v for k, v in ROW.items() if k != "order_created_attributed_sales_currency"}
    session = use([("/ad_account/insights", FakeResponse(200, page([row]))),
                   ("GET", "/v1/ad_account", FakeResponse(200, {"id": "adacct_1", "currency_code": "usd"}))])
    out = openai_ads.summary(START, END)
    assert out["currency"] == "USD" and out["currency_source"] == "ad_account" and "currency_note" not in out
    assert len(session.calls) == 2


def test_currency_code_wins_over_the_older_fields():
    assert openai_ads.account_currency_of({"currency_code": "EUR", "currency": "USD"}) == "EUR"
    assert openai_ads.account_currency_of({"currency": " gbp "}) == "GBP"
    assert openai_ads.account_currency_of({"currency_code": ""}) is None
    assert openai_ads.account_currency_of(None) is None


# ------------------------------------------------------------------ B3: lists (documented endpoints)
CAMPAIGN = {"id": "cmpn_101", "created_at": 1735689600, "status": "active", "bidding_type": "impressions",
            "budget": {"lifetime_spend_limit_micros": 25000000}, "conversion_event_setting_ids": [],
            "description": "Promote the bundle.", "end_time": 1738368000, "mode": None, "name": "Spring launch",
            "start_time": 1735689600, "targeting": {"locations": {"include": [{"id": "2000043"}]}},
            "updated_at": 1735776000}
AD_GROUP = {"id": "adgrp_301", "created_at": 1735689700, "updated_at": 1735776100, "name": "US English",
            "description": "Primary audience.", "context_hints": ["productivity"], "status": "active",
            "bidding_config": {"billing_event_type": "impression", "max_bid_micros": 60000}}
AD = {"id": "ad_501", "name": "Planner card", "created_at": 1735689800, "updated_at": 1735776200,
      "creative": {"type": "chat_card", "title": "Try the planner", "body": "Coordinate tasks.", "file_id": "file_901",
                   "image_url": "https://cdn.openai.com/ads/file_901.png", "target_url": "https://example.com/p"},
      "status": "active", "review_status": "approved"}


def lst(rows, has_more=False, last_id=None):
    return {"object": "list", "data": rows, "first_id": rows[0]["id"] if rows else None,
            "last_id": last_id or (rows[-1]["id"] if rows else None), "has_more": has_more}


def test_campaigns_list_keeps_documented_fields_and_drops_the_rest():
    session = use([("GET", "/v1/campaigns", FakeResponse(200, lst([dict(CAMPAIGN)])))])
    out = openai_ads.openai_ads_campaigns(limit=10, order="desc")
    assert out["rows"] == [{k: CAMPAIGN[k] for k in openai_ads.CAMPAIGN_FIELDS if k in CAMPAIGN}]
    assert set(out["raw_fields_dropped"]) == {"description", "targeting"}
    assert out["untrusted_text"]["fields"] == ["rows[].name"] and "micros" in out["units"]
    method, url, kwargs = session.calls[0]
    assert (method, url) == ("GET", f"{BASE}/campaigns")
    assert kwargs["params"] == {"order": "desc", "limit": 10}
    assert openai_ads.CLIENT.BLOCKED_ATTEMPTS == [] and out["complete"] is True


def test_campaigns_list_pages_with_after_cursor():
    def answer(kwargs):
        if kwargs["params"].get("after") is None:
            return FakeResponse(200, lst([dict(CAMPAIGN, id="cmpn_1")], has_more=True))
        return FakeResponse(200, lst([dict(CAMPAIGN, id="cmpn_2")]))

    session = use([("GET", "/v1/campaigns", answer)])
    out = openai_ads.openai_ads_campaigns(limit=600)
    assert [r["id"] for r in out["rows"]] == ["cmpn_1", "cmpn_2"] and out["pages_read"] == 2
    assert session.calls[0][2]["params"]["limit"] == 500  # documented maximum per page
    assert session.calls[1][2]["params"]["after"] == "cmpn_1"


def test_ad_groups_and_ads_lists_send_their_parent_id():
    session = use([("GET", "/v1/ad_groups", FakeResponse(200, lst([dict(AD_GROUP)]))),
                   ("GET", "/v1/ads", FakeResponse(200, lst([dict(AD)])))])
    groups = openai_ads.openai_ads_ad_groups("cmpn_101")
    ads = openai_ads.openai_ads_ads("adgrp_301")
    assert groups["rows"][0]["bidding_config"]["max_bid_micros"] == 60000 and groups["campaign_id"] == "cmpn_101"
    assert set(groups["raw_fields_dropped"]) == {"description", "context_hints"}
    assert ads["rows"][0]["creative"] == {k: AD["creative"][k] for k in ("type", "title", "body", "target_url",
                                                                          "file_id")}
    assert "image_url" in ads["raw_fields_dropped"] and ads["rows"][0]["review_status"] == "approved"
    assert "rows[].creative.body" in ads["untrusted_text"]["fields"]
    assert session.calls[0][2]["params"]["campaign_id"] == "cmpn_101"
    assert session.calls[1][2]["params"]["ad_group_id"] == "adgrp_301"
    for method, url, _ in session.calls:
        assert openai_ads.ALLOWED_ENDPOINTS.is_allowed(method, url)


@pytest.mark.parametrize("call, code", [
    (lambda: openai_ads.openai_ads_ad_groups(""), "invalid_entity_id"),
    (lambda: openai_ads.openai_ads_ads("../x"), "invalid_entity_id"),
    (lambda: openai_ads.openai_ads_campaigns(limit=0), "invalid_limit"),
    (lambda: openai_ads.openai_ads_campaigns(limit=True), "invalid_limit"),
    (lambda: openai_ads.openai_ads_campaigns(after="a", before="b"), "invalid_cursor"),
    (lambda: openai_ads.openai_ads_campaigns(order="random"), "invalid_order"),
])
def test_invalid_list_parameters_are_refused_without_network(call, code):
    session = use([("", catch_all)])
    assert call()["error"] == code and session.calls == []


def test_list_error_is_returned_as_is():
    use([("GET", "/v1/campaigns", FakeResponse(401, {"error": {"message": "bad key"}}))])
    assert openai_ads.openai_ads_campaigns()["error"] == "auth_failed"


# ------------------------------------------------------------------ B3: client_id (registry mode)
KEY_ACME = "sk-" + "acme" + "B" * 30
KEY_BETA = "sk-" + "beta" + "C" * 30


@pytest.fixture
def two_clients(tmp_path, monkeypatch):
    from clients_fakes import ACME, BETA, write_registry

    path = tmp_path / "clients.json"
    write_registry(path, {"acme": ACME, "beta": BETA})
    monkeypatch.setenv("GEM_CLIENTS_FILE", str(path))
    monkeypatch.setenv("GEM_SECRET_ACME_OPENAI", KEY_ACME)
    monkeypatch.setenv("GEM_SECRET_BETA_OPENAI", KEY_BETA)
    return path


def by_key(kwargs):
    key = kwargs["headers"]["Authorization"].split(" ", 1)[1]
    account = {KEY_ACME: "acct_acme", KEY_BETA: "acct_beta", KEY: "acct_env"}[key]
    return FakeResponse(200, {"id": account, "currency_code": "EUR"})


def test_each_client_reads_with_its_own_key_only(two_clients):
    session = use([("GET", "/v1/ad_account", by_key), ("GET", "/v1/campaigns", FakeResponse(200, lst([])))])
    acme = openai_ads.openai_ads_account(client_id="acme")
    beta = openai_ads.openai_ads_account(client_id="beta")
    listed = openai_ads.openai_ads_campaigns(client_id="beta")
    assert acme["ad_account"]["id"] == "acct_acme" and acme["client_id"] == "acme"
    assert beta["ad_account"]["id"] == "acct_beta" and beta["client_id"] == "beta"
    assert listed["client_id"] == "beta"
    keys = [c[2]["headers"]["Authorization"] for c in session.calls]
    assert keys == [f"Bearer {KEY_ACME}", f"Bearer {KEY_BETA}", f"Bearer {KEY_BETA}"]
    assert KEY not in json.dumps([c[2]["headers"] for c in session.calls])  # never the environment key


def test_client_id_is_required_with_two_clients_and_unknown_is_refused(two_clients):
    session = use([("", catch_all)])
    assert openai_ads.openai_ads_account()["error"] == "client_required"
    assert openai_ads.openai_ads_campaigns(client_id="nobody")["error"] == "unknown_client"
    assert openai_ads.openai_ads_insights(START, END, client_id="nobody")["error"] == "unknown_client"
    assert openai_ads.summary(START, END)["error"] == "client_required"
    assert session.calls == []


def test_key_answering_for_another_account_is_refused(two_clients, monkeypatch):
    monkeypatch.setenv("GEM_SECRET_ACME_OPENAI", KEY_BETA)  # acme configured with the key of beta by mistake
    use([("GET", "/v1/ad_account", by_key)])
    out = openai_ads.openai_ads_account(client_id="acme")
    assert out["error"] == "account_mismatch" and out["client_id"] == "acme" and "acct_beta" not in json.dumps(out)


def test_missing_client_key_is_not_configured(two_clients, monkeypatch):
    monkeypatch.delenv("GEM_SECRET_BETA_OPENAI")
    session = use([("", catch_all)])
    out = openai_ads.openai_ads_campaigns(client_id="beta")
    assert out["error"] == "not_configured" and "beta" in out["detail"] and session.calls == []


def test_pinned_process_refuses_another_client(two_clients, monkeypatch):
    monkeypatch.setenv("GEM_CLIENT", "acme")
    use([("", catch_all)])
    assert openai_ads.openai_ads_account(client_id="beta")["error"] == "pinned_client"

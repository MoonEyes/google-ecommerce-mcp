"""meta_eco read tools: pixel health, dataset quality, catalog diagnostics / products, Instagram and Page insights.
Offline: fake Graph session, fake keyring, two registry clients (acme, beta); no network, no token in any URL."""

from __future__ import annotations

import asyncio
import datetime

import pytest

from ads_fakes import META_TOKEN_PREFIX, FakeResponse, FakeSession, no_network  # noqa: F401  (autouse fixture)
from clients_fakes import ACME, BETA, setup_env, write_registry
from google_ecommerce_mcp import meta_eco

TOKENS = {"acme": META_TOKEN_PREFIX + "acmeTOKENabcdefghijkl", "beta": META_TOKEN_PREFIX + "betaTOKENabcdefghijkl"}
TODAY = datetime.date(2026, 10, 18)


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    _, path = setup_env(monkeypatch, tmp_path)
    write_registry(path, {"acme": ACME, "beta": BETA})
    monkeypatch.setenv("GEM_SECRET_ACME_META", TOKENS["acme"])
    monkeypatch.setenv("GEM_SECRET_BETA_META", TOKENS["beta"])
    monkeypatch.setattr(meta_eco, "_today", lambda: TODAY)
    return path


def fake(monkeypatch, *rules):
    session = FakeSession(list(rules))
    monkeypatch.setattr(meta_eco.CLIENT, "session", session)
    return session


def assert_token_only_in_header(session, client="acme"):
    assert session.calls, "no request was sent"
    for method, url, kwargs in session.calls:
        assert method == "GET"
        for token in TOKENS.values():
            assert token not in url and token not in str(kwargs.get("params"))
        assert "access_token" not in str(kwargs.get("params")) and "access_token" not in url
        assert kwargs["headers"] == {"Authorization": f"Bearer {TOKENS[client]}"}
        assert url.startswith("https://graph.facebook.com/v26.0/")


# ------------------------------------------------------------------ allow-list
def test_read_allow_list_is_get_only_pinned_and_without_joker():
    assert meta_eco.ALLOW_LIST.hosts == {"graph.facebook.com"}
    for method, pattern in meta_eco.ALLOW_LIST.patterns:
        assert method == "GET"
        assert pattern.startswith(r"https://graph\.facebook\.com/v26\.0/")
        assert ".*" not in pattern and ".+" not in pattern
    assert not meta_eco.ALLOW_LIST.is_allowed("POST", "https://graph.facebook.com/v26.0/1113")
    assert not meta_eco.ALLOW_LIST.is_allowed("GET", "https://graph.facebook.com/v25.0/1113")
    assert not meta_eco.ALLOW_LIST.is_allowed("GET", "https://evil.example/v26.0/1113")
    assert not meta_eco.ALLOW_LIST.is_allowed("GET", "https://graph.facebook.com/v26.0/1113/items_batch")


def test_write_and_token_in_url_are_blocked_without_network(monkeypatch):
    session = fake(monkeypatch)
    out = meta_eco.CLIENT.call("POST", "https://graph.facebook.com/v26.0/1113", json={"price": 1})
    assert out["error"] == "blocked"
    out = meta_eco.CLIENT.call("GET", "https://graph.facebook.com/v26.0/1112", params={"access_token": "x" * 10})
    assert out["error"] == "blocked"
    assert session.calls == []


# ------------------------------------------------------------------ pixel
def pixel_rules(last_fired, events):
    stats = {"data": [{"start_time": "2026-10-17T00:00:00+0000", "aggregation": "event",
                       "data": [{"value": name, "count": count} for name, count in events.items()]}]}
    return [("/1112/stats", FakeResponse(200, stats)),
            ("/1112", FakeResponse(200, {"id": "1112", "name": "Acme pixel", "last_fired_time": last_fired,
                                         "is_unavailable": False}))]


def test_pixel_health_healthy(monkeypatch):
    now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
    session = fake(monkeypatch, *pixel_rules(now.strftime("%Y-%m-%dT%H:%M:%S+0000"),
                                             {"PageView": 900, "Purchase": 12}))
    out = meta_eco.meta_pixel_health(client_id="acme", days=7)
    assert out["client_id"] == "acme" and out["pixel_id"] == "1112"
    assert out["healthy"] is True and out["warnings"] == []
    assert out["events"] == {"PageView": 900, "Purchase": 12}
    assert 0.5 <= out["hours_since_last_fire"] <= 1.5
    assert "untrusted_text" in out and out["fetched_at"]
    stats_call = session.calls[1][2]["params"]
    assert stats_call["aggregation"] == "event" and stats_call["end_time"] - stats_call["start_time"] == 7 * 86400
    assert_token_only_in_header(session)


def test_pixel_health_warnings(monkeypatch):
    fake(monkeypatch, *pixel_rules("2026-01-01T00:00:00+0000", {"PageView": 3}))
    out = meta_eco.meta_pixel_health(client_id="acme")
    assert out["healthy"] is False
    assert any("no event for" in w for w in out["warnings"])
    assert any("no Purchase" in w for w in out["warnings"])


def test_pixel_of_another_client_is_refused_without_request(monkeypatch):
    session = fake(monkeypatch)
    out = meta_eco.meta_pixel_health(client_id="acme", pixel_id="2222")  # beta's pixel
    assert out["error"] == "id_not_owned" and "2222" not in out["detail"]
    out = meta_eco.meta_pixel_health(client_id="acme", pixel_id="1113")  # acme's catalog, not its pixel
    assert out["error"] == "id_not_owned"
    assert meta_eco.meta_pixel_health(client_id="acme", pixel_id="12a")["error"] == "invalid_argument"
    assert session.calls == []


def test_two_clients_each_use_their_own_ids_and_token(monkeypatch):
    rules = pixel_rules("2026-10-18T00:00:00+0000", {"Purchase": 1})
    beta_rules = [("/2222/stats", rules[0][1]), ("/2222", FakeResponse(200, {"id": "2222"}))]
    session = fake(monkeypatch, *rules, *beta_rules)
    meta_eco.meta_pixel_health(client_id="acme")
    acme_calls = list(session.calls)
    session.calls.clear()
    out = meta_eco.meta_pixel_health(client_id="beta")
    assert out["client_id"] == "beta" and out["pixel_id"] == "2222"
    assert all("/2222" in url for _, url, _ in session.calls)
    assert all("/1112" in url for _, url, _ in acme_calls)
    assert_token_only_in_header(session, "beta")


def test_client_required_with_two_clients(monkeypatch):
    session = fake(monkeypatch)
    out = meta_eco.meta_pixel_health()
    assert out["error"] == "client_required" and session.calls == []
    assert meta_eco.meta_pixel_health(client_id="nobody")["error"] == "unknown_client"


def test_missing_token_is_not_configured(monkeypatch):
    monkeypatch.delenv("GEM_SECRET_ACME_META")
    session = fake(monkeypatch)
    out = meta_eco.meta_pixel_health(client_id="acme")
    assert out["error"] == "not_configured" and session.calls == []


def test_error_echoing_the_token_never_shows_it(monkeypatch):
    fake(monkeypatch, ("/1112", FakeResponse(400, {"error": {"code": 190, "message": f"bad {TOKENS['acme']}"}})))
    out = meta_eco.meta_pixel_health(client_id="acme")
    assert out["error"] == "auth_failed" and TOKENS["acme"] not in str(out)
    assert out["required_permissions"] and out["client_id"] == "acme"


# ------------------------------------------------------------------ dataset quality
def test_dataset_quality_and_weak_events(monkeypatch):
    body = {"web": [{"event_name": "Purchase", "event_match_quality": {"composite_score": 4.2},
                     "dedupe_key_feedback": [{"x": 1}], "data_freshness": {"upload_frequency": "real_time"}},
                    {"event_name": "PageView", "event_match_quality": {"composite_score": 7.5}}]}
    session = fake(monkeypatch, ("/dataset_quality", FakeResponse(200, body)))
    out = meta_eco.meta_dataset_quality(client_id="acme")
    assert out["count"] == 2 and out["weak_match_quality"] == ["Purchase"]
    assert out["events"][0]["dedup_feedback"] == [{"x": 1}]
    params = session.calls[0][2]["params"]
    assert params["dataset_id"] == "1112" and "dedupe_key_feedback" in params["fields"]
    assert_token_only_in_header(session)


def test_dataset_quality_retries_once_with_minimal_fields(monkeypatch):
    answers = [FakeResponse(400, {"error": {"code": 100, "message": "Tried accessing nonexisting field"}}),
               FakeResponse(200, {"web": [{"event_name": "Purchase"}]})]
    session = fake(monkeypatch, ("/dataset_quality", lambda kwargs: answers.pop(0)))
    out = meta_eco.meta_dataset_quality(client_id="acme")
    assert out["fields_used"] == meta_eco.DATASET_FIELDS_MINIMAL and out["count"] == 1
    assert len(session.calls) == 2


# ------------------------------------------------------------------ catalog
def test_catalog_diagnostics_sorted_and_cleaned(monkeypatch):
    body = {"data": [{"type": "ATTRIBUTES_MISSING", "diagnostics": [
        {"severity": "WARNING", "title": "Missing brand", "number_of_affected_items": 3},
        {"severity": "MUST_FIX", "title": "Bad‮ title\x1b[31m", "number_of_affected_items": 9}]}]}
    session = fake(monkeypatch, ("/1113/diagnostics", FakeResponse(200, body)))
    out = meta_eco.meta_catalog_diagnostics(client_id="acme", types=("EVENT_SOURCE_ISSUES",))
    assert [i["severity"] for i in out["issues"]] == ["MUST_FIX", "WARNING"]
    assert "‮" not in out["issues"][0]["title"] and "\x1b" not in out["issues"][0]["title"]
    assert out["by_severity"] == {"WARNING": 1, "MUST_FIX": 1}
    assert session.calls[0][2]["params"]["types"] == '["EVENT_SOURCE_ISSUES"]'
    assert meta_eco.meta_catalog_diagnostics(client_id="acme", types=("NOPE",))["error"] == "invalid_argument"


def test_catalog_products_bounded_cursor_pagination(monkeypatch):
    def page(kwargs):
        after = int(kwargs["params"].get("after", "0"))
        items = [{"id": str(9000 + after + i), "retailer_id": f"SKU{after + i}", "price": "12,00 €"}
                 for i in range(kwargs["params"]["limit"])]
        return FakeResponse(200, {"data": items, "paging": {"cursors": {"after": str(after + len(items))},
                                                            "next": "https://graph.facebook.com/next?x=1"}})

    session = fake(monkeypatch, ("/1113/products", page))
    out = meta_eco.meta_catalog_products(client_id="acme", limit=150)
    assert out["count"] == 150 and out["complete"] is False and out["pages_read"] == 2
    assert all("next" not in url for _, url, _ in session.calls)
    assert_token_only_in_header(session)
    session.calls.clear()
    out = meta_eco.meta_catalog_products(client_id="acme", retailer_id="SKU1", limit=1)
    assert session.calls[0][2]["params"]["filter"] == '{"retailer_id":{"eq":"SKU1"}}'
    assert meta_eco.meta_catalog_products(client_id="acme", catalog_id="2223")["error"] == "id_not_owned"


# ------------------------------------------------------------------ Instagram / Page
def test_ig_insights_defaults_have_no_deprecated_metric(monkeypatch):
    body = {"data": [{"name": "reach", "total_value": {"value": 120}}, {"name": "views", "total_value": {"value": 900}}]}
    session = fake(monkeypatch, ("/1115/insights", FakeResponse(200, body)))
    out = meta_eco.meta_ig_insights(client_id="acme")
    assert out["metrics"]["reach"] == 120 and out["metrics"]["accounts_engaged"] is None
    sent = session.calls[0][2]["params"]["metric"].split(",")
    assert not set(sent) & meta_eco.IG_DEPRECATED
    assert out["date_range"] == {"start": "2026-09-20", "end": "2026-10-17", "timezone": "UTC", "days": 28}
    assert_token_only_in_header(session)


@pytest.mark.parametrize("metric", sorted(meta_eco.IG_DEPRECATED))
def test_ig_deprecated_metrics_are_refused(monkeypatch, metric):
    session = fake(monkeypatch)
    out = meta_eco.meta_ig_insights(client_id="acme", metrics=(metric,))
    assert out["error"] == "metric_deprecated" and session.calls == []


def test_no_deprecated_metric_is_offered():
    assert not set(meta_eco.IG_METRICS) & meta_eco.IG_DEPRECATED
    assert not [m for m in meta_eco.PAGE_METRICS if "impressions" in m or m.startswith("page_fan")]


def test_ig_period_limits(monkeypatch):
    session = fake(monkeypatch)
    out = meta_eco.meta_ig_insights(client_id="acme", start_date="2026-08-01", end_date="2026-09-15")
    assert out["error"] == "period_too_long"
    out = meta_eco.meta_ig_insights(client_id="acme", start_date="2026-06-01", end_date="2026-06-10")
    assert out["error"] == "period_too_old"
    assert meta_eco.meta_ig_insights(client_id="acme", start_date="2026-10-01")["error"] == "invalid_argument"
    assert session.calls == []


def test_page_insights_totals_and_limits(monkeypatch):
    body = {"data": [{"name": "page_media_view", "values": [{"value": 10, "end_time": "a"},
                                                            {"value": 5, "end_time": "b"}]}]}
    session = fake(monkeypatch, ("/1114/insights", FakeResponse(200, body)))
    out = meta_eco.meta_page_insights(client_id="acme", start_date="2026-09-01", end_date="2026-09-30")
    assert out["totals"]["page_media_view"] == 15 and len(out["daily"]["page_media_view"]) == 2
    params = session.calls[0][2]["params"]
    assert params["until"] - params["since"] == 30 * 86400
    assert "impressions" not in params["metric"]
    assert_token_only_in_header(session)
    session.calls.clear()
    out = meta_eco.meta_page_insights(client_id="acme", start_date="2026-06-01", end_date="2026-09-30")
    assert out["error"] == "period_too_long"
    assert meta_eco.meta_page_insights(client_id="acme", metrics=("page_impressions",))["error"] == \
        "metric_deprecated"
    assert session.calls == []


# ------------------------------------------------------------------ MCP registration
def test_register_declares_read_only_tools_with_client_id():
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("t")
    names = meta_eco.register(server)
    assert names == ["meta_pixel_health", "meta_dataset_quality", "meta_catalog_diagnostics",
                     "meta_catalog_products", "meta_ig_insights", "meta_page_insights"]
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    for name in names:
        tool = tools[name]
        assert tool.annotations.readOnlyHint is True and tool.annotations.destructiveHint is False
        assert "client_id" in tool.inputSchema["properties"]
        assert "client_id" not in tool.inputSchema.get("required", [])
        assert tool.title and tool.description

"""ga4_key_event_audit on a fake Data API: two runReport calls (events, then totals), findings for key events that
inflate conversions, partial result when the second call fails, client ownership, allow-list. Offline."""

from __future__ import annotations

import asyncio
import dataclasses

from google_ecommerce_mcp import ga4_audit
from test_server import CLIENTS, FakeResponse, _registry, server, use  # noqa: F401


def events_report(rows, tz="Europe/Paris", row_count=None) -> dict:
    return {"dimensionHeaders": [{"name": "eventName"}],
            "metricHeaders": [{"name": "eventCount", "type": "TYPE_INTEGER"}, {"name": "keyEvents", "type": "TYPE_INTEGER"}],
            "rows": [{"dimensionValues": [{"value": n}], "metricValues": [{"value": str(c)}, {"value": str(k)}]}
                     for n, c, k in rows],
            "rowCount": row_count if row_count is not None else len(rows),
            "metadata": {"timeZone": tz}}


def totals_report(sessions, key_events, purchases) -> dict:
    return {"metricHeaders": [{"name": "sessions", "type": "TYPE_INTEGER"}, {"name": "keyEvents", "type": "TYPE_INTEGER"},
                              {"name": "ecommercePurchases", "type": "TYPE_INTEGER"}],
            "rows": [{"metricValues": [{"value": str(sessions)}, {"value": str(key_events)}, {"value": str(purchases)}]}],
            "rowCount": 1, "metadata": {"timeZone": "Europe/Paris"}}


def routed(events, totals):
    """Answers request A (it has an eventName dimension) and request B by the body of the call."""
    return lambda kwargs: FakeResponse(200, events if kwargs["json"].get("dimensions") else totals)


INFLATED = events_report([("page_view", 500, 500), ("phone_click", 40, 40), ("purchase", 12, 12), ("scroll", 90, 0)])
INFLATED_TOTALS = totals_report(201, 552, 12)


def codes(out):
    return [(f["code"], f["event_name"]) for f in out["findings"]]


def test_inflated_property_gets_the_three_findings(server, monkeypatch):
    use(server, monkeypatch, [(":runReport", routed(INFLATED, INFLATED_TOTALS))])
    out = server.ga4_key_event_audit(start_date="2026-09-01", end_date="2026-09-30")
    assert ("key_events_exceed_sessions", None) in codes(out)
    assert ("engagement_event_marked_key", "page_view") in codes(out)
    assert ("contact_click_marked_key", "phone_click") in codes(out)
    assert not any(c == "purchase_not_key_event" for c, _ in codes(out))
    assert out["key_events_per_session"] == 2.75
    assert out["totals"] == {"sessions": 201, "key_events": 552, "ecommerce_purchases": 12}
    # rows with keyEvents > 0 only, biggest first, share of all key events with one decimal
    assert [e["event_name"] for e in out["key_events"]] == ["page_view", "phone_click", "purchase"]
    assert out["key_events"][0] == {"event_name": "page_view", "event_count": 500, "key_events": 500,
                                    "share_pct": 90.6, "flags": ["engagement_event_marked_key"]}
    assert out["key_events"][1]["flags"] == ["contact_click_marked_key"] and out["key_events"][2]["flags"] == []
    severities = {f["code"]: f["severity"] for f in out["findings"]}
    assert severities == {"key_events_exceed_sessions": "high", "engagement_event_marked_key": "high",
                          "contact_click_marked_key": "medium"}
    assert out["rule_note"].startswith("heuristics, check in GA4 Admin")


def test_both_requests_are_allowed_runreport_posts_with_the_documented_body(server, monkeypatch):
    session = use(server, monkeypatch, [(":runReport", routed(INFLATED, INFLATED_TOTALS))])
    server.BLOCKED_ATTEMPTS.clear()
    server.ga4_key_event_audit()
    assert server.BLOCKED_ATTEMPTS == []
    assert len(session.calls) == 2
    for method, url, _ in session.calls:
        assert method == "POST" and url.endswith("/properties/123:runReport") and server.is_allowed(method, url)
    first, second = session.calls[0][2]["json"], session.calls[1][2]["json"]
    assert first["dimensions"] == [{"name": "eventName"}]
    assert [m["name"] for m in first["metrics"]] == ["eventCount", "keyEvents"]
    assert first["limit"] == 250 and first["metricAggregations"] == ["TOTAL"]
    assert first["orderBys"] == [{"metric": {"metricName": "keyEvents"}, "desc": True}]
    assert "dimensions" not in second
    assert [m["name"] for m in second["metrics"]] == ["sessions", "keyEvents", "ecommercePurchases"]
    assert first["dateRanges"] == [{"startDate": "28daysAgo", "endDate": "yesterday"}]


def test_purchase_not_key_event_and_no_key_events(server, monkeypatch):
    use(server, monkeypatch, [(":runReport", routed(events_report([("purchase", 5, 0), ("page_view", 100, 0)]),
                                                    totals_report(80, 3, 5)))])
    out = server.ga4_key_event_audit()
    assert ("purchase_not_key_event", "purchase") in codes(out)
    assert out["key_events"] == []
    use(server, monkeypatch, [(":runReport", routed(events_report([("page_view", 100, 0)]), totals_report(80, 0, 0)))])
    out = server.ga4_key_event_audit()
    assert codes(out) == [("no_key_events", None)]
    assert out["key_events_per_session"] == 0.0
    use(server, monkeypatch, [(":runReport", routed(events_report([]), totals_report(0, 0, 0)))])
    out = server.ga4_key_event_audit()
    assert out["key_events_per_session"] is None and codes(out) == [("no_key_events", None)]


def test_a_healthy_property_has_no_finding(server, monkeypatch):
    use(server, monkeypatch, [(":runReport", routed(events_report([("purchase", 12, 12), ("generate_lead", 30, 30)]),
                                                    totals_report(900, 42, 12)))])
    out = server.ga4_key_event_audit()
    assert out["findings"] == [] and out["key_events_per_session"] == 0.05


def test_result_carries_freshness_metrics_and_date_range(server, monkeypatch):
    use(server, monkeypatch, [(":runReport", routed(INFLATED, INFLATED_TOTALS))])
    out = server.ga4_key_event_audit(start_date="2026-09-01", end_date="2026-09-30")
    assert out["fetched_at"] and out["freshness"] and out["property_id"] == "123"
    assert set(out["metrics"]) == {"eventCount", "keyEvents", "sessions", "ecommercePurchases"}
    assert out["metrics"]["keyEvents"]["unit"] and out["metrics"]["keyEvents"]["definition"]
    assert out["date_range"]["data_complete"] is True and out["data_complete"] is True
    assert out["date_range"]["timezone"] == "Europe/Paris"
    recent = server.ga4_key_event_audit()  # ends yesterday: GA4 can still revise it
    assert recent["date_range"]["data_complete"] is False and recent["date_range"]["settling_note"]


def test_second_call_failing_keeps_the_events(server, monkeypatch):
    error = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota"}}
    use(server, monkeypatch, [(":runReport", lambda kwargs: FakeResponse(200, INFLATED) if kwargs["json"].get("dimensions")
                               else FakeResponse(429, error))])
    out = server.ga4_key_event_audit()
    assert out["partial"] is True and out["partial_error"]["step"] == "totals"
    assert out["partial_error"]["error"] == "rate_limited"
    assert out["totals"] == {"sessions": None, "key_events": 552, "ecommerce_purchases": None}
    assert out["key_events_per_session"] is None
    assert ("engagement_event_marked_key", "page_view") in codes(out)
    assert not any(c in ("key_events_exceed_sessions", "purchase_not_key_event") for c, _ in codes(out))


def test_first_call_failing_is_a_plain_error(server, monkeypatch):
    error = {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "Request had insufficient authentication scopes.",
                       "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}}
    session = use(server, monkeypatch, [(":runReport", FakeResponse(403, error))])
    out = server.ga4_key_event_audit()
    assert out["error"] == "missing_scope" and "partial" not in out
    assert len(session.calls) == 1


def test_bad_property_and_missing_configuration(server, monkeypatch):
    session = use(server, monkeypatch, [("", FakeResponse(200, {}))])
    assert server.ga4_key_event_audit(property_id="12/../x")["error"] == "invalid_property_id"
    assert session.calls == []
    monkeypatch.setattr(server, "SETTINGS", dataclasses.replace(server.SETTINGS, ga4_property_id=None))
    assert server.ga4_key_event_audit()["error"] == "not_configured"


def test_foreign_property_is_refused_without_a_request(server, monkeypatch, tmp_path):
    _registry(tmp_path, monkeypatch)
    session = use(server, monkeypatch, [("", FakeResponse(200, {}))])
    out = server.ga4_key_event_audit(property_id=CLIENTS["beta"]["ga4"], client_id="acme")
    assert out["error"] == "id_not_owned" and out["client_id"] == "acme"
    assert server.ga4_key_event_audit()["error"] == "client_required"
    assert server.ga4_key_event_audit(client_id="nobody")["error"] == "unknown_client"
    assert session.calls == []


def test_a_client_is_audited_on_its_own_property_and_token(server, monkeypatch, tmp_path):
    _registry(tmp_path, monkeypatch)
    session = use(server, monkeypatch, [(":runReport", routed(INFLATED, INFLATED_TOTALS))])
    out = server.ga4_key_event_audit(client_id="beta")
    assert out["client_id"] == "beta" and out["property_id"] == CLIENTS["beta"]["ga4"]
    for _, url, kwargs in session.calls:
        assert f"/properties/{CLIENTS['beta']['ga4']}:runReport" in url
        assert kwargs["headers"]["Authorization"] == f"Bearer {CLIENTS['beta']['token']}"


def test_pure_audit_and_rules():
    out = ga4_audit.audit([{"event_name": "whatsapp_chat", "event_count": 3, "key_events": 3},
                           {"event_name": "hotel_booking", "event_count": 2, "key_events": 2},
                           {"event_name": "session_start", "event_count": 9, "key_events": 9}],
                          {"sessions": 100, "key_events": 14, "ecommerce_purchases": 0})
    assert [(e["event_name"], e["flags"]) for e in out["key_events"]] == [
        ("session_start", ["engagement_event_marked_key"]), ("whatsapp_chat", ["contact_click_marked_key"]),
        ("hotel_booking", ["contact_click_marked_key"])]  # "tel" in hotel: the heuristic is documented as loose
    assert "key_events_exceed_sessions" not in [f["code"] for f in out["findings"]]


def test_the_tool_is_registered_read_only_with_its_parameters(server):
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    tool = tools["ga4_key_event_audit"]
    assert tool.title == "GA4 key event audit"
    assert tool.annotations.readOnlyHint is True and tool.annotations.destructiveHint is False
    props = tool.inputSchema["properties"]
    assert set(props) == {"start_date", "end_date", "property_id", "client_id"}
    assert props["start_date"]["default"] == "28daysAgo" and props["end_date"]["default"] == "yesterday"
    assert "client_id" not in tool.inputSchema.get("required", [])

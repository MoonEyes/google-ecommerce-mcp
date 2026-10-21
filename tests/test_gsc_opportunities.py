"""gsc_opportunities: Search Console striking-distance and no-click rows. Offline: the Search Analytics endpoint is
a fake session (same pattern as test_gsc_moves.py), no network, no credentials."""

import datetime

from google_ecommerce_mcp import gsc_opportunities as opp
from test_server import CLIENTS, FakeResponse, _registry, server, use  # noqa: F401  (server is a fixture)


def row(key, clicks, impressions, ctr, position):
    return {"keys": [key], "clicks": clicks, "impressions": impressions, "ctr": ctr, "position": position}


FOUR = [row("top", 50, 900, 0.0556, 3.0), row("mid", 10, 400, 0.025, 8.0),
        row("low", 0, 150, 0, 12.0), row("far", 0, 1000, 0, 20.0)]


def test_defaults_split_striking_distance_and_no_clicks():
    out = opp.find_opportunities(FOUR, "query", 100, 5.0, 15.0, 25)
    assert [r["query"] for r in out["striking_distance"]] == ["mid", "low"]  # 400 before 150
    assert [r["query"] for r in out["no_clicks"]] == ["far", "low"]         # 1000 before 150
    assert out["striking_count"] == 2 and out["no_clicks_count"] == 2 and out["rows_seen"] == 4


def test_min_impressions_and_limit_apply_and_counts_precede_the_limit():
    out = opp.find_opportunities(FOUR, "query", 200, 1.0, 100.0, 1)
    assert [r["query"] for r in out["striking_distance"]] == ["far"]
    assert out["striking_count"] == 3 and out["no_clicks_count"] == 1  # low (150) is under 200


def test_inverted_positions_are_refused_before_any_request(server, monkeypatch):
    session = use(server, monkeypatch, [("searchAnalytics", FakeResponse(200, {"rows": FOUR}))])
    result = server.gsc_opportunities(min_position=15, max_position=5)
    assert result["error"] == "invalid_range" and result["fix"] == "swap min_position and max_position"
    assert session.calls == []


def test_request_is_an_allowed_post_over_the_frozen_period(server, monkeypatch):
    monkeypatch.setattr(server.metrics, "today_in", lambda tz=None: (datetime.date(2026, 10, 21), tz or "UTC"))
    session = use(server, monkeypatch, [("searchAnalytics", FakeResponse(200, {"rows": FOUR}))])
    result = server.gsc_opportunities(days=28, dimension="page")
    assert len(session.calls) == 1
    method, url, kwargs = session.calls[0]
    assert method == "POST" and "searchAnalytics/query" in url and server.is_allowed(method, url)
    assert kwargs["json"] == {"startDate": "2026-09-22", "endDate": "2026-10-19", "dimensions": ["page"],
                              "rowLimit": 5000}
    assert result["striking_distance"][0]["page"] == "mid"
    assert result["date_range"]["start"] == "2026-09-22" and result["date_range"]["end"] == "2026-10-19"
    assert result["filters"]["dimension"] == "page" and "note" not in result  # the anonymized note is for queries


def test_ceiling_sets_truncated_and_values_are_rounded_like_gsc_performance(server, monkeypatch):
    rows = [row(f"k{i}", 1, 200, 0.123456, 9.04) for i in range(5000)]
    use(server, monkeypatch, [("searchAnalytics", FakeResponse(200, {"rows": rows}))])
    result = server.gsc_opportunities(limit=2)
    assert result["truncated"] is True and "ceiling" in result["truncated_note"]
    assert result["striking_count"] == 5000 and len(result["striking_distance"]) == 2
    assert result["striking_distance"][0] == {"query": "k0", "clicks": 1, "impressions": 200, "ctr": 0.1235,
                                              "position": 9.0}


def test_output_carries_metrics_freshness_and_the_anonymized_note(server, monkeypatch):
    use(server, monkeypatch, [("searchAnalytics", FakeResponse(200, {"rows": FOUR}))])
    result = server.gsc_opportunities()
    assert result["truncated"] is False and "truncated_note" not in result
    assert result["metrics"]["ctr"]["unit"] == "ratio 0 to 1"
    assert result["date_range"]["data_complete"] is True
    assert result["fetched_at"] and result["freshness"]
    assert "anonymized" in result["note"]


def test_an_api_error_is_returned_not_raised(server, monkeypatch):
    use(server, monkeypatch, [("searchAnalytics", FakeResponse(429, {"error": {"message": "quota"}}))])
    assert server.gsc_opportunities()["error"] == "rate_limited"


def test_a_foreign_client_site_is_refused_before_any_request(server, monkeypatch, tmp_path):
    _registry(tmp_path, monkeypatch)
    session = use(server, monkeypatch, [("", FakeResponse(200, {}))])
    result = server.gsc_opportunities(site_url=CLIENTS["beta"]["gsc"], client_id="acme")
    assert result["error"] == "id_not_owned" and result["client_id"] == "acme"
    assert session.calls == []

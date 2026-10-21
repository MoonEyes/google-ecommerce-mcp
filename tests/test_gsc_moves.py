"""gsc_moves: Search Console week-over-week losers and gainers. Offline: the Search Analytics endpoint is a fake
session (same pattern as test_server.py), no network, no credentials."""

import datetime

import pytest

from google_ecommerce_mcp import gsc_tools
from test_server import CLIENTS, FakeResponse, _registry, server, use  # noqa: F401  (server is a fixture)


def row(key, clicks, impressions, ctr, position):
    return {"keys": [key], "clicks": clicks, "impressions": impressions, "ctr": ctr, "position": position}


def two_periods(current_rows, previous_rows):
    """Answers the first searchAnalytics call with the current period and the second with the previous one."""
    answers = iter([current_rows, previous_rows])

    def answer(kwargs):
        return FakeResponse(200, {"rows": next(answers)})

    return answer


def test_periods_are_n_complete_days_then_the_n_before():
    cur, prev = gsc_tools.moves_periods(datetime.date(2026, 10, 21), 7)
    assert cur == ("2026-10-13", "2026-10-19")      # ends 2 days ago, 7 days
    assert prev == ("2026-10-06", "2026-10-12")     # the 7 days right before, no gap and no overlap


def test_loss_gain_and_change_are_joined_on_the_dimension(server, monkeypatch):
    current = [row("terrain", 5, 100, 0.05, 4.0), row("new query", 9, 300, 0.03, 6.04)]
    previous = [row("terrain", 12, 120, 0.1, 3.0), row("vanished", 20, 400, 0.05, 2.26)]
    session = use(server, monkeypatch, [("searchAnalytics", two_periods(current, previous))])
    result = server.gsc_moves()
    assert len(session.calls) == 2
    cur_body, prev_body = session.calls[0][2]["json"], session.calls[1][2]["json"]
    assert cur_body["dimensions"] == ["query"] and prev_body["dimensions"] == ["query"]
    assert result["date_range"]["current"]["start"] == cur_body["startDate"]
    assert result["date_range"]["previous"]["end"] == prev_body["endDate"]
    assert prev_body["endDate"] < cur_body["startDate"]

    gone = result["losers"][0]  # the biggest loser: a query that disappeared from period 2 is a full loss
    assert gone["query"] == "vanished" and gone["status"] == "lost" and gone["clicks_delta"] == -20
    assert gone["current"] == {"clicks": 0, "impressions": 0, "ctr": None, "position": None}
    assert gone["previous"] == {"clicks": 20, "impressions": 400, "ctr": 0.05, "position": 2.3}
    assert [m["query"] for m in result["losers"]] == ["vanished", "terrain"]
    new = result["gainers"][0]  # a query absent from the previous period is a full gain
    assert new["query"] == "new query" and new["status"] == "new" and new["clicks_delta"] == 9
    assert new["previous"]["position"] is None and new["current"]["position"] == 6.0
    assert result["gainers_count"] == 1 and result["losers_count"] == 2
    assert result["totals"]["current"]["clicks"] == 14 and result["totals"]["previous"]["clicks"] == 32
    assert result["totals"]["current"]["impressions"] == 400
    assert result["metrics"]["ctr"]["unit"] == "ratio 0 to 1"
    assert "incomplete" in result["date_range"]["note"]
    assert result["date_range"]["current"]["data_complete"] is True  # same convention as gsc_performance
    assert result["fetched_at"] and result["freshness"]
    assert "dataState" not in cur_body  # gsc_performance does not send it either


def test_limit_is_respected_and_ranking_is_by_clicks(server, monkeypatch):
    current = [row(f"q{i}", 0, 10, 0, 5) for i in range(1, 6)] + [row(f"g{i}", i, 10, 0.1, 5) for i in range(1, 6)]
    previous = [row(f"q{i}", i, 10, 0.1, 5) for i in range(1, 6)]
    use(server, monkeypatch, [("searchAnalytics", two_periods(current, previous))])
    result = server.gsc_moves(limit=2)
    assert [m["query"] for m in result["losers"]] == ["q5", "q4"]
    assert [m["query"] for m in result["gainers"]] == ["g5", "g4"]
    assert result["losers_count"] == 5 and result["gainers_count"] == 5


def test_page_dimension_and_days_are_sent(server, monkeypatch):
    session = use(server, monkeypatch, [("searchAnalytics", two_periods([row("https://a/", 3, 9, 0.3, 2)], []))])
    result = server.gsc_moves(days=28, dimension="page")
    body = session.calls[0][2]["json"]
    assert body["dimensions"] == ["page"]
    start = datetime.date.fromisoformat(body["startDate"])
    assert (datetime.date.fromisoformat(body["endDate"]) - start).days == 27
    assert result["gainers"][0]["page"] == "https://a/" and result["days"] == 28 and result["dimension"] == "page"


def test_an_api_error_is_returned_not_raised(server, monkeypatch):
    use(server, monkeypatch, [("searchAnalytics", FakeResponse(429, {"error": {"message": "quota"}}))])
    assert server.gsc_moves()["error"] == "rate_limited"


def test_ceiling_is_flagged_as_truncated(server, monkeypatch):
    rows = [row(f"k{i}", 1, 1, 1, 1) for i in range(gsc_tools.MOVES_FETCH_ROWS)]
    use(server, monkeypatch, [("searchAnalytics", two_periods(rows, []))])
    result = server.gsc_moves()
    assert result["truncated"] is True and "ceiling" in result["truncated_note"]


def test_a_foreign_client_site_is_refused_before_any_request(server, monkeypatch, tmp_path):
    _registry(tmp_path, monkeypatch)
    session = use(server, monkeypatch, [("", FakeResponse(200, {}))])
    result = server.gsc_moves(site_url=CLIENTS["beta"]["gsc"], client_id="acme")
    assert result["error"] == "id_not_owned" and result["client_id"] == "acme"
    assert session.calls == []


def test_own_site_uses_the_clients_token_and_allow_list(server, monkeypatch, tmp_path):
    _registry(tmp_path, monkeypatch)
    session = use(server, monkeypatch, [("searchAnalytics", two_periods([row("a", 1, 2, 0.5, 1)], []))])
    result = server.gsc_moves(client_id="acme")
    assert result["client_id"] == "acme" and "error" not in result
    for method, url, kwargs in session.calls:
        assert server.is_allowed(method, url)
        assert kwargs["headers"]["Authorization"] == f"Bearer {CLIENTS['acme']['token']}"
        assert "acme.fr" in url or "acme" in url.replace("%3A", ":")

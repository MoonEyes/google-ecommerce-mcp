"""ga4_compare on a fake Data API: one runReport with two named date ranges, paired rows, absolute and percent
change, totals, units, resolved date ranges, biggest moves, division by zero, refusal of a foreign client. Offline."""

from __future__ import annotations

import asyncio
import datetime

import pytest

from google_ecommerce_mcp import ga4_tools
from test_server import CLIENTS, FakeResponse, _registry, server, use  # noqa: F401

D = datetime.date
METRICS = (("sessions", "TYPE_INTEGER"), ("purchaseRevenue", "TYPE_CURRENCY"))


def report(current: dict, previous: dict, *, dims=("landingPage",), mets=METRICS, row_count=None, tz="Europe/Paris",
           totals=None) -> dict:
    """A Data API answer for a two-range request: GA4 appends the dateRange dimension, named like the ranges."""
    rows = []
    for side, data in (("current", current), ("previous", previous)):
        for key, values in data.items():
            keys = [key] if isinstance(key, str) else list(key)
            rows.append({"dimensionValues": [{"value": k} for k in keys] + [{"value": side}],
                         "metricValues": [{"value": str(v)} for v in values]})
    if totals is None:
        totals = {side: [sum(v[i] for v in data.values()) for i in range(len(mets))]
                  for side, data in (("current", current), ("previous", previous))}
    out = {"dimensionHeaders": [{"name": d} for d in dims] + [{"name": "dateRange"}],
           "metricHeaders": [{"name": n, "type": t} for n, t in mets], "rows": rows,
           "rowCount": row_count if row_count is not None else len(rows),
           "totals": [{"dimensionValues": [{"value": "RESERVED_TOTAL"}] * len(dims) + [{"value": side}],
                       "metricValues": [{"value": str(v)} for v in values]} for side, values in totals.items()],
           "metadata": {"currencyCode": "EUR", "timeZone": tz}}
    if not dims:
        out["dimensionHeaders"] = [{"name": "dateRange"}]
    return out


def answer(payload: dict):
    return lambda kwargs: FakeResponse(200, payload)


CUSTOM = "2026-09-01:2026-09-30"  # fixed dates: the previous period is 2026-08-02 to 2026-08-31


# ------------------------------------------------------------------ date arithmetic
def test_last_n_days_end_yesterday_and_previous_period_is_the_n_days_before():
    r = ga4_tools.compare_ranges("last_7_days", "previous_period", D(2026, 10, 21))
    assert r == {"current": (D(2026, 10, 14), D(2026, 10, 20)), "previous": (D(2026, 10, 7), D(2026, 10, 13))}
    r = ga4_tools.compare_ranges("last_28_days", "previous_year", D(2026, 10, 21))
    assert r["current"] == (D(2026, 9, 23), D(2026, 10, 20)) and r["previous"] == (D(2025, 9, 23), D(2025, 10, 20))


def test_this_month_is_month_to_date_against_the_same_number_of_days():
    r = ga4_tools.compare_ranges("this_month", "previous_period", D(2026, 10, 21))
    assert r == {"current": (D(2026, 10, 1), D(2026, 10, 20)), "previous": (D(2026, 9, 1), D(2026, 9, 20))}
    # a long month against a short one: the previous range stops at the end of February
    r = ga4_tools.compare_ranges("this_month", "previous_period", D(2026, 3, 31))
    assert r["current"] == (D(2026, 3, 1), D(2026, 3, 30)) and r["previous"] == (D(2026, 2, 1), D(2026, 2, 28))
    with pytest.raises(ValueError, match="no complete day"):
        ga4_tools.compare_ranges("this_month", "previous_period", D(2026, 10, 1))


def test_last_month_is_the_whole_month_against_the_month_before():
    assert ga4_tools.compare_ranges("last_month", "previous_period", D(2026, 10, 21)) == {
        "current": (D(2026, 9, 1), D(2026, 9, 30)), "previous": (D(2026, 8, 1), D(2026, 8, 31))}
    r = ga4_tools.compare_ranges("last_month", "previous_period", D(2026, 1, 5))  # across the new year
    assert r["current"] == (D(2025, 12, 1), D(2025, 12, 31)) and r["previous"] == (D(2025, 11, 1), D(2025, 11, 30))
    r = ga4_tools.compare_ranges("last_month", "previous_year", D(2028, 3, 5))  # 29 February of a leap year
    assert r["current"] == (D(2028, 2, 1), D(2028, 2, 29)) and r["previous"] == (D(2027, 2, 1), D(2027, 2, 28))


def test_custom_period_and_bad_inputs():
    r = ga4_tools.compare_ranges(CUSTOM, "previous_period", D(2026, 10, 21))
    assert r["previous"] == (D(2026, 8, 2), D(2026, 8, 31))
    for period, compare_to in [("last_week", "previous_period"), ("2026-09-30:2026-09-01", "previous_period"),
                               ("2026-02-31:2026-03-01", "previous_period"), ("last_7_days", "yesterday")]:
        with pytest.raises(ValueError):
            ga4_tools.compare_ranges(period, compare_to, D(2026, 10, 21))


# ------------------------------------------------------------------ the tool
def test_one_request_two_named_ranges_and_paired_rows(server, monkeypatch):
    payload = report({"/": [100, 500.0], "/shop": [40, 90.5], "/new": [5, 10]},
                     {"/": [80, 400.0], "/shop": [50, 100.0], "/old": [7, 20]})
    session = use(server, monkeypatch, [(":runReport", answer(payload))])
    out = server.ga4_compare(metrics=["sessions", "purchaseRevenue"], dimensions=["landingPage"], period=CUSTOM)
    assert len(session.calls) == 1
    method, url, kwargs = session.calls[0]
    assert method == "POST" and url.endswith("/properties/123:runReport") and server.is_allowed(method, url)
    body = kwargs["json"]
    assert body["dateRanges"] == [{"startDate": "2026-09-01", "endDate": "2026-09-30", "name": "current"},
                                  {"startDate": "2026-08-02", "endDate": "2026-08-31", "name": "previous"}]
    assert body["dimensions"] == [{"name": "landingPage"}] and body["metricAggregations"] == ["TOTAL"]
    assert body["orderBys"] == [{"metric": {"metricName": "sessions"}, "desc": True}]

    rows = {r["landingPage"]: r for r in out["rows"]}
    assert rows["/"]["metrics"]["sessions"] == {"current": 100, "previous": 80, "change": 20, "change_pct": 25.0}
    assert rows["/shop"]["metrics"]["sessions"] == {"current": 40, "previous": 50, "change": -10, "change_pct": -20.0}
    assert rows["/shop"]["metrics"]["purchaseRevenue"]["change"] == -9.5
    assert "present_in" not in rows["/"]
    assert rows["/new"]["present_in"] == ["current"] and rows["/new"]["metrics"]["sessions"]["previous"] == 0
    assert rows["/old"]["present_in"] == ["previous"] and rows["/old"]["metrics"]["sessions"]["current"] == 0
    assert [r["landingPage"] for r in out["rows"]] == ["/", "/shop", "/new", "/old"]  # largest current value first
    assert out["row_count"] == 4 and out["truncated"] is False and "fetch_truncated" not in out
    assert out["totals"]["sessions"] == {"current": 145, "previous": 137, "change": 8, "change_pct": 5.84}
    assert out["totals"]["purchaseRevenue"]["current"] == 600.5
    assert out["metrics"]["sessions"]["unit"] == "count" and out["metrics"]["purchaseRevenue"]["unit"] == "EUR"
    assert out["metrics"]["sessions"]["definition"] and out["currency"] == "EUR"
    assert out["property_id"] == "123" and out["fetched_at"] and out["freshness"]


def test_both_date_ranges_are_resolved_in_the_property_timezone(server, monkeypatch):
    use(server, monkeypatch, [(":runReport", answer(report({"/": [1, 1]}, {"/": [1, 1]})))])
    out = server.ga4_compare(period=CUSTOM)
    cur, prev = out["date_range"]["current"], out["date_range"]["previous"]
    assert (cur["start"], cur["end"], cur["timezone"], cur["data_complete"]) == ("2026-09-01", "2026-09-30",
                                                                               "Europe/Paris", True)
    assert (prev["start"], prev["end"], prev["data_complete"]) == ("2026-08-02", "2026-08-31", True)
    assert out["date_range"]["period"] == CUSTOM and out["date_range"]["compare_to"] == "previous_period"
    assert out["data_complete"] is True


def test_a_recent_current_range_is_not_complete(server, monkeypatch):
    use(server, monkeypatch, [(":runReport", answer(report({"/": [1, 1]}, {"/": [1, 1]})))])
    out = server.ga4_compare(period="last_7_days")  # ends yesterday: GA4 can still revise it
    assert out["data_complete"] is False and out["date_range"]["current"]["settling_note"]
    assert out["date_range"]["previous"]["data_complete"] is True


def test_identical_periods_give_equal_totals_and_no_moves(server, monkeypatch):
    same = {"/": [100, 500.0], "/shop": [40, 90.5]}
    use(server, monkeypatch, [(":runReport", answer(report(same, same)))])
    out = server.ga4_compare(period=CUSTOM)
    for metric in ("sessions", "purchaseRevenue"):
        t = out["totals"][metric]
        assert t["current"] == t["previous"] and t["change"] == 0 and t["change_pct"] == 0.0
    assert all(m["change"] == 0 and m["change_pct"] == 0.0 for r in out["rows"] for m in r["metrics"].values())
    assert out["biggest_moves"] == []


def test_division_by_zero_gives_no_percent_never_an_error(server, monkeypatch):
    payload = report({"/": [30, 0], "/b": [0, 0], "/c": [0, 5.5]}, {"/": [0, 0], "/b": [0, 0], "/c": [0, 0]})
    use(server, monkeypatch, [(":runReport", answer(payload))])
    out = server.ga4_compare(period=CUSTOM)
    rows = {r["landingPage"]: r["metrics"] for r in out["rows"]}
    assert rows["/"]["sessions"]["change"] == 30 and rows["/"]["sessions"]["change_pct"] is None
    assert "undefined" in rows["/"]["sessions"]["change_pct_note"]
    assert rows["/b"]["sessions"]["change"] == 0 and rows["/b"]["sessions"]["change_pct"] == 0.0
    assert rows["/c"]["purchaseRevenue"]["change_pct"] is None
    assert out["totals"]["sessions"]["previous"] == 0 and out["totals"]["sessions"]["change_pct"] is None
    assert "error" not in out


def test_biggest_moves_are_the_five_largest_absolute_changes_of_the_first_metric(server, monkeypatch):
    current = {f"/p{i}": [100 + i * 10, 0] for i in range(7)}   # p0..p6: 100, 110, ... 160
    previous = {f"/p{i}": [100 + (-1) ** i * i * 7, 0] for i in range(7)}  # p0 does not move: 6 moving rows, 5 kept
    use(server, monkeypatch, [(":runReport", answer(report(current, previous)))])
    out = server.ga4_compare(period=CUSTOM)
    moves = out["biggest_moves"]
    assert len(moves) == 5 and out["ranked_by"] == "sessions"
    changes = [abs(m["change"]) for m in moves]
    assert changes == sorted(changes, reverse=True)
    expected = sorted(((abs(current[k][0] - previous[k][0]), k) for k in current), reverse=True)[:5]
    assert [m["landingPage"] for m in moves] == [k for _, k in expected]
    assert {"metric", "current", "previous", "change", "change_pct"} <= set(moves[0])
    assert len(out["rows"]) == 7  # the rows themselves are not cut to five


def test_rows_are_cut_to_limit_but_totals_and_moves_see_every_row(server, monkeypatch):
    current = {f"/p{i}": [i + 1, 0] for i in range(10)}
    use(server, monkeypatch, [(":runReport", answer(report(current, {})))])
    out = server.ga4_compare(period=CUSTOM, limit=3)
    assert len(out["rows"]) == 3 and out["row_count"] == 10 and out["truncated"] is True
    assert out["rows"][0]["landingPage"] == "/p9"
    assert out["biggest_moves"][0]["landingPage"] == "/p9" and out["totals"]["sessions"]["current"] == 55


def test_a_cut_fetch_is_flagged(server, monkeypatch):
    payload = report({"/": [10, 0]}, {"/": [5, 0]}, row_count=25000)
    use(server, monkeypatch, [(":runReport", answer(payload))])
    out = server.ga4_compare(period=CUSTOM)
    assert out["fetch_truncated"] is True and "25000" in out["fetch_truncated_note"]


def test_no_dimension_gives_one_row_of_totals(server, monkeypatch):
    payload = report({(): [100, 500.0]}, {(): [80, 400.0]}, dims=())
    session = use(server, monkeypatch, [(":runReport", answer(payload))])
    out = server.ga4_compare(dimensions=[], period=CUSTOM)
    assert session.calls[0][2]["json"]["dimensions"] == []
    assert len(out["rows"]) == 1 and out["rows"][0]["metrics"]["sessions"]["change_pct"] == 25.0
    assert out["totals"]["sessions"]["current"] == 100


def test_the_dateRange_dimension_is_never_sent_twice(server, monkeypatch):
    session = use(server, monkeypatch, [(":runReport", answer(report({"/": [1, 1]}, {"/": [1, 1]})))])
    server.ga4_compare(dimensions=["landingPage", "dateRange"], period=CUSTOM)
    assert session.calls[0][2]["json"]["dimensions"] == [{"name": "landingPage"}]


def test_ratio_metrics_say_what_change_means(server, monkeypatch):
    mets = (("engagementRate", "TYPE_FLOAT"),)
    use(server, monkeypatch, [(":runReport", answer(report({"/": [0.5]}, {"/": [0.4]}, mets=mets)))])
    out = server.ga4_compare(metrics=["engagementRate"], period=CUSTOM)
    assert out["metrics"]["engagementRate"]["unit"] == "ratio 0 to 1" and "ratio points" in out["ratio_note"]
    assert out["rows"][0]["metrics"]["engagementRate"]["change"] == 0.1


def test_unknown_period_is_refused_before_any_request(server, monkeypatch):
    session = use(server, monkeypatch, [("", FakeResponse(200, {}))])
    out = server.ga4_compare(period="last_week")
    assert out["error"] == "invalid_period" and "last_month" in out["detail"] and out["fix"]
    assert server.ga4_compare(period=CUSTOM, compare_to="yesterday")["error"] == "invalid_period"
    assert server.ga4_compare(metrics=[], period=CUSTOM)["error"] == "invalid_request"
    assert server.ga4_compare(property_id="12/../x", period=CUSTOM)["error"] == "invalid_property_id"
    assert session.calls == []


def test_google_errors_are_passed_on_structured(server, monkeypatch):
    use(server, monkeypatch, [(":runReport", FakeResponse(429, {"error": {"message": "quota"}}, {"Retry-After": "7"}))])
    out = server.ga4_compare(period=CUSTOM)
    assert out["error"] == "rate_limited" and out["retry_after_seconds"] == "7" and out["fetched_at"]
    use(server, monkeypatch, [(":runReport", FakeResponse(200, {"rows": [{"dimensionValues": [{"value": "/"}],
                                                                           "metricValues": [{"value": "1"}]}]}))])
    assert server.ga4_compare(period=CUSTOM)["error"] == "unexpected_response"


def test_a_property_day_that_shifts_the_month_triggers_one_corrected_request(monkeypatch):
    """The ranges are first computed on the UTC day; if the property's own day moves a month boundary, the report
    is asked again once with the right dates (never more)."""
    bodies = []

    def send(body):
        bodies.append(body)
        return report({"/": [2, 0]}, {"/": [1, 0]}, tz="Pacific/Auckland")

    monkeypatch.setattr(ga4_tools.metrics, "today_in", lambda tz=None: (D(2026, 11, 1), tz or "UTC"))
    out = ga4_tools.run_compare(send, period="last_month", compare_to="previous_period", dimensions=["landingPage"],
                                metric_names=["sessions"], limit=10, today=D(2026, 10, 31))
    assert len(bodies) == 2
    assert bodies[0]["dateRanges"][0]["startDate"] == "2026-09-01"   # last month seen from 31 October
    assert bodies[1]["dateRanges"][0]["startDate"] == "2026-10-01"   # last month seen from 1 November
    assert out["date_range"]["current"]["start"] == "2026-10-01" and out["date_range"]["current"]["timezone"] == "Pacific/Auckland"
    bodies.clear()
    ga4_tools.run_compare(send, period="last_7_days", compare_to="previous_period", dimensions=[],
                          metric_names=["sessions"], limit=10, today=D(2026, 11, 1))
    assert len(bodies) == 1  # same day in the property: nothing to correct


# ------------------------------------------------------------------ clients
def test_a_foreign_property_is_refused_without_a_request(server, monkeypatch, tmp_path):
    _registry(tmp_path, monkeypatch)
    session = use(server, monkeypatch, [("", FakeResponse(200, {}))])
    out = server.ga4_compare(property_id=CLIENTS["beta"]["ga4"], period=CUSTOM, client_id="acme")
    assert out["error"] == "id_not_owned" and out["fix"] and out["client_id"] == "acme"
    assert "222222" not in out["detail"]  # the refused id is masked
    assert session.calls == []
    assert server.ga4_compare(period=CUSTOM)["error"] == "client_required"
    assert server.ga4_compare(period=CUSTOM, client_id="nobody")["error"] == "unknown_client"
    assert session.calls == []


def test_a_client_is_compared_on_its_own_property_and_token(server, monkeypatch, tmp_path):
    _registry(tmp_path, monkeypatch)
    session = use(server, monkeypatch, [(":runReport", answer(report({"/": [3, 0]}, {"/": [2, 0]})))])
    out = server.ga4_compare(period=CUSTOM, client_id="beta")
    assert out["client_id"] == "beta" and out["property_id"] == CLIENTS["beta"]["ga4"]
    [(_, url, kwargs)] = session.calls
    assert f"/properties/{CLIENTS['beta']['ga4']}:runReport" in url
    assert kwargs["headers"]["Authorization"] == f"Bearer {CLIENTS['beta']['token']}"
    assert CLIENTS["acme"]["ga4"] not in url


def test_the_tool_is_registered_read_only_with_its_parameters(server):
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    tool = tools["ga4_compare"]
    assert tool.annotations.readOnlyHint is True and tool.annotations.destructiveHint is False
    props = tool.inputSchema["properties"]
    assert {"metrics", "dimensions", "period", "compare_to", "limit", "property_id", "client_id"} <= set(props)
    assert "client_id" not in tool.inputSchema.get("required", [])

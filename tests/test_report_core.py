"""Report generator, pure helpers: periods, comparisons, deltas (polarity, noise), confidence, brand. No network."""

from datetime import date

import pytest

from google_ecommerce_mcp.report import brand as b
from google_ecommerce_mcp.report import compare as cmp
from google_ecommerce_mcp.report import confidence as conf
from google_ecommerce_mcp.report import period as p
from google_ecommerce_mcp.report.i18n import fmt_money, fmt_pct, tr

MINUS = "−"


# ------------------------------------------------------------------ pct_change / fmt_delta_pct / avg_position
@pytest.mark.parametrize("cur, prev, expected", [
    (120, 100, 20), (80, 100, -20), (0, 0, 0), (5, 0, None), (None, 100, None), (100, None, None),
    (-50, -100, 50), (50, -100, 150), (101, 200, -50), (1, 8, -88), (3, 8, -63),  # half away from zero
])
def test_pct_change(cur, prev, expected):
    assert cmp.pct_change(cur, prev) == expected


def test_pct_change_growth_from_zero_is_never_plus_100():
    assert cmp.pct_change(10, 0) is None


@pytest.mark.parametrize("cur, prev, lang, expected", [
    (118, 100, "en", "+18%"), (96, 100, "en", f"{MINUS}4%"), (100, 100, "en", "0%"),
    (5, 0, "en", "—"), (5, None, "en", "—"), (None, 5, "en", "—"),
    (118, 100, "fr", "+18 %"), (-50, -100, "en", "+50%"),
])
def test_fmt_delta_pct(cur, prev, lang, expected):
    assert cmp.fmt_delta_pct(cur, prev, lang) == expected


def test_fmt_delta_pct_one_decimal_uses_decimal_comma_in_french():
    assert cmp.fmt_delta_pct(106.2, 100, "fr", digits=1) == "+6,2 %"
    assert cmp.fmt_delta_pct(93.8, 100, "en", digits=1) == f"{MINUS}6.2%"


def test_avg_position_weighted_by_impressions():
    assert cmp.avg_position(100, 450) == 4.5
    assert cmp.avg_position(0, 0) is None
    assert cmp.avg_position(None, 3) is None
    rows = [{"impressions": 100, "position": 2.0}, {"impressions": 300, "position": 10.0}]
    assert cmp.weighted_position(rows) == pytest.approx(8.0)  # not the plain mean 6.0
    assert cmp.weighted_position([{"impressions": 0, "position": 3}]) is None


def test_compare_periods_winners_losers_new_and_buckets():
    cur = cmp.agg_from_gsc_rows([
        {"query": "a", "clicks": 50, "impressions": 500, "position": 2.0},
        {"query": "b", "clicks": 5, "impressions": 400, "position": 12.0},
        {"query": "new", "clicks": 3, "impressions": 90, "position": 25.0},
        {"query": "", "clicks": 9, "impressions": 9, "position": 1.0},
    ])
    prev = cmp.agg_from_gsc_rows([
        {"query": "a", "clicks": 30, "impressions": 450, "position": 3.0},
        {"query": "b", "clicks": 20, "impressions": 380, "position": 8.0},
        {"query": "gone", "clicks": 4, "impressions": 60, "position": 7.0},
    ])
    out = cmp.compare_periods(cur, prev)
    assert [w.label for w in out.winners] == ["a", "new"]
    assert [lo.label for lo in out.losers] == ["b", "gone"]
    assert out.losers[0].delta == -15
    assert [r.label for r in out.new_rows] == ["new"]
    assert out.new_rows[0].clicks_pct is None  # new: no baseline, never +100 %
    assert out.buckets == {"1-3": 1, "4-10": 0, "11-20": 1, "21+": 1}
    assert out.total == 3 and out.visible_clicks == 58
    assert out.rows[0].label == "a" and out.rows[0].clicks_pct == 67 and out.rows[0].position == 2.0


def test_agg_from_raw_api_rows():
    agg = cmp.agg_from_gsc_rows([{"keys": ["x"], "clicks": 2, "impressions": 10, "position": 4}])
    assert agg["x"].pos_weighted == 40


# ------------------------------------------------------------------ periods
def test_search_window_lags_two_days():
    w = p.search_window(7, date(2026, 10, 16))
    assert p.GSC_LAG_DAYS == 2
    assert (w.current.start, w.current.end) == (date(2026, 10, 8), date(2026, 10, 14))
    assert (w.previous.start, w.previous.end) == (date(2026, 10, 1), date(2026, 10, 7))
    assert (w.yoy.start, w.yoy.end) == (date(2025, 10, 8), date(2025, 10, 14))


def test_report_window_ends_yesterday_with_yoy():
    w = p.report_window(30, date(2026, 10, 1))
    assert (w.current.start, w.current.end) == (date(2026, 9, 1), date(2026, 9, 30))
    assert w.current.days == 30 and w.previous.end == date(2026, 8, 31) and w.previous.days == 30
    assert (w.yoy.start, w.yoy.end) == (date(2025, 9, 1), date(2025, 9, 30))
    assert w.search.current.end == date(2026, 9, 29)


def test_yoy_maps_february_29_and_full_months():
    assert p.shift_year(date(2028, 2, 29)) == date(2027, 2, 28)
    assert p.yoy_of(p.Window(date(2028, 2, 1), date(2028, 2, 29))) == p.month_window(2027, 2)
    sept = p.month_window(2026, 9)
    assert sept.is_full_month()
    assert p.previous_of(sept) == p.month_window(2026, 8)  # 31 days, still "the previous month"
    assert p.previous_of(p.month_window(2026, 1)) == p.month_window(2025, 12)
    assert p.previous_of(p.Window(date(2026, 9, 10), date(2026, 9, 16))) == p.Window(date(2026, 9, 3),
                                                                                       date(2026, 9, 9))


def test_parse_periods_never_guesses():
    out = p.parse_periods({"current": ["2026-09-01", "2026-09-30"], "previous": ["2026-09-30", "2026-09-01"],
                           "yoy": "2025"})
    assert out["current"] == p.month_window(2026, 9)
    assert out["previous"] is None and out["yoy"] is None
    assert p.parse_periods(None) == {"current": None, "previous": None, "yoy": None}


def test_settling_days_uses_the_gsc_lag():
    w = p.Window(date(2026, 9, 1), date(2026, 9, 30))
    assert p.settling_days(w, date(2026, 10, 5)) == 0
    assert p.settling_days(w, date(2026, 9, 30)) == 2
    with pytest.raises(ValueError):
        p.Window(date(2026, 9, 2), date(2026, 9, 1))


# ------------------------------------------------------------------ polarity and noise
def test_polarity_cpa_down_is_favourable_and_spend_up_is_neutral():
    assert cmp.assess_delta(30.0, 40.0, "cpa").state == "fav"
    assert cmp.assess_delta(40.0, 30.0, "cpa").state == "unfav"
    spend = cmp.assess_delta(9000.0, 7000.0, "spend")
    assert spend.state == "neutral" and spend.direction == "up"
    assert cmp.assess_delta(2000, 1000, "revenue", counts=(400, 200)).state == "fav"
    assert cmp.polarity_of("impressions") == cmp.NEUTRAL and cmp.polarity_of("lcp") == cmp.UP_BAD


def test_two_sigma_rule_for_counts():
    # |N1 - N0| > 2 sqrt(N1 + N0): 140 vs 100 -> 40 > 2*sqrt(240)=31.0 -> significant
    assert cmp.assess_delta(140, 100, "orders").state == "fav"
    # 180 vs 153 -> 27 < 2*sqrt(333)=36.5 -> not significant despite +17.6 %
    info = cmp.assess_delta(180, 153, "orders")
    assert info.state == "ns" and not info.significant and info.pct == pytest.approx(17.647, rel=1e-3)
    assert cmp.counts_significant(140, 100) and not cmp.counts_significant(180, 153)


def test_two_sigma_rule_for_rates_and_fallback():
    # 3.0 % vs 2.0 % on 10 000 sessions each: se ~ 0.0022 -> diff 0.01 significant
    assert cmp.assess_delta(0.03, 0.02, "conversion_rate", n=(10000, 10000)).state == "fav"
    # same rates on 100 sessions: not significant
    assert cmp.assess_delta(0.03, 0.02, "conversion_rate", n=(100, 100)).state == "ns"
    # unknown denominators: 5 % relative fallback
    assert cmp.assess_delta(0.0209, 0.0200, "ctr").state == "ns"   # +4.5 %: under the fallback
    assert cmp.assess_delta(0.0220, 0.0200, "ctr").state == "fav"  # +10 %


def test_revenue_uses_the_order_counts_and_one_percent_floor():
    assert cmp.assess_delta(26208, 22063, "revenue", counts=(180, 153)).state == "ns"
    untested = cmp.assess_delta(26208, 22063, "revenue")  # no order counts: not tested, never "significant"
    assert untested.state == "ns" and untested.tested is False
    stable = cmp.assess_delta(10050, 10000, "revenue", counts=(200, 200))
    assert stable.state == "ns" and abs(stable.pct) < 1


def test_disapproved_any_increase_is_coloured():
    assert cmp.assess_delta(4, 3, "disapproved").state == "unfav"
    assert cmp.assess_delta(3, 4, "disapproved").state == "fav"


def test_missing_value_is_na_never_zero():
    assert cmp.assess_delta(None, 10, "orders").state == "na"
    assert cmp.assess_delta(10, None, "orders").state == "na"


def test_small_volume_and_cwv_zones():
    assert cmp.is_small_volume(1000, 900, orders=171)
    assert cmp.is_small_volume(25, 40)
    assert not cmp.is_small_volume(1000, 900, orders=900)
    assert cmp.cwv_zone("lcp", 2.3) == "good" and cmp.cwv_zone("inp", 240) == "needs_improvement"
    assert cmp.cwv_zone("cls", 0.3) == "poor" and cmp.cwv_zone("lcp", None) is None


# ------------------------------------------------------------------ confidence
def test_confidence_bands():
    assert conf.assess(complete=True, count=900).level == conf.HIGH
    assert conf.assess(complete=True, count=900, claimed=True).level == conf.MEDIUM
    assert conf.assess(complete=False, count=30).level == conf.LOW
    assert conf.assess(complete=True, count=30).level == conf.MEDIUM
    assert conf.assess(complete=True, count=900, partial=True).level == conf.MEDIUM
    assert conf.assess(error=True).level == conf.LOW
    assert "small_count" in conf.assess(count=10).reasons
    assert conf.data_complete({"date_range": {"data_complete": False}}) is False
    assert conf.data_complete({}) is None
    assert tr("fr", "confidence.high") == "haute" and tr("fr", "confidence.low") == "basse"


# ------------------------------------------------------------------ formats and brand
def test_number_formats():
    assert fmt_money(24900, "EUR", "fr") == "24 900 €"
    assert fmt_money(145.6, "EUR", "fr") == "145,6 €"
    assert fmt_money(24900, "EUR", "en") == "€24,900"
    assert fmt_money(None, "EUR", "fr") == "—"
    assert fmt_pct(0.019, 1, "fr") == "1,9 %"


@pytest.mark.parametrize("value, ok", [("#1F6F5B", True), ("#abcdef", True), ("red", False), ("#FFF", False),
                                       ("#1F6F5B;}body{x", False), ("expression(alert(1))", False), (123, False)])
def test_accent_validation(value, ok):
    accent, issue = b.validate_accent(value)
    assert (accent == value.upper() if ok else accent == b.DEFAULT_ACCENT and issue == "accent_invalid")


def test_accent_text_reaches_aa_on_white():
    light = b.Brand(name="X", accent="#FFE600")
    assert b.contrast(light.accent_text, "#FFFFFF") >= 4.5
    assert light.on_accent == b.INK
    assert b.Brand(name="X", accent="#1F6F5B").on_accent == "#FFFFFF"


def _svg_uri(svg: str) -> str:
    import base64
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


@pytest.mark.parametrize("svg", [
    '<svg><script>alert(1)</script></svg>',
    '<svg onload="alert(1)"></svg>',
    '<svg><a href="javascript:alert(1)">x</a></svg>',
    '<svg><foreignObject><div>x</div></foreignObject></svg>',
    '<svg><image href="https://evil.example/x.png"/></svg>',
    '<svg><use xlink:href="//evil.example/s.svg#a"/></svg>',
    '<svg><rect style="fill:url(https://evil.example/x)"/></svg>',
    '<!DOCTYPE svg [<!ENTITY x "y">]><svg></svg>',
])
def test_trapped_svg_logo_is_refused(svg):
    assert b.validate_logo(_svg_uri(svg)) == ("", "logo_unsafe_svg")


def test_logo_validation_accepts_safe_and_refuses_bad_inputs():
    import base64
    safe = _svg_uri('<svg xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="g"/></defs>'
                    '<rect fill="url(#g)" width="4" height="4"/><use href="#g"/></svg>')
    assert b.validate_logo(safe) == (safe, None)
    png = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 20).decode()
    assert b.validate_logo(png)[1] is None
    assert b.validate_logo("data:image/png;base64," + base64.b64encode(b"GIF89a").decode())[1] == "logo_bad_format"
    assert b.validate_logo("https://example.invalid/logo.png")[1] == "logo_bad_format"
    assert b.validate_logo("data:image/gif;base64,R0lGOD==")[1] == "logo_bad_format"
    assert b.validate_logo("data:image/png;base64,@@@")[1] == "logo_bad_format"
    assert b.validate_logo("data:image/png;base64,abc")[1] == "logo_bad_base64"
    big = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * (256 * 1024)).decode()
    assert b.validate_logo(big)[1] == "logo_too_large"


def test_brand_name_fallbacks_and_initials():
    assert b.build_brand({}, "Boutique Exemple").name == "Boutique Exemple"
    assert b.build_brand({"name": "Marque"}, "Client").initials == "M"
    assert b.build_brand(None, "", "Client").name == "Client"
    assert b.build_brand({}, "Boutique Exemple").initials == "BE"

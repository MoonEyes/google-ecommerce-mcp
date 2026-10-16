"""Report generator, review round 2: hostile input, CLI file safety, coherence between summary and pastilles,
significance of amounts, accessibility of "not significant", URL privacy, colours. No network.

Non-regression for the review scripts (scratchpad p3x: fuzz_types, xss, dos, probe*): every case those scripts
found is pinned here. All data is fictitious."""

import base64
import copy
import json
import os
import re
from pathlib import Path

import pytest

from google_ecommerce_mcp.report import ContractError, render_report, sanitize
from google_ecommerce_mcp.report import compare as cmp
from google_ecommerce_mcp.report import contract as c
from google_ecommerce_mcp.report import render as R
from google_ecommerce_mcp.report import svg as svg_mod
from google_ecommerce_mcp.report.brand import Brand, contrast, validate_logo
from google_ecommerce_mcp.report.confidence import MEDIUM, assess
from google_ecommerce_mcp.report.i18n import tr
from google_ecommerce_mcp.report.numbers import is_num
from google_ecommerce_mcp.report.period import Window, parse_periods, period_warnings
from google_ecommerce_mcp.report.render import build_context, short_url, write_report
from google_ecommerce_mcp.report.style import _BASE
from google_ecommerce_mcp.report.summary import build_decisions, build_messages, mer

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "tests" / "data" / "sample_report.json"
FULL = ROOT / "tests" / "data" / "sample_report_full.json"


@pytest.fixture
def full():
    return json.loads(FULL.read_text(encoding="utf-8"))


@pytest.fixture
def sample():
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def _text(html: str) -> str:
    import html as h
    return re.sub(r"\s+", " ", h.unescape(re.sub(r"<[^>]+>", " ", html)))


def _section(html: str, sid: str) -> str:
    m = re.search(r'<section class="[^"]*" id="s-%s".*?</section>' % sid, html, re.S)
    return m.group(0) if m else ""


def _main(argv):
    from google_ecommerce_mcp.__main__ import main
    return main(argv)


# ====================================================================== B1: hostile input, no data loss
def test_lone_surrogate_is_written_not_crashing(tmp_path):
    src = tmp_path / "in.json"
    src.write_text('{"meta": {"client": "A\\ud800B"}, "notes": "x\\udfffy"}', encoding="utf-8")
    out = tmp_path / "out.html"
    assert _main(["report", "--input", str(src), "--output", str(out)]) is None
    html = out.read_text(encoding="utf-8")
    assert "A�B" in html and out.stat().st_size > 1000
    assert "�" in render_report({"meta": {"client": "\ud800"}})


def test_existing_output_is_intact_after_a_failure(tmp_path, capsys):
    out = tmp_path / "report.html"
    out.write_text("OLD REPORT", encoding="utf-8")
    bad = tmp_path / "bigint.json"
    bad.write_text('{"goals": {"revenue": ' + "9" * 5000 + "}}", encoding="utf-8")
    assert _main(["report", "--input", str(bad), "--output", str(out), "--force"]) == 1
    err = capsys.readouterr().err
    assert "Report not written" in err and "Traceback" not in err
    assert out.read_text(encoding="utf-8") == "OLD REPORT"
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_failed_write_leaves_no_temporary_file(tmp_path, monkeypatch):
    out = tmp_path / "report.html"
    out.write_text("OLD", encoding="utf-8")

    def boom(src, dst):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(R.ReportFileError, match="cannot be written"):
        write_report(str(SAMPLE), str(out), force=True)
    assert out.read_text(encoding="utf-8") == "OLD"
    assert [p.name for p in tmp_path.iterdir()] == ["report.html"]


@pytest.mark.parametrize("payload, message", [
    ("[" * 200_000 + "]" * 200_000, "nested too deeply"),
    ('{"a": ' * 3000 + "1" + "}" * 3000, "deeper than 100 levels|nested too deeply"),  # message depends on the interpreter's recursion limit
    ('{"meta": {"client": 1' + "0" * 5000 + "}}", "unusable JSON value"),
    ("[1, 2]", "must be a JSON object"),
    ("{nope", "invalid JSON"),
], ids=["deep-list", "deep-object", "5000-digit-int", "list", "broken"])
def test_cli_rejects_hostile_json_without_traceback(tmp_path, capsys, payload, message):
    src = tmp_path / "in.json"
    src.write_text(payload, encoding="utf-8")
    assert _main(["report", "--input", str(src), "--output", str(tmp_path / "o.html")]) == 1
    err = capsys.readouterr().err
    assert any(m in err for m in message.split("|")) and "Traceback" not in err
    assert not (tmp_path / "o.html").exists()


def test_sanitize_depth_limit_is_iterative():
    deep: dict = {}
    node = deep
    for _ in range(5000):  # far beyond the recursion limit: no RecursionError, a clean ContractError
        node["a"] = {}
        node = node["a"]
    with pytest.raises(ContractError, match="deeper than 100"):
        sanitize({"sources": deep})
    ok: dict = {"sources": {"ga4": {"kpi": {"current": {"totals": {}}}}}}
    assert sanitize(ok)["sources"]["ga4"]["kpi"]["current"] == {"totals": {}}


@pytest.mark.parametrize("value", [10 ** 400, 1e308, float("nan"), float("inf"), -float("inf"), True, False,
                                   "12", None, 10 ** 15, -1e16])
def test_is_num_refuses_invalid_and_huge_numbers(value):
    assert not is_num(value) and not c.is_num(value) and c.num(value) is None


def test_is_num_accepts_plausible_numbers():
    assert all(is_num(v) for v in (0, -3, 2.5, 10 ** 14, -1e14))


def test_huge_numbers_are_not_provided_and_never_crash(full):
    full["sources"]["ga4"]["kpi"]["current"]["totals"]["purchaseRevenue"] = 10 ** 400
    full["sources"]["search_console"]["totals"]["current"]["totals"]["clicks"] = 1e308
    html = render_report(full)
    assert "section-error" not in html.split("</style>")[1]
    kpi = _section(html, "kpi")
    assert "non mesuré" in _text(kpi).split("Commandes")[0]
    assert "1e+308" not in html and "inf" not in _text(html).lower().split()


def test_review_fuzz_values_never_raise(full):
    """A compact version of p3x/fuzz_types.py: hostile values at many leaves never raise nor break a section."""
    values = [None, "abc", [], {}, -5, float("nan"), float("inf"), 1e308, 10 ** 400, True, "12"]
    paths = [("sources", "ga4", "kpi", "current", "totals", "purchaseRevenue"),
             ("sources", "ga4", "kpi", "current", "rows"),
             ("sources", "ads_overview", "current", "combined", "spend"),
             ("sources", "meta_ads", "totals", "purchases"),
             ("sources", "search_console", "totals", "current", "totals"),
             ("goals", "revenue"), ("periods", "current"), ("meta", "brand"), ("events",), ("notes",)]
    for path in paths:
        for value in values:
            d = copy.deepcopy(full)
            node = d
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = value
            html = render_report(d)
            assert "section-error" not in html.split("</style>")[1], (path, value)


# ====================================================================== I5: never overwrite the input
def test_output_equal_to_input_is_refused(tmp_path, capsys):
    src = tmp_path / "data.json"
    src.write_text(SAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    before = src.read_bytes()
    variants = [str(src), str(tmp_path / "." / "data.json"), str(src).upper() if os.name == "nt" else str(src)]
    for target in variants:
        assert _main(["report", "--input", str(src), "--output", target, "--force"]) == 1
        assert "is the input file" in capsys.readouterr().err
    try:
        os.link(src, tmp_path / "hard.json")
    except OSError:
        pass
    else:
        assert _main(["report", "--input", str(src), "--output", str(tmp_path / "hard.json"), "--force"]) == 1
    assert src.read_bytes() == before


def test_input_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "MAX_INPUT_BYTES", 100)
    with pytest.raises(R.ReportFileError, match="larger than"):
        write_report(str(SAMPLE), str(tmp_path / "o.html"))


def test_cli_message_for_a_truncated_name(tmp_path, capsys):
    out = tmp_path / "o.html"
    assert _main(["report", "--input", str(SAMPLE), "--output", str(out), "--brand-name", "N" * 200]) is None
    err = capsys.readouterr().err
    assert "name_too_long: client name too long, truncated to 120 characters" in err
    assert "default was used" not in err


# ====================================================================== B2: summary agrees with the pastilles
def _kpi_pastille_state(html: str, label: str) -> str:
    kpi = _section(html, "kpi")
    card = kpi.split(f'<div class="l">{label}</div>', 1)[1].split('<div class="kpi', 1)[0]
    after = card.split("<ul class=\"cmp\">", 1)[1]
    return re.search(r'class="delta (fav|unfav|neutral|ns)"', after).group(1)


def test_summary_never_contradicts_the_kpi_pastille(full):
    html = render_report(full)
    data = sanitize(full)
    messages = {m.key: m for m in build_messages(data, "fr")}
    rev = messages["revenue"]
    state = _kpi_pastille_state(html, "Revenu")
    assert rev.state == state
    if state == "ns":
        assert "pas de variation significative (+18,8\u00a0%)" in rev.title and "stable" not in rev.title
    summary = _text(_section(html, "summary"))
    assert re.sub(r"\s+", " ", rev.title) in summary


def test_summary_gsc_clicks_agree_with_the_seo_pastille(full):
    totals = full["sources"]["search_console"]["totals"]
    totals["previous"]["totals"]["clicks"] = 1320  # 1 340 vs 1 320: not significant
    full["sources"]["ads_overview"] = {}
    full["sources"]["merchant_center"] = {}
    html = render_report(full)
    gsc = {m.key: m for m in build_messages(sanitize(full), "fr")}["gsc"]
    seo = _section(html, "seo")
    card = seo.split('<div class="l">' + tr("fr", "seo.clicks") + "</div>", 1)[1]
    seo_state = re.search(r'class="delta (fav|unfav|neutral|ns)"', card).group(1)
    assert gsc.state == seo_state == "ns"
    assert "pas de variation significative" in gsc.title and "stable" not in gsc.title


def test_not_significant_lowers_the_confidence(full):
    msg = {m.key: m for m in build_messages(sanitize(full), "fr")}["revenue"]
    assert msg.state == "ns" and msg.confidence.level == "low" and "not_significant" in msg.confidence.reasons
    full["sources"]["ga4"]["kpi"]["previous"]["totals"]["purchaseRevenue"] = 26100.0  # 180 vs 153 orders
    again = {m.key: m for m in build_messages(sanitize(full), "fr")}["revenue"]
    assert again.state == "ns"


def test_significant_revenue_keeps_its_figure(full):
    prev = full["sources"]["ga4"]["kpi"]["previous"]["totals"]
    prev["purchaseRevenue"], prev["ecommercePurchases"] = 12000.0, 180
    msg = {m.key: m for m in build_messages(sanitize(full), "fr")}["revenue"]
    assert msg.state == "fav" and "atteint 26 208 €, +118,4 %" in msg.title


# ====================================================================== I1: amounts
def test_revenue_drop_at_constant_orders_is_significant():
    assert cmp.assess_delta(6000, 10000, "revenue", counts=(200, 200)).state == "unfav"
    assert cmp.assess_delta(26208, 22063, "revenue", counts=(180, 153)).state == "ns"
    assert cmp.assess_delta(150, 100, "aov", counts=(200, 200)).state == "fav"
    assert cmp.assess_delta(150, 100, "aov").state == "fav"
    untested = cmp.assess_delta(6000, 10000, "revenue")
    assert untested.state == "ns" and untested.tested is False


def test_amount_fallback_and_stable_floor():
    assert cmp.assess_delta(104, 100, "cpa").state == "ns"       # 4 % < 5 % fallback
    assert cmp.assess_delta(106, 100, "cpa").state == "unfav"    # 6 % > 5 %
    floor = cmp.assess_delta(1000.5, 1000, "orders")
    assert floor.state == "ns" and abs(floor.pct) < cmp.STABLE_FLOOR_PCT


def test_untested_pastille_says_so(sample):
    ctx = build_context(sanitize(sample))
    out = ctx.delta(110.0, 100.0, "revenue", small=False)
    assert 'title="non testé : volume inconnu"' in out and "(non testé)" in out


def test_ctr_two_sigma_with_denominators():
    assert cmp.assess_delta(0.03, 0.02, "ctr", n=(1000, 1000)).state == "ns"
    assert cmp.assess_delta(0.03, 0.02, "ctr", n=(100000, 100000)).state == "fav"


# ====================================================================== I2: "not significant" is not colour-only
def test_ns_pastille_has_a_word_for_screen_readers_and_print(sample):
    ctx = build_context(sanitize(sample))
    out = ctx.delta(180, 153, "orders", small=False)
    assert out.startswith('<span class="delta ns" title="variation non significative">')
    assert '<span class="sr-only ns-word"> (non significatif)</span>' in out
    print_css = _BASE.split("@media print{", 1)[1]
    assert ".ns-word{position:static" in print_css and "clip:auto" in print_css


# ====================================================================== I3: URLs and identifiers
@pytest.mark.parametrize("url, expected", [
    ("https://shop.invalid/commande?order_id=12&key=wc_order_abc", "/commande"),
    ("https://shop.invalid/p/x?gclid=abc#top", "/p/x"),
    ("/p?fbclid=1", "/p"), ("https://shop.invalid", "/"), ("?x=1", "/"),
])
def test_short_url_drops_query_and_fragment(url, expected):
    assert short_url(url) == expected


def test_identifying_url_parameters_and_keys_are_masked():
    data = sanitize({"events": [{"date": "2026-09-01", "label": "voir /c?order_id=99&gclid=G1&page=2"}],
                     "sources": {"ga4": {"kpi": {"current": {"rows": [{"gclid": "x", "token": "t", "ip": "1.2.3.4",
                                                                       "key": "k", "sessions": 3}]}}}}})
    label = data["events"][0]["label"]
    assert "99" not in label and "G1" not in label and "page=2" in label
    row = data["sources"]["ga4"]["kpi"]["current"]["rows"][0]
    assert row == {"sessions": 3}
    assert any("URL parameter" in e["message"] for e in data["errors"])


def test_email_key_and_email_in_notes_are_removed(sample):
    sample["meta"]["email"] = "boss@example.invalid"
    sample["notes"] = "Contact : chef@example.invalid"
    data = sanitize(sample)
    assert "email" not in data["meta"] and "chef@" not in data["notes"] and "[e-mail]" in data["notes"]
    assert "example.invalid" not in render_report(sample)


# ====================================================================== I4: confidence
def test_confidence_capped_at_medium_under_500():
    assert assess(complete=True, count=180).level == MEDIUM
    assert assess(complete=True, count=499).level == MEDIUM
    assert assess(complete=True, count=500).level == "high"


# ====================================================================== I6: accent contrast
@pytest.mark.parametrize("accent", ["#FFFF00", "#FFFFFF"])
def test_light_accent_is_darkened_for_graphics_and_text(sample, accent):
    brand = Brand(name="X", accent=accent)
    assert contrast(brand.accent_graphic, "#FFFFFF") >= 3.0
    assert contrast(brand.accent_text, "#FFFFFF") >= 4.5
    assert contrast(brand.on_accent, brand.accent_graphic) >= 4.5
    sample["meta"]["brand"]["accent"] = accent
    sample["sources"]["search_console"]["totals"]["current"]["rows"] = [
        {"date": f"2026-09-{d:02d}", "clicks": 40 + d} for d in range(1, 8)]
    html = render_report(sample)
    root = re.search(r"--accent:(#[0-9A-F]{6});--accent-text:(#[0-9A-F]{6})", html)
    assert contrast(root.group(1), "#FFFFFF") >= 3.0 and contrast(root.group(2), "#FFFFFF") >= 4.5
    body = html.split("</style>")[1]
    assert f'"{accent}"' not in body and f'"{accent.lower()}"' not in body  # never drawn raw


def test_reference_line_colour_contrast():
    assert contrast(svg_mod.REFERENCE, "#FFFFFF") >= 3.0


# ====================================================================== minors
def test_column_chart_refuses_negative_and_gaps_none():
    chart = svg_mod.column_chart([("a", 5), ("b", -3), ("c", None), ("d", 0)], "#1F6F5B", label="x")
    assert chart.count("<rect") == 1 and chart.count("<line") == 1
    assert svg_mod.column_chart([("a", -1), ("b", None)], "#000000") == ""


def test_column_chart_bar_title_is_escaped():
    chart = svg_mod.column_chart([("<b>x</b>", 5)], "#1F6F5B", value_fmt=lambda v: '5 "<i>"')
    assert "<b>" not in chart and "&lt;b&gt;x&lt;/b&gt; : 5 &quot;&lt;i&gt;&quot;" in chart


def test_position_rounded_to_zero_is_stable(sample):
    ctx = build_context(sanitize(sample))
    out = ctx.delta(10.0, 10.04, "position", small=False)
    assert "gagne 0,0" not in out and "stable" in out


def test_one_rounding_rule_for_percentages():
    assert cmp.pct_change(97.5, 100) == -3 and cmp.fmt_delta_pct(97.5, 100, "en") == "−3%"
    assert cmp.pct_change(102.5, 100) == 3 and cmp.fmt_delta_pct(102.5, 100, "en") == "+3%"


def test_point_delta_rounded_to_nothing_is_neutral_without_arrow(sample):
    from google_ecommerce_mcp.report.sections import _common as h
    ctx = build_context(sanitize(sample))
    out = h.pt_delta(ctx, 0.20004, 0.2, n=(10 ** 9, 10 ** 9), metric="ctr")
    assert "fav" not in out and "▲" not in out and "stable" in out


def test_percent_shares_add_up_to_100():
    shares = cmp.percent_shares([1, 1, 1])
    assert shares == [33.4, 33.3, 33.3] and round(sum(shares), 1) == 100.0
    assert cmp.percent_shares([2432, 38, 112]) and round(sum(cmp.percent_shares([2432, 38, 112])), 1) == 100.0
    assert cmp.percent_shares([None, 5]) == [None, 100.0] and cmp.percent_shares([0, 0]) == [None, None]


def test_merchant_shares_add_up_to_100(full):
    full["sources"]["merchant_center"]["statuses"] = {"approved": 1, "limited": 1, "disapproved": 1}
    html = _section(render_report(full), "merchant")
    hidden = re.search(r'<table class="sr-only">.*?</table>', html, re.S).group(0)
    shares = [float(x.replace(",", ".")) for x in re.findall(r"<td>(\d+,\d) %</td>", hidden)]
    assert len(shares) == 3 and round(sum(shares), 1) == 100.0


def test_inconsistent_periods_are_reported_and_not_called_previous_month(sample):
    sample["periods"]["previous"] = ["2026-09-10", "2026-10-10"]
    sample["periods"]["yoy"] = ["2024-02-01", "2024-02-29"]
    data = sanitize(sample)
    ctx = build_context(data)
    assert ctx.prev_label_raw() == "Période précédente" and ctx.yoy_label_raw() == "Même période N-1"
    warnings = period_warnings(ctx.periods)
    assert len(warnings) == 2
    html = render_report(sample)
    assert "Mois précédent" not in _text(_section(html, "kpi"))


def test_partial_month_is_not_labelled_as_the_month(sample):
    sample["periods"]["current"] = ["2026-09-01", "2026-09-15"]
    html = render_report(sample)
    assert "du 1er septembre 2026 au 15 septembre 2026" in _text(html)
    assert "Rapport mensuel" not in html and "Mois précédent" not in _text(html)
    assert period_warnings(parse_periods({"current": ["2026-09-01", "2026-09-30"],
                                          "previous": ["2026-08-01", "2026-08-31"],
                                          "yoy": ["2025-09-01", "2025-09-30"]})) == []
    assert Window.__name__ == "Window"


def test_document_head_and_print_mobile_rules(sample):
    html = render_report(sample)
    assert '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; ' \
           'style-src \'unsafe-inline\'">' in html
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
    assert ".sr-only{position:absolute" in html and "@media (max-width:640px)" in html
    assert '<div class="sample" role="note">' in html
    assert '<p class="note" role="note">' in html


def test_brand_name_with_quote_and_valid_logo(sample):
    png = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8).decode()
    sample["meta"]["brand"].update({"name": 'Chez "Lu" & <Cie>', "logo_data_uri": png})
    html = render_report(sample)
    assert 'alt="Logo de Chez &quot;Lu&quot; &amp; &lt;Cie&gt;"' in html


def test_logo_base64_is_strict():
    good = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8).decode()
    assert validate_logo("data:image/png;base64," + good)[1] is None
    for bad in (good[:5] + "\n" + good[5:], good + "=", " " + good, good.replace("A", "-")):
        assert validate_logo("data:image/png;base64," + bad)[0] == ""
    smil = base64.b64encode(b'<svg><animate attributeName="href" values="https://e"/></svg>').decode()
    assert validate_logo("data:image/svg+xml;base64," + smil)[1] == "logo_unsafe_svg"
    ent = base64.b64encode(b'<svg><rect o&#110;click="1"/></svg>').decode()
    assert validate_logo("data:image/svg+xml;base64," + ent)[1] == "logo_unsafe_svg"


def test_mer_currency_unknown_and_zero_spend(sample):
    d = copy.deepcopy(sample)
    d["meta"]["currency"] = None
    kpi = d["sources"]["ga4"]["kpi"]["current"]
    kpi.pop("currency")
    kpi["metrics"] = {}
    assert mer(sanitize(d)).reason == "ads.reason.currency_unknown"
    d = copy.deepcopy(sample)
    d["sources"]["ads_overview"]["current"]["combined"]["spend"] = 0.0
    assert mer(sanitize(d)).reason == "ads.reason.zero_spend"
    assert "MER non calculé : dépense publicitaire nulle" in _text(render_report(d))


def test_spend_chart_needs_one_currency(sample):
    sample["sources"]["meta_ads"]["currency"] = "USD"
    ads = _text(_section(render_report(sample), "ads"))
    assert "Dépense : valeur la plus haute" not in ads and "Clics : valeur la plus haute" in ads


def test_sum_rows_with_a_missing_row_is_none():
    from google_ecommerce_mcp.report.sections import _common as h
    assert h.sum_rows([{"x": 1}, {"x": 2}], ["x"]) == 3
    assert h.sum_rows([{"x": 1}, {"y": 2}], ["x"]) is None
    assert h.sum_rows([], ["x"]) is None


def test_input_caps():
    data = sanitize({"notes": "n" * 30_000, "events": [{"date": "2026-09-01", "label": str(i)} for i in range(500)],
                     "errors": [{"source": "s", "code": "c", "message": "m"}] * 500,
                     "sources": {"mystery": {"a": 1}}})
    assert len(data["notes"]) == c.MAX_NOTES and len(data["events"]) == c.MAX_EVENTS
    assert any(e["code"] == "unknown_source" for e in data["errors"])
    assert len([e for e in data["errors"] if e["code"] == "c"]) == c.MAX_ERRORS


def test_decisions_are_capped_at_five(sample):
    ov = sample["sources"]["ads_overview"]["current"]["platforms"]
    for block in ov.values():
        block["conversions"] = 0
    sample["sources"]["ga4"] = {"kpi": {"current": {"error": "rate_limited"}}}
    steps = build_decisions(sanitize(sample), "fr")
    assert len(steps) == 5 and all(re.search(r"\d", s) for s in steps)


def test_kpi_sparkline_aligns_by_date_with_gaps(sample):
    rows = sample["sources"]["ga4"]["kpi"]["current"]["rows"]
    del rows[3]  # 2026-09-04 missing
    yoy = sample["sources"]["ga4"]["kpi"]["yoy"]["rows"]
    del yoy[0]   # 2025-09-01 missing: N-1 must NOT shift by one day
    html = _section(render_report(sample), "kpi")
    hidden = re.search(r'<table class="sr-only">.*?</table>', html, re.S).group(0)
    assert "<th scope=\"row\">2026-09-04</th><td>—</td>" in hidden
    assert re.search(r'<th scope="row">2026-09-01</th><td>[^<]+</td><td>—</td>', hidden)
    spark = re.search(r'<path d="([^"]+)" fill="none" stroke="#14201B"', html).group(1)
    assert spark.count("M") == 2  # the missing day breaks the line


def test_combined_sources_are_never_high_confidence(full):
    assert assess(complete=True, combined=True).level == MEDIUM
    assert assess(complete=True, count=900, combined=True).level == MEDIUM
    msg = {m.key: m for m in build_messages(sanitize(full), "fr")}["mer"]
    assert msg.confidence.level == MEDIUM and "combined_sources" in msg.confidence.reasons


def test_not_significant_wording_never_says_stable(full):
    for lang, phrase in (("fr", "pas de variation significative"), ("en", "no significant change")):
        rev = {m.key: m for m in build_messages(sanitize(full), lang)}["revenue"]
        assert phrase in rev.title and "stable" not in rev.title.lower() and "+18" in rev.title

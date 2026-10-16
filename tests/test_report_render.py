"""Report generator, end to end: full render, security, missing data, ads rules, summary, svg, CLI. No network.

The sample input tests/data/sample_report.json is FICTITIOUS (no real client, no real figures)."""

import ast
import base64
import copy
import json
import re
import sys
from pathlib import Path

import pytest

from google_ecommerce_mcp.report import render_report, sanitize
from google_ecommerce_mcp.report import svg as svg_mod
from google_ecommerce_mcp.report.render import build_context, markdown_to_html, short_url
from google_ecommerce_mcp.report.sections import SECTION_IDS, Placeholder, registry, warning_text
from google_ecommerce_mcp.report.style import _BASE
from google_ecommerce_mcp.report.summary import build_decisions, build_messages, mer

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "tests" / "data" / "sample_report.json"
REPORT_PKG = ROOT / "src" / "google_ecommerce_mcp" / "report"
DEFAULT_WARNING = ("Les conversions de chaque plateforme sont attribuées indépendamment et se recoupent. "
                   "La somme n'est pas dédupliquée et surestime les commandes réelles.")


@pytest.fixture
def sample():
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def _text(html: str) -> str:
    import html as h
    return h.unescape(re.sub(r"<[^>]+>", " ", html))


def _headings(html: str) -> list[str]:
    return [f"h{m.group(1)} " + _text(m.group(2)).strip()
            for m in re.finditer(r"<h([123])[^>]*>(.*?)</h\1>", html, re.S)]


# ------------------------------------------------------------------ full render
def test_full_render_structure(sample):
    html = render_report(sample)
    assert html.startswith("<!doctype html>")
    assert '<html lang="fr">' in html and "<title>Rapport Boutique Exemple · septembre 2026</title>" in html
    heads = _headings(html)
    assert heads[0] == "h1 Ce qu'il faut retenir du mois"
    h2 = [x for x in heads if x.startswith("h2")]
    assert h2[:11] == ["h2 1. Synthèse : trois messages", "h2 2. Chiffres clés", "h2 3. Entonnoir de conversion",
                       "h2 4. Canaux d'acquisition", "h2 5. Produits et catégories",
                       "h2 6. Recherche Google, sans publicité", "h2 7. Fiches produits sur Google Shopping",
                       "h2 8. Publicité", "h2 9. Vitesse du site (données de visiteurs réels)",
                       "h2 10. Fiabilité des mesures et journal du mois", "h2 Notes de l'équipe"]
    assert h2[-1] == "h2 À décider ensemble"
    assert "h3 Événements du mois à garder en tête" in heads
    assert {"h3 Google Ads", "h3 Meta Ads", "h3 ChatGPT Ads"} <= set(heads)
    assert "section-error" not in html.split("</style>")[1]
    assert html.count("Section non fournie") == 0  # every section module exists (tests/test_report_sections.py)


def test_output_is_self_contained(sample):
    html = render_report(sample).lower()
    for forbidden in ("http://", "https://", "<script", "@import", "url(", "<link", "<iframe", "src=\"//",
                      "made with", "javascript:"):
        assert forbidden not in html, forbidden
    assert "@page{size:a4" in html and "@media print" in html and "break-before:page" in html


def test_accessibility_basics(sample):
    html = render_report(sample)
    assert 'scope="col"' in html and 'scope="row"' in html
    assert 'role="note"' in html and 'role="img"' in html and "aria-label=" in html
    assert "repeat(auto-fit,minmax(" in html
    ctx = build_context(sanitize(sample))
    table = ctx.table("Canaux", ["Canal", "Sessions"], [["Direct", "12"]], numeric=[1])
    assert table.startswith('<div class="scroll" role="region" aria-label="Tableau : Canaux" tabindex="0">')
    assert '<th scope="col" class="num">Sessions</th>' in table and '<th scope="row">Direct</th>' in table


def test_english_render(sample):
    html = render_report(sample, lang="en")
    assert '<html lang="en">' in html and "Key takeaways of the month" in html and "2. Key figures" in html


# ------------------------------------------------------------------ security
def test_client_name_xss_is_escaped(sample):
    sample["meta"]["client"] = sample["meta"]["brand"]["name"] = '<script>alert("x")</script><img src=x onerror=1>'
    sample["events"].append({"date": "2026-09-09", "label": "<b onmouseover=1>x</b>"})
    sample["sources"]["merchant_center"]["product_issues"]["products"][0]["issues"][0]["description"] = "<svg/onload=1>"
    html = render_report(sample)
    assert "<script" not in html.lower() and "<img src=x" not in html and "<svg/onload" not in html
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in html


def test_invalid_accent_falls_back_to_default(sample):
    sample["meta"]["brand"]["accent"] = "red;}</style><script>alert(1)</script>"
    html = render_report(sample)
    assert "--accent:#1F6F5B" in html and "<script" not in html
    assert any(e["code"] == "accent_invalid" for e in _errors_after(sample))


def _errors_after(sample):
    data = sanitize(sample)
    ctx = build_context(data)
    return [{"code": i} for i in ctx.brand.issues]


def test_trapped_svg_logo_is_dropped(sample):
    trap = '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><script>alert(2)</script></svg>'
    sample["meta"]["brand"]["logo_data_uri"] = "data:image/svg+xml;base64," + base64.b64encode(trap.encode()).decode()
    html = render_report(sample)
    assert "data:image/svg+xml" not in html and 'class="initials"' in html


def test_safe_logo_is_inlined_as_data_uri(sample):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    uri = "data:image/png;base64," + base64.b64encode(png).decode()
    sample["meta"]["brand"]["logo_data_uri"] = uri
    html = render_report(sample)
    assert f'<img src="{uri}" alt="Logo de Boutique Exemple">' in html


def test_notes_markdown_is_escaped_first_and_links_lose_their_url():
    out = markdown_to_html("## Titre <i>\n- **gras** [lien](https://example.invalid/x)\n<script>x</script>")
    assert "<h4>Titre &lt;i&gt;</h4>" in out
    assert "<li><strong>gras</strong> lien</li>" in out
    assert "&lt;script&gt;" in out and "example.invalid" not in out and "<a " not in out


def test_personal_data_keys_are_removed_and_reported(sample):
    rows = sample["sources"]["ga4"]["kpi"]["current"]["rows"]
    rows[0]["transactionId"] = "T-1"
    rows[1]["email"] = "someone@example.invalid"
    sample["sources"]["ga4"]["kpi"]["current"]["user_id"] = "u1"
    sample["sources"]["meta_ads"]["client_id"] = "c1"
    sample["events"].append({"date": "2026-09-10", "label": "Appel de contact@example.invalid"})
    data = sanitize(sample)
    dumped = json.dumps(data["sources"])
    assert "transactionId" not in dumped and "someone@" not in dumped and '"user_id"' not in dumped
    assert '"client_id"' not in dumped
    assert "contact@example.invalid" not in json.dumps(data["events"])
    codes = [e for e in data["errors"] if e["code"] == "personal_data_removed"]
    assert len(codes) >= 4
    assert "example.invalid" not in render_report(sample)


def test_sanitize_rejects_non_objects_and_repairs_blocks():
    from google_ecommerce_mcp.report import ContractError
    with pytest.raises(ContractError):
        sanitize([1, 2])
    data = sanitize({"sources": [], "events": "x", "notes": 3})
    assert data["sources"] == {} and data["events"] == [] and data["notes"] == ""
    assert sum(e["code"] == "invalid_input" for e in data["errors"]) == 2


def test_short_url_strips_scheme_and_host():
    assert short_url("https://shop.example.invalid/p/x?y=1") == "/p/x"
    assert short_url("http://shop.example.invalid") == "/"
    assert short_url("/already/short") == "/already/short"


# ------------------------------------------------------------------ missing data
def test_absent_sources_render_nodata_never_zero():
    html = render_report({"meta": {"client": "Vide"}, "periods": {"current": ["2026-09-01", "2026-09-30"]}})
    assert "Source non fournie : GA4" in html
    assert "Aucune régie publicitaire fournie" in html
    assert "Pas assez de données chiffrées pour une synthèse." in html
    assert "0 €" not in html and ">0<" not in html


def test_failed_source_shows_its_error_code(sample):
    sample["sources"]["ga4"] = {"kpi": {"current": {"error": "missing_scope", "required_scope": "x"}}}
    html = render_report(sample)
    assert "Source en erreur : GA4 (missing_scope)" in html
    assert "Rétablir l'accès aux 1 source(s) en erreur" in _text(html)


def test_missing_previous_value_is_grey_not_provided(sample):
    del sample["sources"]["ga4"]["kpi"]["previous"]
    html = render_report(sample)
    assert 'Mois précédent : <span class="na">non fourni</span>' in html


# ------------------------------------------------------------------ deltas: double coding, palette
def test_every_pastille_is_double_coded(sample):
    html = render_report(sample)
    pastilles = re.findall(r'<span class="delta (fav|unfav|neutral|ns)"[^>]*><span aria-hidden="true">(.)</span> '
                           r'([^<]*)(?:<span class="sr-only ns-word">[^<]*</span>)?</span>', html)
    assert pastilles, "no change pastille rendered"
    for state, symbol, text in pastilles:
        assert symbol in "▲▼≈="
        assert re.search(r"[+−]|stable|nouveau|gagne .* place|perd .* place", text), text  # position: places
        assert "-" not in text.replace("−", "")  # typographic minus only
    assert len(re.findall(r'class="delta ', html)) == len(pastilles)
    assert 'class="delta ns" title="variation non significative"' in html


def test_neutral_spend_and_small_volume_text(sample):
    html = render_report(sample)
    # spend 8 610 vs 7 940: neutral, never coloured
    assert '<span class="delta neutral"><span aria-hidden="true">▲</span> +8,4 %</span>' in html
    # 180 orders < 500: absolute value shown with the percentage
    assert "+27 (+17,6 %)" in html


def test_css_has_no_red_or_green():
    css = _BASE.lower()
    for bad in ("#00ff00", "#ff0000", "#f00", "#0f0"):
        assert bad not in css
    assert not re.search(r"\b(red|green)\b", css)
    for color in ("#0b6e99", "#e4f1f7", "#b33a00", "#fbeae0", "#3f5249", "#ecefed", "#5f6b66"):
        assert color in css


def test_headline_is_derived_from_the_delta(sample):
    ctx = build_context(sanitize(sample))
    assert ctx.headline("revenue", "Revenu", 106.2, 100.0, "106 €", counts=(9000, 9000)) == \
        "Le revenu progresse de 6,2 %"
    assert ctx.headline("revenue", "Revenu", 93.8, 100.0, "94 €", counts=(9000, 9000)) == \
        "Le revenu recule de 6,2 %"
    assert ctx.headline("revenue", "Revenu", 100.4, 100.0, "100 €", counts=(90, 90)) == "Revenu : stable à 100 €"
    assert ctx.headline("revenue", "Revenu", 106.2, 100.0, "106 €").endswith("variation non testée : volume inconnu)")
    assert ctx.headline("aov", "Panier moyen", 106.2, 100.0, "106 €") == "Le panier moyen progresse de 6,2 %"
    assert ctx.headline("orders", "Commandes", 180, 153, "180") == \
        "Commandes : 180, pas de variation significative (+17,6\u00a0%)"
    assert ctx.headline("position", "Position", 8.8, 10.0, "8,8") == "La position moyenne gagne 1,2 place(s), à 8,8"
    assert ctx.headline("revenue", "Revenu", 100, None, "100 €") == "Revenu : 100 €"


def test_position_delta_reads_as_places(sample):
    ctx = build_context(sanitize(sample))
    out = ctx.delta(8.8, 10.0, "position", small=False)
    assert 'class="delta fav"' in out and "gagne 1,2 place(s)" in out and "▲" in out
    assert "perd 2,0 place(s)" in ctx.delta(12.0, 10.0, "position", small=False)


# ------------------------------------------------------------------ svg
def test_line_has_a_gap_for_missing_values():
    path = svg_mod.path_with_gaps([(0, 1), (1, 2), None, (3, 4), (4, 5)])
    assert path == "M0.0 1.0 L1.0 2.0 M3.0 4.0 L4.0 5.0"
    chart = svg_mod.line_chart([("09-01", 3), ("09-02", None), ("09-03", 5), ("09-04", 6)], label="Clics")
    d = re.search(r'stroke="#14201B" stroke-width="2"', chart)
    assert d and chart.count(" M") + chart.count('d="M') >= 2
    spark = svg_mod.sparkline([1, None, 3, 4], [1, 2, None, 2], value_label="4")
    assert spark.count("M") >= 4 and 'stroke-dasharray="3 3"' in spark


def test_charts_are_accessible_and_scriptless():
    charts = [
        svg_mod.column_chart([("2026-09-01", 3), ("2026-09-02", 0)], "#1F6F5B", label="c"),
        svg_mod.hbar_chart([("A", 3, "3"), ("B", 9, "9")], "#1F6F5B", label="h", rates=["", "33 %"], sort=False),
        svg_mod.diverging_bars([("w", 5, "+5")] * 7, [("l", -3, "−3")] * 7, label="d"),
        svg_mod.stacked_100([("ok", 90, "#E4F1F7", "90"), ("ko", 10, "#FBEAE0", "10")], label="s"),
        svg_mod.threshold_bar(2.3, 2.5, 4.0, zone_names=["Bon", "À améliorer", "Mauvais"], marker_text="2,3 s",
                              label="t"),
    ]
    for chart in charts:
        assert chart.startswith('<svg class="chart"') and 'role="img"' in chart and "aria-label=" in chart
        assert "<script" not in chart and "xmlns" not in chart and "http" not in chart
    assert charts[2].count("▲") == 5 and charts[2].count("▼") == 5  # 5 max per side
    assert svg_mod.hbar_chart([("A", 3, "3"), ("B", 9, "9")], "#000000").index(">B<") < \
        svg_mod.hbar_chart([("A", 3, "3"), ("B", 9, "9")], "#000000").index(">A<")  # sorted
    fig = svg_mod.figure("Le revenu progresse de 6 %", charts[0], ["Jour", "Valeur"], [("09-01", "3")])
    assert '<table class="sr-only">' in fig and '<th scope="row">09-01</th>' in fig
    assert svg_mod.figure("x", "", [], []) == ""
    assert svg_mod.line_chart([("a", None)]) == ""


# ------------------------------------------------------------------ ads rules
def test_attribution_warning_present_when_a_platform_is_shown(sample):
    html = render_report(sample)
    assert '<div class="warning" role="note"><strong>Lecture prudente.</strong> ' + \
        DEFAULT_WARNING.replace("'", "&#x27;") in html


def test_attribution_warning_default_text_without_overview(sample):
    del sample["sources"]["ads_overview"]
    assert warning_text(sanitize(sample), "fr") == DEFAULT_WARNING
    html = render_report(sample)
    assert 'role="note"' in html and DEFAULT_WARNING.replace("'", "&#x27;") in html
    assert "MER non calculé : dépense publicitaire totale non fournie" in html


def test_mer_requires_same_currency(sample):
    data = sanitize(sample)
    m = mer(data)
    assert m.value == pytest.approx(m.revenue / 8610.0) and m.currency == "EUR"
    sample["sources"]["ads_overview"]["current"]["combined"]["currency"] = "USD"
    m2 = mer(sanitize(sample))
    assert m2.value is None and m2.reason == "ads.reason.currency"
    html = render_report(sample)
    assert "MER non calculé : devises différentes (revenu GA4 en EUR, publicité en USD)" in html


def test_mer_without_combined_spend_says_why(sample):
    ov = sample["sources"]["ads_overview"]["current"]
    ov["combined"] = None
    ov["combined_reason"] = "different currencies (google_ads=EUR, meta=USD): no currency conversion"
    html = render_report(sample)
    assert "la vue d&#x27;ensemble n&#x27;additionne pas les dépenses" in html


def test_platform_conversions_are_never_added_up(sample):
    html = render_report(sample)
    ads = html.split('id="s-ads"')[1].split("</section>")[0]
    text = _text(re.sub(r'<table class="sr-only">.*?</table>', "", ads, flags=re.S))
    assert "256" not in text  # 142 + 96 + 18: the overview's non-deduplicated sum is never shown
    assert "Conversions déclarées" in text and "ROAS déclaré" in text


def test_meta_purchases_not_measured_is_not_zero(sample):
    meta = sample["sources"]["meta_ads"]
    meta["totals"].update({"purchases": None, "purchase_value": None, "roas": None})
    meta["purchase_measurement"] = {"measured": False, "note": "no purchase event"}
    html = render_report(sample)
    assert "Achats non mesurés : no purchase event" in html


def test_not_configured_platform_gets_its_own_card(sample):
    del sample["sources"]["openai_ads"]
    sample["sources"]["ads_overview"]["current"]["platforms"]["openai_ads"] = {"status": "not_configured",
                                                                               "error": "not_configured"}
    html = render_report(sample)
    assert "Régie non configurée" in html


# ------------------------------------------------------------------ summary
def test_summary_sentences_each_cite_a_number(sample):
    data = sanitize(sample)
    messages = build_messages(data, "fr")
    assert [m.key for m in messages] == ["revenue", "mer", "merchant"]
    for m in messages:
        for sentence in re.split(r"(?<=\.)\s+", f"{m.title} {m.detail}".strip()):
            assert re.search(r"\d", sentence), sentence
    for step in build_decisions(data, "fr"):
        assert re.search(r"\d", step), step
    assert messages[0].confidence.level in ("high", "medium", "low")


def test_summary_numbers_come_from_the_input(sample):
    data = sanitize(sample)
    revenue = data["sources"]["ga4"]["kpi"]["current"]["totals"]["purchaseRevenue"]
    msg = build_messages(data, "en")[0]
    assert f"{round(revenue):,}" in msg.figure
    assert "forecast" not in msg.title.lower() and "will" not in msg.title.lower()


def test_summary_is_deterministic(sample):
    assert render_report(copy.deepcopy(sample)) == render_report(copy.deepcopy(sample))


# ------------------------------------------------------------------ sections registry
def test_registry_order_and_placeholders():
    items = registry()
    assert [sid for sid, _ in items] == list(SECTION_IDS)
    sections = dict(items)
    assert not isinstance(sections["kpi"], Placeholder) and not isinstance(sections["ads"], Placeholder)
    assert all(callable(s.render) and isinstance(s.ORDER, int) for s in sections.values())
    assert sections["events"].SUBSECTION is True


def test_a_broken_section_does_not_break_the_report(sample, monkeypatch):
    from google_ecommerce_mcp.report import sections as sec

    def boom(data, ctx):
        raise RuntimeError("x")

    monkeypatch.setitem(sec.BUILTIN, "kpi", sec._Section("kpi", boom))
    html = render_report(sample)
    assert "Section non affichée : erreur de rendu (RuntimeError)" in html and "8. Publicité" in html


# ------------------------------------------------------------------ CLI
def test_cli_report_end_to_end(tmp_path, capsys):
    from google_ecommerce_mcp.__main__ import main

    out = tmp_path / "rapport.html"
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    code = main(["report", "--input", str(SAMPLE), "--output", str(out), "--lang", "en",
                 "--brand-name", "Demo Shop", "--accent", "#2B4C7E", "--logo", str(logo)])
    assert code is None and out.exists()
    html = out.read_text(encoding="utf-8")
    assert "Demo Shop" in html and "--accent:#2B4C7E" in html and "data:image/png;base64," in html
    assert '<html lang="en">' in html
    # no overwrite without --force
    assert main(["report", "--input", str(SAMPLE), "--output", str(out)]) == 1
    assert "already exists" in capsys.readouterr().err
    assert main(["report", "--input", str(SAMPLE), "--output", str(out), "--force", "--accent", "blue"]) is None
    assert "accent_invalid" in capsys.readouterr().err
    assert "--accent:#1F6F5B" in out.read_text(encoding="utf-8")


def test_cli_report_bad_input(tmp_path, capsys):
    from google_ecommerce_mcp.__main__ import main

    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    assert main(["report", "--input", str(bad), "--output", str(tmp_path / "o.html")]) == 1
    assert "invalid JSON" in capsys.readouterr().err
    assert main(["report", "--input", str(tmp_path / "missing.json"), "--output", str(tmp_path / "o.html")]) == 1
    assert not (tmp_path / "o.html").exists()


# ------------------------------------------------------------------ project rules
def test_report_package_parses_as_python_310():
    files = sorted(REPORT_PKG.rglob("*.py")) + [REPORT_PKG.parent / "__main__.py"]
    assert len(files) >= 12
    for path in files:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 10))


def test_report_uses_the_standard_library_only():
    allowed_roots = {"__future__", "html", "json", "string", "datetime", "zoneinfo", "re", "base64", "pathlib",
                     "argparse", "math", "dataclasses", "typing", "copy", "calendar", "binascii", "importlib",
                     "os", "tempfile"}
    for path in REPORT_PKG.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert name.split(".")[0] in allowed_roots, f"{path.name}: {name}"


def test_no_dependency_added_to_pyproject():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    deps = re.search(r"dependencies = \[(.*?)\]", text, re.S).group(1)
    assert sorted(re.findall(r'"([a-zA-Z0-9_-]+)', deps)) == ["google-auth", "google-auth-oauthlib", "keyring", "mcp",
                                                             "requests", "tzdata"]


def test_derived_files_credit_opengsc_and_notice_is_complete():
    derived = ["compare.py", "period.py", "summary.py", "svg.py", "style.py", "brand.py", "render.py"]
    for name in derived:
        first = (REPORT_PKG / name).read_text(encoding="utf-8").splitlines()[0]
        assert first.startswith("# Derived from OpenGSC (MIT), ") and first.endswith("; ported to Python"), name
    notice = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "Copyright (c) 2026 OpenGSC" in notice
    assert "THE SOFTWARE IS PROVIDED \"AS IS\", WITHOUT WARRANTY OF ANY KIND" in notice
    assert "The above copyright notice and this permission notice shall be included in all" in notice


def test_sample_data_is_marked_fictitious():
    data = json.loads(SAMPLE.read_text(encoding="utf-8"))
    assert data["meta"]["sample"] is True
    assert "Données d'exemple" in _text(render_report(data))


def _fstring_310_violations(source: str) -> list[str]:
    """f-string syntax accepted only from Python 3.12 (PEP 701): a backslash or the outer quote reused inside
    a replacement field. ast's feature_version does not check this, so it is checked on the tokens."""
    import io
    import sys
    import tokenize

    if sys.version_info < (3, 12):  # older tokenizers reject such code outright: ast.parse already failed
        return []
    out: list[str] = []
    stack: list[str] = []  # quote of each open f-string
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        name = tokenize.tok_name[tok.type]
        if name == "FSTRING_START":
            quote = tok.string.lstrip("rRfFbBuU")
            if stack and (quote[0] in "".join(stack) or "\\" in tok.string):
                out.append(f"line {tok.start[0]}: nested f-string reuses a quote")
            stack.append(quote)
        elif name == "FSTRING_END":
            stack.pop()
        elif stack and name == "STRING":
            body = tok.string.lstrip("rRbBuU")
            if body[0] in "".join(stack) or "\\" in tok.string:
                out.append(f"line {tok.start[0]}: string in an f-string expression reuses a quote or has a backslash")
        elif len(stack) > 1 and name == "FSTRING_MIDDLE" and "\\" in tok.string:
            out.append(f"line {tok.start[0]}: backslash inside a nested f-string")
    return out


def test_no_python_312_only_fstrings():
    files = sorted(REPORT_PKG.rglob("*.py")) + [REPORT_PKG.parent / "__main__.py"]
    problems = {str(p.name): v for p in files if (v := _fstring_310_violations(p.read_text(encoding="utf-8")))}
    assert problems == {}


def test_fstring_checker_catches_312_syntax():
    assert _fstring_310_violations("x = f'{\"a\" if 1 else \"\"}'\n") == []
    # Python < 3.12 tokenizes these PEP 701 f-strings differently (and ast.parse rejects them), so the
    # self-test of the checker only makes sense where the tokenizer understands the new syntax.
    if sys.version_info >= (3, 12):
        assert _fstring_310_violations("x = f'{'a'}'\n")
        backslash = chr(92)
        assert _fstring_310_violations("x = f'{\"" + backslash + "n\".join(y)}'\n")


def test_line_chart_draws_annotation_bands():
    chart = svg_mod.line_chart([("09-01", 3), ("09-02", 4), ("09-03", 5)], bands=[(1, 1, "Promo <b>")], label="c")
    assert 'fill="#FFF4E0"' in chart and "Promo &lt;b&gt;" in chart

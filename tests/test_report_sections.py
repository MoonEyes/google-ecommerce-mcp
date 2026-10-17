"""Report sections funnel ... events: complete, partial and absent data, escaping, totals, zones. No network.

tests/data/sample_report_full.json is FICTITIOUS (no real client, no real figures): the core sample enriched
with every section's sources (built by a throw-away script; edit it by hand if needed)."""

import ast
import copy
import html as htmllib
import json
import re
from pathlib import Path

import pytest

from google_ecommerce_mcp.report import render_report, sanitize
from google_ecommerce_mcp.report.compare import cwv_zone
from google_ecommerce_mcp.report.i18n import STRINGS
from google_ecommerce_mcp.report.render import build_context
from google_ecommerce_mcp.report.sections import SECTION_IDS, Placeholder, registry
from google_ecommerce_mcp.report.sections import _common, cwv, funnel

ROOT = Path(__file__).resolve().parents[1]
FULL = ROOT / "tests" / "data" / "sample_report_full.json"
SECTIONS_DIR = ROOT / "src" / "google_ecommerce_mcp" / "report" / "sections"
NEW = ("funnel", "channels", "products", "seo", "merchant", "cwv", "data_quality", "events")
XSS = '<script>alert(1)</script><img src=x onerror=1>'


@pytest.fixture
def full():
    return json.loads(FULL.read_text(encoding="utf-8"))


def section(sid: str, raw: dict, lang: str | None = None) -> str:
    """The body of one section, rendered exactly as the renderer does (sanitised input, real context)."""
    data = sanitize(raw)
    ctx = build_context(data, lang=lang)
    return dict(registry())[sid].render(data, ctx)


def text(fragment: str) -> str:
    fragment = re.sub(r'<table class="sr-only">.*?</table>', " ", fragment, flags=re.S)
    fragment = re.sub(r"<svg.*?</svg>", " ", fragment, flags=re.S)
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", fragment)))


def hidden_rows(fragment: str) -> list[list[str]]:
    """Rows of the hidden data tables (the chart equivalents), as plain text cells."""
    out = []
    for table in re.findall(r'<table class="sr-only">.*?</table>', fragment, flags=re.S):
        for tr in re.findall(r"<tbody>(.*?)</tbody>", table, flags=re.S)[0].split("</tr>"):
            cells = [htmllib.unescape(x) for x in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr)]
            if cells:
                out.append(cells)
    return out


def ga4_events(*pairs):
    return {"rows": [{"eventName": n, "eventCount": v} for n, v in pairs], "totals": {"sessions": 1000}}


# ------------------------------------------------------------------ registry and full document
def test_every_section_module_is_loaded():
    sections = dict(registry())
    assert [sid for sid, _ in registry()] == list(SECTION_IDS)
    for sid in NEW:
        assert not isinstance(sections[sid], Placeholder), sid
        assert sections[sid].__name__.endswith("sections." + sid)
    assert sections["events"].SUBSECTION is True
    assert [sections[s].ORDER for s in SECTION_IDS] == sorted(sections[s].ORDER for s in SECTION_IDS)


def test_full_report_structure_and_no_placeholder(full):
    out = render_report(full)
    h2 = [text(m).strip() for m in re.findall(r"<h2[^>]*>(.*?)</h2>", out, flags=re.S)]
    assert h2 == ["1. Synthèse : trois messages", "2. Chiffres clés", "3. Entonnoir de conversion",
                  "4. Canaux d'acquisition", "5. Produits et catégories", "6. Recherche Google, sans publicité",
                  "7. Fiches produits sur Google Shopping", "8. Publicité",
                  "9. Vitesse du site (données de visiteurs réels)", "10. Fiabilité des mesures et journal du mois",
                  "Notes de l'équipe", "À décider ensemble"]
    assert "Section non fournie" not in out and "section-error" not in out.split("</style>")[1]
    assert out.index("<h3>Événements du mois à garder en tête</h3>") > out.index('id="s-data_quality"')
    assert out.count("<h1>") == 1


@pytest.mark.parametrize("lang", ["fr", "en"])
def test_full_report_is_self_contained_and_scriptless(full, lang):
    full["events"].append({"date": "2026-09-09", "label": "voir https://exemple.invalid/x"})
    full["errors"].append({"source": "agency", "code": "x", "message": "see http://exemple.invalid/y"})
    low = render_report(full, lang=lang).lower()
    for forbidden in ("<script", "http://", "https://", "@import", "url(", "javascript:", "<iframe", "<link"):
        assert forbidden not in low, forbidden
    assert "voir exemple.invalid/x" in low  # the text stays readable, without its scheme


def test_event_text_with_markup_is_escaped_not_linked(full):
    full["events"] = [{"date": "2026-09-09", "label": "<a href='https://x.invalid'>x</a>"}]
    out = section("events", full)
    assert "<a " not in out and "&lt;a href=&#x27;x.invalid&#x27;&gt;x&lt;/a&gt;" in out


def test_every_chart_title_cites_a_number(full):
    out = render_report(full)
    captions = re.findall(r"<figcaption>(.*?)</figcaption>", out)
    assert len(captions) >= 12
    for cap in captions:
        assert re.search(r"\d", cap), cap
    for lead in re.findall(r'<p class="lead">(.*?)</p>', out, flags=re.S):
        assert re.search(r"\d", lead), lead


def test_every_figure_has_a_hidden_equivalent_table(full):
    out = render_report(full)
    figures = re.findall(r"<figure.*?</figure>", out, flags=re.S)
    assert figures
    for fig in figures:
        assert '<svg class="chart"' in fig and 'role="img"' in fig and '<table class="sr-only">' in fig


def test_every_new_pastille_is_double_coded(full):
    out = render_report(full)
    pastilles = re.findall(r'<span class="delta (fav|unfav|neutral|ns)"[^>]*><span aria-hidden="true">(.)</span> '
                           r'([^<]*)(?:<span class="sr-only ns-word">[^<]*</span>)?</span>', out)
    assert len(pastilles) == len(re.findall(r'class="delta ', out)) > 30
    for _, symbol, label in pastilles:
        assert symbol in "▲▼≈=" and re.search(r"[+−]|stable|nouveau|gagne|perd", label), label


def test_english_render_has_no_missing_key(full):
    out = text(render_report(full, lang="en"))
    assert not re.search(r"\b(funnel|channels|products|seo|merchant|cwv|dq|gloss|events|sec|zone)\.[a-z_]+", out)
    assert "Conversion funnel" in out and "Glossary" in out and "No event provided" not in out


def test_new_strings_exist_in_both_languages():
    prefixes = ("funnel.", "channels.", "products.", "seo.", "merchant.", "cwv.", "dq.", "gloss.", "events.", "sec.")
    fr = {k for k in STRINGS["fr"] if k.startswith(prefixes)}
    en = {k for k in STRINGS["en"] if k.startswith(prefixes)}
    assert fr == en and len(fr) > 150
    assert "source.gtm_inventory" in STRINGS["en"] and "source.ga4_realtime" in STRINGS["fr"]


def test_empty_input_never_shows_a_zero():
    out = render_report({"meta": {"client": "Vide"}, "periods": {"current": ["2026-09-01", "2026-09-30"]}})
    assert ">0<" not in out and "0 €" not in out
    body = text(out)
    for expected in ("Source non fournie : GA4", "Source non fournie : Search Console",
                     "Source non fournie : Merchant Center", "Source non fournie : PageSpeed (CrUX)",
                     "Aucun événement renseigné", "Non renseigné"):
        assert expected in body, expected


def test_report_is_deterministic(full):
    assert render_report(copy.deepcopy(full)) == render_report(copy.deepcopy(full))


# ------------------------------------------------------------------ funnel
def test_funnel_complete_rates_and_point_changes(full):
    out = section("funnel", full)
    rows = hidden_rows(out)
    assert [r[0] for r in rows] == ["Sessions", "Fiche produit vue", "Ajout au panier", "Début de paiement", "Achat"]
    counts = [8965, 4140, 720, 310, 180]
    assert [r[1] for r in rows] == ["8 965", "4 140", "720", "310", "180"]
    for i in range(1, 5):  # each rate is the step over the previous one, rounded to 0.1 %
        expected = f"{counts[i] / counts[i - 1] * 100:.1f}".replace(".", ",") + " %"
        assert rows[i][2] == expected
    assert "La plus forte perte est entre « Fiche produit vue » et « Ajout au panier » : 17,4 %" in text(out)
    assert "▲</span> +1,7 pt" in out  # 46,2 % vs 44,5 % in August
    assert "De la session à l'achat : 2,01 % (180 achats pour 8 965 sessions)" in text(out).replace(" ", " ") \
        .replace(" ", " ")
    assert "<polygon" not in out and "trapez" not in out  # bars, never a trapezoid


def test_funnel_missing_step_is_a_gap_not_a_zero(full):
    for period in ("current", "previous"):
        rows = full["sources"]["ga4"]["funnel"][period]["rows"]
        full["sources"]["ga4"]["funnel"][period]["rows"] = [r for r in rows if r["eventName"] != "add_to_cart"]
    out = section("funnel", full)
    rows = hidden_rows(out)
    assert rows[2] == ["Ajout au panier", "non mesuré", "—"]
    assert rows[3][2] == "—"  # begin_checkout / add_to_cart cannot be computed
    assert 'stroke-dasharray="4 3"' in out and "taux non calculable" in out
    assert "Étape(s) non mesurée(s) : Ajout au panier" in text(out)
    assert ">0<" not in out and "0,0 %" not in out


def test_funnel_without_previous_shows_not_provided(full):
    del full["sources"]["ga4"]["funnel"]["previous"]
    out = section("funnel", full)
    assert out.count('<span class="na">non fourni</span>') == 4 and "pt</span>" not in out


def test_funnel_sessions_fall_back_to_kpi_then_session_start():
    raw = {"sources": {"ga4": {"funnel": {"rows": [{"eventName": "view_item", "eventCount": 50}]},
                               "kpi": {"totals": {"sessions": 200}}}}}
    assert "Sessions : total GA4 de la période (chiffres clés)." in text(section("funnel", raw))
    raw = {"sources": {"ga4": {"funnel": {"rows": [{"eventName": "session_start", "eventCount": 300},
                                                   {"eventName": "view_item", "eventCount": 60}]}}}}
    out = section("funnel", raw)
    assert "session_start" in out and hidden_rows(out)[1][2] == "20,0 %"


def test_funnel_absent_failed_or_empty():
    assert "Source non fournie : GA4" in section("funnel", {})
    failed = section("funnel", {"sources": {"ga4": {"funnel": {"error": "missing_scope"}}}})
    assert "Source en erreur : GA4 (missing_scope)" in failed
    empty = section("funnel", {"sources": {"ga4": {"funnel": {"rows": [{"eventName": "page_view", "eventCount": 9}]}}}})
    assert 'class="nodata"' in empty and "Aucun des événements" in empty


def test_funnel_rates_helper_never_divides_by_a_missing_or_zero_step():
    assert funnel.rates([100, None, 10, 0, 5]) == [None, None, None, 0.0, None]


# ------------------------------------------------------------------ channels
def test_channels_sorted_bars_shares_and_attribution(full):
    out = section("channels", full)
    rows = hidden_rows(out)
    assert [r[0] for r in rows] == ["Organic Search", "Paid Search", "Email", "Direct", "Organic Social"]
    shares = [float(r[2].replace(" %", "").replace(",", ".")) for r in rows]
    assert sum(shares) == pytest.approx(100.0, abs=0.3)  # rows sum to the GA4 total here
    svg = re.search(r"<svg.*?</svg>", out, flags=re.S).group(0)
    assert svg.index("Organic Search") < svg.index("Paid Search") < svg.index("Organic Social")
    assert "9 060 €</text>" in svg  # value written at the end of the bar
    body = text(out)
    assert "« Organic Search » apporte le plus de revenu : 9 060 €, soit 34,6 % du revenu GA4" in \
        body.replace(" ", " ").replace(" ", " ")
    assert "Modèle d'attribution de la propriété : Basé sur les données (déclaré par l'agence)" in body
    assert "Revenu vs mois précédent" in body and 'class="delta ' in out


def test_channels_share_uses_the_report_total_not_the_row_sum(full):
    cur = full["sources"]["ga4"]["channels"]["current"]
    cur["rows"] = cur["rows"][:2]  # truncated list: the shares must not be inflated to 100 %
    cur["truncated"] = True
    rows = hidden_rows(section("channels", full))
    assert rows[0][2] == "34,6 %" and "Liste tronquée" in section("channels", full)


def test_channels_without_revenue_or_model(full):
    for period in ("current", "previous"):
        for row in full["sources"]["ga4"]["channels"][period]["rows"]:
            del row["purchaseRevenue"]
    del full["meta"]["ga4_attribution_model"]
    out = section("channels", full)
    body = text(out)
    assert "apporte le plus de sessions : 3 410" in body.replace(" ", " ")
    assert "Modèle d'attribution de la propriété : non renseigné" in body
    assert "0 €" not in out


def test_channels_absent_and_escaped(full):
    assert "Source non fournie : GA4" in section("channels", {})
    full["sources"]["ga4"]["channels"]["current"]["rows"][0]["sessionDefaultChannelGroup"] = XSS
    out = section("channels", full)
    assert "<script" not in out and "<img src=x" not in out and "&lt;script&gt;" in out


# ------------------------------------------------------------------ products
def test_products_top_flop_and_stockout_note(full):
    out = section("products", full)
    body = text(out)
    assert "« Donjon modulaire, kit 24 pièces » est la meilleure vente : 4 140 € pour 46 unités" in \
        body.replace(" ", " ").replace(" ", " ")
    top, flop = out.split("Ventes les plus faibles", 1)
    assert "Tour de guet" in top and "Grotte souterraine" not in top
    assert flop.index("Pont en bois") < flop.index("Table de jeu pliable") < flop.index("Grotte souterraine")
    assert "Journal, du 18 septembre 2026 au 22 septembre 2026 : Rupture de stock du decor de taverne." in body
    assert "Plus forte baisse de revenu : « Décor de taverne, set complet »" in body


def test_products_no_previous_and_xss(full):
    del full["sources"]["ga4"]["products"]["previous"]
    full["sources"]["ga4"]["products"]["current"]["rows"][0]["itemName"] = XSS
    out = section("products", full)
    assert "<script" not in out and "&lt;script&gt;alert(1)&lt;/script&gt;" in out
    assert '<span class="na">non fourni</span>' in out and "Plus forte baisse" not in out


def test_products_absent_and_quantity_only():
    assert "Source non fournie : GA4" in section("products", {})
    raw = {"sources": {"ga4": {"products": {"rows": [{"itemName": "A", "itemsPurchased": 4},
                                                     {"itemName": "B", "itemsPurchased": 9}]}}}}
    assert "« B » est le produit le plus vendu : 9 unités" in text(section("products", raw))


# ------------------------------------------------------------------ seo
def test_seo_uses_totals_never_the_row_sum(full):
    out = section("seo", full)
    body = text(out).replace(" ", " ").replace(" ", " ")
    assert "Le nombre de clics progresse de 10,7 %. 1 340 clics contre 1 210" in body
    assert ("Le total de 1 340 clics vient de Search Console et inclut les requêtes anonymisées : les 5 requêtes "
            "listées en totalisent 1 082.") in body
    assert "1 082 clics" not in body  # the row sum is quoted as such, never as a total


def test_seo_position_reads_as_places_and_ctr_in_points(full):
    body = text(section("seo", full))
    assert "gagne 0,7 place(s)" in body and "perd 1,6 place(s)" in body
    assert "+0,2 pt" in body  # text() folds no-break spaces


def test_seo_winners_and_losers(full):
    out = section("seo", full)
    fig = out.split("Gagnants et perdants", 1)[1]
    assert "« decor de jeu de role » gagne le plus de clics : +62 ; « terrain pour figurines » en perd le plus : −35" \
        in htmllib.unescape(fig)
    svg = re.search(r"<svg.*?</svg>", fig, flags=re.S).group(0)
    assert svg.count("▲") == 2 and svg.count("▼") == 3  # +62, +35 / −35, −33, −22
    assert hidden_rows(fig)[0] == ["decor de jeu de role", "412", "350", "+62"]


def test_seo_query_missing_from_a_truncated_list_is_not_a_winner(full):
    q = full["sources"]["search_console"]["queries"]
    q["current"]["rows"].append({"query": "nouvelle requete", "clicks": 90, "impressions": 900, "position": 7.0})
    q["previous"]["truncated"] = True
    out = section("seo", full)
    assert "nouvelle requete" not in out.split("Gagnants et perdants", 1)[1].split("</figure>")[0]
    assert "Une liste de requêtes est tronquée" in text(out)
    q["previous"]["truncated"] = False
    assert "nouvelle requete" in section("seo", full).split("Gagnants et perdants", 1)[1].split("</figure>")[0]


def test_seo_daily_curve_gap_and_event_bands(full):
    rows = full["sources"]["search_console"]["totals"]["current"]["rows"]
    del rows[10]
    out = section("seo", full)
    svg = re.search(r"<figure[^>]*><figcaption>Clics Google par jour.*?</svg>", out, flags=re.S).group(0)
    accent_path = re.findall(r'<path d="([^"]+)" fill="none" stroke="#1F6F5B"', svg)[0]
    assert accent_path.count("M") == 2  # a missing day breaks the line
    assert 'fill="#FFF4E0"' in svg and "Rupture de stock du decor de taverne." in svg
    assert ["2026-09-11", "—", hidden_rows(out)[10][2]] == hidden_rows(out)[10]


def test_seo_xss_in_queries_and_pages(full):
    full["sources"]["search_console"]["queries"]["current"]["rows"][0]["query"] = XSS
    full["sources"]["search_console"]["pages"]["current"]["rows"][0]["page"] = "https://x.invalid/" + XSS
    out = section("seo", full)
    assert "<script" not in out and "<img src=x" not in out and "x.invalid" not in out


def test_seo_absent_and_partial():
    assert "Source non fournie : Search Console" in section("seo", {})
    raw = {"sources": {"search_console": {"queries": {"rows": [{"query": "a", "clicks": 3, "impressions": 9,
                                                                "position": 2.0}]}}}}
    body = text(section("seo", raw))
    assert "Source non fournie : Search Console" in body and "les lignes ne sont pas additionnées" in body
    failed = section("seo", {"sources": {"search_console": {"totals": {"error": "403"}}}})
    assert "Source en erreur : Search Console (403)" in failed


# ------------------------------------------------------------------ merchant
def test_merchant_stacked_bar_labels_and_issues(full):
    out = section("merchant", full)
    svg = re.search(r"<svg.*?</svg>", out, flags=re.S).group(0)
    xs = [(float(x), float(w)) for x, w in re.findall(r'<rect x="([\d.]+)" y="0" width="([\d.]+)"', svg)]
    assert len(xs) == 3
    assert xs[1][0] == pytest.approx(xs[0][0] + xs[0][1] + 2, abs=0.11)  # 2 px gap
    assert xs[2][0] == pytest.approx(xs[1][0] + xs[1][1] + 2, abs=0.11)
    assert "Approuvées 2 432" in svg and "Limitées 38" in svg and "Refusées 112" in svg
    shares = [float(r[2].replace(" %", "").replace(",", ".")) for r in hidden_rows(out)]
    assert sum(shares) == pytest.approx(100.0, abs=0.2)
    assert "112 fiches refusées sur 2 582 (4,3 %)" in text(out).replace(" ", " ").replace(" ", " ")
    assert '<span class="delta unfav"><span aria-hidden="true">▲</span> +9 (+8,7 %)</span>' in out
    assert "Poids du produit trop eleve" in out and "refus" in out


def test_merchant_any_rise_of_disapproved_is_coloured(full):
    full["sources"]["merchant_center"]["statuses"] = {"approved": 10000, "limited": 10, "disapproved": 1005,
                                                      "previous": {"approved": 10000, "limited": 10,
                                                                   "disapproved": 1000}}
    out = section("merchant", full)
    assert '<span class="delta unfav"><span aria-hidden="true">▲</span> +5 (+0,5 %)</span>' in out


def test_merchant_without_disapproved(full):
    full["sources"]["merchant_center"] = {"statuses": {"approved": 500, "limited": 4, "disapproved": 0},
                                          "product_issues": {"products": [], "scanned": 504, "truncated": False}}
    out = section("merchant", full)
    body = text(out)
    assert "Aucune fiche refusée sur 504" in body.replace(" ", " ")
    svg = re.search(r"<svg.*?</svg>", out, flags=re.S).group(0)
    assert "Refusées" not in svg and len(re.findall(r'<rect x="[\d.]+" y="0"', svg)) == 2
    assert "Aucun problème signalé sur les 504 fiches analysées." in body


def test_merchant_issues_only_and_absent(full):
    del full["sources"]["merchant_center"]["statuses"]
    out = section("merchant", full)
    assert "<svg" not in out and "Approuvées et limitées non fournies" in text(out)
    assert "Source non fournie : Merchant Center" in section("merchant", {})
    failed = section("merchant", {"sources": {"merchant_center": {"product_issues": {"error": "missing_scope"}}}})
    assert "Source en erreur : Merchant Center (missing_scope)" in failed


def test_merchant_issue_xss(full):
    full["sources"]["merchant_center"]["product_issues"]["products"][0]["issues"][0]["description"] = XSS
    out = section("merchant", full)
    assert "<script" not in out and "&lt;script&gt;" in out


# ------------------------------------------------------------------ cwv
@pytest.mark.parametrize("metric,value,zone", [
    ("lcp", 2.5, "good"), ("lcp", 2.51, "needs_improvement"), ("lcp", 4.0, "needs_improvement"),
    ("lcp", 4.01, "poor"), ("inp", 200, "good"), ("inp", 201, "needs_improvement"),
    ("inp", 500, "needs_improvement"), ("inp", 501, "poor"), ("cls", 0.1, "good"),
    ("cls", 0.11, "needs_improvement"), ("cls", 0.25, "needs_improvement"), ("cls", 0.26, "poor"),
])
def test_cwv_zones_at_the_exact_thresholds(metric, value, zone):
    assert cwv_zone(metric, value) == zone
    raw = {"sources": {"web_vitals": [{"url": "/", "strategy": "mobile", "field": {metric: value}}]}}
    out = section("cwv", raw)
    label = {"good": "Bon", "needs_improvement": "À améliorer", "poor": "Mauvais"}[zone]
    assert f"zone « {label} »" in htmllib.unescape(out)
    assert f'<span class="pill {"ok" if zone == "good" else "warn"}">{htmllib.escape(label)}</span>' in out


def test_cwv_field_card_has_marker_threshold_bar_and_text(full):
    out = section("cwv", full)
    body = text(out)
    assert "1 mesure(s) terrain sur 3 hors de la zone « Bon » : INP 240 ms (à améliorer)" in \
        body.replace(" ", " ")
    assert out.count("Bon jusqu&#x27;à 2,5 s, mauvais au-delà de 4 s.") == 1
    svg = re.search(r"<figure[^>]*><figcaption>INP au 75e percentile.*?</svg>", out, flags=re.S).group(0)
    assert 'stroke="#14201B" stroke-width="2"' in svg and "240 ms</text>" in svg  # value marker
    assert all(name in svg for name in ("Bon", "À améliorer", "Mauvais"))
    assert "Mois précédent : 2,5 s" in body.replace(" ", " ") and "▼</span> −8,0" in out


def test_cwv_lab_data_alone_has_no_verdict():
    raw = {"sources": {"web_vitals": {"performance": 61, "seo": 92, "lcp": "3.9 s", "cls": "0.02",
                                      "tbt": "380 ms", "field_data": None}}}
    out = section("cwv", raw)
    body = text(out)
    assert "Données de laboratoire, à ne pas comparer aux seuils" in body
    # lab values in the report's number format (French decimal comma), never the API's "3.9 s"
    assert "3,9 s" in body and "3.9 s" not in body  # text() turns the no-break space into a space
    assert "Pas de mesure terrain : 1 page(s) testée(s) en laboratoire seulement" in body
    for verdict in ("Bon", "À améliorer", "Mauvais", "zone «"):
        assert verdict not in body.replace("Bon jusqu", ""), verdict
    assert "<svg" not in out


def test_cwv_raw_crux_metrics_are_read():
    crux = {"metrics": {"LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 4100},
                        "INTERACTION_TO_NEXT_PAINT": {"percentile": 180},
                        "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 25}}}
    assert cwv.field_values({"loadingExperience": crux}) == {"lcp": pytest.approx(4.1), "inp": 180.0,
                                                             "cls": pytest.approx(0.25)}
    out = section("cwv", {"sources": {"web_vitals": [{"url": "/p", "loadingExperience": crux}]}})
    assert "zone « Mauvais »" in htmllib.unescape(out) and "4,1 s" in out


def test_cwv_absent_error_and_missing_metric():
    assert "Source non fournie : PageSpeed (CrUX)" in section("cwv", {})
    out = section("cwv", {"sources": {"web_vitals": [{"url": "/a", "error": "network"},
                                                     {"url": "/b", "field": {"lcp": 1.8}}]}})
    body = text(out)
    assert "Mesure en erreur (network)" in body and "Les 1 mesures terrain sont dans la zone « Bon »" in body
    assert body.count("non fourni") == 2  # INP and CLS of /b: not provided, never 0


def test_cwv_url_is_shortened_and_escaped():
    out = section("cwv", {"sources": {"web_vitals": [{"url": "https://shop.invalid/p?x=" + XSS,
                                                      "field": {"lcp": 2.0}}]}})
    assert "shop.invalid" not in out and "<script" not in out and "/p?x" not in out and ">/p<" in out


# ------------------------------------------------------------------ data quality
def test_data_quality_reports_removed_personal_data_and_failures(full):
    full["sources"]["ga4"]["kpi"]["current"]["rows"][0]["transactionId"] = "T-1"
    full["sources"]["google_ads"]["previous"] = {"error": "rate_limited"}
    full["errors"].append({"source": "agency", "code": "note", "message": XSS})
    out = section("data_quality", full)
    body = text(out)
    assert "1 lecture(s) en erreur : Google Ads (rate_limited)." in body
    assert "1 retrait(s) de données personnelles avant le rendu" in body
    assert "personal data key removed: ga4.kpi.current.rows[].transactionId (1x)" in body
    assert "T-1" not in out and "<script" not in out and "&lt;script&gt;" in out
    assert "point(s) à voir sur" in body


def test_data_quality_consent_only_when_provided(full):
    assert "Non renseigné : ni la bannière ni le Consent Mode" in text(section("data_quality", full))
    full["meta"]["consent_mode"] = "avancé"
    assert "Consent Mode : avancé." in text(section("data_quality", full))
    del full["meta"]["consent_mode"]
    full["notes"] += "\n- Bannière de consentement changée le 3"
    assert "Mentionné dans les notes de l'équipe" in text(section("data_quality", full))


def test_data_quality_gtm_duplicates_and_realtime(full):
    tags = full["sources"]["gtm_inventory"]["tags"]
    tags.append({"name": "GA4 config bis", "type": "googtag", "paused": False, "parameters": {"tagId": "G-XXXX"}})
    tags.append({"name": "GA4 purchase bis", "type": "gaawe", "paused": False, "parameters": {"eventName": "purchase"}})
    body = text(section("data_quality", full))
    assert "1 identifiant(s) Google porté(s) par plusieurs balises actives" in body
    assert "2 balises GA4 actives envoient l'événement purchase" in body
    assert "3 utilisateur(s) actif(s), 7 page(s) vue(s)" in body
    assert "Version publiée : 12." in body and "G-XXXX" not in body


def test_data_quality_without_any_source():
    body = text(section("data_quality", {}))
    assert "Aucune source fournie." in body and "Inventaire Tag Manager non fourni." in body
    assert "GA4 peut masquer les lignes de petit volume" in body and "Glossaire" in body


def test_settling_days_are_flagged(full):
    dr = full["sources"]["ga4"]["kpi"]["current"]["date_range"]
    dr.update({"data_complete": False, "settling_from": "2026-09-29"})
    assert "Derniers jours encore révisables : GA4 à partir du 29 septembre 2026." in \
        text(section("data_quality", full))


# ------------------------------------------------------------------ events
def test_events_sorted_by_date_and_escaped(full):
    full["events"].append({"date": "2026-09-01", "label": XSS})
    full["events"].append({"label": "sans date"})
    out = section("events", full)
    dates = [htmllib.unescape(d) for d in re.findall(r'<th scope="row">(.*?)</th>', out)]
    assert dates == ["1er septembre 2026", "5 septembre 2026", "du 18 septembre 2026 au 22 septembre 2026",
                     "27 septembre 2026", "date non fournie"]
    assert "<script" not in out and "&lt;script&gt;" in out


def test_events_empty_is_a_nodata_block():
    assert section("events", {"events": []}) == '<p class="nodata">Aucun événement renseigné</p>'
    assert section("events", {}, lang="en") == '<p class="nodata">No event provided</p>'


def test_event_helper_ignores_entries_without_label():
    data = sanitize({"events": [{"date": "2026-09-02"}, {"date": "2026-09-03", "label": " x "}]})
    evts = _common.events(data)
    assert [e.label for e in evts] == ["x"]


# ------------------------------------------------------------------ project rules
def test_new_section_modules_parse_as_python_310():
    from test_report_render import _fstring_310_violations

    files = [SECTIONS_DIR / f"{sid}.py" for sid in NEW] + [SECTIONS_DIR / "_common.py"]
    for path in files:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path), feature_version=(3, 10))
        assert not any(isinstance(node, ast.Match) for node in ast.walk(tree)), path.name
        assert _fstring_310_violations(source) == [], path.name

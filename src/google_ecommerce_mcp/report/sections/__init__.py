"""Report sections: the registry, the section contract, and two complete reference sections (kpi, ads).

Section contract
================

A section is a module ``google_ecommerce_mcp.report.sections.<id>`` (``<id>`` from SECTION_IDS) exposing:

``ORDER: int``
    Sort key; the defaults are in DEFAULT_ORDER (kpi 20 ... events 110). Sections are numbered in this
    order after the summary (1.), which the renderer owns.
``render(data: dict, ctx: ReportContext) -> str``
    The section BODY as HTML that is already safe: every value from ``data`` must go through ``ctx.esc``
    or one of the ctx formatters, which all escape. The renderer adds ``<section>`` and the numbered
    ``<h2>`` title (``ctx.t("section.<id>")``). A missing or failed source must render ``ctx.unavailable``
    / ``ctx.nodata`` (a ``.nodata`` block), never a zero. An exception is caught and shown as a section
    error; do not rely on it.
``SUBSECTION: bool`` (optional)
    True renders the body as an ``<h3>`` sub-block of the previous section, unnumbered (``events`` sits
    inside ``data_quality``, as in the mock-up).

``data`` is the sanitised input (see contract.py for the shape of each source and ``contract.view`` to
read one). ``ctx`` (render.ReportContext) offers: ``esc``, ``t`` / ``t_raw``, ``money``, ``pct``,
``integer``, ``number``, ``na``, ``delta(cur, prev, metric, n=, counts=, small=, currency=)`` (the
double-coded change pastille with polarity and 2-sigma noise rules, see compare.py), ``headline`` (the
title-message of a chart), ``nodata``, ``unavailable``, ``view``, ``table`` (th scope + scroll container),
``figure`` (chart + hidden data table), ``kpi_card``, ``confidence`` / ``confidence_badge``, the period
labels, ``short_url`` and ``ctx.svg`` (sparkline, column_chart, line_chart, hbar_chart, diverging_bars,
stacked_100, threshold_bar). Charts: bars from zero, no pie, no 3D, no dual axis, at most 4 series, a gap
(not a zero) for a missing value.

A section module that does not exist yet is replaced by a placeholder showing "Section non fournie". The
``kpi`` and ``ads`` sections are implemented below, as references, by objects that follow the same
contract; a ``sections/kpi.py`` or ``sections/ads.py`` module would take precedence over them.
"""

from __future__ import annotations

import importlib
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Callable

from .. import contract as c
from ..compare import fmt_delta_pct
from ..i18n import fmt_date, fmt_int, fmt_money, fmt_number, fmt_pct, tr
from ..period import parse_date, shift_year
from ..summary import ORDER_KEYS, REVENUE_KEYS, failed_platforms, first_num, ga4_revenue, mer

if TYPE_CHECKING:  # pragma: no cover
    from ..render import ReportContext

SECTION_IDS: tuple[str, ...] = ("kpi", "funnel", "channels", "products", "seo", "merchant", "ads", "cwv",
                                "data_quality", "events")
DEFAULT_ORDER: dict[str, int] = {sid: 20 + 10 * i for i, sid in enumerate(SECTION_IDS)}
SUBSECTIONS = frozenset({"events"})


class Placeholder:
    """Stands for a section module not written yet: a .nodata block 'Section non fournie'."""

    def __init__(self, sid: str) -> None:
        self.sid = sid
        self.ORDER = DEFAULT_ORDER.get(sid, 999)
        self.SUBSECTION = sid in SUBSECTIONS
        self.placeholder = True

    def render(self, data: dict, ctx: "ReportContext") -> str:
        return ctx.nodata(tr(ctx.lang, "nodata.section_missing"))


class _Section:
    """A built-in section object with the module contract (ORDER, render, SUBSECTION)."""

    def __init__(self, sid: str, render: Callable[[dict, "ReportContext"], str]) -> None:
        self.sid = sid
        self.ORDER = DEFAULT_ORDER[sid]
        self.SUBSECTION = False
        self.render = render


# ============================================================================================== KPI section
def _ga4_values(totals: dict) -> dict[str, float | None]:
    revenue = first_num(totals, REVENUE_KEYS)
    orders = first_num(totals, ORDER_KEYS)
    sessions = c.num(totals.get("sessions"))
    return {
        "revenue": revenue,
        "orders": orders,
        "aov": revenue / orders if revenue is not None and orders else None,
        "conversion_rate": orders / sessions if orders is not None and sessions else None,
        "sessions": sessions,
        "new_customers": c.num(totals.get("firstTimePurchasers")),
    }


MAX_SPARK_DAYS = 120


def _day(value: Any) -> date | None:
    """A GA4 'date' dimension value: YYYY-MM-DD or GA4's own YYYYMMDD."""
    if isinstance(value, str) and len(value) == 8 and value.isdigit():
        value = f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return parse_date(value)


def _by_day(node: Any, metric: str, years: int = 0) -> dict[date, float | None]:
    """{day: value of ``metric``} from a ga4_report run with the 'date' dimension, days shifted by ``years``
    (N-1 rows are moved one year forward to sit on the same calendar day); {} without daily rows."""
    out: dict[date, float | None] = {}
    for row in c.rows(node):
        day = _day(row.get("date"))
        if day is None:
            return {}
        out[shift_year(day, years) if years else day] = _ga4_values(row).get(metric)
    return out


def _daily(cur_node: Any, ref_node: Any, metric: str) -> tuple[list[tuple[str, float | None]], list[float | None]]:
    """Every day from the first to the last current row (a missing day is a None: a gap, never a zero), and
    the N-1 values aligned BY DATE (same calendar day one year earlier), not by index."""
    current = _by_day(cur_node, metric)
    if len(current) < 2:
        return [], []
    start, end = min(current), max(current)
    if (end - start).days >= MAX_SPARK_DAYS:
        return [], []
    reference = _by_day(ref_node, metric, years=1) if c.state(ref_node) == "ok" else {}
    days, ref, day = [], [], start
    while day <= end:
        days.append((day.isoformat(), current.get(day)))
        ref.append(reference.get(day))
        day += timedelta(days=1)
    return days, ref if any(v is not None for v in ref) else []


def _fmt_kpi(ctx: "ReportContext", metric: str, value: Any, currency: str | None, orders: Any = None) -> str:
    """Plain text of a KPI value."""
    if value is None:
        return "—"
    if metric in ("revenue", "aov"):
        return fmt_money(value, currency, ctx.lang)
    if metric == "conversion_rate":
        return fmt_pct(value, 1, ctx.lang)
    if metric == "new_customers" and c.is_num(orders):
        return tr(ctx.lang, "kpi.new_of", new=fmt_int(value, ctx.lang), orders=fmt_int(orders, ctx.lang))
    return fmt_int(value, ctx.lang)


KPI_METRICS = (("revenue", "kpi.revenue"), ("orders", "kpi.orders"), ("aov", "kpi.aov"),
               ("conversion_rate", "kpi.conversion"), ("sessions", "kpi.sessions"),
               ("new_customers", "kpi.new_customers"))


def render_kpi(data: dict, ctx: "ReportContext") -> str:
    """Section 2: revenue, orders, AOV, conversion rate, sessions, new customers; each against the previous
    period, N-1 and the target, with a sparkline when the GA4 report has daily rows."""
    cur_node = ctx.view("ga4", "kpi")
    missing = ctx.unavailable(cur_node, "ga4")
    if missing:
        return missing
    prev_node, yoy_node = ctx.view("ga4", "kpi", "previous"), ctx.view("ga4", "kpi", "yoy")
    cur, prev, yoy = (_ga4_values(c.totals(n)) for n in (cur_node, prev_node, yoy_node))
    _, currency, _ = ga4_revenue(data)
    prev_ok, yoy_ok = c.state(prev_node) == "ok", c.state(yoy_node) == "ok"
    sessions_n = (cur["sessions"], prev["sessions"])
    cards = []
    for metric, key in KPI_METRICS:
        if metric == "new_customers" and cur[metric] is None:
            continue
        label = tr(ctx.lang, key)
        value_text = _fmt_kpi(ctx, metric, cur[metric], currency, cur["orders"])
        kwargs: dict[str, Any] = {}
        if metric == "conversion_rate":
            kwargs["n"] = sessions_n
        elif metric == "revenue":  # AOV: basket variance unknown, 5 % fallback (see compare.py)
            kwargs["counts"] = (cur["orders"], prev["orders"])
        lines = []
        for ok, label_raw, other in ((prev_ok, ctx.prev_label_raw(), prev), (yoy_ok, ctx.yoy_label_raw(), yoy)):
            other_text = _fmt_kpi(ctx, metric, other[metric], currency, other["orders"]) if ok else "—"
            if ok and other[metric] is not None and cur[metric] is not None:
                y_kwargs = dict(kwargs)
                if "n" in y_kwargs:
                    y_kwargs["n"] = (cur["sessions"], other["sessions"])
                if "counts" in y_kwargs:
                    y_kwargs["counts"] = (cur["orders"], other["orders"])
                pastille = ctx.delta(cur[metric], other[metric], metric, currency=currency, **y_kwargs)
                lines.append(f"{ctx.esc(label_raw)} : {ctx.esc(other_text)} {pastille}" if ctx.lang == "fr"
                             else f"{ctx.esc(label_raw)}: {ctx.esc(other_text)} {pastille}")
            else:
                lines.append(f"{ctx.esc(label_raw)}{' :' if ctx.lang == 'fr' else ':'} {ctx.na()}")
        goal = ctx.goals.get(metric)
        goal_label = tr(ctx.lang, "kpi.goal") + (" :" if ctx.lang == "fr" else ":")
        if goal is not None and cur[metric] is not None:
            gap = fmt_delta_pct(cur[metric], goal, ctx.lang, digits=1)
            lines.append(f"{ctx.esc(goal_label)} {ctx.esc(_fmt_kpi(ctx, metric, goal, currency))} "
                         f"({ctx.esc(gap)})")
        else:
            lines.append(f"{ctx.esc(goal_label)} {ctx.t('kpi.no_goal')}")

        extra = ""
        series, ref = _daily(cur_node, yoy_node, metric) if metric != "new_customers" else ([], [])
        if len(series) > 1:
            svg = ctx.svg.sparkline([v for _, v in series], ref or None, value_label=value_text,
                                    label=tr(ctx.lang, "kpi.spark", label=label))
            title = ctx.headline(metric, label, cur[metric], prev[metric] if prev_ok else None, value_text, **kwargs)
            table_rows = [(day, _fmt_kpi(ctx, metric, v, currency),
                           _fmt_kpi(ctx, metric, ref[i], currency) if i < len(ref) else "—")
                          for i, (day, v) in enumerate(series)]
            extra = ctx.figure(title, svg, [tr(ctx.lang, "chart.day"), tr(ctx.lang, "chart.period"),
                                            tr(ctx.lang, "chart.yoy")], table_rows)
        value_html = ctx.esc(value_text) if cur[metric] is not None else ctx.t("kpi.not_measured")
        cards.append(ctx.kpi_card(label, value_html, lines, extra=extra))
    cards.append(ctx.kpi_card(tr(ctx.lang, "kpi.margin"), ctx.t("kpi.margin_value"),
                              [ctx.t("kpi.margin_note")], todo=True))

    notes = []
    if c.is_num(cur["orders"]) and cur["orders"] < 500:
        notes.append(ctx.t("kpi.low_volume", orders=fmt_int(cur["orders"], ctx.lang)))
    dr = cur_node.get("date_range") if isinstance(cur_node.get("date_range"), dict) else {}
    start, end = dr.get("start"), dr.get("end")
    if parse_date(start) and parse_date(end):
        notes.append(ctx.t("kpi.source", range=tr(ctx.lang, "period.range", start=fmt_date(parse_date(start), ctx.lang),
                                                  end=fmt_date(parse_date(end), ctx.lang))))
    if dr.get("data_complete") is False and isinstance(dr.get("settling_from"), str):
        notes.append(ctx.t("kpi.settling", date=dr["settling_from"]))
    return (f'<div class="grid">{"".join(cards)}</div>'
            + "".join(f'<p class="note" role="note">{n}</p>' for n in notes))


# ============================================================================================== ads section
#: (overview key, source name, metrics shown in the card as (normalised key, label key)).
PLATFORM_CARDS: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    ("google_ads", "google_ads", (("spend", "ads.m.spend"), ("clicks", "ads.m.clicks"), ("ctr", "ads.m.ctr"),
                                  ("cpc", "ads.m.cpc"), ("conversions", "ads.m.conversions"),
                                  ("value", "ads.m.value"), ("roas", "ads.m.roas"), ("cpa", "ads.m.cpa"))),
    ("meta", "meta_ads", (("spend", "ads.m.spend"), ("reach", "ads.m.reach"), ("frequency", "ads.m.frequency"),
                          ("cpm", "ads.m.cpm"), ("conversions", "ads.m.purchases"), ("value", "ads.m.value"),
                          ("roas", "ads.m.roas"), ("cpa", "ads.m.cpp"))),
    ("openai_ads", "openai_ads", (("spend", "ads.m.spend"), ("clicks", "ads.m.clicks"), ("ctr", "ads.m.ctr"),
                                  ("cpc", "ads.m.cpc"), ("conversions", "ads.m.conversions"),
                                  ("value", "ads.m.sales"), ("roas", "ads.m.roas"), ("cpa", "ads.m.cpa"))),
)
MONEY_METRICS = frozenset({"spend", "cpc", "cpm", "value", "cpa"})
RATIO_METRICS = frozenset({"roas", "frequency"})
CHART_METRICS = (("spend", "ads.m.spend"), ("clicks", "ads.m.clicks"), ("conversions", "ads.m.conversions"),
                 ("roas", "ads.m.roas"))


def _ratio(a: Any, b: Any) -> float | None:
    return a / b if c.is_num(a) and c.is_num(b) and b else None


def _normalise(overview_key: str, node: Any, *, from_overview: bool) -> dict:
    """One platform's figures under common keys, from its own tool output or from its ads_overview block.

    Only ratios WITHIN the platform are derived (CTR, CPC, CPA) when the tool did not return them; nothing
    is ever combined across platforms."""
    t = node if from_overview else c.totals(node)
    get = lambda k: c.num(t.get(k))  # noqa: E731
    if overview_key == "meta" and not from_overview:
        conversions, value, roas = get("purchases"), get("purchase_value"), get("roas")
    elif overview_key == "openai_ads" and not from_overview:
        conversions, value, roas = get("conversions"), get("order_created_attributed_sales"), \
            get("order_created_roas")
    else:
        conversions, value = get("conversions"), get("conversion_value")
        roas = get("roas_reported_by_platform") if from_overview else get("roas")
    out = {
        "spend": get("spend"), "impressions": get("impressions"), "clicks": get("clicks"),
        "reach": get("reach"), "frequency": get("frequency"), "cpm": get("cpm"),
        "ctr": get("ctr"), "cpc": get("cpc"), "cpa": get("cpa"),
        "conversions": conversions, "value": value, "roas": roas,
    }
    out["ctr"] = out["ctr"] if out["ctr"] is not None else _ratio(out["clicks"], out["impressions"])
    out["cpc"] = out["cpc"] if out["cpc"] is not None else _ratio(out["spend"], out["clicks"])
    out["cpa"] = out["cpa"] if out["cpa"] is not None else _ratio(out["spend"], out["conversions"])
    meta_node = node if isinstance(node, dict) else {}
    tot = t if isinstance(t, dict) else {}
    out["currency"] = meta_node.get("currency") if isinstance(meta_node.get("currency"), str) else None
    out["attribution"] = meta_node.get("attribution")
    out["totals_complete"] = meta_node.get("totals_complete")
    out["no_activity"] = meta_node.get("no_activity") is True or tot.get("no_activity") is True
    out["purchase_measurement"] = meta_node.get("purchase_measurement")
    return out


def _window_text(ctx: "ReportContext", overview_key: str, attribution: Any) -> str:
    """Plain text of the attribution window the platform reported."""
    if overview_key == "google_ads":
        return tr(ctx.lang, "ads.window.google")
    att = attribution if isinstance(attribution, dict) else {}
    parts = []
    windows = att.get("windows")
    if isinstance(windows, list):
        for w in windows:
            if not isinstance(w, str) or "_" not in w:
                continue
            days, kind = w.split("_", 1)
            key = {"click": "ads.win.click", "view": "ads.win.view", "ev": "ads.win.ev"}.get(kind)
            if key and days.rstrip("d").isdigit():
                parts.append(tr(ctx.lang, key, n=days.rstrip("d")))
    if c.is_num(att.get("click_window_days")):
        parts.append(tr(ctx.lang, "ads.win.click", n=fmt_int(att["click_window_days"], ctx.lang)))
    if c.is_num(att.get("view_window_days")):
        parts.append(tr(ctx.lang, "ads.win.view", n=fmt_int(att["view_window_days"], ctx.lang)))
    return ", ".join(parts) if parts else tr(ctx.lang, "ads.window.unknown")


def _fmt_metric(ctx: "ReportContext", key: str, value: Any, currency: str | None) -> str:
    if value is None:
        return ctx.na()
    if key in MONEY_METRICS:
        return ctx.money(value, currency)
    if key == "ctr":
        return ctx.pct(value, 1)
    if key in RATIO_METRICS:
        return ctx.number(value, 1)
    if key == "conversions":
        return ctx.number(value, 0) if float(value).is_integer() else ctx.number(value, 1)
    return ctx.integer(value)


def _platform(ctx: "ReportContext", overview: Any, overview_key: str, source: str) -> tuple[dict | None, str]:
    """(normalised figures or None, status) for one platform: ok, error:<code>, not_configured or absent."""
    own = ctx.view(source)
    block = None
    if c.state(overview) == "ok" and isinstance(overview.get("platforms"), dict):
        block = overview["platforms"].get(overview_key)
    if c.state(own) == "ok":
        return _normalise(overview_key, own, from_overview=False), "ok"
    if isinstance(block, dict) and block.get("status") == "ok":
        return _normalise(overview_key, block, from_overview=True), "ok"
    if c.state(own) == "error":
        return None, "error:" + c.error_code(own)
    if isinstance(block, dict):
        if block.get("status") == "not_configured":
            return None, "not_configured"
        return None, "error:" + str(block.get("error") or block.get("status") or "?")[:60]
    return None, "absent"


def _previous_spend(ctx: "ReportContext", overview_key: str, source: str) -> tuple[float | None, str | None]:
    own = ctx.view(source, period="previous")
    if c.state(own) == "ok":
        n = _normalise(overview_key, own, from_overview=False)
        return n["spend"], n["currency"]
    ov = ctx.view("ads_overview", period="previous")
    if c.state(ov) == "ok" and isinstance(ov.get("platforms"), dict):
        block = ov["platforms"].get(overview_key)
        if isinstance(block, dict) and block.get("status") == "ok":
            return c.num(block.get("spend")), block.get("currency")
    return None, None


def _platform_card(ctx: "ReportContext", overview_key: str, source: str, metrics: tuple[tuple[str, str], ...],
                   figures: dict | None, status: str) -> str:
    name = ctx.esc(tr(ctx.lang, f"source.{source}"))
    if figures is None:
        text = (tr(ctx.lang, "ads.not_configured") if status == "not_configured"
                else tr(ctx.lang, "ads.platform_error", code=status.split(":", 1)[-1]))
        return (f'<article class="card"><div class="platform-head"><h3>{name}</h3></div>'
                f'{ctx.nodata(text)}</article>')
    cur = figures["currency"]
    measured = figures["purchase_measurement"]
    not_measured = isinstance(measured, dict) and measured.get("measured") is False and not figures["no_activity"]
    rows = []
    for key, label in metrics:
        if not_measured and key in ("conversions", "value", "roas", "cpa"):
            value_html = f'<span class="na">{ctx.t("kpi.not_measured")}</span>'
        else:
            value_html = _fmt_metric(ctx, key, figures[key], cur)
        rows.append(f"<dt>{ctx.t(label)}</dt><dd>{value_html}</dd>")
    spend, conv = figures["spend"], figures["conversions"]
    if figures["no_activity"]:
        takeaway = ctx.t("ads.insight_idle")
    elif spend is not None and conv is not None and conv > 0:
        takeaway = ctx.t("ads.insight", spend=fmt_money(spend, cur, ctx.lang),
                         conversions=fmt_number(conv, 0 if float(conv).is_integer() else 1, ctx.lang))
    elif spend is not None and conv == 0:
        takeaway = ctx.t("ads.insight_no_conv", spend=fmt_money(spend, cur, ctx.lang))
    else:
        takeaway = ctx.t("ads.insight_unknown")
    if not_measured:
        note = measured.get("note") if isinstance(measured.get("note"), str) and measured.get("note") else \
            tr(ctx.lang, "ads.not_measured_default")
        takeaway += " " + ctx.t("ads.not_measured", note=note)
    prev_spend, prev_cur = _previous_spend(ctx, overview_key, source)
    if prev_spend is not None and spend is not None and (prev_cur or cur) == cur:
        takeaway += (" " + ctx.t("ads.prev_spend", prev=fmt_money(prev_spend, cur, ctx.lang)) + " "
                     + ctx.delta(spend, prev_spend, "spend", currency=cur, small=False))
    if figures["totals_complete"] is False:
        takeaway += " " + ctx.t("ads.partial")
    window = ctx.t("ads.window", text=_window_text(ctx, overview_key, figures["attribution"]))
    return (f'<article class="card"><div class="platform-head"><h3>{name}</h3><span class="meta">{window}</span>'
            f'</div><dl class="metrics">{"".join(rows)}</dl>'
            f'<p class="takeaway"><strong>{ctx.t("ads.takeaway")}</strong> {takeaway}</p></article>')


def _metric_charts(ctx: "ReportContext", shown: list[tuple[str, dict]]) -> str:
    """One small chart PER metric (never ROAS and conversions together), same scale for every platform."""
    if len(shown) < 2:
        return ""
    figures_html = []
    currencies = {f["currency"] for _, f in shown}
    for key, label_key in CHART_METRICS:
        if key == "spend" and (len(currencies) != 1 or None in currencies):
            continue
        items = [(tr(ctx.lang, f"source.{src}"), f[key]) for src, f in shown if c.is_num(f[key])]
        if len(items) < 2:
            continue
        cur = next(iter(currencies)) if len(currencies) == 1 else None
        def text(v: float, key: str = key, cur: str | None = cur) -> str:
            if key == "spend":
                return fmt_money(v, cur, ctx.lang)
            if key == "roas":
                return fmt_number(v, 1, ctx.lang)
            return fmt_number(v, 0 if float(v).is_integer() else 1, ctx.lang)
        label = tr(ctx.lang, label_key)
        best = max(items, key=lambda it: it[1])
        title = tr(ctx.lang, "ads.chart", label=label, platform=best[0], value=text(best[1]))
        svg = ctx.svg.hbar_chart([(n, v, text(v)) for n, v in items], ctx.accent, label=title, sort=False,
                                 width=420)
        figures_html.append(ctx.figure(title, svg, [tr(ctx.lang, "chart.platform"), label],
                                       [(n, text(v)) for n, v in items]))
    return f'<div class="grid wide">{"".join(figures_html)}</div>' if figures_html else ""


def _platform_names(ctx: "ReportContext", sources: Any) -> str:
    """'Meta Ads, ChatGPT Ads' from source keys (plain text)."""
    if isinstance(sources, str):
        return sources
    return ", ".join(tr(ctx.lang, f"source.{s}") for s in sources)


def warning_text(data: dict, lang: str) -> str:
    """The attribution warning: ads_overview's own ``warning`` when present, else the fixed default text."""
    overview = c.view(data, "ads_overview")
    if isinstance(overview, dict) and isinstance(overview.get("warning"), str) and overview["warning"].strip():
        return overview["warning"].strip()
    return tr(lang, "ads.warning_default")


def render_ads(data: dict, ctx: "ReportContext") -> str:
    """Section 8: MER (GA4 revenue / combined spend), total spend, one card per platform, per-metric charts,
    and the attribution warning (role="note") whenever a platform is shown."""
    overview = ctx.view("ads_overview")
    cards, shown = [], []
    for overview_key, source, metrics in PLATFORM_CARDS:
        figures, status = _platform(ctx, overview, overview_key, source)
        if status == "absent":
            continue
        cards.append(_platform_card(ctx, overview_key, source, metrics, figures, status))
        if figures is not None:
            shown.append((source, figures))
    if not cards and c.state(overview) == "missing":
        return ctx.nodata(tr(ctx.lang, "ads.no_platform"))

    m = mer(data)
    if m.value is not None:
        detail = ctx.t("ads.mer_detail", revenue=fmt_money(m.revenue, m.currency, ctx.lang),
                       spend=fmt_money(m.spend, m.currency, ctx.lang))
        mer_card = (f'<div class="card dark"><div class="l">{ctx.t("ads.mer_label")}</div>'
                    f'<div class="fig-value mer">{ctx.number(m.value, 1)}</div>'
                    f'<p class="meta">{detail}</p></div>')
    else:
        args = {k: _platform_names(ctx, v) if k == "platforms" else v for k, v in m.reason_args.items()}
        reason = tr(ctx.lang, m.reason or "ads.reason.no_spend", **args)
        mer_card = (f'<div class="card"><div class="l">{ctx.t("ads.mer_label")}</div>'
                    f'{ctx.nodata(tr(ctx.lang, "ads.mer_none", reason=reason))}</div>')
    combined = overview.get("combined") if c.state(overview) == "ok" else None
    if isinstance(combined, dict) and c.is_num(combined.get("spend")):
        cur = combined.get("currency") if isinstance(combined.get("currency"), str) else None
        included = combined.get("platforms_included")
        lines = []
        if isinstance(included, list):
            lines.append(ctx.t("ads.spend_detail", count=fmt_int(len(included), ctx.lang)))
        failed = failed_platforms(overview)
        if failed:
            lines.append(ctx.t("ads.spend_incomplete", platforms=_platform_names(ctx, failed)))
        prev = ctx.view("ads_overview", period="previous")
        prev_comb = prev.get("combined") if c.state(prev) == "ok" else None
        if isinstance(prev_comb, dict) and c.is_num(prev_comb.get("spend")) and prev_comb.get("currency") == cur:
            lines.append(ctx.t("ads.spend_prev", prev=fmt_money(prev_comb["spend"], cur, ctx.lang)) + " "
                         + ctx.delta(combined["spend"], prev_comb["spend"], "spend", currency=cur, small=False))
        spend_card = ctx.kpi_card(tr(ctx.lang, "ads.spend_label"), ctx.money(combined["spend"], cur), lines)
    else:
        reason = overview.get("combined_reason") if c.state(overview) == "ok" else None
        spend_card = (f'<div class="card"><div class="l">{ctx.t("ads.spend_label")}</div>'
                      f'{ctx.nodata(str(reason) if reason else tr(ctx.lang, "ads.reason.no_spend"))}</div>')

    warning = (f'<div class="warning" role="note"><strong>{ctx.t("ads.warning_title")}</strong> '
               f'{ctx.esc(warning_text(data, ctx.lang))}</div>')
    return (f'<p class="lead">{ctx.t("ads.intro")}</p>'
            f'<div class="grid">{mer_card}{spend_card}</div>'
            f'<div class="grid wide">{"".join(cards)}</div>'
            f'{_metric_charts(ctx, shown)}{warning}')


BUILTIN: dict[str, Any] = {"kpi": _Section("kpi", render_kpi), "ads": _Section("ads", render_ads)}


def _load(sid: str) -> Any:
    name = f"{__name__}.{sid}"
    try:
        module = importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name != name:
            raise
        return BUILTIN.get(sid) or Placeholder(sid)
    if not callable(getattr(module, "render", None)):
        return BUILTIN.get(sid) or Placeholder(sid)
    if not hasattr(module, "ORDER"):
        module.ORDER = DEFAULT_ORDER[sid]  # type: ignore[attr-defined]
    return module


def registry() -> list[tuple[str, Any]]:
    """[(section id, section)] sorted by ORDER: a module if it exists, else the built-in, else a placeholder."""
    sections = [(sid, _load(sid)) for sid in SECTION_IDS]
    return sorted(sections, key=lambda item: (getattr(item[1], "ORDER", 999), SECTION_IDS.index(item[0])))

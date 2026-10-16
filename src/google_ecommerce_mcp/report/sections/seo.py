"""Section 6, Google Search without ads (Search Console).

Sources: ``sources.search_console.totals`` (its ``totals`` are THE figures; run with ``dimensions: ["date"]``
its rows give the daily curve), ``queries`` and ``pages`` (periodic ``gsc_performance`` by query / page).

* the totals are always the tool's ``totals``, never the sum of the rows: anonymised queries are in the
  total and absent from the rows (a note quotes both figures);
* the daily curve has a gap for a day without a row and pale bands for the dated entries of ``events``;
* winners and losers come from ``compare.compare_periods``; a query absent from a TRUNCATED list of the
  other period is left out (it may simply sit below that list's cut, which is not a zero);
* position changes read "gains / loses X place(s)" (a lower position is better).
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from .. import contract as c
from ..compare import agg_from_gsc_rows, avg_position, compare_periods
from ..i18n import fmt_int, fmt_number, fmt_pct, tr
from ..period import parse_date
from . import _common as h

if TYPE_CHECKING:  # pragma: no cover
    from ..render import ReportContext

ORDER = 60
TOP_QUERIES = 10
TOP_PAGES = 10
MAX_DAYS = 120


def _totals(node: Any) -> dict[str, Any]:
    t = c.totals(node)
    return {k: c.num(t.get(k)) for k in ("clicks", "impressions", "ctr", "position")}


def _daily(ctx: "ReportContext", node: Any) -> list[tuple[str, Any]]:
    """[(YYYY-MM-DD, clicks or None)] over every day of the rows' span (a missing day is a gap)."""
    values = {}
    for row in c.rows(node):
        day = parse_date(row.get("date"))
        if day is None:
            return []
        values[day] = c.num(row.get("clicks"))
    if len(values) < 2:
        return []
    start, end = min(values), max(values)
    if (end - start).days >= MAX_DAYS:
        return []
    out, day = [], start
    while day <= end:
        out.append((day.isoformat(), values.get(day)))
        day += timedelta(days=1)
    return out


def _bands(ctx: "ReportContext", days: list[tuple[str, Any]]) -> list[tuple[int, int, str]]:
    dates = [parse_date(d) for d, _ in days]
    out = []
    for e in h.events(ctx.data):
        idx = [i for i, d in enumerate(dates) if d and e.covers(d)]
        if idx:
            out.append((idx[0], idx[-1], e.label))
    return out


def _kpis(ctx: "ReportContext", cur: dict, prev: dict, prev_ok: bool) -> str:
    cards = []
    label_prev = ctx.prev_label_raw()

    def line(pastille: str, prev_text: str) -> str:
        return f"{ctx.esc(label_prev)}{ctx.esc(h.colon(ctx))} {ctx.esc(prev_text)} {pastille}"

    specs = (("clicks", "seo.clicks", lambda v: fmt_int(v, ctx.lang)),
             ("impressions", "seo.impressions", lambda v: fmt_int(v, ctx.lang)),
             ("ctr", "seo.ctr", lambda v: fmt_pct(v, 2, ctx.lang)),
             ("position", "seo.position", lambda v: fmt_number(v, 1, ctx.lang)))
    for key, label_key, fmt in specs:
        value = cur[key]
        value_html = ctx.esc(fmt(value)) if value is not None else ctx.na()
        if not prev_ok or prev[key] is None or value is None:
            lines = [f"{ctx.esc(label_prev)}{ctx.esc(h.colon(ctx))} {ctx.na()}"]
        elif key == "ctr":
            lines = [line(h.pt_delta(ctx, value, prev[key], n=(cur["impressions"], prev["impressions"]), metric="ctr"),
                          fmt(prev[key]))]
        else:
            lines = [line(ctx.delta(value, prev[key], key), fmt(prev[key]))]
        cards.append(ctx.kpi_card(tr(ctx.lang, label_key), value_html, lines))
    return f'<div class="grid">{"".join(cards)}</div>'


def _keep_mover(label: str, cur: dict, prev: dict, cur_trunc: bool, prev_trunc: bool) -> bool:
    in_cur, in_prev = label in cur, label in prev
    if in_cur and in_prev:
        return True
    if not in_prev:
        return not prev_trunc
    return not cur_trunc


def _queries(ctx: "ReportContext", cur_node: Any, prev_node: Any, total_clicks: Any) -> str:
    cur = agg_from_gsc_rows(c.rows(cur_node), "query")
    if not cur:
        return ctx.nodata(tr(ctx.lang, "seo.no_queries"))
    prev_ok = h.ok(prev_node)
    prev = agg_from_gsc_rows(c.rows(prev_node), "query") if prev_ok else {}
    comp = compare_periods(cur, prev)
    rows = []
    for r in comp.rows[:TOP_QUERIES]:
        p = prev.get(r.label)
        p_pos = avg_position(p.impressions, p.pos_weighted) if p else None
        clicks_delta = ctx.delta(r.clicks, p.clicks, "clicks") if p else ctx.na()
        pos_delta = (ctx.delta(r.position, round(p_pos, 1), "position") if p and p_pos is not None
                     and r.position is not None else ctx.na())
        rows.append([ctx.esc(r.label), ctx.integer(r.clicks), ctx.integer(r.impressions),
                     ctx.value_or_na(ctx.number(r.position, 1)), clicks_delta, pos_delta])
    vs = tr(ctx.lang, "seo.col.vs", prev=h.prev_raw(ctx))
    out = [f"<h3>{ctx.t('seo.queries')}</h3>",
           ctx.table(tr(ctx.lang, "seo.queries"), [tr(ctx.lang, "seo.col.query"), tr(ctx.lang, "seo.clicks"),
                                                  tr(ctx.lang, "seo.impressions"), tr(ctx.lang, "seo.position"),
                                                  tr(ctx.lang, "seo.col.clicks_vs", vs=vs),
                                                  tr(ctx.lang, "seo.col.position_vs", vs=vs)],
                     rows, numeric=[1, 2, 3])]

    if prev_ok:
        cur_trunc, prev_trunc = cur_node.get("truncated") is True, prev_node.get("truncated") is True
        win = [m for m in comp.winners if _keep_mover(m.label, cur, prev, cur_trunc, prev_trunc)][:5]
        lose = [m for m in comp.losers if _keep_mover(m.label, cur, prev, cur_trunc, prev_trunc)][:5]
        if win or lose:
            def signed(v: float) -> str:
                return ("+" if v > 0 else "−") + fmt_int(abs(v), ctx.lang)
            parts = []
            if win:
                parts.append(tr(ctx.lang, "seo.movers.win", label=win[0].label[:80], delta=signed(win[0].delta)))
            if lose:
                parts.append(tr(ctx.lang, "seo.movers.lose", label=lose[0].label[:80], delta=signed(lose[0].delta)))
            title = " ; ".join(parts) if ctx.lang == "fr" else "; ".join(parts)
            svg = ctx.svg.diverging_bars([(m.label, m.delta, signed(m.delta)) for m in win],
                                         [(m.label, m.delta, signed(m.delta)) for m in lose], label=title)
            hidden = [(m.label, fmt_int(m.current, ctx.lang), fmt_int(m.previous, ctx.lang), signed(m.delta))
                      for m in win + lose]
            out.append(f"<h3>{ctx.t('seo.movers', prev=h.prev_raw(ctx))}</h3>")
            out.append(ctx.figure(title, svg, [tr(ctx.lang, "seo.col.query"), tr(ctx.lang, "chart.period"),
                                               ctx.prev_label_raw(), tr(ctx.lang, "seo.col.diff")], hidden))
        if prev_trunc or cur_trunc:
            out.append(h.notes([ctx.t("seo.movers_truncated")]))

    listed = sum(a.clicks for a in cur.values())
    if c.is_num(total_clicks):
        out.append(h.notes([ctx.t("seo.anonymised", total=fmt_int(total_clicks, ctx.lang),
                                  n=fmt_int(len(cur), ctx.lang), listed=fmt_int(listed, ctx.lang))]))
    else:
        out.append(h.notes([ctx.t("seo.no_totals_rows")]))
    return "".join(out)


def _pages(ctx: "ReportContext", node: Any, prev_node: Any) -> str:
    cur = agg_from_gsc_rows(c.rows(node), "page")
    if not cur:
        return ""
    prev = agg_from_gsc_rows(c.rows(prev_node), "page") if h.ok(prev_node) else {}
    comp = compare_periods(cur, prev)
    rows = []
    for r in comp.rows[:TOP_PAGES]:
        p = prev.get(r.label)
        rows.append([ctx.short_url(r.label), ctx.integer(r.clicks), ctx.integer(r.impressions),
                     ctx.value_or_na(ctx.number(r.position, 1)),
                     ctx.delta(r.clicks, p.clicks, "clicks") if p else ctx.na()])
    vs = tr(ctx.lang, "seo.col.vs", prev=h.prev_raw(ctx))
    return (f"<h3>{ctx.t('seo.pages')}</h3>"
            + ctx.table(tr(ctx.lang, "seo.pages"), [tr(ctx.lang, "seo.col.page"), tr(ctx.lang, "seo.clicks"),
                                                   tr(ctx.lang, "seo.impressions"), tr(ctx.lang, "seo.position"),
                                                   tr(ctx.lang, "seo.col.clicks_vs", vs=vs)], rows, numeric=[1, 2, 3]))


def render(data: dict, ctx: "ReportContext") -> str:
    t_node = ctx.view("search_console", "totals")
    q_node = ctx.view("search_console", "queries")
    p_node = ctx.view("search_console", "pages")
    if all(c.state(n) == "missing" for n in (t_node, q_node, p_node)):
        return ctx.unavailable(None, "search_console") or ""
    out = []
    t_prev = ctx.view("search_console", "totals", "previous")
    cur, prev = _totals(t_node), _totals(t_prev)
    prev_ok = h.ok(t_prev)
    if h.ok(t_node) and cur["clicks"] is not None:
        value = fmt_int(cur["clicks"], ctx.lang)
        head = ctx.headline("clicks", tr(ctx.lang, "seo.clicks_label"), cur["clicks"],
                            prev["clicks"] if prev_ok else None, value)
        sentence = (tr(ctx.lang, "seo.lead_prev", clicks=value, prev=fmt_int(prev["clicks"], ctx.lang),
                       label=h.prev_raw(ctx))
                    if prev_ok and prev["clicks"] is not None else tr(ctx.lang, "seo.lead", clicks=value))
        out.append(h.lead(ctx.esc(head + ". " + sentence)))
        out.append(_kpis(ctx, cur, prev, prev_ok))
        days = _daily(ctx, t_node)
        if days:
            prev_days = [v for _, v in _daily(ctx, t_prev)] if prev_ok else []
            svg = ctx.svg.line_chart(days, color=ctx.accent, reference=prev_days or None,
                                     bands=_bands(ctx, days), label=tr(ctx.lang, "seo.daily_label",
                                                                       clicks=value))
            hidden = [(d, fmt_int(v, ctx.lang) if v is not None else "—",
                       fmt_int(prev_days[i], ctx.lang) if i < len(prev_days) and prev_days[i] is not None else "—")
                      for i, (d, v) in enumerate(days)]
            out.append(ctx.figure(tr(ctx.lang, "seo.daily", clicks=value), svg,
                                  [tr(ctx.lang, "chart.day"), tr(ctx.lang, "seo.clicks"), ctx.prev_label_raw()],
                                  hidden))
            out.append(h.notes([ctx.t("seo.daily_note")]))
    else:
        out.append(ctx.unavailable(t_node, "search_console") or ctx.nodata(tr(ctx.lang, "seo.no_totals")))

    if c.state(q_node) == "error":
        out.append(ctx.unavailable(q_node, "search_console") or "")
    elif h.ok(q_node):
        out.append(_queries(ctx, q_node, ctx.view("search_console", "queries", "previous"),
                            cur["clicks"] if h.ok(t_node) else None))
    if h.ok(p_node):
        out.append(_pages(ctx, p_node, ctx.view("search_console", "pages", "previous")))
    out.append(h.notes([ctx.esc(h.settling_raw(ctx, t_node)), ctx.t("seo.lag")]))
    return "".join(out)

"""Section 3, conversion funnel: sessions, view_item, add_to_cart, begin_checkout, purchase.

Source: ``sources.ga4.funnel``, a periodic ``ga4_report`` by ``eventName`` with ``eventCount`` (the sessions
step comes from that report's ``totals.sessions``, else from the GA4 KPI report of the same period, else from
the ``session_start`` event). Horizontal bars from zero (no trapezoid), the pass-through rate between steps,
and its change in percentage points against the previous period. A step missing from the GA4 rows is a GAP
("not measured"), never a zero: GA4 can withhold small rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .. import contract as c
from .. import svg as S
from ..i18n import fmt_int, fmt_pct, tr
from . import _common as h

if TYPE_CHECKING:  # pragma: no cover
    from ..render import ReportContext

ORDER = 30
STEPS: tuple[str, ...] = ("sessions", "view_item", "add_to_cart", "begin_checkout", "purchase")


def _sessions(ctx: "ReportContext", node: Any, by_event: dict[str, list[dict]], period: str) -> tuple[Any, str]:
    """(sessions, origin key) for one period."""
    value = c.num(c.totals(node).get("sessions"))
    if value is not None:
        return value, "funnel.origin.report"
    kpi = c.num(c.totals(ctx.view("ga4", "kpi", period)).get("sessions"))
    if kpi is not None:
        return kpi, "funnel.origin.kpi"
    if "session_start" in by_event:
        return h.sum_rows(by_event["session_start"], ("eventCount",)), "funnel.origin.event"
    return None, ""


def counts(ctx: "ReportContext", node: Any, period: str = "current") -> tuple[list[Any], str]:
    """Counts of the five steps (None = not measured) and where the sessions figure comes from."""
    if not h.ok(node):
        return [None] * len(STEPS), ""
    by_event = h.rows_by(node, "eventName")
    sessions, origin = _sessions(ctx, node, by_event, period)
    out: list[Any] = [sessions]
    for step in STEPS[1:]:
        out.append(h.sum_rows(by_event[step], ("eventCount",)) if step in by_event else None)
    return out, origin


def rates(values: list[Any]) -> list[float | None]:
    """Pass-through rate from the previous step (index 0 is None)."""
    out: list[float | None] = [None]
    for i in range(1, len(values)):
        a, b = values[i - 1], values[i]
        out.append(b / a if c.is_num(a) and c.is_num(b) and a > 0 else None)
    return out


def _chart(ctx: "ReportContext", labels: list[str], values: list[Any], rate_texts: list[str], title: str) -> str:
    """Horizontal bars from zero in funnel order, a dashed empty track for a step not measured."""
    width, name_w, value_w, row_h, gap = 640.0, 170.0, 150.0, 26.0, 16.0
    bar_w = width - name_w - value_w
    known = [v for v in values if c.is_num(v) and v > 0]
    peak = max(known) if known else 1.0
    body = []
    for i, (name, value) in enumerate(zip(labels, values)):
        y = 2 + i * (row_h + gap)
        body.append(f'<text x="0" y="{S._f(y + row_h * 0.65)}" font-size="12" fill="{S.INK}">{S.esc(name[:28])}</text>')
        if c.is_num(value):
            w = max(0.0, value / peak * bar_w)
            body.append(f'<rect x="{S._f(name_w)}" y="{S._f(y + 4)}" width="{S._f(bar_w)}" height="{S._f(row_h - 8)}" '
                        f'fill="{S.TRACK}"/><rect x="{S._f(name_w)}" y="{S._f(y + 4)}" '
                        f'width="{S._f(max(w, 1.0 if value > 0 else 0.0))}" height="{S._f(row_h - 8)}" '
                        f'fill="{S.esc(ctx.accent)}"/><text x="{S._f(name_w + w + 6)}" y="{S._f(y + row_h * 0.65)}" '
                        f'font-size="12" fill="{S.INK}">{S.esc(fmt_int(value, ctx.lang))}</text>')
        else:
            body.append(f'<rect x="{S._f(name_w)}" y="{S._f(y + 4)}" width="{S._f(bar_w)}" height="{S._f(row_h - 8)}" '
                        f'fill="none" stroke="{S.REFERENCE}" stroke-dasharray="4 3"/><text x="{S._f(name_w + 6)}" '
                        f'y="{S._f(y + row_h * 0.65)}" font-size="12" fill="{S.LABEL}">'
                        f'{S.esc(tr(ctx.lang, "kpi.not_measured"))}</text>')
        if i + 1 < len(values):
            body.append(f'<text x="{S._f(name_w)}" y="{S._f(y + row_h + gap - 3)}" font-size="10" fill="{S.LABEL}">'
                        f'↓ {S.esc(rate_texts[i + 1])}</text>')
    height = len(values) * row_h + (len(values) - 1) * gap + 4
    return S._svg(width, height, title, "".join(body))


def render(data: dict, ctx: "ReportContext") -> str:
    node = ctx.view("ga4", "funnel")
    missing = ctx.unavailable(node, "ga4")
    if missing:
        return missing
    cur, origin = counts(ctx, node)
    if all(v is None for v in cur[1:]):
        return ctx.nodata(tr(ctx.lang, "funnel.no_events"))
    prev_node = ctx.view("ga4", "funnel", "previous")
    prev, _ = counts(ctx, prev_node, "previous")
    r_cur, r_prev = rates(cur), rates(prev)
    labels = [tr(ctx.lang, "funnel.step." + s) for s in STEPS]

    rate_texts = []
    for i, r in enumerate(r_cur):
        if i == 0:
            rate_texts.append("")
        elif r is None:
            rate_texts.append(tr(ctx.lang, "funnel.rate_none"))
        else:
            rate_texts.append(tr(ctx.lang, "funnel.rate", rate=fmt_pct(r, 1, ctx.lang)))

    known = [(r, i) for i, r in enumerate(r_cur) if r is not None]
    if known:
        worst, idx = min(known)
        title = tr(ctx.lang, "funnel.head", rate=fmt_pct(worst, 1, ctx.lang), a=labels[idx - 1], b=labels[idx])
    else:
        measured = sum(1 for v in cur if v is not None)
        title = tr(ctx.lang, "funnel.head_count", n=fmt_int(measured, ctx.lang), total=fmt_int(len(STEPS), ctx.lang))

    svg = _chart(ctx, labels, cur, rate_texts, title)
    hidden = [(labels[i], fmt_int(v, ctx.lang) if v is not None else tr(ctx.lang, "kpi.not_measured"),
               fmt_pct(r_cur[i], 1, ctx.lang) if r_cur[i] is not None else "—") for i, v in enumerate(cur)]
    figure = ctx.figure(title, svg, [tr(ctx.lang, "funnel.col.step"), tr(ctx.lang, "funnel.col.count"),
                                     tr(ctx.lang, "funnel.col.rate")], hidden)

    prev_ok = h.ok(prev_node)
    rows = []
    for i, v in enumerate(cur):
        count_html = ctx.integer(v) if v is not None else f'<span class="na">{ctx.t("kpi.not_measured")}</span>'
        if i == 0:
            rate_html, delta_html = ctx.esc("100 %" if ctx.lang == "fr" else "100%"), ""
        else:
            rate_html = ctx.pct(r_cur[i], 1) if r_cur[i] is not None else ctx.na()
            delta_html = (h.pt_delta(ctx, r_cur[i], r_prev[i], n=(cur[i - 1], prev[i - 1]))
                          if prev_ok else ctx.na())
        rows.append([ctx.esc(labels[i]), count_html, rate_html, delta_html])
    table = ctx.table(tr(ctx.lang, "funnel.table"), [tr(ctx.lang, "funnel.col.step"), tr(ctx.lang, "funnel.col.count"),
                                                    tr(ctx.lang, "funnel.col.rate"),
                                                    tr(ctx.lang, "funnel.col.vs", prev=h.prev_raw(ctx))],
                      rows, numeric=[1, 2])

    extra = []
    if c.is_num(cur[0]) and c.is_num(cur[-1]) and cur[0] > 0:
        extra.append(ctx.t("funnel.overall", rate=fmt_pct(cur[-1] / cur[0], 2, ctx.lang),
                           purchases=fmt_int(cur[-1], ctx.lang), sessions=fmt_int(cur[0], ctx.lang)))
    gaps = [labels[i] for i, v in enumerate(cur) if v is None]
    if gaps:
        extra.append(ctx.t("funnel.gap", steps=", ".join(gaps)))
    extra.append(ctx.t("funnel.note_events"))
    if origin:
        extra.append(ctx.t(origin))
    extra.append(ctx.t("funnel.note_pt", prev=h.prev_raw(ctx)))
    extra.append(ctx.esc(h.settling_raw(ctx, node)))
    return figure + table + h.notes(extra)

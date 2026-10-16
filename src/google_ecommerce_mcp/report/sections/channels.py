"""Section 4, acquisition channels: revenue, sessions, orders and conversion rate per channel.

Source: ``sources.ga4.channels``, a periodic ``ga4_report`` by ``sessionDefaultChannelGroup`` (metrics
sessions, purchaseRevenue or totalRevenue, ecommercePurchases or transactions). Bars sorted by revenue with
the value written at the end, the change against the previous period, and the attribution model stated:
``meta.ga4_attribution_model`` (or an ``attribution_model`` field of the tool output) when the user gives it,
"not provided" otherwise. Shares use the report's ``totals``, never the sum of the rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .. import contract as c
from ..compare import percent_shares
from ..i18n import fmt_money, fmt_pct, tr
from . import _common as h

if TYPE_CHECKING:  # pragma: no cover
    from ..render import ReportContext

ORDER = 40
DIMENSIONS = ("sessionDefaultChannelGroup", "sessionPrimaryChannelGroup", "sessionSourceMedium")
MAX_ROWS = 12


def _dimension(node: Any) -> str | None:
    for row in c.rows(node):
        for dim in DIMENSIONS:
            if isinstance(row.get(dim), str):
                return dim
    return None


def _figures(rows: list[dict]) -> dict[str, Any]:
    sessions = h.sum_rows(rows, ("sessions",))
    orders = h.sum_rows(rows, h.ORDER_KEYS)
    revenue = h.sum_rows(rows, h.REVENUE_KEYS)
    return {"sessions": sessions, "orders": orders, "revenue": revenue,
            "conversion_rate": orders / sessions if orders is not None and sessions else None}


def channel_table(node: Any) -> list[tuple[str, dict[str, Any]]]:
    """[(channel, figures)] sorted by revenue (then sessions) descending, deterministic."""
    dim = _dimension(node)
    if dim is None:
        return []
    items = [(name, _figures(rows)) for name, rows in h.rows_by(node, dim).items()]
    items.sort(key=lambda it: (-(it[1]["revenue"] if it[1]["revenue"] is not None else -1),
                               -(it[1]["sessions"] if it[1]["sessions"] is not None else -1), it[0]))
    return items


def _attribution(ctx: "ReportContext", node: Any) -> str:
    meta = ctx.data.get("meta", {})
    model = meta.get("ga4_attribution_model") if isinstance(meta, dict) else None
    if not (isinstance(model, str) and model.strip()) and h.ok(node):
        model = node.get("attribution_model")
    if isinstance(model, str) and model.strip():
        return ctx.t("channels.attribution", model=model.strip()[:120])
    return ctx.t("channels.attribution_none")


def render(data: dict, ctx: "ReportContext") -> str:
    node = ctx.view("ga4", "channels")
    missing = ctx.unavailable(node, "ga4")
    if missing:
        return missing
    items = channel_table(node)
    if not items:
        return ctx.nodata(tr(ctx.lang, "channels.no_rows"))
    prev_node = ctx.view("ga4", "channels", "previous")
    prev = dict(channel_table(prev_node)) if h.ok(prev_node) else {}
    prev_ok = h.ok(prev_node)
    currency = c.currency_of(node, "purchaseRevenue") or c.currency_of(node, "totalRevenue") or ctx.currency
    totals = c.totals(node)
    total_rev = h.first(totals, h.REVENUE_KEYS)
    total_sessions = c.num(totals.get("sessions"))

    by_revenue = any(f["revenue"] is not None for _, f in items)
    metric = "revenue" if by_revenue else "sessions"
    total = total_rev if by_revenue else total_sessions
    shown = [(n, f) for n, f in items if f[metric] is not None][:MAX_ROWS]

    def value_text(v: Any) -> str:
        return fmt_money(v, currency, ctx.lang) if metric == "revenue" else h.fmt_number(v, 0, ctx.lang)

    top_name, top = shown[0] if shown else (items[0][0], items[0][1])
    if shown and total:
        title = tr(ctx.lang, "channels.head_share." + metric, channel=top_name, value=value_text(top[metric]),
                   share=fmt_pct(top[metric] / total, 1, ctx.lang))
    elif shown:
        title = tr(ctx.lang, "channels.head." + metric, channel=top_name, value=value_text(top[metric]))
    else:
        title = tr(ctx.lang, "channels.head_none", n=h.fmt_number(len(items), 0, ctx.lang))

    figure = ""
    if shown:
        bars = [(n, f[metric], value_text(f[metric])) for n, f in shown]
        svg = ctx.svg.hbar_chart(bars, ctx.accent, label=title, sort=True)
        values = [f[metric] for _, f in shown]
        if total and abs(sum(values) - total) < 1e-6 * max(1.0, abs(total)):
            # Every channel shown: largest-remainder rounding, so the shares add up to exactly 100 %.
            shares = [fmt_pct(s / 100, 1, ctx.lang) if s is not None else "—" for s in percent_shares(values)]
        else:
            shares = [fmt_pct(v / total, 1, ctx.lang) if total else "—" for v in values]
        hidden = [(n, value_text(f[metric]), share) for (n, f), share in zip(shown, shares)]
        figure = ctx.figure(title, svg, [tr(ctx.lang, "channels.col.channel"),
                                         tr(ctx.lang, "channels.col." + metric),
                                         tr(ctx.lang, "channels.col.share")], hidden)

    rows = []
    for name, f in items[:MAX_ROWS]:
        p = prev.get(name)
        if not prev_ok:
            delta = ctx.na()
        elif p is None:
            delta = ctx.na()
        elif metric == "revenue":
            delta = ctx.delta(f["revenue"], p["revenue"], "revenue", counts=(f["orders"], p["orders"]),
                              currency=currency)
        else:
            delta = ctx.delta(f["sessions"], p["sessions"], "sessions")
        rows.append([ctx.esc(name), ctx.value_or_na(ctx.integer(f["sessions"])),
                     ctx.value_or_na(ctx.integer(f["orders"])), ctx.value_or_na(ctx.money(f["revenue"], currency)),
                     ctx.value_or_na(ctx.pct(f["conversion_rate"], 1)), delta])
    table = ctx.table(tr(ctx.lang, "channels.table"),
                      [tr(ctx.lang, "channels.col.channel"), tr(ctx.lang, "kpi.sessions"), tr(ctx.lang, "kpi.orders"),
                       tr(ctx.lang, "kpi.revenue"), tr(ctx.lang, "kpi.conversion"),
                       tr(ctx.lang, "channels.col.vs." + metric, prev=h.prev_raw(ctx))],
                      rows, numeric=[1, 2, 3, 4])

    extra = [_attribution(ctx, node), ctx.t("channels.note_totals")]
    if len(items) > MAX_ROWS:
        extra.append(ctx.t("channels.more", n=h.fmt_number(len(items) - MAX_ROWS, 0, ctx.lang)))
    extra.append(ctx.esc(h.truncated_raw(ctx, node)))
    extra.append(ctx.esc(h.settling_raw(ctx, node)))
    return figure + table + h.notes(extra)

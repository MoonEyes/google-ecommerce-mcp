"""Section 5, products: best and weakest sellers by revenue and quantity, with their change.

Source: ``sources.ga4.products``, a periodic ``ga4_report`` by ``itemName`` (metrics itemRevenue and
itemsPurchased; itemsViewed optional). Stock-outs are not in GA4: an entry of ``events`` is attached to a
product when its ``item`` (or ``product``) field equals the itemName, or when its label contains the
itemName; the note quotes the entry as given, it never guesses a cause.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .. import contract as c
from ..i18n import fmt_int, fmt_money, tr
from . import _common as h

if TYPE_CHECKING:  # pragma: no cover
    from ..render import ReportContext

ORDER = 50
REVENUE = ("itemRevenue", "itemPurchaseRevenue", "purchaseRevenue")
QUANTITY = ("itemsPurchased", "itemPurchaseQuantity")
TOP = 5


def items_of(node: Any) -> list[tuple[str, dict[str, Any]]]:
    """[(itemName, {revenue, quantity, views})], best first (revenue, then quantity, then name)."""
    out = []
    for name, rows in h.rows_by(node, "itemName").items():
        out.append((name, {"revenue": h.sum_rows(rows, REVENUE), "quantity": h.sum_rows(rows, QUANTITY),
                           "views": h.sum_rows(rows, ("itemsViewed",))}))
    out.sort(key=lambda it: (-(it[1]["revenue"] if it[1]["revenue"] is not None else -1),
                             -(it[1]["quantity"] if it[1]["quantity"] is not None else -1), it[0]))
    return out


def _event_note(ctx: "ReportContext", name: str, evts: list[h.Event]) -> str:
    low = name.casefold()
    hits = [e for e in evts if (e.item and e.item.casefold() == low) or (len(low) >= 4 and low in e.label.casefold())]
    return " ".join(tr(ctx.lang, "products.event", date=e.date_raw(ctx.lang), label=e.label) for e in hits[:2])


def _table(ctx: "ReportContext", caption: str, items: list[tuple[str, dict]], prev: dict[str, dict], prev_ok: bool,
           currency: str | None, evts: list[h.Event]) -> str:
    rows = []
    for name, f in items:
        p = prev.get(name)
        delta = (ctx.delta(f["revenue"], p["revenue"], "revenue", counts=(f["quantity"], p["quantity"]),
                           currency=currency) if prev_ok and p else ctx.na())
        note = _event_note(ctx, name, evts)
        rows.append([ctx.esc(name), ctx.value_or_na(ctx.integer(f["quantity"])),
                     ctx.value_or_na(ctx.money(f["revenue"], currency)), delta, ctx.esc(note)])
    return ctx.table(caption, [tr(ctx.lang, "products.col.product"), tr(ctx.lang, "products.col.qty"),
                               tr(ctx.lang, "kpi.revenue"), tr(ctx.lang, "products.col.vs", prev=h.prev_raw(ctx)),
                               tr(ctx.lang, "products.col.note")], rows, numeric=[1, 2])


def render(data: dict, ctx: "ReportContext") -> str:
    node = ctx.view("ga4", "products")
    missing = ctx.unavailable(node, "ga4")
    if missing:
        return missing
    items = items_of(node)
    if not items:
        return ctx.nodata(tr(ctx.lang, "products.no_rows"))
    prev_node = ctx.view("ga4", "products", "previous")
    prev_ok = h.ok(prev_node)
    prev = dict(items_of(prev_node)) if prev_ok else {}
    currency = c.currency_of(node, "itemRevenue") or ctx.currency
    evts = h.events(data)

    with_rev = [it for it in items if it[1]["revenue"] is not None]
    top = with_rev[:TOP]
    rest = with_rev[TOP:]
    flop = sorted(rest, key=lambda it: (it[1]["revenue"], it[1]["quantity"] if it[1]["quantity"] is not None else -1,
                                        it[0]))[:TOP]
    out = []
    if top:
        name, f = top[0]
        title = (tr(ctx.lang, "products.head", name=name, revenue=fmt_money(f["revenue"], currency, ctx.lang),
                    qty=fmt_int(f["quantity"], ctx.lang)) if f["quantity"] is not None else
                 tr(ctx.lang, "products.head_noqty", name=name, revenue=fmt_money(f["revenue"], currency, ctx.lang)))
        bars = [(n, x["revenue"], fmt_money(x["revenue"], currency, ctx.lang)) for n, x in top]
        svg = ctx.svg.hbar_chart(bars, ctx.accent, label=title, sort=True)
        out.append(ctx.figure(title, svg, [tr(ctx.lang, "products.col.product"), tr(ctx.lang, "kpi.revenue"),
                                           tr(ctx.lang, "products.col.qty")],
                              [(n, fmt_money(x["revenue"], currency, ctx.lang), fmt_int(x["quantity"], ctx.lang))
                               for n, x in top]))
        out.append(f"<h3>{ctx.t('products.top')}</h3>")
        out.append(_table(ctx, tr(ctx.lang, "products.top"), top, prev, prev_ok, currency, evts))
    else:
        by_qty = sorted((it for it in items if it[1]["quantity"] is not None), key=lambda it: (-it[1]["quantity"], it[0]))
        if not by_qty:
            return ctx.nodata(tr(ctx.lang, "products.no_metrics"))
        name, f = by_qty[0]
        out.append(h.lead(ctx.t("products.head_qty", name=name, qty=fmt_int(f["quantity"], ctx.lang))))
        out.append(_table(ctx, tr(ctx.lang, "products.top_qty"), by_qty[:TOP], prev, prev_ok, currency, evts))
    if flop:
        out.append(f"<h3>{ctx.t('products.flop')}</h3>")
        out.append(_table(ctx, tr(ctx.lang, "products.flop"), flop, prev, prev_ok, currency, evts))
    elif top:
        out.append(h.notes([ctx.t("products.no_flop", n=fmt_int(len(with_rev), ctx.lang))]))

    # Products that dropped the most against the previous period, when both periods are given.
    if prev_ok:
        drops = []
        for n, f in with_rev:
            p = prev.get(n)
            if p and p["revenue"] is not None and f["revenue"] < p["revenue"]:
                drops.append((f["revenue"] - p["revenue"], n))
        if drops:
            diff, n = sorted(drops)[0]
            out.append(h.notes([ctx.t("products.biggest_drop", name=n,
                                      diff=fmt_money(abs(diff), currency, ctx.lang),
                                      prev=h.prev_raw(ctx))]))
    stock = [e for e in evts if e.item]
    unmatched = [e for e in stock if e.item.casefold() not in {n.casefold() for n, _ in items}]
    extra = [ctx.t("products.note_source")]
    if unmatched:
        extra.append(ctx.t("products.unmatched", n=fmt_int(len(unmatched), ctx.lang)))
    extra.append(ctx.esc(h.truncated_raw(ctx, node)))
    extra.append(ctx.esc(h.settling_raw(ctx, node)))
    out.append(h.notes(extra))
    return "".join(out)

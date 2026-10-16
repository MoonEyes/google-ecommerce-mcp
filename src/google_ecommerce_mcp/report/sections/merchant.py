"""Section 7, product listings on Google Shopping (Merchant Center).

Sources: ``sources.merchant_center.statuses`` (``{"approved": n, "limited": n, "disapproved": n}`` counted
by the assistant from ``merchant_report_query``; an optional ``"previous": {...}`` of the same shape gives
the change), ``product_issues`` (``merchant_product_issues``) and ``performance`` (``merchant_report_query``
on ``product_performance_view``: free-listing clicks per product).

The catalogue split is ONE 100 % bar with 2 px gaps and direct labels; the counts come from
``summary.merchant_counts`` (the same figures as the summary and the decisions). Any rise of the disapproved
count is coloured (the "any" rule of compare.py); a missing count is "not provided", never zero.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .. import contract as c
from ..compare import fmt_delta_pct, percent_shares
from ..i18n import fmt_int, fmt_pct, tr
from ..render import ARROWS
from ..summary import merchant_counts
from . import _common as h

if TYPE_CHECKING:  # pragma: no cover
    from ..render import ReportContext

ORDER = 70
TOP_ISSUES = 5
FILLS = {"approved": "#8DB8CC", "limited": "#E9B872", "disapproved": "#B33A00"}
STATUS_KEYS = ("approved", "limited", "disapproved")


def issue_tally(node: Any) -> list[tuple[str, str, int]]:
    """[(issue label, severity, products)] most frequent first; a product counts once per issue."""
    if not h.ok(node):
        return []
    tally: dict[tuple[str, str], int] = {}
    for product in node.get("products") or []:
        if not isinstance(product, dict):
            continue
        seen = set()
        for issue in product.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            label = h.no_scheme(issue.get("description") or issue.get("code") or "?")[:120]
            severity = str(issue.get("severity") or "").upper()[:30]
            seen.add((label, severity))
        for key in seen:
            tally[key] = tally.get(key, 0) + 1
    rank = {"DISAPPROVED": 0, "DEMOTED": 1}
    return sorted(((k[0], k[1], n) for k, n in tally.items()),
                  key=lambda x: (-x[2], rank.get(x[1], 2), x[0]))


def _int_like(value: Any) -> float | None:
    """A count from the Merchant API: a number, or an int64 written as a decimal string (the API's format)."""
    if c.is_num(value):
        return value
    if isinstance(value, str) and value.isdigit() and len(value) < 16:
        return int(value)
    return None


def performance_rows(node: Any) -> list[tuple[str, float, float | None]]:
    """[(title, clicks, impressions)] from a merchant_report_query result, most clicks first."""
    if not h.ok(node):
        return []
    results = node.get("results") if isinstance(node.get("results"), list) else node.get("rows")
    out = []
    for item in results if isinstance(results, list) else []:
        if not isinstance(item, dict):
            continue
        view = item.get("productPerformanceView") or item.get("product_performance_view") or item
        if not isinstance(view, dict):
            continue
        title = view.get("title") or view.get("offerId") or view.get("offer_id")
        clicks = _int_like(view.get("clicks"))
        if not isinstance(title, str) or clicks is None:
            continue
        out.append((title[:150], clicks, _int_like(view.get("impressions"))))
    out.sort(key=lambda r: (-r[1], r[0]))
    return out


def status_delta(ctx: "ReportContext", value: float, previous: float, key: str) -> str:
    """The change of one status count. A RISE of the disapproved count is always coloured unfavourable, even
    under the 1 % 'stable' floor: one more refused listing is one product out of Shopping."""
    if key == "disapproved" and value > previous:
        diff = "+" + fmt_int(value - previous, ctx.lang)
        pct = fmt_delta_pct(value, previous, ctx.lang, digits=1)
        text = f"{diff} ({pct})" if pct != "—" else f"{diff} ({tr(ctx.lang, 'delta.new')})"
        return f'<span class="delta unfav"><span aria-hidden="true">{ARROWS["up"]}</span> {ctx.esc(text)}</span>'
    return ctx.delta(value, previous, key, small=True)


def _severity(ctx: "ReportContext", severity: str) -> str:
    key = "merchant.sev." + severity.lower()
    text = tr(ctx.lang, key)
    return text if text != key else (severity.lower() or "—")


def render(data: dict, ctx: "ReportContext") -> str:
    statuses = ctx.view("merchant_center", "statuses")
    issues = ctx.view("merchant_center", "product_issues")
    perf = ctx.view("merchant_center", "performance")
    mc = merchant_counts(data)
    if mc is None and not h.ok(perf):
        node = issues if c.state(issues) == "error" else statuses if c.state(statuses) == "error" else perf
        return ctx.unavailable(node, "merchant_center") or ctx.nodata()
    mc = mc or {"approved": None, "limited": None, "disapproved": None, "truncated": False}
    out = []

    known = {k: mc.get(k) for k in STATUS_KEYS}
    total = sum(v for v in known.values() if c.is_num(v)) if all(c.is_num(v) for v in known.values()) else None
    scanned = c.num(issues.get("scanned")) if h.ok(issues) else None
    dis = known["disapproved"]
    if c.is_num(dis):
        base = total if total else scanned
        if dis == 0:
            title = (tr(ctx.lang, "merchant.head_zero_of", total=fmt_int(base, ctx.lang)) if base
                     else tr(ctx.lang, "merchant.head_zero"))
        elif base:
            title = tr(ctx.lang, "merchant.head_of", n=fmt_int(dis, ctx.lang), total=fmt_int(base, ctx.lang),
                       share=fmt_pct(dis / base, 1, ctx.lang))
        else:
            title = tr(ctx.lang, "merchant.head", n=fmt_int(dis, ctx.lang))
        out.append(h.lead(ctx.esc(title)))

    if total:
        segs = [(tr(ctx.lang, "merchant." + k), known[k], FILLS[k], fmt_int(known[k], ctx.lang)) for k in STATUS_KEYS]
        label = tr(ctx.lang, "merchant.bar", total=fmt_int(total, ctx.lang))
        svg = ctx.svg.stacked_100(segs, label=label)
        out.append(ctx.figure(label, svg, [tr(ctx.lang, "merchant.col.status"), tr(ctx.lang, "merchant.col.count"),
                                           tr(ctx.lang, "merchant.col.share")],
                              [(s[0], s[3], fmt_pct(share / 100 if share is not None else None, 1, ctx.lang))
                               for s, share in zip(segs, percent_shares([s[1] for s in segs]))]))

    prev = statuses.get("previous") if h.ok(statuses) and isinstance(statuses.get("previous"), dict) else {}
    cards = []
    for k in STATUS_KEYS:
        value = known[k]
        p = c.num(prev.get(k)) if prev else None
        lines = []
        if p is not None and value is not None:
            lines.append(f"{ctx.esc(ctx.prev_label_raw())}{ctx.esc(h.colon(ctx))} {ctx.integer(p)} "
                         f"{status_delta(ctx, value, p, k)}")
        else:
            lines.append(f"{ctx.esc(ctx.prev_label_raw())}{ctx.esc(h.colon(ctx))} {ctx.na()}")
        cards.append(ctx.kpi_card(tr(ctx.lang, "merchant." + k),
                                  ctx.integer(value) if value is not None else ctx.na(), lines))
    out.append(f'<div class="grid">{"".join(cards)}</div>')

    tally = issue_tally(issues)
    if tally:
        top = tally[:TOP_ISSUES]
        out.append(f"<h3>{ctx.t('merchant.issues')}</h3>")
        out.append(ctx.table(tr(ctx.lang, "merchant.issues"),
                             [tr(ctx.lang, "merchant.col.issue"), tr(ctx.lang, "merchant.col.severity"),
                              tr(ctx.lang, "merchant.col.products")],
                             [[ctx.esc(label), ctx.esc(_severity(ctx, sev)), ctx.integer(n)] for label, sev, n in top],
                             numeric=[2]))
    elif h.ok(issues):
        out.append(h.notes([ctx.t("merchant.no_issues", n=fmt_int(scanned, ctx.lang)) if scanned
                            else ctx.t("merchant.no_issues_noscan")]))
    elif c.state(issues) == "error":
        out.append(ctx.unavailable(issues, "merchant_center") or "")

    perf_rows = performance_rows(perf)
    if perf_rows:
        top = perf_rows[:5]
        out.append(f"<h3>{ctx.t('merchant.perf')}</h3>")
        out.append(ctx.table(tr(ctx.lang, "merchant.perf"), [tr(ctx.lang, "products.col.product"),
                                                            tr(ctx.lang, "seo.clicks"),
                                                            tr(ctx.lang, "seo.impressions")],
                             [[ctx.esc(t), ctx.integer(cl), ctx.value_or_na(ctx.integer(im))] for t, cl, im in top],
                             numeric=[1, 2]))

    extra = []
    if not total:
        extra.append(ctx.t("merchant.no_statuses"))
    if scanned:
        extra.append(ctx.t("merchant.scanned", n=fmt_int(scanned, ctx.lang)))
    if mc.get("truncated"):
        extra.append(ctx.t("merchant.truncated"))
    if h.ok(issues) and issues.get("partial") is True:
        extra.append(ctx.t("merchant.partial"))
    extra.append(ctx.t("merchant.note"))
    out.append(h.notes(extra))
    return "".join(out)

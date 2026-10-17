# Derived from OpenGSC (MIT), src/lib/reports/render.ts (buildSummary, buildNextSteps); ported to Python
"""The summary without an AI: three messages and the decisions list, read straight off the numbers.

Deterministic: the same input always gives the same sentences, which is what a frozen report demands. Every
sentence cites a figure of the input and nothing is forecast: everything said happened inside the period.
Messages are picked in a fixed order an operator can argue with (revenue, MER, disapproved listings, organic
clicks, ad spend), the first three available. All text returned here is PLAIN text: the renderer escapes it.

Shared helpers used by several sections live here too: :func:`mer` and :func:`merchant_counts`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import contract as c
from .compare import agg_from_gsc_rows, assess_delta, compare_periods, fmt_delta_pct
from .confidence import HIGH, LOW, MEDIUM, Confidence, assess, data_complete
from .i18n import fmt_int, fmt_money, fmt_number, tr

REVENUE_KEYS = ("purchaseRevenue", "totalRevenue")
ORDER_KEYS = ("ecommercePurchases", "transactions")
PLATFORMS = (("google_ads", "google_ads"), ("meta", "meta_ads"), ("openai_ads", "openai_ads"))


@dataclass(frozen=True)
class Message:
    figure: str
    title: str
    detail: str
    confidence: Confidence
    key: str
    state: str = ""  # state of the change the title speaks of (fav, unfav, neutral, ns), "" when none


def lower(conf: Confidence) -> Confidence:
    """One notch lower (high -> medium -> low): used when the change a message speaks of is not significant."""
    level = {HIGH: MEDIUM, MEDIUM: LOW}.get(conf.level, LOW)
    return Confidence(level, conf.score, conf.reasons + ("not_significant",))


def first_num(mapping: Any, keys: tuple[str, ...]) -> float | None:
    """The first numeric value among ``keys`` of ``mapping``."""
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        if c.is_num(mapping.get(key)):
            return mapping[key]
    return None


def ga4_revenue(data: dict, period: str = "current") -> tuple[float | None, str | None, Any]:
    """(revenue, currency, node) from the GA4 KPI report of ``period``."""
    node = c.view(data, "ga4", "kpi", period)
    revenue = first_num(c.totals(node), REVENUE_KEYS)
    currency = (c.currency_of(node, "purchaseRevenue") or c.currency_of(node, "totalRevenue")
                or (data.get("meta", {}).get("currency") if revenue is not None else None))
    return revenue, currency if isinstance(currency, str) else None, node


def ga4_orders(data: dict, period: str = "current") -> float | None:
    return first_num(c.totals(c.view(data, "ga4", "kpi", period)), ORDER_KEYS)


@dataclass(frozen=True)
class Mer:
    value: float | None
    revenue: float | None
    spend: float | None
    currency: str | None
    reason: str | None           # i18n key under ads.reason.*, None when value is set
    reason_args: dict


def failed_platforms(overview: Any) -> tuple[str, ...]:
    """Source keys (google_ads, meta_ads, openai_ads) of the platforms an ads_overview output reports as FAILED.

    ads_overview adds up the spend of the platforms that answered (``combined.platforms_included``) and lists
    the failing ones in ``errors`` (block ``status: "error"``). Their spend is unknown, so ``combined.spend`` is
    then NOT the total ad spend. A platform that is not configured is not a failure (the shop does not use it)."""
    if c.state(overview) != "ok":
        return ()
    platforms = overview.get("platforms") if isinstance(overview.get("platforms"), dict) else {}
    listed = overview.get("errors") if isinstance(overview.get("errors"), list) else []
    return tuple(source for key, source in PLATFORMS
                 if key in listed or (isinstance(platforms.get(key), dict)
                                      and platforms[key].get("status") == "error"))


def mer(data: dict, period: str = "current") -> Mer:
    """MER = GA4 revenue / combined ad spend (ads_overview.combined), only when both exist in one currency and
    no platform failed (the spend of a failed platform is unknown: the ratio would be overstated).

    Never sums anything itself: the spend is the overview's own ``combined.spend``. With a failed platform the
    reason is ``ads.reason.platform_failed`` and ``reason_args["platforms"]`` is a tuple of source keys."""
    revenue, rev_cur, _ = ga4_revenue(data, period)
    overview = c.view(data, "ads_overview", None, period)
    combined = overview.get("combined") if c.state(overview) == "ok" else None
    spend = c.num(combined.get("spend")) if isinstance(combined, dict) else None
    spend_cur = combined.get("currency") if isinstance(combined, dict) else None
    spend_cur = spend_cur if isinstance(spend_cur, str) and spend_cur else None
    if revenue is None:
        return Mer(None, None, spend, spend_cur, "ads.reason.no_revenue", {})
    if c.state(overview) == "ok" and not isinstance(combined, dict):
        reason = overview.get("combined_reason")
        return Mer(None, revenue, None, None, "ads.reason.combined_null",
                   {"reason": str(reason)[:200] if reason else "—"})
    failed = failed_platforms(overview)
    if failed:
        return Mer(None, revenue, spend, spend_cur, "ads.reason.platform_failed", {"platforms": failed})
    if spend is None:
        return Mer(None, revenue, None, None, "ads.reason.no_spend", {})
    if not rev_cur or not spend_cur:
        return Mer(None, revenue, spend, spend_cur, "ads.reason.currency_unknown", {})
    if rev_cur != spend_cur:
        return Mer(None, revenue, spend, spend_cur, "ads.reason.currency",
                   {"revenue_currency": rev_cur, "spend_currency": spend_cur})
    if spend <= 0:
        return Mer(None, revenue, spend, spend_cur, "ads.reason.zero_spend", {})
    return Mer(revenue / spend, revenue, spend, spend_cur, None, {})


def _severity_disapproved(issue: Any) -> bool:
    return isinstance(issue, dict) and str(issue.get("severity", "")).upper() == "DISAPPROVED"


def merchant_counts(data: dict) -> dict | None:
    """Counts from Merchant Center: {"disapproved", "limited", "approved", "top_issue", "top_issue_count",
    "truncated"}; None when no Merchant Center data was given. Unknown counts stay None.

    ``statuses`` (counted by the assistant) wins; otherwise ``merchant_product_issues`` gives the
    disapproved count (products with at least one DISAPPROVED issue) and the most frequent such issue."""
    statuses = c.view(data, "merchant_center", "statuses")
    issues_node = c.view(data, "merchant_center", "product_issues")
    if c.state(statuses) != "ok" and c.state(issues_node) != "ok":
        return None
    out: dict[str, Any] = {"disapproved": None, "limited": None, "approved": None, "top_issue": None,
                           "top_issue_count": None, "truncated": False}
    if c.state(statuses) == "ok":
        for key in ("approved", "limited", "disapproved"):
            out[key] = c.num(statuses.get(key))
    if c.state(issues_node) == "ok":
        products = [p for p in issues_node.get("products") or [] if isinstance(p, dict)]
        tally: dict[str, int] = {}
        disapproved = 0
        for product in products:
            bad = [i for i in product.get("issues") or [] if _severity_disapproved(i)]
            if bad:
                disapproved += 1
            for label in {str(i.get("description") or i.get("code") or "?")[:120] for i in bad}:
                tally[label] = tally.get(label, 0) + 1
        if out["disapproved"] is None:
            out["disapproved"] = disapproved
        if tally:
            top = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))[0]
            out["top_issue"], out["top_issue_count"] = top
        out["truncated"] = issues_node.get("truncated") is True
    return out


def _pct_words(cur: float, prev: float, lang: str) -> str:
    return fmt_delta_pct(cur, prev, lang, digits=1)


def build_messages(data: dict, lang: str) -> list[Message]:
    """Up to three messages; each sentence cites a number from the input."""
    meta = data.get("meta", {})
    out: list[Message] = []

    revenue, cur, node = ga4_revenue(data)
    if revenue is not None:
        prev, _, _ = ga4_revenue(data, "previous")
        yoy, _, _ = ga4_revenue(data, "yoy")
        orders = ga4_orders(data)
        value = fmt_money(revenue, cur, lang)
        confidence = assess(complete=data_complete(node), count=orders)
        state = ""
        if prev is not None and prev != 0:
            # The SAME test as the KPI pastille: revenue judged with the two order counts.
            info = assess_delta(revenue, prev, "revenue", counts=(orders, ga4_orders(data, "previous")))
            state = info.state
            key = ("msg.revenue.title_delta" if info.state != "ns" else
                   "msg.revenue.title_ns" if info.tested else "msg.revenue.title_untested")
            title = tr(lang, key, value=value, delta=_pct_words(revenue, prev, lang), prev=fmt_money(prev, cur, lang))
            if info.state == "ns":
                confidence = lower(confidence)
        else:
            title = tr(lang, "msg.revenue.title", value=value)
        details = []
        if yoy is not None and yoy != 0:
            info_y = assess_delta(revenue, yoy, "revenue", counts=(orders, ga4_orders(data, "yoy")))
            details.append(tr(lang, "msg.revenue.yoy_ns" if info_y.state == "ns" else "msg.revenue.yoy",
                              yoy=fmt_money(yoy, cur, lang), delta=_pct_words(revenue, yoy, lang)))
        if orders is not None:
            details.append(tr(lang, "msg.revenue.orders", orders=fmt_int(orders, lang)))
        out.append(Message(value, title, " ".join(details), confidence, "revenue", state))

    m = mer(data)
    if m.value is not None:
        overview = c.view(data, "ads_overview")
        combined = overview.get("combined") if isinstance(overview, dict) else {}
        figure = fmt_number(m.value, 1, lang)
        out.append(Message(
            figure, tr(lang, "msg.mer.title", mer=figure),
            tr(lang, "msg.mer.detail", revenue=fmt_money(m.revenue, m.currency, lang),
               spend=fmt_money(m.spend, m.currency, lang)),
            assess(complete=data_complete(node), combined=True,
                   partial=isinstance(combined, dict) and combined.get("totals_complete") is False),
            "mer"))

    mc = merchant_counts(data)
    if mc and c.is_num(mc["disapproved"]) and mc["disapproved"] > 0:
        count = fmt_int(mc["disapproved"], lang)
        detail = (tr(lang, "msg.merchant.detail", issue=mc["top_issue"],
                     issue_count=fmt_int(mc["top_issue_count"], lang)) if mc["top_issue"] else "")
        out.append(Message(count, tr(lang, "msg.merchant.title", count=count), detail,
                           assess(complete=True, partial=mc["truncated"]), "merchant"))

    gsc = c.view(data, "search_console", "totals")
    clicks = c.num(c.totals(gsc).get("clicks"))
    if clicks is not None:
        prev_clicks = c.num(c.totals(c.view(data, "search_console", "totals", "previous")).get("clicks"))
        value = fmt_int(clicks, lang)
        confidence = assess(complete=data_complete(gsc), count=clicks)
        state = ""
        if prev_clicks:
            info = assess_delta(clicks, prev_clicks, "clicks")  # the SAME test as the SEO pastille
            state = info.state
            title = tr(lang, "msg.gsc.title_ns" if info.state == "ns" else "msg.gsc.title_delta", clicks=value,
                       delta=_pct_words(clicks, prev_clicks, lang), prev=fmt_int(prev_clicks, lang))
            if info.state == "ns":
                confidence = lower(confidence)
        else:
            title = tr(lang, "msg.gsc.title", clicks=value)
        t = c.totals(gsc)
        detail = ""
        if c.is_num(t.get("impressions")) and c.is_num(t.get("position")):
            detail = tr(lang, "msg.gsc.detail", impressions=fmt_int(t["impressions"], lang),
                        position=fmt_number(t["position"], 1, lang))
        out.append(Message(value, title, detail, confidence, "gsc", state))

    overview = c.view(data, "ads_overview")
    combined = overview.get("combined") if c.state(overview) == "ok" else None
    # Not quoted as "total ad spend" when a platform failed: the overview's sum leaves that platform out.
    if (isinstance(combined, dict) and c.is_num(combined.get("spend")) and m.value is None
            and not failed_platforms(overview)):
        spend = fmt_money(combined["spend"], combined.get("currency") or meta.get("currency"), lang)
        included = combined.get("platforms_included")
        n = len(included) if isinstance(included, list) else None
        out.append(Message(spend, tr(lang, "msg.ads.title", spend=spend),
                           tr(lang, "msg.ads.detail", count=fmt_int(n, lang)) if n else "",
                           assess(complete=None, partial=combined.get("totals_complete") is False), "ads_spend"))
    return out[:3]


def build_decisions(data: dict, lang: str) -> list[str]:
    """Up to five decisions to discuss; each one names a number from the period. [] when nothing qualifies."""
    steps: list[str] = []
    mc = merchant_counts(data)
    if mc and c.is_num(mc["disapproved"]) and mc["disapproved"] > 0 and mc["top_issue"]:
        steps.append(tr(lang, "dec.merchant", count=fmt_int(mc["disapproved"], lang), issue=mc["top_issue"],
                        issue_count=fmt_int(mc["top_issue_count"], lang)))

    cur_q = c.view(data, "search_console", "queries")
    prev_q = c.view(data, "search_console", "queries", "previous")
    if c.state(cur_q) == "ok" and c.state(prev_q) == "ok":
        comparison = compare_periods(agg_from_gsc_rows(c.rows(cur_q)), agg_from_gsc_rows(c.rows(prev_q)))
        if comparison.losers:
            lo = comparison.losers[0]
            steps.append(tr(lang, "dec.gsc_loser", label=lo.label[:80], current=fmt_int(lo.current, lang),
                            previous=fmt_int(lo.previous, lang)))
        if comparison.winners:
            wi = comparison.winners[0]
            steps.append(tr(lang, "dec.gsc_winner", label=wi.label[:80], delta=fmt_int(wi.delta, lang)))

    overview = c.view(data, "ads_overview")
    platforms = overview.get("platforms") if c.state(overview) == "ok" else None
    if isinstance(platforms, dict):
        for key, source in PLATFORMS:
            block = platforms.get(key)
            if not isinstance(block, dict) or block.get("status") != "ok":
                continue
            spend, conv = c.num(block.get("spend")), c.num(block.get("conversions"))
            if spend and spend > 0 and conv == 0:
                steps.append(tr(lang, "dec.ads_no_conv", platform=tr(lang, f"source.{source}"),
                                spend=fmt_money(spend, block.get("currency"), lang)))

    failing = [s for s in c.SOURCES if _source_failed(data, s)]
    if failing:
        steps.append(tr(lang, "dec.errors", count=fmt_int(len(failing), lang)))
    return steps[:5]


def _source_failed(data: dict, source: str) -> bool:
    value = data.get("sources", {}).get(source)
    if c.state(value) == "error":
        return True
    if not isinstance(value, dict):
        return False
    wrappers = set(c.PERIOD_KEYS) | set(c.PARTS.get(source, ()))
    for key, sub in value.items():
        if key not in wrappers:
            continue
        if c.state(sub) == "error":
            return True
        if isinstance(sub, dict) and any(c.state(sub.get(p)) == "error" for p in c.PERIOD_KEYS):
            return True
    return False

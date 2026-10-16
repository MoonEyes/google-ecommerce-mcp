# Derived from OpenGSC (MIT), src/lib/gsc/periodCompare.ts and src/lib/reports/render.ts (fmtDeltaPct); ported to Python
"""This period against the previous one, for queries or pages: winners, losers, new rows, position buckets.

Pure functions, no network. Both aggregates must be complete for the comparison to mean anything: a key
that merely fell outside a top-N list of the previous period would read as "new". The report shows what it
was given and says so; it never fills a missing previous value with zero.

Differences from upstream:

* ``avg_position`` returns None (not 0) when there are no impressions: no position is not position 0.
* ``pct_change`` and ``fmt_delta_pct`` divide by ``abs(previous)`` so that a negative baseline (a refund
  balance, a negative margin) still gives the direction of ``current - previous``.
* rounding is half away from zero (2.5 -> 3, -2.5 -> -3) everywhere, so ``pct_change`` and ``fmt_delta_pct``
  always agree; upstream's Math.round rounds -2.5 to -2.
"""

from __future__ import annotations

from .numbers import is_num as _numbers_is_num

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

TABLE_ROWS = 1000
MOVERS_ROWS = 20
NEW_ROWS = 100

MINUS = "−"  # typographic minus sign
NBSP = " "

GSC_METRICS = frozenset({"clicks", "impressions", "ctr", "position"})


def _round_half_up(x: float) -> int:
    """Round half away from zero: 2.5 -> 3, -2.5 -> -3 (one rule for every percentage of the report)."""
    return int(math.copysign(math.floor(abs(x) + 0.5), x))


def _is_num(value: Any) -> bool:
    return _numbers_is_num(value)


@dataclass(frozen=True)
class KeyAgg:
    """One key's aggregate: clicks, impressions and sum(position * impressions)."""

    clicks: float = 0
    impressions: float = 0
    pos_weighted: float = 0


def pct_change(curr: float | None, prev: float | None) -> int | None:
    """Whole-number % change, or None when there is no baseline.

    Growth from zero is not "+100 %": it is unbounded, and the report says "new" instead of a number that
    reads like a doubling. 0 -> 0 is 0 (no change). A missing value on either side is None."""
    if not _is_num(curr) or not _is_num(prev):
        return None
    if prev == 0:
        return 0 if curr == 0 else None
    return _round_half_up((curr - prev) / abs(prev) * 100)


def avg_position(impressions: float, pos_weighted: float) -> float | None:
    """Period position weighted by impressions (Google's own definition), None without impressions."""
    if not _is_num(impressions) or impressions <= 0 or not _is_num(pos_weighted):
        return None
    return pos_weighted / impressions


def weighted_position(rows: Iterable[Mapping[str, Any]]) -> float | None:
    """Average position of several rows ({"impressions", "position"}), weighted by impressions."""
    impressions = weighted = 0.0
    for row in rows:
        imp, pos = row.get("impressions"), row.get("position")
        if _is_num(imp) and _is_num(pos) and imp > 0:
            impressions += imp
            weighted += pos * imp
    return avg_position(impressions, weighted)


def fmt_delta_pct(current: float | None, previous: float | None, lang: str = "en", digits: int = 0) -> str:
    """"+18 %" / "−4 %" against the previous period; "—" when either side is missing or the baseline is 0.

    Never "+0 %" for an unknown baseline. The minus sign is the typographic one (U+2212). French puts a
    no-break space before the percent sign."""
    if not _is_num(current) or not _is_num(previous) or previous == 0:
        return "—"
    pct = (current - previous) / abs(previous) * 100
    scale = 10 ** max(0, digits)
    rounded = _round_half_up(abs(pct) * scale) / scale
    sign = "" if rounded == 0 else ("+" if pct > 0 else MINUS)
    text = f"{rounded:.{max(0, digits)}f}"
    if lang == "fr":
        text = text.replace(".", ",")
    return f"{sign}{text}{NBSP if lang == 'fr' else ''}%"


@dataclass(frozen=True)
class TableRow:
    label: str
    clicks: float
    impressions: float
    ctr: float | None
    position: float | None
    clicks_pct: int | None
    impressions_pct: int | None


@dataclass(frozen=True)
class Mover:
    label: str
    current: float
    previous: float
    delta: float


@dataclass
class Comparison:
    rows: list[TableRow]
    total: int
    winners: list[Mover]
    losers: list[Mover]
    new_rows: list[TableRow]
    buckets: dict[str, int] = field(default_factory=dict)
    visible_clicks: float = 0


def _round1(x: float | None) -> float | None:
    return None if x is None else _round_half_up(x * 10) / 10


def _to_row(label: str, c: KeyAgg, p: KeyAgg | None) -> TableRow:
    return TableRow(
        label=label,
        clicks=c.clicks,
        impressions=c.impressions,
        ctr=_round1(c.clicks / c.impressions * 100) if c.impressions > 0 else None,
        position=_round1(avg_position(c.impressions, c.pos_weighted)),
        clicks_pct=pct_change(c.clicks, p.clicks if p else 0),
        impressions_pct=pct_change(c.impressions, p.impressions if p else 0),
    )


def _by_traffic(item: tuple[str, KeyAgg]) -> tuple[float, float, str]:
    # Deterministic: clicks desc, impressions desc, label asc.
    return (-item[1].clicks, -item[1].impressions, item[0])


def compare_periods(curr: Mapping[str, KeyAgg], prev: Mapping[str, KeyAgg]) -> Comparison:
    """Compare two complete per-key aggregates (label -> KeyAgg)."""
    ordered = sorted(curr.items(), key=_by_traffic)

    movers: list[Mover] = []
    for key in set(curr) | set(prev):
        if not key:
            continue
        c = curr[key].clicks if key in curr else 0
        p = prev[key].clicks if key in prev else 0
        if c != p:
            movers.append(Mover(key, c, p, c - p))
    winners = sorted((m for m in movers if m.delta > 0),
                     key=lambda m: (-m.delta, -m.current, m.label))[:MOVERS_ROWS]
    losers = sorted((m for m in movers if m.delta < 0),
                    key=lambda m: (m.delta, -m.current, m.label))[:MOVERS_ROWS]

    new_rows = [_to_row(k, c, None) for k, c in ordered
                if c.impressions > 0 and not (k in prev and prev[k].impressions)][:NEW_ROWS]

    buckets = {"1-3": 0, "4-10": 0, "11-20": 0, "21+": 0}
    visible = 0.0
    for _, c in ordered:
        visible += c.clicks
        pos = avg_position(c.impressions, c.pos_weighted)
        if pos is None:
            continue
        if pos <= 3:
            buckets["1-3"] += 1
        elif pos <= 10:
            buckets["4-10"] += 1
        elif pos <= 20:
            buckets["11-20"] += 1
        else:
            buckets["21+"] += 1

    return Comparison(
        rows=[_to_row(k, c, prev.get(k)) for k, c in ordered[:TABLE_ROWS]],
        total=len(ordered),
        winners=winners,
        losers=losers,
        new_rows=new_rows,
        buckets=buckets,
        visible_clicks=visible,
    )


def dimension_of(row: Mapping[str, Any]) -> str | None:
    """The dimension key of a gsc_performance row (the first key that is not a metric)."""
    for key in row:
        if key not in GSC_METRICS:
            return key
    return None


def agg_from_gsc_rows(rows: Any, dimension: str | None = None) -> dict[str, KeyAgg]:
    """gsc_performance rows ({"query": ..., "clicks", "impressions", "position"}) -> label -> KeyAgg.

    Also accepts the raw API shape ({"keys": [...]}). Rows without a label are skipped."""
    out: dict[str, KeyAgg] = {}
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        if isinstance(row.get("keys"), list) and row["keys"]:
            label = row["keys"][0]
        else:
            dim = dimension or dimension_of(row)
            label = row.get(dim) if dim else None
        if not isinstance(label, str) or not label:
            continue
        impressions = row.get("impressions") if _is_num(row.get("impressions")) else 0
        clicks = row.get("clicks") if _is_num(row.get("clicks")) else 0
        position = row.get("position") if _is_num(row.get("position")) else 0
        out[label] = KeyAgg(clicks=clicks, impressions=impressions, pos_weighted=position * impressions)
    return out


# ---------------------------------------------------------------------------------------------------------------
# Change direction, polarity and statistical noise (not ported: written for this report).
#
# These significance rules are statistical APPROXIMATIONS chosen for a monthly report, not a published norm:
#
# * counts (orders, clicks, new customers, conversions): a change is shown as real only when
#   |N1 - N0| > 2 * sqrt(N1 + N0), i.e. beyond about two standard deviations of a Poisson difference;
# * rates (conversion rate, CTR): |p1 - p0| > 2 * sqrt(p1(1-p1)/n1 + p0(1-p0)/n0) when both denominators are
#   known; without them, a fallback threshold of 5 % relative change is used;
# * revenue (and any amount given its order counts): the RELATIVE change of the amount is compared with the
#   relative noise of the order counts, 2 * sqrt(N1 + N0) / ((N1 + N0) / 2). With 200 and 200 orders the
#   threshold is 20 %: -40 % of revenue at constant orders is significant, +18.8 % with 180 / 153 orders is
#   not. Without the order counts, revenue is "not tested" (state ns, ``tested`` False), never "significant";
# * average order value and other ratios (ROAS, CPA, CPC, CPM, MER, spend): the variance of a basket is unknown,
#   so a 5 % relative fallback is used;
# * "any" metrics (disapproved or limited products): any change of at least one product counts;
# * whatever the rule, a relative change under 1 % is always "stable".
#
# Polarity decides favourable / unfavourable; the arrow and the sign carry the direction. Neutral metrics
# (ad spend, impressions, frequency, reach) are never coloured.
# ---------------------------------------------------------------------------------------------------------------

UP_GOOD = "up_good"
UP_BAD = "up_bad"
NEUTRAL = "neutral"

POLARITY: dict[str, str] = {
    **{m: UP_GOOD for m in ("revenue", "orders", "new_customers", "conversion_rate", "aov", "clicks", "ctr", "roas",
                            "mer", "impression_share", "approved", "sessions", "conversions", "purchases",
                            "conversion_value", "users")},
    **{m: UP_BAD for m in ("bounce_rate", "cpa", "cpc", "cpm", "limited", "disapproved", "lcp", "inp", "cls",
                           "position")},
    **{m: NEUTRAL for m in ("spend", "impressions", "frequency", "reach")},
}

KIND: dict[str, str] = {
    **{m: "count" for m in ("orders", "clicks", "new_customers", "conversions", "purchases", "sessions", "users",
                            "approved", "impressions", "reach")},
    **{m: "rate" for m in ("conversion_rate", "ctr", "bounce_rate", "impression_share")},
    **{m: "any" for m in ("disapproved", "limited")},
    "position": "position",
}

#: Amounts that are judged with their order counts and are "not tested" without them.
COUNT_TESTED_AMOUNTS = frozenset({"revenue"})

STABLE_FLOOR_PCT = 1.0
FALLBACK_PCT = 5.0
SMALL_ORDERS = 500
SMALL_BASE = 30

#: Core Web Vitals thresholds at the 75th percentile: (good up to, poor above). LCP in s, INP in ms.
CWV_THRESHOLDS: dict[str, tuple[float, float]] = {"lcp": (2.5, 4.0), "inp": (200.0, 500.0), "cls": (0.1, 0.25)}


@dataclass(frozen=True)
class DeltaInfo:
    """How a value moved: state (fav, unfav, neutral, ns, na), direction (up, down, flat) and the numbers."""

    state: str
    direction: str
    diff: float | None = None
    pct: float | None = None
    significant: bool = False
    new: bool = False
    tested: bool = True  # False: no test was possible (e.g. revenue without order counts)


def polarity_of(metric: str) -> str:
    return POLARITY.get(metric, NEUTRAL)


def counts_significant(n1: float, n0: float) -> bool:
    """|N1 - N0| > 2 * sqrt(N1 + N0) (Poisson approximation)."""
    if n1 < 0 or n0 < 0:
        return abs(n1 - n0) > 0
    return abs(n1 - n0) > 2 * math.sqrt(n1 + n0)


def rates_significant(p1: float, p0: float, n1: float | None, n0: float | None) -> bool:
    """Two-proportion test at about 2 standard errors; 5 % relative fallback when a denominator is unknown."""
    if _is_num(n1) and _is_num(n0) and n1 > 0 and n0 > 0 and 0 <= p1 <= 1 and 0 <= p0 <= 1:
        se = math.sqrt(p1 * (1 - p1) / n1 + p0 * (1 - p0) / n0)
        return abs(p1 - p0) > 2 * se
    if p0 == 0:
        return p1 != 0
    return abs((p1 - p0) / abs(p0) * 100) > FALLBACK_PCT


def amount_significant(pct: float | None, n1: float, n0: float) -> bool:
    """An amount's relative change against the relative noise of its order counts (see the block comment).

    pct None (growth from zero) is significant when the counts themselves moved significantly."""
    if n1 < 0 or n0 < 0:
        return False
    mean = (n1 + n0) / 2
    if pct is None or mean <= 0:
        return counts_significant(n1, n0)
    return abs(pct) / 100 > 2 * math.sqrt(n1 + n0) / mean


def assess_delta(current: Any, previous: Any, metric: str, *, n: tuple[Any, Any] | None = None,
                 counts: tuple[Any, Any] | None = None) -> DeltaInfo:
    """Classify the move from ``previous`` to ``current`` for ``metric``.

    ``n`` = (n_current, n_previous) denominators for a rate; ``counts`` = (orders_current, orders_previous)
    to judge an amount such as revenue. A missing side is state "na", never a zero."""
    if not _is_num(current) or not _is_num(previous):
        return DeltaInfo("na", "flat")
    diff = current - previous
    direction = "up" if diff > 0 else "down" if diff < 0 else "flat"
    pct = None if previous == 0 else diff / abs(previous) * 100
    new = previous == 0 and current != 0
    if direction == "flat" or (pct is not None and abs(pct) < STABLE_FLOOR_PCT):
        return DeltaInfo("ns", direction, diff, pct, False, new)
    kind = KIND.get(metric, "amount")
    has_counts = bool(counts) and _is_num(counts[0]) and _is_num(counts[1])
    if kind == "any":
        significant = abs(diff) >= 1
    elif kind == "count":
        significant = counts_significant(current, previous)
    elif kind == "rate":
        significant = rates_significant(current, previous, *(n or (None, None)))
    elif has_counts:
        significant = amount_significant(pct, counts[0], counts[1])
    elif metric in COUNT_TESTED_AMOUNTS:
        return DeltaInfo("ns", direction, diff, pct, False, new, tested=False)
    else:
        significant = True if pct is None else abs(pct) > FALLBACK_PCT
    if not significant:
        return DeltaInfo("ns", direction, diff, pct, False, new)
    polarity = polarity_of(metric)
    if polarity == NEUTRAL:
        state = "neutral"
    elif (direction == "up") == (polarity == UP_GOOD):
        state = "fav"
    else:
        state = "unfav"
    return DeltaInfo(state, direction, diff, pct, True, new)


def is_small_volume(current: Any, previous: Any, orders: Any = None) -> bool:
    """Small volume: fewer than 500 orders in the period, or a reference value under 30."""
    if _is_num(orders) and orders < SMALL_ORDERS:
        return True
    values = [v for v in (current, previous) if _is_num(v)]
    return bool(values) and min(abs(v) for v in values) < SMALL_BASE


def cwv_zone(metric: str, value: Any) -> str | None:
    """'good', 'needs_improvement' or 'poor' for a p75 value against Google's thresholds; None if unknown."""
    if metric not in CWV_THRESHOLDS or not _is_num(value):
        return None
    good, poor = CWV_THRESHOLDS[metric]
    return "good" if value <= good else "poor" if value > poor else "needs_improvement"


def percent_shares(values: list[Any], digits: int = 1) -> list[float | None]:
    """Shares in % of ``values`` rounded with the largest-remainder method, so that the shown shares add up to
    exactly 100 (never 99.9 or 100.1). None values stay None and count for nothing; [] when the total is 0."""
    known = [(i, v) for i, v in enumerate(values) if _is_num(v) and v >= 0]
    total = sum(v for _, v in known)
    if total <= 0:
        return [None] * len(values)
    unit = 10 ** digits
    raw = {i: v / total * 100 * unit for i, v in known}
    floors = {i: math.floor(x) for i, x in raw.items()}
    missing = 100 * unit - sum(floors.values())
    for i in sorted(raw, key=lambda k: (-(raw[k] - floors[k]), k))[:max(0, missing)]:
        floors[i] += 1
    return [floors[i] / unit if i in floors else None for i in range(len(values))]

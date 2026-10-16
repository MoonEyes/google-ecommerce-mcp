"""Confidence level of a reported figure: high, medium or low, with the reasons that set it.

Banded on purpose: a reader gets "haute / moyenne / basse", never a false-precision score. The idea of
combining a few 0..1 evidence components into a band is inspired by seo-monster's scoring helper (no code
copied). The rules follow docs/REPORTING.md:

* high: read from one complete source over complete days, with enough volume (at least 500 events: below
  that, the level is capped at medium, as the "small volume" note of the KPI section says);
* medium: part of the period is still settling, totals are partial, the figure is claimed by an ad platform
  under its own attribution, or it combines several sources;
* low: small counts (under 50), a source in error, or data marked incomplete and small at the same time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .numbers import is_num

HIGH = "high"
MEDIUM = "medium"
LOW = "low"
LEVELS = (HIGH, MEDIUM, LOW)

#: Volume bands (orders, conversions, clicks...). Below LOW_COUNT the figure moves with a handful of events.
LOW_COUNT = 50
SOLID_COUNT = 500

_WEIGHTS = {"complete": 0.4, "volume": 0.4, "provenance": 0.2}


@dataclass(frozen=True)
class Confidence:
    level: str
    score: float
    reasons: tuple[str, ...] = field(default_factory=tuple)


def _band(score: float) -> str:
    return HIGH if score >= 0.75 else MEDIUM if score >= 0.45 else LOW


def assess(*, complete: Any = None, count: Any = None, claimed: bool = False, combined: bool = False,
           partial: bool = False, error: bool = False) -> Confidence:
    """Band a figure.

    complete: the source's ``date_range.data_complete`` (None = not reported); count: the event volume behind
    the figure (orders, clicks...); claimed: conversions declared by an ad platform; combined: the figure
    divides one source by another (e.g. MER); partial: ``totals_complete`` false or a partial result;
    error: one of the sources needed failed."""
    if error:
        return Confidence(LOW, 0.0, ("source_error",))
    reasons: list[str] = []
    comp = {True: 1.0, None: 0.7, False: 0.4}.get(complete if isinstance(complete, bool) else None, 0.7)
    if complete is False:
        reasons.append("data_settling")
    elif complete is None:
        reasons.append("completeness_unknown")
    if is_num(count):
        if count < LOW_COUNT:
            vol = 0.0
            reasons.append("small_count")
        elif count < SOLID_COUNT:
            vol = 0.7
            reasons.append("moderate_count")
        else:
            vol = 1.0
    else:
        vol = 0.7
    prov = 1.0
    if claimed:
        prov = 0.4
        reasons.append("platform_claimed")
    if combined:
        prov = min(prov, 0.7)
        reasons.append("combined_sources")
    score = (comp * _WEIGHTS["complete"] + vol * _WEIGHTS["volume"] + prov * _WEIGHTS["provenance"])
    level = _band(score)
    if partial:
        reasons.append("partial_totals")
        if level == HIGH:
            level = MEDIUM
    if combined and level == HIGH:  # a ratio of two sources (e.g. MER) is never "high"
        level = MEDIUM
    if claimed and level == HIGH:
        level = MEDIUM
    if ("small_count" in reasons or "moderate_count" in reasons) and level == HIGH:
        level = MEDIUM
    return Confidence(level, round(score, 3), tuple(reasons))


def data_complete(node: Any) -> bool | None:
    """``date_range.data_complete`` of a tool output, or None when not reported."""
    if isinstance(node, dict) and isinstance(node.get("date_range"), dict):
        value = node["date_range"].get("data_complete")
        return value if isinstance(value, bool) else None
    return None

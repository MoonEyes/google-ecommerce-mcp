"""Helpers shared by the section modules (funnel ... events). Not a section: the registry never loads it.

Everything returning HTML returns already-escaped HTML; everything ending in ``_raw`` returns plain text.
"""

from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from .. import contract as c
from ..i18n import NBSP, fmt_date, fmt_number, tr
from ..period import parse_date
from ..render import ARROWS

if TYPE_CHECKING:  # pragma: no cover
    from ..render import ReportContext

REVENUE_KEYS = ("purchaseRevenue", "totalRevenue")
ORDER_KEYS = ("ecommercePurchases", "transactions")


def colon(ctx: "ReportContext") -> str:
    """' :' in French, ':' in English."""
    return " :" if ctx.lang == "fr" else ":"


SCHEME_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://")


def no_scheme(value: Any) -> str:
    """Free text from the input without any 'scheme://' (the report holds no URL; the text stays readable)."""
    return SCHEME_RE.sub("", "" if value is None else str(value))


def prev_raw(ctx: "ReportContext") -> str:
    """'mois précédent' / 'previous month': the previous-period label inside a sentence (plain text)."""
    label = ctx.prev_label_raw()
    return label[:1].lower() + label[1:]


def first(mapping: Any, keys: Sequence[str]) -> float | None:
    """The first finite number among ``keys`` of ``mapping`` (never a numeric string)."""
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        if c.is_num(mapping.get(key)):
            return mapping[key]
    return None


def ok(node: Any) -> bool:
    return c.state(node) == "ok"


def rows_by(node: Any, dim: str) -> dict[str, list[dict]]:
    """Rows of a tool output grouped by the text value of ``dim`` (rows without it are skipped)."""
    out: dict[str, list[dict]] = {}
    for row in c.rows(node):
        key = row.get(dim)
        if isinstance(key, str) and key.strip():
            out.setdefault(key, []).append(row)
    return out


def sum_rows(rows: Iterable[dict], keys: Sequence[str]) -> float | None:
    """Sum of one metric over rows; None as soon as one row lacks it (a missing value is never a zero)."""
    total, seen = 0.0, False
    for row in rows:
        value = first(row, keys)
        if value is None:
            return None
        total += value
        seen = True
    return total if seen else None


def pt_delta(ctx: "ReportContext", current: Any, previous: Any, *, n: tuple[Any, Any] | None = None,
             metric: str = "conversion_rate") -> str:
    """Change of a 0..1 rate in percentage POINTS, double-coded like ``ctx.delta``: arrow + sign + text, the
    same classes (fav / unfav / ns) and the same 2-sigma rule (``n`` = the two denominators)."""
    info = ctx.delta_info(current, previous, metric, n=n)
    if info.state == "na":
        return ctx.na()
    diff = (current - previous) * 100
    shown = fmt_number(abs(diff), 1, ctx.lang)
    if info.direction == "flat" or abs(diff) < 0.05:
        # Rounded to nothing: "stable", never coloured and without an arrow (ns keeps its own marker).
        text = tr(ctx.lang, "delta.stable")
        if info.state != "ns":
            return ctx.pastille("neutral", ARROWS["flat"], text)
    else:
        text = ("+" if diff > 0 else "−") + shown + NBSP + tr(ctx.lang, "delta.pt")
    return ctx.pastille(info.state, ARROWS[info.direction], text, tested=info.tested)


def lead(text_html: str) -> str:
    return f'<p class="lead">{text_html}</p>'


def notes(items: Iterable[str]) -> str:
    """``<p class="note">`` blocks from HTML items (empty ones skipped)."""
    return "".join(f'<p class="note" role="note">{n}</p>' for n in items if n)


def settling_raw(ctx: "ReportContext", node: Any) -> str:
    """'GA4 can still revise from ...' when the tool output says the last days are not complete; else ''."""
    dr = node.get("date_range") if ok(node) and isinstance(node.get("date_range"), dict) else {}
    if dr.get("data_complete") is False:
        day = parse_date(dr.get("settling_from"))
        if day:
            return tr(ctx.lang, "sec.settling", date=fmt_date(day, ctx.lang))
        return tr(ctx.lang, "sec.settling_nodate")
    return ""


def truncated_raw(ctx: "ReportContext", node: Any) -> str:
    if ok(node) and node.get("truncated") is True:
        return tr(ctx.lang, "sec.truncated", n=fmt_number(len(c.rows(node)), 0, ctx.lang))
    return ""


def pill(ctx: "ReportContext", status: str) -> str:
    """Status pill: ok / warn / unknown / info (text always written, colour last)."""
    cls = {"ok": "pill ok", "warn": "pill warn"}.get(status, "pill")
    return f'<span class="{cls}">{ctx.t("dq.status." + status)}</span>'


class Event:
    """One entry of the operator's event log (``events`` in the contract)."""

    __slots__ = ("start", "end", "label", "item", "raw_date", "index")

    def __init__(self, start: date | None, end: date | None, label: str, item: str | None, raw_date: str,
                 index: int) -> None:
        self.start, self.end, self.label, self.item, self.raw_date, self.index = (start, end, label, item,
                                                                                   raw_date, index)

    def covers(self, day: date) -> bool:
        if self.start is None:
            return False
        return self.start <= day <= (self.end or self.start)

    def date_raw(self, lang: str) -> str:
        if self.start is None:
            return self.raw_date[:20] if self.raw_date else tr(lang, "events.no_date")
        if self.end and self.end != self.start:
            return tr(lang, "period.range", start=fmt_date(self.start, lang), end=fmt_date(self.end, lang))
        return fmt_date(self.start, lang)


def events(data: dict) -> list[Event]:
    """The event log sorted by date (undated entries last, input order kept on ties).

    Accepted keys: ``date`` (YYYY-MM-DD), optional ``end`` / ``date_end``, ``label`` (or ``text``), optional
    ``item`` / ``product`` (the exact GA4 itemName an entry is about, e.g. a stock-out)."""
    out = []
    for i, raw in enumerate(data.get("events") or []):
        if not isinstance(raw, dict):
            continue
        label = raw.get("label") if isinstance(raw.get("label"), str) else raw.get("text")
        if not isinstance(label, str) or not label.strip():
            continue
        start = parse_date(raw.get("date"))
        end = parse_date(raw.get("end") or raw.get("date_end"))
        if start and end and end < start:
            end = None
        item = raw.get("item") if isinstance(raw.get("item"), str) else raw.get("product")
        out.append(Event(start, end, no_scheme(label.strip())[:300], item.strip() if isinstance(item, str) else None,
                         raw.get("date") if isinstance(raw.get("date"), str) else "", i))
    out.sort(key=lambda e: (e.start is None, e.start or date.min, e.index))
    return out

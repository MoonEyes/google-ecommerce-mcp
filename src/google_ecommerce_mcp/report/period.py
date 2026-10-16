# Derived from OpenGSC (MIT), src/lib/reports/window.ts; ported to Python
"""Report date windows: the period, the previous period and the same period one year earlier (N-1).

Pure date arithmetic, no network. Every window here is a pair of *inclusive* calendar dates
(``start <= end``), which is what the MCP tools take (``start_date`` / ``end_date``).

Two kinds of windows, as in the upstream module:

* the calendar window: ``days`` complete days ending yesterday. Event-like data (the operator's
  event log, Merchant Center state) reads it.
* the search window: the same length, but ending ``GSC_LAG_DAYS`` days ago, because Search Console
  data lags about two days; a report whose clicks disagree with Search Console for "the same 7 days"
  is a report nobody trusts.

Added here (absent upstream): the N-1 window, the same calendar dates one year earlier
(29 February maps to 28 February), and calendar-month helpers for monthly reports.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

#: How far Search Console data lags; the search window ends this many days before today.
GSC_LAG_DAYS = 2

PERIOD_KEYS: tuple[str, ...] = ("current", "previous", "yoy")


@dataclass(frozen=True)
class Window:
    """An inclusive range of calendar dates."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"window ends ({self.end}) before it starts ({self.start})")

    @property
    def days(self) -> int:
        """Number of days in the window, both ends included."""
        return (self.end - self.start).days + 1

    @property
    def start_str(self) -> str:
        return self.start.isoformat()

    @property
    def end_str(self) -> str:
        return self.end.isoformat()

    def is_full_month(self) -> bool:
        """True when the window is exactly one calendar month (1st to last day)."""
        last = calendar.monthrange(self.start.year, self.start.month)[1]
        return (self.start.day == 1 and self.end.year == self.start.year
                and self.end.month == self.start.month and self.end.day == last)


@dataclass(frozen=True)
class SearchWindow:
    """Search Console window: current, previous (same length, just before) and N-1, lagged by GSC_LAG_DAYS."""

    current: Window
    previous: Window
    yoy: Window


@dataclass(frozen=True)
class ReportWindow:
    """Calendar window (ending yesterday) with its previous and N-1 windows, plus the lagged search window."""

    current: Window
    previous: Window
    yoy: Window
    search: SearchWindow


def _today(now: datetime | date | None) -> date:
    if now is None:
        return datetime.now(timezone.utc).date()
    if isinstance(now, datetime):
        return (now.astimezone(timezone.utc) if now.tzinfo else now).date()
    return now


def _clamp_days(days: Any) -> int:
    try:
        n = int(round(float(days)))
    except (TypeError, ValueError):
        n = 1
    return max(1, n)


def shift_year(d: date, years: int = -1) -> date:
    """Same calendar date ``years`` years away; 29 February becomes 28 February in a non-leap year."""
    year = d.year + years
    day = min(d.day, calendar.monthrange(year, d.month)[1])
    return date(year, d.month, day)


def previous_of(window: Window) -> Window:
    """The window of the same length that ends the day before ``window`` starts.

    A full calendar month maps to the whole previous calendar month (September -> August), even when the
    two months differ in length: that is what a monthly report compares."""
    if window.is_full_month():
        return previous_month(window)
    end = window.start - timedelta(days=1)
    return Window(end - timedelta(days=window.days - 1), end)


def yoy_of(window: Window) -> Window:
    """The same calendar dates one year earlier (N-1). A full month maps to the same month last year."""
    if window.is_full_month():
        return month_window(window.start.year - 1, window.start.month)
    return Window(shift_year(window.start), shift_year(window.end))


def month_window(year: int, month: int) -> Window:
    """The whole calendar month."""
    return Window(date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1]))


def previous_month(window: Window) -> Window:
    """The calendar month before the month ``window`` starts in."""
    first = window.start.replace(day=1) - timedelta(days=1)
    return month_window(first.year, first.month)


def search_window(days: int, now: datetime | date | None = None) -> SearchWindow:
    """``days`` days ending GSC_LAG_DAYS days before ``now`` (UTC), with the previous and N-1 windows."""
    n = _clamp_days(days)
    last = _today(now) - timedelta(days=GSC_LAG_DAYS)
    current = Window(last - timedelta(days=n - 1), last)
    prev_end = current.start - timedelta(days=1)
    previous = Window(prev_end - timedelta(days=n - 1), prev_end)
    return SearchWindow(current, previous, Window(shift_year(current.start), shift_year(current.end)))


def report_window(days: int, now: datetime | date | None = None) -> ReportWindow:
    """``days`` complete days ending yesterday (UTC), with previous, N-1 and the lagged search window.

    Upstream ends the calendar window at the current instant; here it ends on the last complete day,
    because the MCP tools take whole dates."""
    n = _clamp_days(days)
    last = _today(now) - timedelta(days=1)
    current = Window(last - timedelta(days=n - 1), last)
    prev_end = current.start - timedelta(days=1)
    previous = Window(prev_end - timedelta(days=n - 1), prev_end)
    yoy = Window(shift_year(current.start), shift_year(current.end))
    return ReportWindow(current, previous, yoy, search_window(n, now))


def settling_days(window: Window, generated_at: date, lag_days: int = GSC_LAG_DAYS) -> int:
    """How many days at the end of ``window`` fall within ``lag_days`` of ``generated_at`` (data that can still
    move). 0 means every day of the window is settled."""
    first_unsettled = generated_at - timedelta(days=lag_days - 1)
    if window.end < first_unsettled:
        return 0
    return (window.end - max(window.start, first_unsettled)).days + 1


def parse_date(value: Any) -> date | None:
    """A ``YYYY-MM-DD`` string as a date, or None."""
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def parse_window(value: Any) -> Window | None:
    """``["YYYY-MM-DD", "YYYY-MM-DD"]`` as a Window, or None when absent or invalid."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    start, end = parse_date(value[0]), parse_date(value[1])
    if start is None or end is None or end < start:
        return None
    return Window(start, end)


def parse_periods(raw: Any) -> dict[str, Window | None]:
    """The ``periods`` block of the input as {current, previous, yoy} -> Window or None (never guessed)."""
    raw = raw if isinstance(raw, dict) else {}
    return {key: parse_window(raw.get(key)) for key in PERIOD_KEYS}


def period_warnings(periods: dict[str, Window | None]) -> list[str]:
    """Inconsistencies between the declared periods (plain English, for the input ``errors``).

    previous must end before current starts; yoy must end before current starts and sit about one year
    earlier (the same dates, or the same month). The windows are kept; only the labels change (a previous
    period that is not the month before is never called "previous month")."""
    out: list[str] = []
    cur, prev, yoy = periods.get("current"), periods.get("previous"), periods.get("yoy")
    if cur and prev and prev.end >= cur.start:
        out.append(f"periods.previous ({prev.start_str} to {prev.end_str}) overlaps or follows periods.current "
                   f"({cur.start_str} to {cur.end_str})")
    if cur and yoy:
        shift = (cur.start - yoy.start).days
        if yoy.end >= cur.start or (yoy != yoy_of(cur) and not 362 <= shift <= 368):
            out.append(f"periods.yoy ({yoy.start_str} to {yoy.end_str}) is not the same dates one year before "
                       f"periods.current ({cur.start_str} to {cur.end_str})")
    return out

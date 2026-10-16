# Derived from OpenGSC (MIT), src/lib/reports/render.ts (clicksSvg); ported to Python
"""Inline SVG charts: pure string building, deterministic, printable, no script, no library.

The upstream daily-clicks column chart is generalised into a family of small charts. Rules kept everywhere:

* every chart has ``role="img"`` and an ``aria-label``; ``figure()`` adds a visible title-message and an
  equivalent data table hidden visually (``.sr-only``) for screen readers;
* bars always start at zero (no truncated axis), one Y axis only, at most 4 series, no pie, no 3D;
* a missing value (None) is a GAP: the line is broken with a new ``M`` segment, never drawn at zero;
* no ``xmlns`` attribute (inline SVG in HTML does not need it, and the report holds no URL at all);
* every text from the data is HTML-escaped.
"""

from __future__ import annotations

from .numbers import is_num as _numbers_is_num

import html
import math
from typing import Any, Iterable, Sequence

INK = "#14201B"
REFERENCE = "#6F7F77"  # N-1 / reference lines: 4.2:1 on white (>= 3:1 for graphics)
FAV = "#0B6E99"
UNFAV = "#B33A00"
GRID = "#D5DDD7"
TRACK = "#E6EBE7"
LABEL = "#3F5249"
ZONES = (("#E4F1F7", "good"), ("#FFF4E0", "needs_improvement"), ("#FBEAE0", "poor"))
MAX_SERIES = 4


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _num(value: Any) -> bool:
    return _numbers_is_num(value)


def _f(x: float) -> str:
    return f"{x:.1f}"


def _svg(width: float, height: float, label: str, body: str) -> str:
    return (f'<svg class="chart" viewBox="0 0 {_f(width)} {_f(height)}" width="100%" role="img" '
            f'aria-label="{esc(label)}" preserveAspectRatio="xMidYMid meet">{body}</svg>')


def path_with_gaps(points: Sequence[tuple[float, float] | None]) -> str:
    """SVG path data through ``points``; a None starts a new 'M' segment (a gap, never a zero)."""
    parts: list[str] = []
    pen_down = False
    for point in points:
        if point is None:
            pen_down = False
            continue
        parts.append(f"{'L' if pen_down else 'M'}{_f(point[0])} {_f(point[1])}")
        pen_down = True
    return " ".join(parts)


def _scale_max(values: Iterable[Any]) -> float:
    nums = [abs(v) for v in values if _num(v)]
    return max(nums) if nums and max(nums) > 0 else 1.0


def sparkline(current: Sequence[Any], reference: Sequence[Any] | None = None, *, value_label: str = "",
              label: str = "", width: float = 240, height: float = 48) -> str:
    """Period series (solid ink) over the N-1 series (dashed grey), the final value written at the end."""
    series = [list(current)] + ([list(reference)] if reference else [])
    if not any(_num(v) for s in series for v in s):
        return ""
    text_w = 64 if value_label else 0
    inner_w, top, bottom = width - text_w - 4, 4.0, height - 4.0
    hi = max(v for s in series for v in s if _num(v))
    lo = min(0.0, min(v for s in series for v in s if _num(v)))
    span = (hi - lo) or 1.0
    n = max(len(s) for s in series)

    def pts(values: Sequence[Any]) -> list[tuple[float, float] | None]:
        step = inner_w / (n - 1) if n > 1 else 0
        return [(2 + i * step, bottom - (v - lo) / span * (bottom - top)) if _num(v) else None
                for i, v in enumerate(values)]

    body = ""
    if reference:
        body += (f'<path d="{path_with_gaps(pts(reference))}" fill="none" stroke="{REFERENCE}" stroke-width="1.5" '
                 f'stroke-dasharray="3 3"/>')
    body += f'<path d="{path_with_gaps(pts(current))}" fill="none" stroke="{INK}" stroke-width="2"/>'
    if value_label:
        body += (f'<text x="{_f(width - 2)}" y="{_f(height / 2 + 4)}" font-size="12" text-anchor="end" '
                 f'fill="{INK}">{esc(value_label)}</text>')
    return _svg(width, height, label, body)


def column_chart(points: Sequence[tuple[str, Any]], color: str, *, label: str = "", width: float = 640,
                 height: float = 140, value_fmt: Any = None) -> str:
    """Columns from zero (the upstream daily-clicks chart). A None value draws nothing (gap); a zero is a flat
    mark on the baseline. A NEGATIVE value is refused explicitly: it is not drawn (a gap) and not counted in
    the scale, because these columns count things from zero; use ``diverging_bars`` for signed changes."""
    points = [(name, value if _num(value) and value >= 0 else None) for name, value in points]
    if not points or not any(_num(v) for _, v in points):
        return ""
    left, right, top, bottom = 4.0, 4.0, 8.0, 18.0
    inner_w, inner_h = width - left - right, height - top - bottom
    peak = _scale_max(v for _, v in points)
    step = inner_w / (len(points) - 1) if len(points) > 1 else inner_w
    bar_w = max(1.5, min(18.0, step * 0.62))

    def x(i: int) -> float:
        return left + (i * step if len(points) > 1 else inner_w / 2)

    base = top + inner_h
    bars = []
    for i, (name, value) in enumerate(points):
        if not _num(value):
            continue
        if value == 0:
            bars.append(f'<line x1="{_f(x(i) - bar_w / 2)}" y1="{_f(base)}" x2="{_f(x(i) + bar_w / 2)}" '
                        f'y2="{_f(base)}" stroke="{GRID}" stroke-width="2" stroke-linecap="round"/>')
            continue
        h = max(1.0, value / peak * inner_h)
        shown = value_fmt(value) if value_fmt else value
        bars.append(f'<rect x="{_f(x(i) - bar_w / 2)}" y="{_f(base - h)}" width="{_f(bar_w)}" height="{_f(h)}" '
                    f'rx="1.5" fill="{esc(color)}"><title>{esc(name)} : {esc(shown)}</title></rect>')
    every = math.ceil(len(points) / 8)
    labels = [f'<text x="{_f(x(i))}" y="{_f(height - 5)}" font-size="9" fill="{LABEL}" text-anchor="middle">'
              f'{esc(str(name)[-5:])}</text>'
              for i, (name, _) in enumerate(points) if i % every == 0 or i == len(points) - 1]
    return _svg(width, height, label, "".join(bars) + "".join(labels))


def line_chart(points: Sequence[tuple[str, Any]], *, color: str = INK, reference: Sequence[Any] | None = None,
               bands: Sequence[tuple[int, int, str]] = (), label: str = "", width: float = 640,
               height: float = 170) -> str:
    """Daily line with optional N-1 dashed line and annotation bands ((first index, last index, text)).

    None values break the line (gap)."""
    if not points or not any(_num(v) for _, v in points):
        return ""
    left, right, top, bottom = 6.0, 6.0, 18.0, 20.0
    inner_w, inner_h = width - left - right, height - top - bottom
    peak = _scale_max([v for _, v in points] + list(reference or []))
    step = inner_w / (len(points) - 1) if len(points) > 1 else 0

    def xy(i: int, v: Any) -> tuple[float, float] | None:
        return (left + i * step, top + inner_h - v / peak * inner_h) if _num(v) and v >= 0 else None

    body = []
    for first, last, text in bands[:6]:
        if not (0 <= first < len(points)):
            continue
        last = min(max(first, last), len(points) - 1)
        x0 = left + first * step - step / 2 if len(points) > 1 else left
        x1 = left + last * step + step / 2 if len(points) > 1 else width - right
        x0, x1 = max(left, x0), min(width - right, x1)
        body.append(f'<rect x="{_f(x0)}" y="{_f(top)}" width="{_f(max(2.0, x1 - x0))}" height="{_f(inner_h)}" '
                    f'fill="#FFF4E0"/><text x="{_f(x0 + 2)}" y="{_f(top - 5)}" font-size="9" fill="{LABEL}">'
                    f'{esc(text[:40])}</text>')
    body.append(f'<line x1="{_f(left)}" y1="{_f(top + inner_h)}" x2="{_f(width - right)}" y2="{_f(top + inner_h)}" '
                f'stroke="{GRID}"/>')
    if reference:
        body.append(f'<path d="{path_with_gaps([xy(i, v) for i, v in enumerate(reference)])}" fill="none" '
                    f'stroke="{REFERENCE}" stroke-width="1.5" stroke-dasharray="4 3"/>')
    body.append(f'<path d="{path_with_gaps([xy(i, v) for i, (_, v) in enumerate(points)])}" fill="none" '
                f'stroke="{esc(color)}" stroke-width="2"/>')
    every = math.ceil(len(points) / 8)
    for i, (name, _) in enumerate(points):
        if i % every == 0 or i == len(points) - 1:
            body.append(f'<text x="{_f(left + i * step)}" y="{_f(height - 5)}" font-size="9" fill="{LABEL}" '
                        f'text-anchor="middle">{esc(str(name)[-5:])}</text>')
    return _svg(width, height, label, "".join(body))


def hbar_chart(items: Sequence[tuple[str, Any, str]], color: str, *, label: str = "", sort: bool = True,
               rates: Sequence[str | None] | None = None, width: float = 640, row_h: float = 26) -> str:
    """Horizontal bars from zero, label left, value text at the bar's end (channels, funnel).

    ``items`` = (name, value, value text). ``rates`` (funnel) are written between consecutive rows, e.g. the
    pass-through rate from one step to the next; a funnel keeps its order (``sort=False``)."""
    rows = [it for it in items if _num(it[1])]
    if not rows:
        return ""
    if sort:
        rows.sort(key=lambda it: -it[1])
    name_w, value_w = 170.0, 110.0
    bar_w = width - name_w - value_w
    peak = _scale_max(v for _, v, _ in rows)
    gap = 14.0 if rates else 0.0
    height = len(rows) * row_h + max(0, len(rows) - 1) * gap + 4
    body = []
    for i, (name, value, text) in enumerate(rows):
        y = 2 + i * (row_h + gap)
        w = max(0.0, value / peak * bar_w)
        body.append(f'<text x="0" y="{_f(y + row_h * 0.65)}" font-size="12" fill="{INK}">{esc(str(name)[:28])}</text>'
                    f'<rect x="{_f(name_w)}" y="{_f(y + 4)}" width="{_f(bar_w)}" height="{_f(row_h - 8)}" '
                    f'fill="{TRACK}"/>'
                    f'<rect x="{_f(name_w)}" y="{_f(y + 4)}" width="{_f(max(w, 1.0 if value > 0 else 0.0))}" '
                    f'height="{_f(row_h - 8)}" fill="{esc(color)}"/>'
                    f'<text x="{_f(name_w + w + 6)}" y="{_f(y + row_h * 0.65)}" font-size="12" fill="{INK}">'
                    f'{esc(text)}</text>')
        if rates and i + 1 < len(rows) and i + 1 < len(rates) and rates[i + 1]:
            body.append(f'<text x="{_f(name_w)}" y="{_f(y + row_h + gap - 2)}" font-size="10" fill="{LABEL}">'
                        f'↓ {esc(rates[i + 1])}</text>')
    return _svg(width, height, label, "".join(body))


def diverging_bars(winners: Sequence[tuple[str, float, str]], losers: Sequence[tuple[str, float, str]], *,
                   label: str = "", width: float = 640, row_h: float = 22) -> str:
    """Winners right of zero (blue, ▲) and losers left of zero (orange-brown, ▼), five at most on each side.

    Items are (name, delta, delta text); deltas are drawn by absolute size on one shared scale."""
    win = [w for w in winners if _num(w[1]) and w[1] > 0][:5]
    lose = [w for w in losers if _num(w[1]) and w[1] < 0][:5]
    rows = [(n, v, t, FAV, "▲") for n, v, t in win] + [(n, v, t, UNFAV, "▼") for n, v, t in lose]
    if not rows:
        return ""
    mid = width / 2
    half = mid - 150
    peak = _scale_max(v for _, v, *_ in rows)
    body = [f'<line x1="{_f(mid)}" y1="0" x2="{_f(mid)}" y2="{_f(len(rows) * row_h + 4)}" stroke="{GRID}"/>']
    for i, (name, value, text, color, arrow) in enumerate(rows):
        y = 2 + i * row_h
        w = max(1.0, abs(value) / peak * half)
        if value > 0:
            body.append(f'<rect x="{_f(mid)}" y="{_f(y + 3)}" width="{_f(w)}" height="{_f(row_h - 6)}" fill="{color}"/>'
                        f'<text x="{_f(mid + w + 4)}" y="{_f(y + row_h * 0.7)}" font-size="11" fill="{INK}">'
                        f'{arrow} {esc(text)} · {esc(str(name)[:30])}</text>')
        else:
            body.append(f'<rect x="{_f(mid - w)}" y="{_f(y + 3)}" width="{_f(w)}" height="{_f(row_h - 6)}" '
                        f'fill="{color}"/><text x="{_f(mid - w - 4)}" y="{_f(y + row_h * 0.7)}" font-size="11" '
                        f'fill="{INK}" text-anchor="end">{esc(str(name)[:30])} · {arrow} {esc(text)}</text>')
    return _svg(width, len(rows) * row_h + 4, label, "".join(body))


def stacked_100(segments: Sequence[tuple[str, Any, str, str]], *, label: str = "", width: float = 640,
                height: float = 54) -> str:
    """One 100 % bar (e.g. approved / limited / disapproved), 2 px gaps, direct labels under each segment.

    ``segments`` = (name, value, fill colour, value text)."""
    segs = [s for s in segments if _num(s[1]) and s[1] > 0][:MAX_SERIES]
    total = sum(s[1] for s in segs)
    if total <= 0:
        return ""
    gap = 2.0
    usable = width - gap * (len(segs) - 1)
    x = 0.0
    body = []
    for name, value, fill, text in segs:
        w = value / total * usable
        body.append(f'<rect x="{_f(x)}" y="0" width="{_f(max(w, 1.0))}" height="24" fill="{esc(fill)}"/>'
                    f'<text x="{_f(x + 1)}" y="42" font-size="11" fill="{INK}">{esc(name)} {esc(text)}</text>')
        x += w + gap
    return _svg(width, height, label, "".join(body))


def threshold_bar(value: Any, good: float, poor: float, *, zone_names: Sequence[str], marker_text: str,
                  label: str = "", width: float = 320, height: float = 46) -> str:
    """Three pale named zones (good, needs improvement, poor) with a marker at the p75 ``value``.

    The scale runs from 0 to 1.5 x the 'poor' threshold (or the value, when larger)."""
    if not _num(value):
        return ""
    top = max(poor * 1.5, value * 1.05)
    def sx(v: float) -> float:
        return max(0.0, min(width, v / top * width))
    bounds = [(0.0, good), (good, poor), (poor, top)]
    body = []
    for (lo, hi), (fill, _), name in zip(bounds, ZONES, zone_names):
        body.append(f'<rect x="{_f(sx(lo))}" y="14" width="{_f(sx(hi) - sx(lo))}" height="14" fill="{fill}"/>'
                    f'<text x="{_f(sx(lo) + 2)}" y="42" font-size="10" fill="{LABEL}">{esc(name)}</text>')
    mx = sx(value)
    body.append(f'<line x1="{_f(mx)}" y1="8" x2="{_f(mx)}" y2="32" stroke="{INK}" stroke-width="2"/>'
                f'<text x="{_f(min(max(mx, 20.0), width - 20))}" y="8" font-size="10" fill="{INK}" '
                f'text-anchor="middle">{esc(marker_text)}</text>')
    return _svg(width, height, label, "".join(body))


def figure(title: str, svg: str, headers: Sequence[str], rows: Sequence[Sequence[Any]], *,
           css_class: str = "fig") -> str:
    """A chart with its visible title-message and an equivalent data table hidden visually.

    ``title``, ``headers`` and the cells are plain text (escaped here). An empty ``svg`` gives ""."""
    if not svg:
        return ""
    head = "".join(f'<th scope="col">{esc(h)}</th>' for h in headers)
    body = "".join("<tr>" + "".join((f'<th scope="row">{esc(c)}</th>' if j == 0 else f"<td>{esc(c)}</td>")
                                    for j, c in enumerate(r)) + "</tr>" for r in rows)
    return (f'<figure class="{esc(css_class)}"><figcaption>{esc(title)}</figcaption>{svg}'
            f'<table class="sr-only"><caption>{esc(title)}</caption><thead><tr>{head}</tr></thead>'
            f'<tbody>{body}</tbody></table></figure>')

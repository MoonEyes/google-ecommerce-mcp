"""Section 9, site speed: Core Web Vitals as FIELD data (CrUX, 75th percentile) against Google's thresholds.

Source: ``sources.web_vitals`` (see contract.py): a list of pages ``{"url", "strategy", **pagespeed output}``
or one ``pagespeed`` output. The field values are read from, in this order:

* ``field``: ``{"lcp": seconds, "inp": milliseconds, "cls": score}`` (p75, given by the assistant; ``lcp_ms``
  is accepted too), with an optional ``field_previous`` of the same shape for the change;
* ``field_metrics``: the key of a raw ``pagespeed`` output (``{"lcp_ms", "inp_ms", "cls", ...,
  "origin_fallback"}``), present only when Google has field data for the URL or, failing that, its origin
  (``origin_fallback: true``, noted under the page);
* ``field_data`` or ``loadingExperience`` as the raw CrUX object of the PageSpeed API
  (``metrics.LARGEST_CONTENTFUL_PAINT_MS.percentile`` in ms, ``INTERACTION_TO_NEXT_PAINT`` in ms,
  ``CUMULATIVE_LAYOUT_SHIFT_SCORE`` x 100).

The ``lcp`` / ``cls`` / ``tbt`` / ``performance`` keys of a bare ``pagespeed`` output are LAB values (a
simulated load): they are shown as "lab data, not to be compared with the thresholds", without a verdict.
Thresholds (good up to, poor above): LCP 2.5 s / 4 s, INP 200 ms / 500 ms, CLS 0.1 / 0.25 (compare.cwv_zone).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from .. import contract as c
from ..compare import CWV_THRESHOLDS, cwv_zone
from ..i18n import NBSP, fmt_int, fmt_number, tr
from . import _common as h

if TYPE_CHECKING:  # pragma: no cover
    from ..render import ReportContext

ORDER = 90
METRICS = ("lcp", "inp", "cls")
CRUX = {"lcp": ("LARGEST_CONTENTFUL_PAINT_MS", 0.001), "inp": ("INTERACTION_TO_NEXT_PAINT", 1.0),
        "cls": ("CUMULATIVE_LAYOUT_SHIFT_SCORE", 0.01)}
LAB_KEYS = ("performance", "lcp", "cls", "tbt")
MAX_PAGES = 10


def fmt_value(metric: str, value: Any, lang: str) -> str:
    """'2,3 s' / '240 ms' / '0,05'."""
    if not c.is_num(value):
        return "—"
    if metric == "lcp":
        return fmt_number(value, 1, lang) + " s"
    if metric == "inp":
        return fmt_int(value, lang) + " ms"
    return fmt_number(value, 2, lang)


def _from_simple(block: Any) -> dict[str, float | None] | None:
    if not isinstance(block, dict):
        return None
    lcp = c.num(block.get("lcp"))
    if lcp is None and c.is_num(block.get("lcp_ms")):
        lcp = block["lcp_ms"] / 1000
    out = {"lcp": lcp, "inp": c.num(block.get("inp")), "cls": c.num(block.get("cls"))}
    return out if any(v is not None for v in out.values()) else None


def _from_crux(block: Any) -> dict[str, float | None] | None:
    metrics = block.get("metrics") if isinstance(block, dict) else None
    if not isinstance(metrics, dict):
        return None
    out: dict[str, float | None] = {}
    for key, (name, scale) in CRUX.items():
        entry = metrics.get(name)
        value = entry.get("percentile") if isinstance(entry, dict) else None
        out[key] = value * scale if c.is_num(value) else None
    return out if any(v is not None for v in out.values()) else None


def _from_tool(block: Any) -> dict[str, float | None] | None:
    """The ``field_metrics`` of a raw ``pagespeed`` output: {lcp_ms, inp_ms, cls} (CLS already divided by 100)."""
    if not isinstance(block, dict):
        return None
    lcp = c.num(block.get("lcp_ms"))
    out = {"lcp": lcp / 1000 if lcp is not None else None, "inp": c.num(block.get("inp_ms")),
           "cls": c.num(block.get("cls"))}
    return out if any(v is not None for v in out.values()) else None


def field_values(page: dict) -> dict[str, float | None] | None:
    """p75 field values {lcp (s), inp (ms), cls} of one page, or None when no field data was given. Read from, in
    order: ``field`` (given by the operator), ``field_metrics`` (the raw ``pagespeed`` output), a raw CrUX object
    in ``field_data`` or ``loadingExperience``."""
    return (_from_simple(page.get("field")) or _from_tool(page.get("field_metrics"))
            or _from_crux(page.get("field_data")) or _from_crux(page.get("loadingExperience")))


_LAB_NUMBER = re.compile(r"^(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(?:\s*(\S.*))?$")


def lab_text(value: str, lang: str) -> str:
    """A Lighthouse displayValue ("3.1 s", "1,230 ms", "0.08", written in English by the API) in the report's
    number format ("3,1 s" in French); any other text is returned as given."""
    text = value.strip()
    match = _LAB_NUMBER.match(text)
    if not match:
        return text
    whole, decimals, unit = match.group(1).replace(",", ""), match.group(2) or "", match.group(3)
    number = fmt_number(float(f"{whole}.{decimals}" if decimals else whole), len(decimals), lang)
    return f"{number}{NBSP}{unit.strip()}" if unit else number


def pages_of(node: Any) -> list[dict]:
    if isinstance(node, list):
        return [p for p in node if isinstance(p, dict)][:MAX_PAGES]
    if isinstance(node, dict):
        return [node]
    return []


def _page_title(ctx: "ReportContext", page: dict, i: int) -> str:
    url = page.get("url")
    name = ctx.short_url(url) if isinstance(url, str) and url else ctx.t("cwv.page_n", n=i + 1)
    strategy = page.get("strategy")
    if isinstance(strategy, str) and strategy in ("mobile", "desktop"):
        return f"{name} · {ctx.t('cwv.strategy.' + strategy)}"
    return name


def _metric_card(ctx: "ReportContext", metric: str, value: Any, previous: Any) -> str:
    label = tr(ctx.lang, "cwv.m." + metric)
    good, poor = CWV_THRESHOLDS[metric]
    rule = ctx.t("cwv.rule." + metric)
    if value is None:
        return ctx.kpi_card(label, ctx.na(), [rule])
    zone = cwv_zone(metric, value) or "needs_improvement"
    zone_text = tr(ctx.lang, "zone." + zone)
    text = fmt_value(metric, value, ctx.lang)
    title = tr(ctx.lang, "cwv.fig", label=tr(ctx.lang, "cwv.short." + metric), value=text, zone=zone_text)
    svg = ctx.svg.threshold_bar(value, good, poor, zone_names=[tr(ctx.lang, "zone." + z) for z in
                                                               ("good", "needs_improvement", "poor")],
                                marker_text=text, label=title)
    hidden = [(tr(ctx.lang, "zone.good"), tr(ctx.lang, "cwv.upto", v=fmt_value(metric, good, ctx.lang))),
              (tr(ctx.lang, "zone.needs_improvement"),
               tr(ctx.lang, "cwv.between", a=fmt_value(metric, good, ctx.lang), b=fmt_value(metric, poor, ctx.lang))),
              (tr(ctx.lang, "zone.poor"), tr(ctx.lang, "cwv.above", v=fmt_value(metric, poor, ctx.lang))),
              (tr(ctx.lang, "cwv.p75"), text)]
    figure = ctx.figure(title, svg, [tr(ctx.lang, "cwv.col.zone"), tr(ctx.lang, "cwv.col.value")], hidden)
    pill = f'<span class="{"pill ok" if zone == "good" else "pill warn"}">{ctx.esc(zone_text)}</span>'
    lines = [f"{ctx.t('cwv.zone')}{ctx.esc(h.colon(ctx))} {pill}"]
    if previous is not None:
        lines.append(f"{ctx.esc(ctx.prev_label_raw())}{ctx.esc(h.colon(ctx))} "
                     f"{ctx.esc(fmt_value(metric, previous, ctx.lang))} {ctx.delta(value, previous, metric, small=False)}")
    lines.append(rule)
    return ctx.kpi_card(label, ctx.esc(text), lines, extra=figure)


def _lab_block(ctx: "ReportContext", page: dict) -> str:
    items = []
    for key in LAB_KEYS:
        value = page.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)) and c.is_num(value):
            text = fmt_number(value, 0 if float(value).is_integer() else 2, ctx.lang)
        elif isinstance(value, str) and value.strip():
            text = lab_text(value, ctx.lang)[:20]
        else:
            continue
        items.append(f"<dt>{ctx.t('cwv.lab.' + key)}</dt><dd>{ctx.esc(text)}</dd>")
    if not items:
        return ""
    return (f'<div class="card"><p class="meta"><strong>{ctx.t("cwv.lab_title")}</strong></p>'
            f'<dl class="metrics">{"".join(items)}</dl></div>')


def render(data: dict, ctx: "ReportContext") -> str:
    node = ctx.view("web_vitals")
    missing = ctx.unavailable(node, "web_vitals")
    if missing:
        return missing
    pages = pages_of(node)
    if not pages:
        return ctx.nodata(tr(ctx.lang, "cwv.no_pages"))
    blocks, zones = [], {"good": 0, "needs_improvement": 0, "poor": 0}
    off: list[str] = []
    any_field = False
    for i, page in enumerate(pages):
        head = f"<h3>{_page_title(ctx, page, i)}</h3>"
        if c.state(page) == "error":
            blocks.append(head + ctx.nodata(tr(ctx.lang, "cwv.page_error", code=c.error_code(page))))
            continue
        field = field_values(page)
        lab = _lab_block(ctx, page)
        parts = [head]
        if field:
            any_field = True
            prev = _from_simple(page.get("field_previous")) or {}
            cards = []
            for metric in METRICS:
                value = field.get(metric)
                cards.append(_metric_card(ctx, metric, value, prev.get(metric)))
                zone = cwv_zone(metric, value)
                if zone:
                    zones[zone] += 1
                    if zone != "good":
                        off.append(tr(ctx.lang, "cwv.off_item", label=tr(ctx.lang, "cwv.short." + metric),
                                      value=fmt_value(metric, value, ctx.lang),
                                      zone=tr(ctx.lang, "zone." + zone).lower()))
            parts.append(f'<div class="grid">{"".join(cards)}</div>')
        else:
            parts.append(ctx.nodata(tr(ctx.lang, "cwv.no_field")))
        tool_field = page.get("field_metrics")
        if (field and not _from_simple(page.get("field")) and isinstance(tool_field, dict)
                and tool_field.get("origin_fallback") is True):
            parts.append(h.notes([ctx.t("cwv.origin_fallback")]))
        if lab:
            parts.append(lab)
        category = page.get("field_data")
        if isinstance(category, str) and category.strip():
            parts.append(h.notes([ctx.t("cwv.category", category=category.strip()[:30])]))
        blocks.append("".join(parts))

    measured = sum(zones.values())
    if measured:
        if off:
            title = tr(ctx.lang, "cwv.head_off", n=fmt_int(len(off), ctx.lang), total=fmt_int(measured, ctx.lang),
                       items=", ".join(off[:3]))
        else:
            title = tr(ctx.lang, "cwv.head_good", total=fmt_int(measured, ctx.lang))
        lead = h.lead(ctx.esc(title))
    else:
        lead = h.lead(ctx.t("cwv.head_lab", n=fmt_int(len(pages), ctx.lang)))
    intro = h.notes([ctx.t("cwv.intro") if any_field else ctx.t("cwv.intro_lab")])
    return lead + intro + "".join(blocks)

# Derived from OpenGSC (MIT), src/lib/reports/render.ts (renderReportHTML, markdownToHtml, escapeHtml); ported to Python
"""The renderer: report JSON in, one self-contained HTML string out. No network, no script.

The output is deliberately boring and self-contained:

* inline ``<style>``, inline SVG, the logo as a validated data URI: one file, no external request, renders
  offline; no ``<script>``, no remote font, no ``@import``, no external URL in any attribute;
* print CSS: ``@page`` A4 and a page break before every section, so "Save as PDF" gives a clean document;
* the only brand shown is the client's; there is no "Made with" line;
* every text coming from the JSON is HTML-escaped, the operator's notes included (escaped first, then a tiny
  markdown subset becomes tags; links are rendered as their text only).

``ReportContext`` is what each section receives: see ``sections/__init__.py`` for the section contract.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable, Sequence

from . import contract as c
from . import svg as svg_mod
from .brand import Brand, build_brand
from .compare import DeltaInfo, assess_delta, fmt_delta_pct, is_small_volume, KIND
from .confidence import Confidence, assess
from .i18n import (MINUS, fmt_date, fmt_int, fmt_money, fmt_month, fmt_number, fmt_pct, money_digits, norm_lang,
                   tr)
from .period import GSC_LAG_DAYS, Window, parse_date, parse_periods, period_warnings, previous_of, yoy_of
from .style import css
from .summary import build_decisions, build_messages, ga4_orders

#: The HTML holds no script, font, frame or fetch: everything but inline style and data: images is denied.
CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'"
#: What happens for each brand issue (also printed by the CLI).
BRAND_ISSUES = {
    "accent_invalid": "invalid accent colour, the default colour was used",
    "name_too_long": "client name too long, truncated to 120 characters",
    "logo_bad_format": "logo refused (not a PNG, JPEG or SVG data URI), initials shown instead",
    "logo_bad_base64": "logo refused (invalid base64), initials shown instead",
    "logo_too_large": "logo refused (over 256 KB), initials shown instead",
    "logo_unsafe_svg": "SVG logo refused (script, event handler or external reference), initials shown instead",
}
ARROWS = {"up": "▲", "down": "▼", "flat": "="}
APPROX = "≈"


def esc(value: Any) -> str:
    """HTML-escape any value (None -> "")."""
    return html.escape("" if value is None else str(value), quote=True)


def short_url(url: Any) -> str:
    """The path of a URL, without scheme, host, query string or fragment ("https://shop.example/p/x?gclid=1#a"
    -> "/p/x"): the report holds no external URL and no tracking or order parameter. Other text is cut at the
    first "?" or "#" too, then truncated to 120 characters."""
    text = "" if url is None else str(url)
    match = re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://[^/?#]*(.*)$", text)
    if match:
        text = match.group(1) or "/"
    text = re.split(r"[?#]", text, maxsplit=1)[0] or "/"
    return text[:120]


# --------------------------------------------------------------------------------------- markdown (notes)
def markdown_to_html(md: str) -> str:
    """A deliberately small subset: paragraphs, ``#``..``###`` headings (as h3/h4), ``-`` lists, ``**bold**``,
    ``*italic*``. The text is HTML-escaped FIRST; ``[text](url)`` keeps the text only (no link, no URL)."""
    lines = (md or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    in_list = False

    def inline(s: str) -> str:
        s = re.sub(r"\[([^\]\n]+)\]\([^)\n]*\)", r"\1", s)
        s = esc(s)
        s = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", s)
        return re.sub(r"\*([^*\n]+)\*", r"<em>\1</em>", s)

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            if in_list:
                out.append("</ul>")
                in_list = False
            continue
        heading = re.match(r"^(#{1,3})\s+(.*)$", line)
        bullet = re.match(r"^\s*[-*•]\s+(.*)$", line)
        if bullet and not heading:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(bullet.group(1))}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if heading:
            level = min(4, len(heading.group(1)) + 2)  # the section owns h2
            out.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
        else:
            out.append(f"<p>{inline(line)}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


# --------------------------------------------------------------------------------------- context
@dataclass
class ReportContext:
    """Everything a section needs. Every method returning text returns HTML-SAFE text (already escaped),
    except those whose name ends in ``_raw`` and ``headline`` (plain text, to escape or to pass to figure())."""

    data: dict
    lang: str
    currency: str | None
    brand: Brand
    periods: dict[str, Window | None]
    goals: dict
    generated_at: date
    svg: Any = field(default=svg_mod)

    # -- text
    def esc(self, value: Any) -> str:
        return esc(value)

    def t(self, key: str, **kwargs: Any) -> str:
        """Translated template, formatted and escaped."""
        return esc(tr(self.lang, key, **kwargs))

    def t_raw(self, key: str, **kwargs: Any) -> str:
        """Translated template, formatted, NOT escaped (plain text)."""
        return tr(self.lang, key, **kwargs)

    @property
    def accent(self) -> str:
        """The accent for strokes, bars and fills: darkened to at least 3:1 against white (see brand.py)."""
        return self.brand.accent_graphic

    # -- numbers (escaped; "—" for a missing value)
    def money(self, value: Any, currency: str | None = None) -> str:
        return esc(fmt_money(value, currency or self.currency, self.lang))

    def pct(self, ratio: Any, digits: int = 1) -> str:
        return esc(fmt_pct(ratio, digits, self.lang))

    def integer(self, value: Any) -> str:
        return esc(fmt_int(value, self.lang))

    def number(self, value: Any, digits: int = 1) -> str:
        return esc(fmt_number(value, digits, self.lang))

    def na(self) -> str:
        """Grey "not provided" marker for one missing value (no arrow, never a zero)."""
        return f'<span class="na">{self.t("na")}</span>'

    def value_or_na(self, text: str) -> str:
        return self.na() if text in ("", "—") else text

    # -- deltas
    def delta_info(self, current: Any, previous: Any, metric: str, *, n: tuple[Any, Any] | None = None,
                   counts: tuple[Any, Any] | None = None) -> DeltaInfo:
        return assess_delta(current, previous, metric, n=n, counts=counts)

    def _abs_text(self, diff: float, metric: str, currency: str | None) -> str:
        """Signed absolute change ("+38", "−0,4 pt", "+12 €"). Rounded FIRST, at the precision shown: a change
        that rounds to zero is "stable", never "−0,0 pt" nor "+0"."""
        kind = KIND.get(metric, "amount")
        money = metric in ("revenue", "aov", "spend", "cpa", "cpc", "cpm", "conversion_value")
        if kind == "rate":
            shown, digits = abs(diff) * 100, 1
        elif kind in ("count", "any"):
            shown, digits = abs(diff), 0
        elif money:
            shown, digits = abs(diff), money_digits(abs(diff))
        else:
            shown, digits = abs(diff), 1
        if float(f"{shown:.{digits}f}") == 0:
            return tr(self.lang, "delta.stable")
        sign = "+" if diff > 0 else MINUS
        if kind == "rate":
            body = f"{fmt_number(shown, 1, self.lang)}{chr(160)}{tr(self.lang, 'delta.pt')}"
        elif kind in ("count", "any"):
            body = fmt_int(shown, self.lang)
        elif money:
            body = fmt_money(shown, currency or self.currency, self.lang)
        else:
            body = fmt_number(shown, 1, self.lang)
        return sign + body

    def delta_text_raw(self, info: DeltaInfo, current: Any, previous: Any, metric: str, *,
                       small: bool = False, currency: str | None = None) -> str:
        """Plain text of a change: "+6,2 %", "+38 (+9,0 %)", "gagne 1,2 place(s)", "nouveau", "stable"."""
        if info.state == "na":
            return tr(self.lang, "na")
        if metric == "position":
            places = fmt_number(abs(info.diff or 0), 1, self.lang)
            if not info.diff or places == fmt_number(0, 1, self.lang):
                return tr(self.lang, "delta.stable")
            return tr(self.lang, "delta.gain" if info.diff < 0 else "delta.loss", n=places)
        if info.new:
            pct = tr(self.lang, "delta.new")
        else:
            pct = fmt_delta_pct(current, previous, self.lang, digits=1)
        if info.direction == "flat":
            return tr(self.lang, "delta.stable")
        if info.pct is not None and abs(info.pct) < 1:
            return f"{tr(self.lang, 'delta.stable')} ({pct})"
        if small and info.diff is not None:
            return f"{self._abs_text(info.diff, metric, currency)} ({pct})"
        return pct

    def delta(self, current: Any, previous: Any, metric: str, *, n: tuple[Any, Any] | None = None,
              counts: tuple[Any, Any] | None = None, small: bool | None = None,
              currency: str | None = None) -> str:
        """The change pastille, double-coded (arrow + sign + text, colour last):

        ``<span class="delta fav"><span aria-hidden="true">▲</span> +6,2 %</span>``. Classes: fav, unfav
        (by the metric's polarity), neutral (never coloured), ns (not significant: ≈ and a title), and
        ``<span class="na">`` when a side is missing. ``small`` (default: fewer than 500 orders in the period
        or a base under 30) adds the absolute change: "+38 (+9,0 %)"."""
        info = self.delta_info(current, previous, metric, n=n, counts=counts)
        if info.state == "na":
            return self.na()
        if small is None:
            small = is_small_volume(current, previous, ga4_orders(self.data))
        text = self.delta_text_raw(info, current, previous, metric, small=small, currency=currency)
        if metric == "position" and info.diff:
            symbol = ARROWS["up"] if info.diff < 0 else ARROWS["down"]
        else:
            symbol = ARROWS[info.direction]
        state = info.state
        if state in ("fav", "unfav", "neutral") and text == tr(self.lang, "delta.stable"):
            state, symbol = "neutral", ARROWS["flat"]  # rounded to nothing: no colour, no arrow
        return self.pastille(state, symbol, text, tested=info.tested)

    def pastille(self, state: str, symbol: str, text: str, *, tested: bool = True) -> str:
        """One change pastille from plain ``text``. ns (not significant or not tested) shows "≈", carries a
        ``title`` AND a word in a ``.ns-word`` span, read by screen readers and printed (never colour only)."""
        if state == "ns":
            title = self.t("delta.ns_title" if tested else "delta.untested_title")
            word = self.t("delta.ns_word" if tested else "delta.untested_word")
            return (f'<span class="delta ns" title="{title}"><span aria-hidden="true">{APPROX}</span> '
                    f'{esc(text)}<span class="sr-only ns-word"> ({word})</span></span>')
        return f'<span class="delta {esc(state)}"><span aria-hidden="true">{symbol}</span> {esc(text)}</span>'

    def headline(self, metric: str, label: str, current: Any, previous: Any, value_text: str, *,
                 n: tuple[Any, Any] | None = None, counts: tuple[Any, Any] | None = None) -> str:
        """Title-message derived from the delta, plain text, always citing a figure:
        "Le revenu progresse de 6,2 %", "Revenu stable à 24 900 €", "La position moyenne gagne 1,2 place(s)"."""
        info = self.delta_info(current, previous, metric, n=n, counts=counts)
        the = tr(self.lang, f"the.{metric}")
        the = label if the == f"the.{metric}" else the
        if info.state == "na":
            return tr(self.lang, "head.value", label=label, value=value_text)
        if metric == "position" and info.diff and info.state != "ns":
            key = "head.pos_gain" if info.diff < 0 else "head.pos_loss"
            return tr(self.lang, key, the=the, n=fmt_number(abs(info.diff), 1, self.lang), value=value_text)
        if info.new and info.state != "ns":
            return tr(self.lang, "head.new", the=the, value=value_text)
        pct = fmt_delta_pct(current, previous, self.lang, digits=1)
        if info.state == "ns" and not info.tested:
            return tr(self.lang, "head.untested", label=label, value=value_text, pct=pct)
        if info.state == "ns":
            if info.pct is not None and abs(info.pct) >= 1:
                return tr(self.lang, "head.up_ns", label=label, value=value_text, pct=pct)
            return tr(self.lang, "head.stable", label=label, value=value_text)
        return tr(self.lang, "head.up" if info.direction == "up" else "head.down", the=the,
                  pct=pct.lstrip("+" + MINUS))

    # -- blocks
    def nodata(self, text: str | None = None) -> str:
        """A ``.nodata`` block; ``text`` is plain text (escaped here)."""
        return f'<p class="nodata">{esc(text if text else tr(self.lang, "nodata.default"))}</p>'

    def view(self, source: str, part: str | None = None, period: str = "current") -> Any:
        return c.view(self.data, source, part, period)

    def unavailable(self, node: Any, source: str) -> str | None:
        """The .nodata block for a missing or failed source, or None when ``node`` holds data."""
        st = c.state(node)
        name = tr(self.lang, f"source.{source}")
        if st == "missing":
            return self.nodata(tr(self.lang, "nodata.source_missing", source=name))
        if st == "error":
            return self.nodata(tr(self.lang, "nodata.source_error", source=name, code=c.error_code(node)))
        return None

    def table(self, caption: str, headers: Sequence[str], rows: Iterable[Sequence[str]], *,
              numeric: Iterable[int] = ()) -> str:
        """An accessible table in a scroll container. ``caption``/``headers`` are plain text; cells are HTML
        (already escaped). The first cell of each row is a row header (``th scope="row"``)."""
        num = set(numeric)
        num_cls = ' class="num"'
        head = "".join(f'<th scope="col"{num_cls if i in num else ""}>{esc(h)}</th>'
                       for i, h in enumerate(headers))
        body = []
        for row in rows:
            cells = []
            for i, cell in enumerate(row):
                cls = ' class="num"' if i in num else ""
                cells.append(f'<th scope="row"{cls}>{cell}</th>' if i == 0 else f"<td{cls}>{cell}</td>")
            body.append("<tr>" + "".join(cells) + "</tr>")
        label = esc(tr(self.lang, "table.region", caption=caption))
        return (f'<div class="scroll" role="region" aria-label="{label}" tabindex="0"><table>'
                f'<caption class="sr-only">{esc(caption)}</caption><thead><tr>{head}</tr></thead>'
                f'<tbody>{"".join(body)}</tbody></table></div>')

    def figure(self, title: str, svg: str, headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
        """Chart + visible title-message + hidden equivalent table (plain text in, escaped here)."""
        return svg_mod.figure(title, svg, headers, rows)

    def kpi_card(self, label: str, value_html: str, lines: Sequence[str] = (), *, extra: str = "",
                 todo: bool = False) -> str:
        """A KPI card. ``label`` plain text; ``value_html``, ``lines`` and ``extra`` are HTML."""
        items = "".join(f"<li>{line}</li>" for line in lines)
        cmp = f'<ul class="cmp">{items}</ul>' if items else ""
        cls = "kpi todo" if todo else "kpi"
        return (f'<div class="{cls}"><div class="l">{esc(label)}</div>'
                f'<div class="v">{value_html}</div>{extra}{cmp}</div>')

    def confidence(self, **kwargs: Any) -> Confidence:
        return assess(**kwargs)

    def confidence_badge(self, conf: Confidence) -> str:
        level = tr(self.lang, f"confidence.{conf.level}")
        return f'<span class="badge">{self.t("confidence.label", level=level)}</span>'

    # -- periods
    def period(self, name: str = "current") -> Window | None:
        return self.periods.get(name)

    def period_text_raw(self, name: str = "current") -> str:
        """'septembre 2026' for a full month, else 'du 3 au 9 septembre 2026'; plain text."""
        w = self.periods.get(name)
        if w is None:
            return tr(self.lang, "period.none")
        if w.is_full_month():
            return fmt_month(w.start, self.lang)
        return tr(self.lang, "period.range", start=fmt_date(w.start, self.lang), end=fmt_date(w.end, self.lang))

    def prev_label_raw(self) -> str:
        """'Mois précédent' only when current is a full month and previous is exactly the month before."""
        cur, prev = self.periods.get("current"), self.periods.get("previous")
        month = bool(cur and prev and cur.is_full_month() and prev == previous_of(cur))
        return tr(self.lang, "kpi.prev_month" if month else "kpi.prev_period")

    def yoy_label_raw(self) -> str:
        """'Même mois 2025' only when current is a full month and yoy is that month one year earlier."""
        cur, yoy = self.periods.get("current"), self.periods.get("yoy")
        if cur and yoy and cur.is_full_month() and yoy == yoy_of(cur):
            return tr(self.lang, "kpi.yoy_month", year=yoy.start.year)
        return tr(self.lang, "kpi.yoy_period")

    def short_url(self, url: Any) -> str:
        return esc(short_url(url))


def build_context(data: dict, *, lang: str | None = None) -> ReportContext:
    meta = data.get("meta", {})
    language = norm_lang(lang or meta.get("lang"))
    brand = build_brand(meta.get("brand"), meta.get("client"), tr(language, "report.default_client"))
    currency = meta.get("currency") if isinstance(meta.get("currency"), str) and \
        re.fullmatch(r"[A-Z]{3}", meta.get("currency", "")) else None
    generated = parse_date(meta.get("generated_at")) or datetime.now(timezone.utc).date()
    goals = {k: v for k, v in data.get("goals", {}).items() if c.is_num(v)}
    return ReportContext(data=data, lang=language, currency=currency, brand=brand,
                         periods=parse_periods(data.get("periods")), goals=goals, generated_at=generated)


# --------------------------------------------------------------------------------------- document
def _header(ctx: ReportContext) -> str:
    b = ctx.brand
    if b.logo_data_uri:
        mark = f'<img src="{esc(b.logo_data_uri)}" alt="{ctx.t("report.logo_alt", name=b.name)}">'
    else:
        mark = f'<div class="initials" aria-hidden="true">{esc(b.initials)}</div>'
    sample = (f'<div class="sample" role="note">{ctx.t("report.sample")}</div>'
              if ctx.data.get("meta", {}).get("sample") is True else "")
    return (f'<header class="rpt"><div class="brand">{mark}<div class="client">{esc(b.name)}</div></div>'
            f'{sample}</header>')


def _intro(ctx: ReportContext) -> str:
    cur = ctx.period("current")
    month = bool(cur and cur.is_full_month())
    kicker = ctx.t("report.kicker_month" if month else "report.kicker", period=ctx.period_text_raw())
    lead = ctx.t("report.intro", current=ctx.period_text_raw("current"), previous=ctx.period_text_raw("previous"),
                 yoy=ctx.period_text_raw("yoy")) + " " + ctx.t("report.intro_readonly")
    return (f'<div class="intro"><div class="kicker">{kicker}</div>'
            f'<h1>{ctx.t("report.h1_month" if month else "report.h1")}</h1><p class="lead">{lead}</p></div>')


def _summary(ctx: ReportContext, number: int) -> str:
    messages = build_messages(ctx.data, ctx.lang)
    if not messages:
        body = ctx.nodata(tr(ctx.lang, "summary.none"))
    else:
        cards = []
        for m in messages:
            detail = (f'<p class="detail"><strong>{ctx.t("summary.figures")}</strong> {esc(m.detail)}</p>'
                      if m.detail else "")
            cards.append(f'<article class="card msg"><div class="head"><div class="figure">{esc(m.figure)}</div>'
                         f'{ctx.confidence_badge(m.confidence)}</div><p class="title">{esc(m.title)}</p>'
                         f'{detail}</article>')
        body = f'<div class="grid wide">{"".join(cards)}</div>'
    return _wrap("summary", f"{number}. {tr(ctx.lang, 'section.summary')}", body, first=True, plain=True)


def _wrap(sid: str, title: str, body: str, *, first: bool = False, plain: bool = False,
          extra_class: str = "") -> str:
    classes = " ".join(x for x in ("section", "first" if first else "", "plain" if plain else "", extra_class) if x)
    return (f'<section class="{classes}" id="s-{esc(sid)}" aria-labelledby="h-{esc(sid)}">'
            f'<h2 id="h-{esc(sid)}">{esc(title)}</h2>{body}</section>')


def _decisions(ctx: ReportContext) -> str:
    steps = build_decisions(ctx.data, ctx.lang)
    items = "".join(f"<li>{esc(s)}</li>" for s in steps)
    body = (f'<p class="meta">{ctx.t("decisions.intro")}</p>'
            + (f"<ol>{items}</ol>" if items else f'<p class="meta">{ctx.t("decisions.none")}</p>'))
    return _wrap("decisions", tr(ctx.lang, "section.decisions"), body, extra_class="decisions")


def _footer(ctx: ReportContext) -> str:
    names = [tr(ctx.lang, f"source.{s}") for s in c.provided_sources(ctx.data) if s != "ads_overview"]
    sources = (ctx.t("footer.sources", sources=", ".join(names)) if names else ctx.t("footer.no_sources"))
    by = ctx.data.get("meta", {}).get("prepared_by")
    prepared = (ctx.t("footer.prepared", client=ctx.brand.name, by=by) if isinstance(by, str) and by.strip()
                else ctx.t("footer.prepared_noby", client=ctx.brand.name))
    lines = [sources, ctx.t("footer.periods", lag=GSC_LAG_DAYS), ctx.t("footer.causal"), prepared,
             ctx.t("footer.generated", date=fmt_date(ctx.generated_at, ctx.lang))]
    return '<footer class="rpt">' + "".join(f"<p>{line}</p>" for line in lines) + "</footer>"


def render_report(raw: Any, *, lang: str | None = None, brand_name: str | None = None,
                  accent: str | None = None, logo_data_uri: str | None = None) -> str:
    """Render the report document ``raw`` (see contract.py) to one HTML string.

    The keyword overrides replace meta.lang / meta.brand values (the CLI flags); they are validated like the
    JSON ones. Raises contract.ContractError when ``raw`` is not an object."""
    from .sections import registry  # late import: sections import ReportContext from here

    data = c.sanitize(raw)
    brand = data["meta"].get("brand") if isinstance(data["meta"].get("brand"), dict) else {}
    brand = dict(brand)
    for key, value in (("name", brand_name), ("accent", accent), ("logo_data_uri", logo_data_uri)):
        if value is not None:
            brand[key] = value
    data["meta"]["brand"] = brand
    ctx = build_context(data, lang=lang)
    for issue in ctx.brand.issues:
        data["errors"].append({"source": "report", "code": issue,
                               "message": f"brand: {BRAND_ISSUES.get(issue, issue)}"})
    for warning in period_warnings(ctx.periods):
        data["errors"].append({"source": "report", "code": "period_inconsistent", "message": warning})

    parts = [_header(ctx), _intro(ctx), _summary(ctx, 1)]
    number = 1
    for sid, section in registry():
        subsection = bool(getattr(section, "SUBSECTION", False))
        try:
            body = section.render(data, ctx)
            failed = False
        except Exception as exc:  # one broken section must not hide the others
            body = ctx.nodata(tr(ctx.lang, "nodata.section_error", error=type(exc).__name__))
            failed = True
        title = tr(ctx.lang, f"section.{sid}")
        if subsection and parts and parts[-1].endswith("</section>"):
            sub = (f'<div class="subsection" id="s-{esc(sid)}"><h3>{esc(title)}</h3>{body}</div>')
            parts[-1] = parts[-1][: -len("</section>")] + sub + "</section>"
            continue
        number += 1
        parts.append(_wrap(sid, f"{number}. {title}", body, extra_class="section-error" if failed else ""))

    notes = data.get("notes", "").strip()
    if notes:
        parts.append(_wrap("notes", tr(ctx.lang, "section.notes"), markdown_to_html(notes), extra_class="notes"))
    parts.append(_decisions(ctx))
    parts.append(_footer(ctx))

    title = tr(ctx.lang, "report.title", client=ctx.brand.name, period=ctx.period_text_raw())
    return ("<!doctype html>\n"
            f'<html lang="{ctx.lang}">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<meta name="referrer" content="no-referrer">\n'
            f'<meta http-equiv="Content-Security-Policy" content="{CSP}">\n'
            f"<title>{esc(title)}</title>\n<style>{css(ctx.brand)}</style>\n</head>\n<body>\n"
            f'<main class="wrap">\n' + "\n".join(parts) + "\n</main>\n</body>\n</html>\n")


MAX_INPUT_BYTES = 50 * 1024 * 1024
MAX_LOGO_BYTES = 256 * 1024


class ReportFileError(Exception):
    """A report file could not be read or written (message is user-facing)."""


def _same_file(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` are the same file (same path, symlink, hard link or case variant)."""
    import os

    try:
        if os.path.exists(a) and os.path.exists(b) and os.path.samefile(a, b):
            return True
    except OSError:
        pass
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _read_json(input_path: str) -> Any:
    import json
    import os

    try:
        if os.path.getsize(input_path) > MAX_INPUT_BYTES:
            raise ReportFileError(f"{input_path}: larger than {MAX_INPUT_BYTES // (1024 * 1024)} MB")
        with open(input_path, encoding="utf-8") as fh:
            text = fh.read()
        return json.loads(text)
    except FileNotFoundError:
        raise ReportFileError(f"{input_path}: file not found") from None
    except json.JSONDecodeError as exc:
        raise ReportFileError(f"{input_path}: invalid JSON ({exc.msg}, line {exc.lineno})") from None
    except UnicodeDecodeError:
        raise ReportFileError(f"{input_path}: not UTF-8 text") from None
    except OSError as exc:
        raise ReportFileError(f"{input_path}: cannot be read ({exc.strerror or exc})") from None
    except RecursionError:
        raise ReportFileError(f"{input_path}: JSON nested too deeply") from None
    except ValueError as exc:  # e.g. an integer longer than Python's digit limit
        raise ReportFileError(f"{input_path}: unusable JSON value ({str(exc)[:120]})") from None
    except MemoryError:
        raise ReportFileError(f"{input_path}: too large to load") from None


def _write_atomic(output_path: str, payload: bytes, *, force: bool) -> None:
    """Write ``payload`` to a temporary file next to ``output_path``, then move it into place.

    An existing output is never truncated: on any failure it is left untouched and the temporary file is
    removed. Without ``force``, an existing output is never replaced (os.link refuses atomically)."""
    import os
    import tempfile

    exists_msg = f"{output_path}: already exists (use --force to replace it)"
    directory = os.path.dirname(os.path.abspath(output_path))
    try:
        fd, tmp = tempfile.mkstemp(prefix=".report-", suffix=".tmp", dir=directory)
    except OSError as exc:
        raise ReportFileError(f"{output_path}: cannot be written ({exc.strerror or exc})") from None
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        if force:
            os.replace(tmp, output_path)
        else:
            try:
                os.link(tmp, output_path)
            except FileExistsError:
                raise ReportFileError(exists_msg) from None
            except (OSError, NotImplementedError):  # no hard links on this file system
                if os.path.lexists(output_path):
                    raise ReportFileError(exists_msg) from None
                os.replace(tmp, output_path)
    except OSError as exc:
        raise ReportFileError(f"{output_path}: cannot be written ({exc.strerror or exc})") from None
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def write_report(input_path: str, output_path: str, *, force: bool = False, lang: str | None = None,
                 brand_name: str | None = None, accent: str | None = None,
                 logo_path: str | None = None) -> tuple[str, list[str]]:
    """Read the JSON ``input_path``, render it and write ``output_path`` (UTF-8).

    Never overwrites an existing file unless ``force``, never writes over the input file, never leaves a
    truncated output (temporary file + atomic move). Returns (output path, brand issue codes such as
    'accent_invalid', explained in BRAND_ISSUES). Raises ReportFileError with a readable message for every
    expected failure: unreadable, too large, too deeply nested or invalid input, unwritable output."""
    import os

    from .brand import logo_from_bytes

    if _same_file(input_path, output_path):
        raise ReportFileError(f"{output_path}: is the input file; choose another output (the JSON is kept)")
    if not force and os.path.lexists(output_path):
        raise ReportFileError(f"{output_path}: already exists (use --force to replace it)")
    raw = _read_json(input_path)
    logo = None
    if logo_path:
        try:
            with open(logo_path, "rb") as fh:
                data = fh.read(MAX_LOGO_BYTES + 1)
            logo = logo_from_bytes(data, logo_path)
        except (OSError, ValueError) as exc:
            raise ReportFileError(f"{logo_path}: {exc}") from None
    try:
        document = render_report(raw, lang=lang, brand_name=brand_name, accent=accent, logo_data_uri=logo)
    except c.ContractError as exc:
        raise ReportFileError(f"{input_path}: {exc}") from None
    except (RecursionError, ValueError, OverflowError, MemoryError) as exc:
        raise ReportFileError(f"{input_path}: the input cannot be rendered ({type(exc).__name__})") from None
    meta = raw.get("meta") if isinstance(raw, dict) else None
    brand_meta = meta.get("brand") if isinstance(meta, dict) else None
    probe = dict(brand_meta) if isinstance(brand_meta, dict) else {}
    for key, value in (("name", brand_name), ("accent", accent), ("logo_data_uri", logo)):
        if value is not None:
            probe[key] = value
    issues = list(build_brand(probe, "").issues)
    _write_atomic(output_path, document.encode("utf-8", errors="replace"), force=force)
    return output_path, issues

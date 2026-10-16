"""Section 10, measurement reliability: checks read off the input, the input log, consent, GA4 thresholds,
and a short glossary. The event log follows as a sub-block (sections/events.py).

Every check is deduced from what was supplied, never assumed:

* failed sources: every tool output of ``sources`` that is an error object;
* the input log: the ``errors`` entries of the contract, including those written by ``contract.sanitize``
  (personal-data keys removed, e-mail addresses masked, invalid blocks, unknown sources);
* complete days: the ``date_range.data_complete`` of GA4 and Search Console;
* partial totals: ``partial: true`` or ``totals_complete: false`` in a current tool output;
* Tag Manager (``sources.gtm_inventory``, the ``gtm_inventory`` output): Google tags configured twice for
  the same id, the number of active tags sending ``purchase``, the live version;
* GA4 realtime (``sources.ga4_realtime``, the ``ga4_realtime`` output): the counts of the last 30 minutes;
* consent and Consent Mode: ONLY from ``meta.consent`` / ``meta.consent_mode`` or a mention in ``notes``;
  "not provided" otherwise;
* GA4 thresholds: a fixed reminder (``meta.ga4_thresholds`` is quoted when given).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from .. import contract as c
from ..i18n import fmt_date, fmt_int, tr
from ..period import PERIOD_KEYS, parse_date
from . import _common as h

if TYPE_CHECKING:  # pragma: no cover
    from ..render import ReportContext

ORDER = 100
MAX_LOG = 30
GOOGLE_TAG_TYPES = frozenset({"googtag", "gaawc"})
GLOSSARY = ("session", "conversion", "pass", "pt", "ns", "mer", "roas", "p75", "position", "anonymised")
CONSENT_RE = re.compile(r"consent", re.I)


def failures(data: dict) -> list[tuple[str, str]]:
    """[(source, error code)] for every failed tool output of the input, in SOURCES order."""
    out: list[tuple[str, str]] = []
    sources = data.get("sources") if isinstance(data.get("sources"), dict) else {}
    for source in c.SOURCES:
        value = sources.get(source)
        found: list[str] = []

        def visit(node: Any, depth: int) -> None:
            if c.state(node) == "error":
                found.append(c.error_code(node))
            elif isinstance(node, list) and depth < 3:
                for item in node:
                    if c.state(item) == "error":
                        found.append(c.error_code(item))
            elif isinstance(node, dict) and depth < 3:
                wrappers = set(PERIOD_KEYS) | set(c.PARTS.get(source, ()))
                for key, sub in node.items():
                    if key in wrappers:
                        visit(sub, depth + 1)

        visit(value, 0)
        out.extend((source, code) for code in dict.fromkeys(found))
    return out


def _check(ctx: "ReportContext", status: str, title_key: str, detail: str) -> dict:
    return {"status": status, "title": tr(ctx.lang, title_key), "detail": detail}


def _sources_check(ctx: "ReportContext", data: dict) -> dict:
    failed = failures(data)
    provided = c.provided_sources(data)
    if failed:
        listed = ", ".join(f"{tr(ctx.lang, 'source.' + s)} ({code})" for s, code in failed)
        return _check(ctx, "warn", "dq.sources", ctx.t("dq.sources_failed", n=fmt_int(len(failed), ctx.lang),
                                                       list=listed))
    if not provided:
        return _check(ctx, "unknown", "dq.sources", ctx.t("dq.sources_none"))
    return _check(ctx, "ok", "dq.sources", ctx.t("dq.sources_ok", n=fmt_int(len(provided), ctx.lang)))


def _log_check(ctx: "ReportContext", data: dict) -> dict:
    entries = [e for e in data.get("errors") or [] if isinstance(e, dict)]
    pii = [e for e in entries if e.get("code") == "personal_data_removed"]
    others = [e for e in entries if e.get("code") != "personal_data_removed"]
    if not entries:
        return _check(ctx, "ok", "dq.log", ctx.t("dq.log_empty"))
    parts = []
    if pii:
        parts.append(ctx.t("dq.log_pii", n=fmt_int(len(pii), ctx.lang)))
    if others:
        parts.append(ctx.t("dq.log_other", n=fmt_int(len(others), ctx.lang)))
    return _check(ctx, "warn", "dq.log", " ".join(parts))


def _complete_check(ctx: "ReportContext") -> dict:
    found = []
    for source, part in (("ga4", "kpi"), ("search_console", "totals")):
        node = ctx.view(source, part)
        dr = node.get("date_range") if h.ok(node) and isinstance(node.get("date_range"), dict) else None
        if dr is None or not isinstance(dr.get("data_complete"), bool):
            continue
        found.append((source, dr["data_complete"], parse_date(dr.get("settling_from"))))
    if not found:
        return _check(ctx, "unknown", "dq.complete", ctx.t("dq.complete_none"))
    late = [(s, d) for s, ok, d in found if not ok]
    if late:
        items = ", ".join(tr(ctx.lang, "source." + s) + (" " + tr(ctx.lang, "dq.from", date=fmt_date(d, ctx.lang))
                                                        if d else "") for s, d in late)
        return _check(ctx, "warn", "dq.complete", ctx.t("dq.complete_late", list=items))
    names = ", ".join(tr(ctx.lang, "source." + s) for s, _, _ in found)
    return _check(ctx, "ok", "dq.complete", ctx.t("dq.complete_ok", list=names))


def _partial_check(ctx: "ReportContext") -> dict | None:
    flagged = []
    for source in c.SOURCES:
        node = ctx.view(source)
        if isinstance(node, dict) and h.ok(node) and (node.get("partial") is True
                                                      or node.get("totals_complete") is False):
            flagged.append(tr(ctx.lang, "source." + source))
    for part in ("product_issues",):
        node = ctx.view("merchant_center", part)
        if h.ok(node) and node.get("partial") is True and tr(ctx.lang, "source.merchant_center") not in flagged:
            flagged.append(tr(ctx.lang, "source.merchant_center"))
    if not flagged:
        return None
    return _check(ctx, "warn", "dq.partial", ctx.t("dq.partial_list", list=", ".join(flagged)))


def _gtm_checks(ctx: "ReportContext") -> list[dict]:
    node = ctx.view("gtm_inventory")
    if c.state(node) == "missing":
        return [_check(ctx, "unknown", "dq.gtm", ctx.t("dq.gtm_none"))]
    if c.state(node) == "error":
        return [_check(ctx, "warn", "dq.gtm", ctx.t("dq.gtm_error", code=c.error_code(node)))]
    tags = [t for t in node.get("tags") or [] if isinstance(t, dict)]
    active = [t for t in tags if t.get("paused") is not True]
    out = []
    google: dict[str, int] = {}
    for t in active:
        if str(t.get("type")) in GOOGLE_TAG_TYPES:
            params = t.get("parameters") if isinstance(t.get("parameters"), dict) else {}
            key = str(params.get("tagId") or params.get("measurementId") or t.get("name") or "?")
            google[key] = google.get(key, 0) + 1
    doubled = sum(1 for n in google.values() if n > 1)
    if doubled:
        out.append(_check(ctx, "warn", "dq.gtm_google", ctx.t("dq.gtm_google_twice", n=fmt_int(doubled, ctx.lang))))
    elif google:
        out.append(_check(ctx, "ok", "dq.gtm_google", ctx.t("dq.gtm_google_once", n=fmt_int(len(google), ctx.lang))))
    else:
        out.append(_check(ctx, "info", "dq.gtm_google", ctx.t("dq.gtm_google_none")))
    purchase = 0
    for t in active:
        params = t.get("parameters") if isinstance(t.get("parameters"), dict) else {}
        if str(t.get("type")) == "gaawe" and str(params.get("eventName", "")).strip().lower() == "purchase":
            purchase += 1
    if purchase == 1:
        out.append(_check(ctx, "ok", "dq.gtm_purchase", ctx.t("dq.gtm_purchase_one")))
    elif purchase > 1:
        out.append(_check(ctx, "warn", "dq.gtm_purchase", ctx.t("dq.gtm_purchase_many",
                                                                n=fmt_int(purchase, ctx.lang))))
    else:
        out.append(_check(ctx, "info", "dq.gtm_purchase", ctx.t("dq.gtm_purchase_none")))
    live = node.get("live_version") if isinstance(node.get("live_version"), dict) else {}
    detail = ctx.t("dq.gtm_summary", tags=fmt_int(len(tags), ctx.lang), paused=fmt_int(len(tags) - len(active), ctx.lang))
    if live.get("id") is not None:
        detail += " " + ctx.t("dq.gtm_live", id=str(live.get("id"))[:20])
    status = "info"
    if node.get("partial") is True:
        status = "warn"
        scopes = node.get("missing_scopes")
        detail += " " + ctx.t("dq.gtm_partial", scopes=", ".join(map(str, scopes))[:200] if isinstance(scopes, list)
                              and scopes else "—")
    out.append(_check(ctx, status, "dq.gtm", detail))
    return out


def _realtime_check(ctx: "ReportContext") -> dict | None:
    node = ctx.view("ga4_realtime")
    if c.state(node) == "missing":
        return None
    if c.state(node) == "error":
        return _check(ctx, "warn", "dq.realtime", ctx.t("dq.realtime_error", code=c.error_code(node)))
    t = c.totals(node)
    parts = []
    for key in ("activeUsers", "screenPageViews", "eventCount"):
        if c.is_num(t.get(key)):
            parts.append(tr(ctx.lang, "dq.rt." + key, n=fmt_int(t[key], ctx.lang)))
    if not parts:
        return _check(ctx, "info", "dq.realtime", ctx.t("dq.realtime_empty"))
    return _check(ctx, "info", "dq.realtime", ctx.t("dq.realtime_counts", list=", ".join(parts)))


def _consent_check(ctx: "ReportContext", data: dict) -> dict:
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    given = []
    for key, label_key in (("consent", "dq.consent_banner"), ("consent_mode", "dq.consent_mode")):
        value = meta.get(key)
        if isinstance(value, dict):
            value = ", ".join(f"{k}: {v}" for k, v in value.items() if isinstance(v, (str, int, float, bool)))
        if isinstance(value, (str, int, float)) and not isinstance(value, bool) and str(value).strip():
            given.append(tr(ctx.lang, label_key, value=h.no_scheme(str(value).strip())[:200]))
    if given:
        return _check(ctx, "info", "dq.consent", ctx.esc(" ".join(given)))
    if CONSENT_RE.search(data.get("notes") or ""):
        return _check(ctx, "info", "dq.consent", ctx.t("dq.consent_notes"))
    return _check(ctx, "unknown", "dq.consent", ctx.t("dq.consent_none"))


def _thresholds_check(ctx: "ReportContext", data: dict) -> dict:
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    given = meta.get("ga4_thresholds")
    detail = ctx.t("dq.thresholds")
    if isinstance(given, str) and given.strip():
        detail += " " + ctx.t("dq.thresholds_given", text=h.no_scheme(given.strip())[:200])
    return _check(ctx, "info", "dq.thresholds_title", detail)


def checks(data: dict, ctx: "ReportContext") -> list[dict]:
    out = [_sources_check(ctx, data), _log_check(ctx, data), _complete_check(ctx)]
    partial = _partial_check(ctx)
    if partial:
        out.append(partial)
    out.extend(_gtm_checks(ctx))
    rt = _realtime_check(ctx)
    if rt:
        out.append(rt)
    out.extend([_consent_check(ctx, data), _thresholds_check(ctx, data)])
    return out


def _log_table(ctx: "ReportContext", data: dict) -> str:
    entries = [e for e in data.get("errors") or [] if isinstance(e, dict)][:MAX_LOG]
    if not entries:
        return ""
    rows = [[ctx.esc(str(e.get("source") or "—")[:40]), ctx.esc(str(e.get("code") or "—")[:60]),
             ctx.esc(h.no_scheme(e.get("message") or "")[:200])] for e in entries]
    return (f"<h3>{ctx.t('dq.log_table')}</h3>"
            + ctx.table(tr(ctx.lang, "dq.log_table"), [tr(ctx.lang, "dq.col.source"), tr(ctx.lang, "dq.col.code"),
                                                      tr(ctx.lang, "dq.col.message")], rows))


def render(data: dict, ctx: "ReportContext") -> str:
    items = checks(data, ctx)
    warn = sum(1 for i in items if i["status"] == "warn")
    unknown = sum(1 for i in items if i["status"] == "unknown")
    total = len(items)
    if warn:
        head = ctx.t("dq.head_warn", n=fmt_int(warn, ctx.lang), total=fmt_int(total, ctx.lang))
    else:
        head = ctx.t("dq.head_ok", total=fmt_int(total, ctx.lang))
    if unknown:
        head += " " + ctx.t("dq.head_unknown", n=fmt_int(unknown, ctx.lang))
    cards = []
    for i in items:
        cards.append(f'<div class="card"><div class="platform-head"><strong>{ctx.esc(i["title"])}</strong>'
                     f'{h.pill(ctx, i["status"])}</div><p class="meta">{i["detail"]}</p></div>')
    glossary = ctx.table(tr(ctx.lang, "dq.glossary"), [tr(ctx.lang, "dq.col.term"), tr(ctx.lang, "dq.col.definition")],
                         [[ctx.t("gloss." + k), ctx.t("gloss." + k + ".def")] for k in GLOSSARY])
    return (h.lead(head) + f'<div class="grid wide">{"".join(cards)}</div>' + _log_table(ctx, data)
            + f"<h3>{ctx.t('dq.glossary')}</h3>" + glossary)

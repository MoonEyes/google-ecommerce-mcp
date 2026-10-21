"""ga4_key_event_audit: pure logic. Which GA4 events are marked as key events, and do they inflate conversions.

Two Data API runReport answers go in (events with eventCount and keyEvents; property totals of sessions, keyEvents
and ecommercePurchases), a list of findings comes out. Heuristics only: nothing here knows the business, so every
finding is a question to check in GA4 Admin > Events > Key events, never a verdict.

Fields: https://developers.google.com/analytics/devguides/reporting/data/v1/api-schema
"""

from __future__ import annotations

import re
from typing import Any

from . import metrics

ENGAGEMENT_EVENTS = frozenset({"page_view", "session_start", "first_visit", "user_engagement", "scroll"})
CONTACT_RX = re.compile(r"(?i)(phone|tel|mail|contact|whatsapp|click)")
RULE_NOTE = "heuristics, check in GA4 Admin > Events > Key events before changing anything"

EVENT_METRICS = ["eventCount", "keyEvents"]
TOTAL_METRICS = ["sessions", "keyEvents", "ecommercePurchases"]
ROW_LIMIT = 250


def request_events(start_date: str, end_date: str) -> dict:
    """Body of request A: events with their count and key event count, biggest key event count first."""
    return {"dateRanges": [{"startDate": start_date, "endDate": end_date}],
            "dimensions": [{"name": "eventName"}], "metrics": [{"name": m} for m in EVENT_METRICS],
            "metricAggregations": ["TOTAL"], "limit": ROW_LIMIT,
            "orderBys": [{"metric": {"metricName": "keyEvents"}, "desc": True}]}


def request_totals(start_date: str, end_date: str) -> dict:
    """Body of request B: the property totals, no dimension."""
    return {"dateRanges": [{"startDate": start_date, "endDate": end_date}],
            "metrics": [{"name": m} for m in TOTAL_METRICS]}


def _values(row: dict, headers: list[dict]) -> dict:
    return {h.get("name"): metrics.to_number(v.get("value"), h.get("type", ""))
            for h, v in zip(headers, row.get("metricValues", []))}


def parse_events(resp: dict) -> list[dict]:
    """[{event_name, event_count, key_events}] from the answer to request A (missing values count as 0)."""
    headers = resp.get("metricHeaders", [])
    names = [d.get("name") for d in resp.get("dimensionHeaders", [])]
    index = names.index("eventName") if "eventName" in names else 0
    out = []
    for row in resp.get("rows", []):
        dims = row.get("dimensionValues", [])
        vals = _values(row, headers)
        out.append({"event_name": dims[index]["value"] if len(dims) > index else "",
                    "event_count": vals.get("eventCount") or 0, "key_events": vals.get("keyEvents") or 0})
    return out


def parse_totals(resp: dict) -> dict:
    """{sessions, key_events, ecommerce_purchases} from the answer to request B (no row means no data: zeros)."""
    rows = resp.get("rows", [])
    vals = _values(rows[0], resp.get("metricHeaders", [])) if rows else {}
    return {"sessions": vals.get("sessions") or 0, "key_events": vals.get("keyEvents") or 0,
            "ecommerce_purchases": vals.get("ecommercePurchases") or 0}


def _share(part: float, whole: float) -> float | None:
    return round(part / whole * 100, 1) if whole else None


def _flags(name: str) -> list[str]:
    if name in ENGAGEMENT_EVENTS:
        return ["engagement_event_marked_key"]
    if CONTACT_RX.search(name):
        return ["contact_click_marked_key"]
    return []


def audit(rows_a: list[dict], totals_b: dict | None) -> dict:
    """Key events, totals and findings. `rows_a` is parse_events(...); `totals_b` is parse_totals(...), or None when
    request B failed (the sessions and purchases checks are then skipped and their totals are null)."""
    key_rows = [r for r in rows_a if (r.get("key_events") or 0) > 0]
    key_rows.sort(key=lambda r: r["key_events"], reverse=True)
    if totals_b is None:
        totals = {"sessions": None, "key_events": sum(r["key_events"] for r in key_rows), "ecommerce_purchases": None}
    else:
        totals = {"sessions": totals_b.get("sessions") or 0, "key_events": totals_b.get("key_events") or 0,
                  "ecommerce_purchases": totals_b.get("ecommerce_purchases") or 0}
    sessions, key_total, purchases = totals["sessions"], totals["key_events"], totals["ecommerce_purchases"]

    events = [{"event_name": r["event_name"], "event_count": r["event_count"], "key_events": r["key_events"],
               "share_pct": _share(r["key_events"], key_total), "flags": _flags(r["event_name"])} for r in key_rows]
    findings: list[dict[str, Any]] = []

    if sessions is not None and key_total > sessions:
        findings.append({"code": "key_events_exceed_sessions", "severity": "high", "event_name": None,
                         "detail": f"{key_total:g} key events for {sessions:g} sessions: one visit counts as more "
                                   "than one conversion, so conversion rates and ROAS built on key events are inflated"})
    for e in events:
        if "engagement_event_marked_key" in e["flags"]:
            findings.append({"code": "engagement_event_marked_key", "severity": "high", "event_name": e["event_name"],
                             "detail": f"{e['event_name']} is a key event ({e['key_events']:g} counted): it fires on "
                                       "ordinary visits, not on a goal"})
    for e in events:
        if "contact_click_marked_key" in e["flags"]:
            findings.append({"code": "contact_click_marked_key", "severity": "medium", "event_name": e["event_name"],
                             "detail": f"{e['event_name']} is a key event ({e['key_events']:g} counted): a contact "
                                       "click is an intent signal, not a sale; check it is meant to count as a conversion"})
    if purchases and not any(e["event_name"] == "purchase" for e in events):
        findings.append({"code": "purchase_not_key_event", "severity": "info", "event_name": "purchase",
                         "detail": f"{purchases:g} ecommerce purchases but the purchase event is not a key event: "
                                   "revenue conversions are missing from key event reports"})
    if key_total == 0:
        findings.append({"code": "no_key_events", "severity": "info", "event_name": None,
                         "detail": "no key event recorded in this period: conversions are not measured here, or "
                                   "none was marked"})

    return {"key_events": events, "totals": totals,
            "key_events_per_session": round(key_total / sessions, 2) if sessions else None,
            "findings": findings, "rule_note": RULE_NOTE}

"""Units, definitions and time helpers returned next to the numbers.

A model reads the response, not the tool description, when it compares figures. So every report says what each
metric means, in which unit it is expressed, in which timezone its dates are, and whether the last days can still
change. Without that, models confuse sessions with engagedSessions, read a 0.42 rate as 0.42 %, or compare a
partial day with a full one."""

from __future__ import annotations

import datetime
import re

# Short definitions of the GA4 metrics a shop asks about most; the ones models confuse are worded against each other.
GA4_DEFINITIONS = {
    "sessions": "All sessions started on the site, engaged or not.",
    "engagedSessions": "Sessions that lasted over 10 s, had a key event or 2+ page views. A subset of sessions.",
    "engagementRate": "engagedSessions / sessions.",
    "bounceRate": "1 - engagementRate: share of sessions that were not engaged.",
    "totalUsers": "Distinct users who logged any event in the period.",
    "activeUsers": "Distinct users with an engaged session or a first visit; GA4's default 'Users'. <= totalUsers.",
    "newUsers": "Users whose first visit falls in the period.",
    "screenPageViews": "Page (web) and screen (app) views, repeated views included.",
    "eventCount": "Number of events of any kind.",
    "keyEvents": "Events marked as key events (formerly conversions) in the property.",
    "conversions": "Legacy name of keyEvents.",
    "sessionKeyEventRate": "Share of sessions with at least one key event.",
    "ecommercePurchases": "Purchase events (orders). Counted once per transaction id.",
    "transactions": "Distinct transactions (purchase and in-app purchase events).",
    "purchaseRevenue": "Revenue from purchase events only, before refunds.",
    "totalRevenue": "purchaseRevenue + subscription + ad revenue, minus refunds.",
    "itemRevenue": "Revenue per item from purchase events (use with item dimensions).",
    "itemsPurchased": "Units sold (item quantity in purchase events).",
    "itemsViewed": "Units of items viewed (view_item).",
    "addToCarts": "add_to_cart events.",
    "checkouts": "begin_checkout events.",
    "averageSessionDuration": "Average session length.",
    "userEngagementDuration": "Total time the site was in the foreground for users.",
    "averagePurchaseRevenue": "purchaseRevenue / transactions (average order value).",
}

GA4_UNITS = {
    "TYPE_INTEGER": "count",
    "TYPE_SECONDS": "seconds",
    "TYPE_MILLISECONDS": "milliseconds",
    "TYPE_MINUTES": "minutes",
    "TYPE_HOURS": "hours",
    "TYPE_STANDARD": "number",
    "TYPE_FLOAT": "number",
}

GSC_METRICS = {
    "clicks": {"unit": "count", "definition": "Clicks from Google Search results to the site."},
    "impressions": {"unit": "count", "definition": "Times a link to the site appeared in search results."},
    "ctr": {"unit": "ratio 0 to 1", "definition": "clicks / impressions (0.05 means 5 %)."},
    "position": {"unit": "rank", "definition": "Average topmost position of the site in results, weighted by "
                                               "impressions. 1 is the top; lower is better."},
}

# Search Console reports dates in Pacific Time, whatever the site's country (Google documentation).
GSC_TIMEZONE = "America/Los_Angeles"


def ga4_unit(name: str, metric_type: str, currency: str | None) -> str:
    if metric_type == "TYPE_CURRENCY":
        return currency or "currency of the property"
    if metric_type == "TYPE_FLOAT" and name.endswith("Rate"):
        return "ratio 0 to 1"
    return GA4_UNITS.get(metric_type, "number")


def ga4_metric_info(headers: list[dict], currency: str | None) -> dict:
    """{metric: {unit, definition}} for every metric of a GA4 response."""
    info = {}
    for header in headers:
        name = header.get("name", "")
        entry = {"unit": ga4_unit(name, header.get("type", ""), currency)}
        entry["definition"] = GA4_DEFINITIONS.get(
            name, "See https://developers.google.com/analytics/devguides/reporting/data/v1/api-schema")
        info[name] = entry
    return info


def to_number(value, metric_type: str):
    """GA4 sends every value as a string; return an int or float so nothing compares '9' > '10'."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    if metric_type == "TYPE_INTEGER" and number.is_integer():
        return int(number)
    return round(number, 6)


# ------------------------------------------------------------------ time
def today_in(timezone: str | None) -> tuple[datetime.date, str]:
    """Today's date where the data is counted, and the timezone actually used."""
    if timezone:
        try:
            from zoneinfo import ZoneInfo

            return datetime.datetime.now(ZoneInfo(timezone)).date(), timezone
        except Exception:  # unknown zone, or no tz database on this machine
            pass
    return datetime.datetime.now(datetime.timezone.utc).date(), "UTC (property timezone unknown)"


_DAYS_AGO = re.compile(r"(\d+)daysAgo")


def resolve_date(value: str, today: datetime.date) -> datetime.date | None:
    """GA4 date syntax (YYYY-MM-DD, today, yesterday, NdaysAgo) to a calendar date; None if not understood."""
    value = (value or "").strip()
    if value == "today":
        return today
    if value == "yesterday":
        return today - datetime.timedelta(days=1)
    match = _DAYS_AGO.fullmatch(value)
    if match:
        return today - datetime.timedelta(days=int(match.group(1)))
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def date_range_info(start: str, end: str, timezone: str | None, settle_days: int, note: str) -> dict:
    """Resolved date range plus whether its last days can still change.

    settle_days: how many most recent days the source may still revise (GA4: 2, Search Console: 3)."""
    today, tz_used = today_in(timezone)
    start_date, end_date = resolve_date(start, today), resolve_date(end, today)
    info = {"start": start_date.isoformat() if start_date else start,
            "end": end_date.isoformat() if end_date else end,
            "timezone": tz_used}
    if (start_date and start_date.isoformat() != start) or (end_date and end_date.isoformat() != end):
        info["requested"] = {"start": start, "end": end}
    if end_date:
        first_settling = today - datetime.timedelta(days=settle_days - 1)
        info["data_complete"] = end_date < first_settling
        if not info["data_complete"]:
            info["settling_from"] = max(first_settling, start_date or first_settling).isoformat()
            info["settling_note"] = note
    return info

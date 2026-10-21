"""GA4 Admin API: custom definitions and key events (read), and two creations through the write path.

Read (read token, analytics.readonly): ga4_custom_definitions, ga4_key_events.
Write plans (write token, analytics.edit; plan_* -> google_apply):
    ga4_plan_key_event_create          POST properties/{id}/keyEvents        {eventName, countingMethod}
    ga4_plan_custom_dimension_create   POST properties/{id}/customDimensions {parameterName, displayName,
                                       description, scope, disallowAdsPersonalization}
Both are medium risk, not financial, and have no automatic rollback: deleting a key event and archiving a
custom dimension (Google documents no way back, and the slot stays taken) are deliberately absent in v1, as are
custom metrics and dimension updates. parameterName and scope of a custom dimension can never be changed: the
plan says so.

Handler "ga4": snapshot = the existing key events / custom dimensions of the property (write token); dry_run =
local validation against it (format, duplicates, quotas: 50 event / 25 user / 10 item dimensions on standard
properties); execute re-checks duplicates on the fresh snapshot of the apply, then sends the POST with its
WriteGrant. Account of an op = the GA4 property id (in the URL path: bound by write_policy).
"""

from __future__ import annotations

import dataclasses
import datetime
import re
from typing import Annotated, Any, Literal, Optional

from pydantic import Field

from . import google_write as gw
from . import metrics
from .write_core import PlatformHandler, WriteOp

PLATFORM = "ga4"
ADMIN = "https://analyticsadmin.googleapis.com/v1beta"
_P = r"https://analyticsadmin\.googleapis\.com/v1beta/properties/[0-9]+"

READ_RULES = [("GET", _P + "/customDimensions"), ("GET", _P + "/customMetrics"), ("GET", _P + "/keyEvents")]
SNAPSHOT_RULES = [("GET", _P + "/customDimensions"), ("GET", _P + "/keyEvents")]
WRITE_RULES = [("POST", _P + "/keyEvents", None, "write"), ("POST", _P + "/customDimensions", None, "write")]

READ = gw.google_read_client(PLATFORM, READ_RULES)
WRITE = gw.google_write_client(PLATFORM, WRITE_RULES, read_rules=SNAPSHOT_RULES)

KEY_EVENT = "key_event_create"
CUSTOM_DIMENSION = "custom_dimension_create"
# (fields, money_ok) per action: registered by register_write() only.
ALLOWLISTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    KEY_EVENT: (("eventName", "countingMethod"), ()),
    CUSTOM_DIMENSION: (("parameterName", "displayName", "description", "scope", "disallowAdsPersonalization"), ()),
}
COUNTING_METHODS = ("ONCE_PER_EVENT", "ONCE_PER_SESSION")
SCOPES = ("EVENT", "USER", "ITEM")
DIMENSION_QUOTA = {"EVENT": 50, "USER": 25, "ITEM": 10}  # standard properties (GA4 360: higher)
METRIC_QUOTA = 50
_NAME_RX = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,39}")
_DISPLAY_RX = re.compile(r"[A-Za-z][A-Za-z0-9_ ]{0,81}")
_RESERVED_PREFIXES = ("google_", "ga_", "firebase_")
IMMUTABLE_NOTE = ("parameterName and scope can never be changed after creation, and archiving a dimension (not "
                  "offered here) keeps its slot for 48 h")
TOOLS = ("ga4_custom_definitions", "ga4_key_events", "ga4_plan_key_event_create", "ga4_plan_custom_dimension_create")
MAX_PAGES = 5

PROPERTY_ID = Annotated[str, Field(description="Numeric GA4 property id of the client (empty: the configured one)")]
CLIENT_ID = Annotated[Optional[str], Field(description="Client id from clients_list. Required when several clients "
                                                       "are registered; empty uses the only client")]


# ------------------------------------------------------------------ helpers
def _property(ctx: Any, property_id: str) -> str:
    prop = (property_id or "").strip().removeprefix("properties/")
    if prop and not prop.isdigit():
        raise ValueError(f"property_id must be the numeric GA4 property id, got {property_id!r}")
    return gw.google_account_binding(ctx, PLATFORM, "property_id", prop)


def _list(call: Any, client: Any, ctx: Any, url: str, key: str) -> tuple[list[dict], dict | None]:
    """Every item of a paged Admin API list (pageSize 200, at most MAX_PAGES pages)."""
    items: list[dict] = []
    token = None
    for _ in range(MAX_PAGES):
        params = {"pageSize": 200, **({"pageToken": token} if token else {})}
        resp = call(client, ctx, "GET", url, params=params)
        if "error" in resp:
            return items, resp
        items.extend(i for i in resp.get(key, []) if isinstance(i, dict))
        token = resp.get("nextPageToken")
        if not token:
            break
    return items, None


def _dimension(d: dict) -> dict:
    return {"name": d.get("name"), "parameter_name": d.get("parameterName"), "display_name": d.get("displayName"),
            "scope": d.get("scope"), "description": d.get("description"),
            "disallow_ads_personalization": bool(d.get("disallowAdsPersonalization", False))}


def _metric(m: dict) -> dict:
    return {"name": m.get("name"), "parameter_name": m.get("parameterName"), "display_name": m.get("displayName"),
            "scope": m.get("scope"), "measurement_unit": m.get("measurementUnit"),
            "restricted_metric_type": m.get("restrictedMetricType", [])}


def _key_event(k: dict) -> dict:
    return {"name": k.get("name"), "event_name": k.get("eventName"), "counting_method": k.get("countingMethod"),
            "custom": k.get("custom"), "deletable": k.get("deletable"), "create_time": k.get("createTime"),
            "default_value": k.get("defaultValue")}


# ------------------------------------------------------------------ read tools
@gw.tool_guard("current configuration")
def ga4_custom_definitions(property_id: PROPERTY_ID = "", client_id: CLIENT_ID = None) -> dict:
    """Custom dimensions and custom metrics of a GA4 property (parameter name, display name, scope, unit) and how
    many slots each scope uses against the standard quotas (50 event, 25 user, 10 item dimensions; 50 metrics).
    Read only."""
    ctx = gw.resolve(client_id)
    prop = _property(ctx, property_id)
    dims, error = _list(gw.read_call, READ, ctx, f"{ADMIN}/properties/{prop}/customDimensions", "customDimensions")
    if error:
        return {**error, "client_id": ctx.client_id}
    mets, error = _list(gw.read_call, READ, ctx, f"{ADMIN}/properties/{prop}/customMetrics", "customMetrics")
    out: dict[str, Any] = {"client_id": ctx.client_id, "property_id": prop,
                           "custom_dimensions": [_dimension(d) for d in dims],
                           "custom_metrics": [_metric(m) for m in mets],
                           "quota": {"dimensions": {s: {"used": sum(1 for d in dims if d.get("scope") == s),
                                                        "limit": DIMENSION_QUOTA[s]} for s in SCOPES},
                                     "metrics": {"used": len(mets), "limit": METRIC_QUOTA},
                                     "note": "standard property limits; GA4 360 properties allow more"},
                           "untrusted_text": gw.untrusted("custom_dimensions[].display_name",
                                                          "custom_dimensions[].description",
                                                          "custom_metrics[].display_name")}
    if error:
        out.update(partial=True, partial_errors=[{"step": "custom metrics", **error}])
    return out


@gw.tool_guard("current configuration")
def ga4_key_events(property_id: PROPERTY_ID = "", client_id: CLIENT_ID = None) -> dict:
    """Key events (formerly conversions) of a GA4 property: event name, counting method, default value, whether
    it is custom and deletable. Read only."""
    ctx = gw.resolve(client_id)
    prop = _property(ctx, property_id)
    items, error = _list(gw.read_call, READ, ctx, f"{ADMIN}/properties/{prop}/keyEvents", "keyEvents")
    if error:
        return {**error, "client_id": ctx.client_id}
    return {"client_id": ctx.client_id, "property_id": prop, "key_events": [_key_event(k) for k in items],
            "count": len(items)}


# ------------------------------------------------------------------ handler
def _url_property(url: str) -> str | None:
    match = re.fullmatch(re.escape(ADMIN) + r"/properties/([0-9]+)/(keyEvents|customDimensions)", url)
    return match.group(1) if match else None


def _snapshot(ctx: Any, op: WriteOp) -> dict:
    """The property's current key events or custom dimensions (write token). Raises gw.SnapshotError."""
    prop = _url_property(op.url)
    if prop is None:
        raise gw.SnapshotError("bad_url", "the op URL is not a GA4 key event / custom dimension collection")
    gw.require_bound(ctx, PLATFORM, prop, op)  # before any request: never read another client's property
    if op.action == KEY_EVENT:
        items, error = _list(gw.write_call, WRITE, ctx, f"{ADMIN}/properties/{prop}/keyEvents", "keyEvents")
        if error:
            raise gw.SnapshotError.from_result(error, f"key events of property {prop}")
        return {"property": f"properties/{prop}", "key_events": sorted(str(k.get("eventName")) for k in items)}
    items, error = _list(gw.write_call, WRITE, ctx, f"{ADMIN}/properties/{prop}/customDimensions",
                         "customDimensions")
    if error:
        raise gw.SnapshotError.from_result(error, f"custom dimensions of property {prop}")
    # renamed keys: the plan diff matches body leaves by name, the existing dimensions must not look like "before"
    return {"property": f"properties/{prop}",
            "dimensions": [{"parameter": d.get("parameterName"), "dimension_scope": d.get("scope")} for d in items]}


def _problems(op: WriteOp) -> tuple[list[str], list[str]]:
    """(blocking problems, warnings) of an op against its `before` snapshot."""
    body, before = op.body or {}, op.before or {}
    problems: list[str] = []
    warnings: list[str] = []
    prop = _url_property(op.url)
    if prop is None or str(op.account_id) != prop or before.get("property") != f"properties/{prop}":
        problems.append("the URL, the op account and the snapshot do not name the same property")
    if op.action == KEY_EVENT:
        name = body.get("eventName")
        if not isinstance(name, str) or not _NAME_RX.fullmatch(name):
            problems.append("eventName: a letter then letters, digits or _ (40 characters at most)")
        elif name.lower().startswith(_RESERVED_PREFIXES):
            problems.append(f"eventName {name!r} uses a reserved prefix ({', '.join(_RESERVED_PREFIXES)})")
        elif name in (before.get("key_events") or []):
            problems.append(f"{name!r} is already a key event of this property")
        if body.get("countingMethod") not in COUNTING_METHODS:
            problems.append(f"countingMethod must be one of {COUNTING_METHODS}")
        warnings.append("a key event changes conversion reporting and, when the property is linked to Google Ads, "
                        "what Ads may import")
    elif op.action == CUSTOM_DIMENSION:
        name, scope = body.get("parameterName"), body.get("scope")
        if scope not in SCOPES:
            problems.append(f"scope must be one of {SCOPES}")
        limit = 24 if scope == "USER" else 40
        if not isinstance(name, str) or not _NAME_RX.fullmatch(name) or len(name) > limit:
            problems.append(f"parameterName: a letter then letters, digits or _ ({limit} characters at most)")
        display = body.get("displayName")
        if not isinstance(display, str) or not _DISPLAY_RX.fullmatch(display):
            problems.append("displayName: a letter then letters, digits, spaces or _ (82 characters at most)")
        if len(str(body.get("description", ""))) > 150:
            problems.append("description: 150 characters at most")
        if body.get("disallowAdsPersonalization") and scope != "USER":
            problems.append("disallowAdsPersonalization is only supported on USER scoped dimensions")
        existing = [d for d in before.get("dimensions") or [] if isinstance(d, dict)]
        if any(d.get("parameter") == name and d.get("dimension_scope") == scope for d in existing):
            problems.append(f"a {scope} custom dimension on parameter {name!r} already exists")
        used = sum(1 for d in existing if d.get("dimension_scope") == scope)
        if scope in DIMENSION_QUOTA and used >= DIMENSION_QUOTA[scope]:
            warnings.append(f"{used} {scope} dimensions already use the standard quota of {DIMENSION_QUOTA[scope]}; "
                            "Google refuses the creation unless the property is GA4 360")
        warnings.append(IMMUTABLE_NOTE)
    else:
        problems.append(f"unknown GA4 action {op.action!r}")
    return problems, warnings


def _dry_run(ctx: Any, op: WriteOp) -> dict:
    problems, warnings = _problems(op)
    if problems:
        return gw.local_result(False, "; ".join(problems))
    return gw.local_result(True, {"checked": "format, duplicates and quota against the current property",
                                  "warnings": warnings})


def _execute(ctx: Any, op: WriteOp, grant: Any) -> dict:
    problems, _ = _problems(op)  # op.before is the snapshot taken at apply time
    if problems:
        return {"error": "precondition_failed", "detail": "; ".join(problems),
                "fix": "Check the property with the GA4 read tools, then plan again; nothing was sent."}
    result = gw.write_call(WRITE, ctx, op.method, op.url, params=op.params, json=op.body, grant=grant)
    if "error" in result:
        return result
    return gw.trim(result, ("name", "eventName", "countingMethod", "parameterName", "displayName", "scope"))


def _entity_key(ctx: Any, op: WriteOp) -> str:
    body = op.body or {}
    name = body.get("eventName") if op.action == KEY_EVENT else f"{body.get('scope')}:{body.get('parameterName')}"
    return f"ga4/{(op.before or {}).get('property')}/{op.action}/{name}"


HANDLER = PlatformHandler(dry_run=_dry_run, snapshot=_snapshot, execute=_execute, invert=None,
                          entity_key=_entity_key)


# ------------------------------------------------------------------ plan tools
def _plan(ctx: Any, prop: str, action: str, body: dict, summary: str, title: str) -> dict:
    from . import write_core

    url = f"{ADMIN}/properties/{prop}/{'keyEvents' if action == KEY_EVENT else 'customDimensions'}"
    op = WriteOp(op_id=f"{action}-1", platform=PLATFORM, account_id=prop, action=action, method="POST", url=url,
                 body=body, risk="medium", financial=False, summary=summary, rollback="none")
    try:
        before = _snapshot(ctx, op)
    except gw.SnapshotError as exc:
        return {**exc.as_dict(), "client_id": ctx.client_id}
    op = dataclasses.replace(op, before=before)
    problems, _ = _problems(op)
    if problems:
        return {"error": "invalid_request", "detail": "; ".join(problems), "client_id": ctx.client_id,
                "fix": "Fix the values (ga4_key_events / ga4_custom_definitions show what exists); nothing was "
                       "planned."}
    return write_core.create_plan(ctx, PLATFORM, [op], title=title)


@gw.tool_guard()
def ga4_plan_key_event_create(
    event_name: Annotated[str, Field(description="GA4 event name to mark as a key event, e.g. generate_lead")],
    counting_method: Annotated[Literal["ONCE_PER_EVENT", "ONCE_PER_SESSION"],
                               Field(description="Count every event, or once per session")] = "ONCE_PER_EVENT",
    property_id: PROPERTY_ID = "",
    client_id: CLIENT_ID = None,
) -> dict:
    """PLAN (nothing is written): mark an event as a key event of the client's GA4 property. Returns the plan
    with a confirmation_token; apply it with google_apply after the user agreed. No automatic rollback (deleting
    a key event is not offered in this version: remove it in GA4 Admin > Key events)."""
    ctx = gw.resolve(client_id)
    prop = _property(ctx, property_id)
    blocked = gw.write_precheck(ctx, PLATFORM, prop)
    if blocked:
        return blocked
    body = {"eventName": str(event_name).strip(), "countingMethod": counting_method}
    return _plan(ctx, prop, KEY_EVENT, body,
                 f"Mark event {body['eventName']!r} as a key event ({counting_method}) on property {prop}",
                 f"GA4: key event {body['eventName']}")


@gw.tool_guard()
def ga4_plan_custom_dimension_create(
    parameter_name: Annotated[str, Field(description="Event or user parameter to register, e.g. payment_type "
                                                     "(can never be changed later)")],
    display_name: Annotated[str, Field(description="Name shown in GA4 reports")],
    scope: Annotated[Literal["EVENT", "USER", "ITEM"], Field(description="Scope (can never be changed later)")] =
    "EVENT",
    description: Annotated[str, Field(description="Optional description, 150 characters at most")] = "",
    disallow_ads_personalization: Annotated[bool, Field(description="USER scope only: exclude it from ads "
                                                                    "personalization")] = False,
    property_id: PROPERTY_ID = "",
    client_id: CLIENT_ID = None,
) -> dict:
    """PLAN (nothing is written): register a custom dimension on the client's GA4 property. Warning:
    parameterName and scope are immutable and the slot counts against the quota (50 event / 25 user / 10 item).
    Apply with google_apply after the user agreed. No automatic rollback (archiving is not offered)."""
    ctx = gw.resolve(client_id)
    prop = _property(ctx, property_id)
    blocked = gw.write_precheck(ctx, PLATFORM, prop)
    if blocked:
        return blocked
    body: dict[str, Any] = {"parameterName": str(parameter_name).strip(), "displayName": str(display_name).strip(),
                            "scope": scope}
    if description:
        body["description"] = str(description).strip()
    if disallow_ads_personalization:
        body["disallowAdsPersonalization"] = True
    return _plan(ctx, prop, CUSTOM_DIMENSION, body,
                 f"Create {scope} custom dimension {body['displayName']!r} on parameter {body['parameterName']!r} "
                 f"(property {prop}); {IMMUTABLE_NOTE}",
                 f"GA4: custom dimension {body['parameterName']}")


# ------------------------------------------------------------------ ga4_compare (pure logic)
# The tool itself lives in server.py (it sends the runReport request with the server's own read-only client); the
# period arithmetic and the pairing of the two date ranges live here so they can be tested without any request.
# API: properties.runReport with two dateRanges, DateRange.name, and the dateRange dimension that GA4 adds to the
# response when a request has several ranges:
# https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/properties/runReport
COMPARE_PERIODS = ("last_7_days", "last_14_days", "last_28_days", "last_30_days", "last_90_days", "this_month",
                   "last_month")
COMPARE_TO = ("previous_period", "previous_year")
COMPARE_FETCH_LIMIT = 10000  # rows asked from GA4 (both ranges together); the tool returns far fewer
COMPARE_SIDES = ("current", "previous")
BIGGEST_MOVES = 5
SETTLING_NOTE = "GA4 can still revise the last 24 to 48 hours; compare complete days only"
_CUSTOM_PERIOD = re.compile(r"(\d{4}-\d{2}-\d{2}):(\d{4}-\d{2}-\d{2})")


def _years_ago(day: datetime.date) -> datetime.date:
    try:
        return day.replace(year=day.year - 1)
    except ValueError:  # 29 February
        return day.replace(year=day.year - 1, day=28)


def _month_before(first_of_month: datetime.date) -> tuple[datetime.date, datetime.date]:
    """(first day, last day) of the month before the month that starts on first_of_month."""
    last = first_of_month - datetime.timedelta(days=1)
    return last.replace(day=1), last


def compare_ranges(period: str, compare_to: str, today: datetime.date) -> dict[str, tuple[datetime.date, datetime.date]]:
    """{"current": (start, end), "previous": (start, end)} as calendar dates. Raises ValueError (message = what to
    change) for an unknown period, an unknown compare_to or a period with no complete day yet.

    last_N_days end yesterday (today is partial). this_month is the month to date up to yesterday and, against
    previous_period, is aligned: the same number of days of the month before, so a partial month is never set
    against a full one. last_month is the whole previous calendar month. A custom period is "YYYY-MM-DD:YYYY-MM-DD"."""
    period = (period or "").strip()
    compare_to = (compare_to or "").strip()
    if compare_to not in COMPARE_TO:
        raise ValueError(f"compare_to must be one of {', '.join(COMPARE_TO)}, got {compare_to!r}")
    one_day = datetime.timedelta(days=1)
    yesterday = today - one_day
    custom = _CUSTOM_PERIOD.fullmatch(period)
    if custom:
        try:
            start, end = (datetime.date.fromisoformat(g) for g in custom.groups())
        except ValueError:
            raise ValueError(f"period {period!r} holds an impossible date") from None
        if end < start:
            raise ValueError(f"period {period!r} ends before it starts")
        kind = "custom"
    elif period in COMPARE_PERIODS and period.endswith("_days"):
        days = int(period.split("_")[1])
        start, end, kind = yesterday - datetime.timedelta(days=days - 1), yesterday, "days"
    elif period == "this_month":
        start, end, kind = today.replace(day=1), yesterday, "month_to_date"
        if end < start:
            raise ValueError("this_month has no complete day yet (today is the 1st); use last_month or last_7_days")
    elif period == "last_month":
        start, end = _month_before(today.replace(day=1))
        kind = "month"
    else:
        raise ValueError(f"period must be one of {', '.join(COMPARE_PERIODS)} or YYYY-MM-DD:YYYY-MM-DD, got {period!r}")
    if compare_to == "previous_year":
        previous = (_years_ago(start), _years_ago(end))
    elif kind == "month":
        previous = _month_before(start)
    elif kind == "month_to_date":
        first, last = _month_before(start)
        previous = (first, min(last, first + (end - start)))
    else:
        length = (end - start) + one_day
        previous = (start - length, start - one_day)
    return {"current": (start, end), "previous": previous}


def compare_body(ranges: dict, dimensions: list[str], metric_names: list[str]) -> dict:
    """runReport body: both ranges named current / previous, ordered by the first metric so that, if GA4 cuts the
    rows, it is the small ones that go. The dateRange dimension is added by GA4 itself."""
    dims = [d for d in dimensions if d != "dateRange"]
    return {"dateRanges": [{"startDate": ranges[side][0].isoformat(), "endDate": ranges[side][1].isoformat(),
                            "name": side} for side in COMPARE_SIDES],
            "dimensions": [{"name": d} for d in dims], "metrics": [{"name": m} for m in metric_names],
            "orderBys": [{"metric": {"metricName": metric_names[0]}, "desc": True}],
            "limit": COMPARE_FETCH_LIMIT, "metricAggregations": ["TOTAL"]}


def _change(current: Any, previous: Any) -> dict:
    """Absolute change and percent change (12.5 = +12.5 %). Previous 0: percent is None, never a division."""
    both = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (current, previous))
    if not both:
        return {"change": None, "change_pct": None}
    diff = round(current - previous, 6)
    if previous == 0:
        if current == 0:
            return {"change": diff, "change_pct": 0.0}
        return {"change": diff, "change_pct": None, "change_pct_note": "previous value is 0: percent change undefined"}
    return {"change": diff, "change_pct": round(diff / abs(previous) * 100, 2)}


def _pair(current: dict, previous: dict, names: list[str]) -> dict:
    """{metric: {current, previous, change, change_pct}}; a range with no row for the key counts as 0."""
    return {n: {"current": current.get(n, 0), "previous": previous.get(n, 0),
                **_change(current.get(n, 0), previous.get(n, 0))} for n in names}


def _side(value: Any) -> str | None:
    return {"current": "current", "previous": "previous", "date_range_0": "current",
            "date_range_1": "previous"}.get(str(value or "").strip())


def compare_response(resp: dict, limit: int) -> dict:
    """Pair the rows of the two ranges. Returns rows (each: dimension values, per metric current / previous /
    change / change_pct, and present_in when a key exists in one range only), totals, metrics (unit and
    definition), currency, row counts and biggest_moves. An error response is returned unchanged."""
    if "error" in resp:
        return resp
    dims = [d["name"] for d in resp.get("dimensionHeaders", [])]
    headers = resp.get("metricHeaders", [])
    names = [m["name"] for m in headers]
    types = [m.get("type", "") for m in headers]
    meta = resp.get("metadata", {})
    raw_rows = resp.get("rows", [])
    if "dateRange" not in dims and (raw_rows or resp.get("totals")):
        return {"error": "unexpected_response", "detail": "GA4 answered without the dateRange dimension",
                "fix": "retry; if it persists, call ga4_report once per period"}
    at = dims.index("dateRange") if "dateRange" in dims else -1
    key_dims = [d for d in dims if d != "dateRange"]

    def values(row: dict) -> dict:
        return {n: metrics.to_number(v.get("value"), t) for n, t, v in zip(names, types, row.get("metricValues", []))}

    def dimension(row: dict, index: int) -> Any:
        found = row.get("dimensionValues", [])
        return found[index].get("value") if 0 <= index < len(found) else None

    paired: dict[tuple, dict] = {}
    for row in raw_rows:
        side = _side(dimension(row, at))
        if side:
            key = tuple(v.get("value") for i, v in enumerate(row.get("dimensionValues", [])) if i != at)
            paired.setdefault(key, {})[side] = values(row)
    pairs = []
    for key, sides in paired.items():
        item = dict(zip(key_dims, key))
        item["metrics"] = _pair(sides.get("current", {}), sides.get("previous", {}), names)
        if len(sides) < 2:
            item["present_in"] = sorted(sides)
        pairs.append(item)
    first = names[0] if names else None
    if first:
        pairs.sort(key=lambda r: (r["metrics"][first]["current"], r["metrics"][first]["previous"]), reverse=True)
    out: dict = {"rows": pairs[:limit], "row_count": len(pairs), "truncated": len(pairs) > limit}
    fetched_total = resp.get("rowCount", len(raw_rows))
    if fetched_total > len(raw_rows):
        out["fetch_truncated"] = True
        out["fetch_truncated_note"] = (f"GA4 holds {fetched_total} rows for the two ranges and {len(raw_rows)} were "
                                       "fetched (largest first by the first metric): rows beyond are missing from "
                                       "the comparison, totals still cover everything")
    totals: dict[str, dict] = {}
    for row in resp.get("totals", []):
        side = _side(dimension(row, at))
        if side:
            totals[side] = values(row)
    if totals:
        out["totals"] = _pair(totals.get("current", {}), totals.get("previous", {}), names)
    out["metrics"] = metrics.ga4_metric_info(headers, meta.get("currencyCode"))
    if meta.get("currencyCode"):
        out["currency"] = meta["currencyCode"]
    if first:
        moving = [p for p in pairs if p["metrics"][first]["change"]]
        moving.sort(key=lambda p: abs(p["metrics"][first]["change"]), reverse=True)
        out["ranked_by"] = first
        out["biggest_moves"] = [{**{d: p[d] for d in key_dims}, "metric": first, **p["metrics"][first]}
                                for p in moving[:BIGGEST_MOVES]]
    if any(i.get("unit", "").startswith("ratio") for i in out["metrics"].values()):
        out["ratio_note"] = ("for a ratio metric, change is a difference in ratio points and change_pct the "
                             "relative change")
    out["change_pct_note"] = "change_pct is a percentage: 12.5 means +12.5 % against the previous range"
    return out


def run_compare(send: Any, *, period: str, compare_to: str, dimensions: list[str], metric_names: list[str],
                limit: int, today: datetime.date | None = None) -> dict:
    """Send ONE runReport with both ranges (a second only when the property's own calendar day differs from the UTC
    day the ranges were first computed on and that shifts them) and return the paired result with both date ranges
    resolved in the property timezone. send(body) -> the Data API response dict (or an error dict)."""
    if not metric_names:
        return {"error": "invalid_request", "detail": "metrics is empty", "fix": "pass at least one GA4 metric"}
    assumed = today or metrics.today_in(None)[0]
    try:
        ranges = compare_ranges(period, compare_to, assumed)
    except ValueError as exc:
        return {"error": "invalid_period", "detail": str(exc),
                "fix": "use a period and a compare_to from the tool description"}
    resp = send(compare_body(ranges, dimensions, metric_names))
    if "error" in resp:
        return resp
    timezone = resp.get("metadata", {}).get("timeZone")
    local_today, _ = metrics.today_in(timezone)
    if local_today != assumed:
        try:
            fixed = compare_ranges(period, compare_to, local_today)
        except ValueError as exc:
            return {"error": "invalid_period", "detail": str(exc), "fix": "use another period"}
        if fixed != ranges:
            ranges = fixed
            resp = send(compare_body(ranges, dimensions, metric_names))
            if "error" in resp:
                return resp
    out = compare_response(resp, limit)
    if "error" in out:
        return out
    info = {side: metrics.date_range_info(ranges[side][0].isoformat(), ranges[side][1].isoformat(), timezone, 2,
                                          SETTLING_NOTE) for side in COMPARE_SIDES}
    out["date_range"] = {**info, "period": period, "compare_to": compare_to}
    out["data_complete"] = info["current"].get("data_complete", False)
    return out


# ------------------------------------------------------------------ registration
def register_write() -> None:
    """Field allow-lists and the "ga4" PlatformHandler (idempotent: the same lists again are a no-op)."""
    from . import write_core, write_policy

    for action, (fields, money_ok) in ALLOWLISTS.items():
        write_policy.register_field_allowlist(PLATFORM, action, fields, money_ok=money_ok)
    write_core.register_handler(PLATFORM, HANDLER)


def register(mcp: Any) -> list[str]:
    """Register the GA4 read tools (read-only) and plan tools (read-only hint: they only plan)."""
    register_write()
    names = gw.register_tools(mcp, [("GA4 custom definitions", ga4_custom_definitions),
                                    ("GA4 key events", ga4_key_events)], gw.READ_ONLY)
    names += gw.register_tools(mcp, [("Plan a GA4 key event", ga4_plan_key_event_create),
                                     ("Plan a GA4 custom dimension", ga4_plan_custom_dimension_create)],
                               gw.PLAN_ONLY)
    return names

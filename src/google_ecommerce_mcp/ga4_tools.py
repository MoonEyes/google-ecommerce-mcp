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
import re
from typing import Annotated, Any, Literal, Optional

from pydantic import Field

from . import google_write as gw
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

"""Read-only connector for OpenAI Ads (ads in ChatGPT): ad account, insights and conversion insights.

Authentication: the OPENAI_ADS_API_KEY secret (environment first, then the OS keyring entry "openai-ads-key"),
sent only as `Authorization: Bearer <key>`, never in a URL. It is read once per request group (one insights read,
whatever its number of pages; summary() adds one read for its optional ad account lookup), never per page.

Host: fixed to https://api.ads.openai.com/v1. It is a constant of this module and can NOT be configured through
the environment or a tool parameter, so the key can never be sent to another host. Every request also goes through
the AllowList of this module (ads_common.AdsClient), which refuses anything else before it leaves the process:
    GET  /v1/ad_account
    GET  /v1/ad_account/insights
    GET  /v1/(campaigns|ad_groups|ads)/<id>/insights        (<id> = [A-Za-z0-9_-]+)
    POST /v1/conversions/insights                           (a read query; body checked by a validator)

API facts used (developers.openai.com/ads/api-reference/insights and /ads/reporting, read 2026-10-16):
- GET insights: array parameters use the bracket form of the documentation: time_ranges[] (one JSON object, here
  {"type":"date_range","since","until"(inclusive)[,"timezone"]}), fields[] (canonical names; rows use flat keys,
  campaign.id -> campaign_id, metadata.timezone -> timezone), filters[] (JSON {"field","operator","value"}, operator
  IN with a list of strings, GREATER_THAN / LESS_THAN with a number), sort[] (JSON {"field","direction"},
  repeatable), segments[] (at most one of product, country, device, platform), includes[]
  (zero_impression_items). time_granularity hourly|daily|monthly|none (with a segment: none|daily|monthly).
  limit 1..2000 per page; after/before = last_id/first_id of the previous page. Answer: {"object":"list",
  "data":[...], "count", "first_id", "last_id", "has_more"}.
- POST conversions/insights: aggregation_level (required: campaign, ad_group or ad), time_ranges with one
  unix_range (end EXCLUSIVE, whole days in the account timezone, 365 days max), entity_ids (required and non-empty
  when group_by_entity is true, the default; omitted with group_by_entity false to report across the account),
  breakdown country|device, include ["attributed_events"], event_names (requires include), include_zero_rows. No
  cursor; more than 2000 rows answers 413 without partial result (too_many_rows here). Answer carries
  account_currency.
- spend, cpc, cpm are decimals in MAJOR units of the account currency (12.50 = 12,50); ctr is a ratio (0.04 = 4 %).
  Spend, CPC and CPM are finalised later than clicks; conversions need at least one day (settle_days = 2 here).
- Timezone: the account timezone unless one is sent in time_ranges. This server requests metadata.timezone and
  reports the timezone the API returned; only when that field is absent is the timezone reported as unknown.
- Currency: only from order_created_attributed_sales_currency (insights rows), account_currency (conversions) or,
  in summary() when the rows carry none, the 'currency' / 'account_currency' field of GET /v1/ad_account if it has
  one (not documented); None with a note when absent, never guessed.
- Attribution: attribution_window_days 7|14|30 (default 30), view_through_attribution_window_days 0|1 (default 1),
  attribution_time_basis ad_event_time (default) | conversion_time; the values used are copied in `attribution`.
- null metric values mean "non calculable" (not zero) and are kept as null.

Privacy: openai_ads_account keeps only id, name, status, currency and timezone (other keys are listed in
raw_fields_dropped). Attributed event details (include_attributed_events) can contain order or user identifiers:
only the documented aggregate fields are kept and the output carries a warning.

Known risk: the OpenAI Ads documentation does not confirm that an API key can be restricted to reading. The key may
be able to create or change campaigns and budgets. This server only ever sends the read requests above, but a
leaked key could be used elsewhere: prefer a dedicated key and revoke it when unused. No automatic retry: a 429 is
returned as rate_limited with retry_after_seconds (Retry-After header)."""

import datetime
import functools
import json
import re
from typing import Annotated, Any, Callable, Literal

from pydantic import Field

from .ads_common import (AdsClient, AllowList, date_range, guard, redact, register_tools, safe_ratio, total_roas,
                         validate_dates)
from .ads_config import AdsSettings
from .config import NotConfigured

PLATFORM = "openai_ads"
# Fixed on purpose: never read from the environment or a parameter (the key must not be sent anywhere else).
BASE_URL = "https://api.ads.openai.com/v1"
_HOST_RX = r"https://api\.ads\.openai\.com/v1"

MAX_PAGE_SIZE = 2000
MAX_ROWS = 6000
MAX_PAGES = 5
MAX_FIELDS = 50
MAX_FILTERS = 10
MAX_SORTS = 5
MAX_ENTITY_IDS = 100
# Stricter than the documentation (1-500 names of up to 256 characters, any non-blank text): names here are
# limited to 20 of up to 64 characters among letters, digits and _ . : -
MAX_EVENT_NAMES = 20
MAX_CONVERSION_DAYS = 365
SETTLE_DAYS = 2

DEFAULT_FIELDS: list[str] = ["impressions", "clicks", "spend", "ctr", "cpc", "cpm", "conversions", "cpa",
                             "post_click_cvr", "order_created_attributed_sales",
                             "order_created_attributed_sales_currency", "order_created_roas", "metadata.timezone"]
ADDITIVE = ("impressions", "clicks", "spend", "conversions", "order_created_attributed_sales")
CONVERSION_ADDITIVE = ("conversions", "click_through_conversions", "view_through_conversions",
                       "order_created_attributed_sales")
ACCOUNT_FIELDS = ("id", "name", "status", "currency", "timezone")
CONVERSION_ROW_FIELDS = ("entity_id", "date", "country", "device", "conversions", "click_through_conversions",
                         "view_through_conversions", "order_created_attributed_sales",
                         "order_created_attributed_sales_currency", "attributed_events")
EVENT_FIELDS = ("event_name", "event_kind", "attributed_event_count", "click_through_conversions",
                "view_through_conversions", "attributed_event_value_amount", "attributed_event_value_count",
                "attributed_event_value_currency")

UNITS_NOTE = ("spend, cpc and cpm are decimals in major units of the ad account currency (12.50 = 12,50); ctr and "
              "post_click_cvr are ratios (0.04 = 4 %)")
CURRENCY_NOTE = ("currency not returned: request the field order_created_attributed_sales_currency (insights) or use "
                 "openai_ads_conversions (account_currency); never guessed")
ACCOUNT_TZ = "ad account timezone (metadata.timezone not returned)"
NULL_MEANS = "non calculable"
FRESHNESS = ("OpenAI Ads reported data; impressions and clicks within minutes, spend/CPC/CPM finalised later, "
             "conversions after at least one day")
SETTLING_NOTE = ("OpenAI Ads finalises spend, CPC and CPM later than clicks and needs at least one day for "
                 "conversions: the last 2 days can still change")
EVENTS_WARNING = ("attributed event details can contain order or user identifiers: only the documented aggregate "
                  "fields are kept; do not copy them outside this conversation")

METRICS = {
    "impressions": {"unit": "count", "definition": "Ad impressions recorded"},
    "clicks": {"unit": "count", "definition": "Ad clicks recorded"},
    "spend": {"unit": "account currency, major units (12.50 = 12,50)", "definition": "Amount spent"},
    "ctr": {"unit": "ratio 0..1 (0.04 = 4 %)", "definition": "Clicks / impressions"},
    "cpc": {"unit": "account currency, major units", "definition": "Average spend per click (finalised activity)"},
    "cpm": {"unit": "account currency, major units", "definition": "Spend per 1000 impressions (finalised activity)"},
    "conversions": {"unit": "count", "definition": "Goal conversions from clicks plus views within the windows"},
    "cpa": {"unit": "account currency, major units", "definition": "Spend / conversions"},
    "post_click_cvr": {"unit": "ratio 0..1", "definition": "Click-through goal conversions / clicks"},
    "order_created_attributed_sales": {"unit": "account currency", "definition": "Value of attributed "
                                                                                 "order_created events"},
    "order_created_attributed_sales_currency": {"unit": "ISO 4217 code",
                                                "definition": "Currency of order_created_attributed_sales"},
    "order_created_roas": {"unit": "ratio", "definition": "Attributed purchase value / spend (4 = 4 units of sales "
                                                          "per unit spent)"},
    "metadata.timezone": {"unit": "IANA name", "definition": "Timezone of the report days (row key: timezone)"},
}
CONVERSION_METRICS = {
    "conversions": {"unit": "count", "definition": "Goal conversions (click-through + view-through)"},
    "click_through_conversions": {"unit": "count", "definition": "Goal conversions after an eligible click"},
    "view_through_conversions": {"unit": "count", "definition": "Goal conversions after an eligible impression"},
    "order_created_attributed_sales": {"unit": "account_currency (decimal string in the API, number here)",
                                       "definition": "Attributed purchase value, goal and non-goal"},
}
TOTALS_NOTE = ("totals are recomputed here from the returned rows: additive metrics are summed (nulls left out), "
               "ratios are recomputed from the sums (ctr = clicks / impressions, cpc = spend / clicks, cpm = spend / "
               "impressions x 1000, cpa = spend / conversions, roas = sum of sales / sum of spend, never a mean of "
               "row ratios); post_click_cvr cannot be recomputed and is null. The API computes cpc/cpm on "
               "finalised activity, so recent recomputed values can differ from the row values. Rows that overlap "
               "(e.g. several aggregation levels) must not be summed")

_ENTITY_ID = re.compile(r"[A-Za-z0-9_-]{1,128}", re.ASCII)
_FIELD = re.compile(r"[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63}){0,2}", re.ASCII)
_EVENT_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,64}", re.ASCII)
# Cursors are opaque (the documentation shows %3D, %7C, ':' and 'sort_values=...'); requests encodes them.
_PAGE_ID = re.compile(r"[^\x00-\x1f\x7f]{1,1024}")
_TIMEZONE = re.compile(r"UTC|[A-Z][A-Za-z_]+(?:/[A-Za-z0-9_+-]+){1,2}", re.ASCII)
_LEVEL_PATH = {"campaign": "campaigns", "ad_group": "ad_groups", "ad": "ads"}
_OPERATORS = {"IN", "GREATER_THAN", "LESS_THAN"}

ConversionLevel = Literal["campaign", "ad_group", "ad"]
InsightsGranularity = Literal["none", "daily", "monthly", "hourly"]
Segment = Literal["", "product", "country", "device", "platform"]
ClickWindow = Literal[7, 14, 30]
ViewWindow = Literal[0, 1]
TimeBasis = Literal["", "ad_event_time", "conversion_time"]

_AGG_LEVELS = {"ad_account", "campaign", "ad_group", "ad"}
_CONVERSION_LEVELS = {"campaign", "ad_group", "ad"}
_BODY_KEYS = {"aggregation_level", "time_granularity", "time_ranges", "entity_ids", "group_by_entity", "breakdown",
              "attribution_window_days", "view_through_attribution_window_days", "attribution_time_basis",
              "include", "event_names", "include_zero_rows"}


# ------------------------------------------------------------------ allow-list
def _unix_seconds(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit() and value.isascii():
        return int(value)
    raise ValueError("unix seconds expected")


def _is_int_in(value: Any, allowed: tuple) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value in allowed


def conversions_body_ok(body: Any) -> bool:
    """Validator of POST /v1/conversions/insights: only the documented read-query keys, with checked values."""
    if not isinstance(body, dict) or not set(body) <= _BODY_KEYS:
        return False
    if body.get("aggregation_level") not in _CONVERSION_LEVELS:
        return False
    ranges = body.get("time_ranges")
    if not isinstance(ranges, list) or len(ranges) != 1 or not isinstance(ranges[0], str):
        return False
    parsed = json.loads(ranges[0])
    if not isinstance(parsed, dict) or set(parsed) != {"type", "start", "end"} or parsed["type"] != "unix_range":
        return False
    start, end = _unix_seconds(parsed["start"]), _unix_seconds(parsed["end"])
    if not 0 < start < end or end - start > MAX_CONVERSION_DAYS * 86400 + 3600:  # + 1 h for a DST change
        return False
    group_by_entity = body.get("group_by_entity", True)
    if not isinstance(group_by_entity, bool):
        return False
    ids = body.get("entity_ids")
    if group_by_entity:
        if not (isinstance(ids, list) and 1 <= len(ids) <= MAX_ENTITY_IDS
                and all(isinstance(i, str) and _ENTITY_ID.fullmatch(i) for i in ids)):
            return False
    elif "entity_ids" in body:
        return False
    if "event_names" in body and body.get("include") != ["attributed_events"]:
        return False
    checks = {
        "time_granularity": lambda v: v in ("daily", "none"),
        "breakdown": lambda v: v in ("country", "device"),
        "attribution_window_days": lambda v: _is_int_in(v, (7, 14, 30)),
        "view_through_attribution_window_days": lambda v: _is_int_in(v, (0, 1)),
        "attribution_time_basis": lambda v: v in ("ad_event_time", "conversion_time"),
        "include": lambda v: v == ["attributed_events"],
        "event_names": lambda v: isinstance(v, list) and 1 <= len(v) <= MAX_EVENT_NAMES
        and all(isinstance(e, str) and _EVENT_NAME.fullmatch(e) for e in v),
        "include_zero_rows": lambda v: isinstance(v, bool),
    }
    return all(check(body[key]) for key, check in checks.items() if key in body)


ALLOWED_ENDPOINTS = AllowList([
    ("GET", _HOST_RX + r"/ad_account"),
    ("GET", _HOST_RX + r"/ad_account/insights"),
    ("GET", _HOST_RX + r"/(?:campaigns|ad_groups|ads)/[A-Za-z0-9_-]+/insights"),
    ("POST", _HOST_RX + r"/conversions/insights", conversions_body_ok),
])

# The key travels in the Authorization header, which AdsClient masks in every error; no keyring read per request.
CLIENT = AdsClient(PLATFORM, ALLOWED_ENDPOINTS, secrets=None)


def _headers() -> dict:
    """Read the key (environment, then keyring) and build the headers of one request group (all its pages)."""
    key = AdsSettings.from_env().openai_ads_api_key
    if not key:
        raise NotConfigured("OPENAI_ADS_API_KEY")
    return {"Authorization": f"Bearer {key}", "Accept": "application/json", "Content-Type": "application/json"}


# ------------------------------------------------------------------ helpers
def _invalid(code: str, detail: str) -> dict:
    return {"error": code, "platform": PLATFORM, "detail": detail}


class InvalidInput(ValueError):
    """A parameter refused before any request; returned as {"error": code, "platform", "detail"}, never sent."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code, self.detail = code, detail

    def as_dict(self) -> dict:
        return _invalid(self.code, self.detail)


INTERNAL_FIX = "unexpected failure in the OpenAI Ads connector; retry, and report it if it persists"


def _internal_error(exc: BaseException) -> dict:
    return {"error": "internal_error", "platform": PLATFORM,
            "detail": str(redact(f"{type(exc).__name__}: {exc}"))[:300], "fix": INTERNAL_FIX}


def _contained(fn: Callable[..., dict]) -> Callable[..., dict]:
    """Inside guard() (same as google_ads._contained): InvalidInput becomes its error dict and any unexpected
    exception a structured internal_error with secrets masked, instead of crashing the tool. NotConfigured is left
    to guard()."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict:
        try:
            return fn(*args, **kwargs)
        except NotConfigured:
            raise
        except InvalidInput as exc:
            return exc.as_dict()
        except Exception as exc:
            return _internal_error(exc)

    return wrapper


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sum(rows: list, metric: str) -> float | None:
    values = [_number(r.get(metric)) for r in rows if isinstance(r, dict)]
    values = [v for v in values if v is not None]
    return round(sum(values), 6) if values else None


def _totals(rows: list) -> dict:
    sums = {metric: _sum(rows, metric) for metric in ADDITIVE}
    spend, clicks, impressions = sums["spend"], sums["clicks"], sums["impressions"]
    return {
        **sums,
        "ctr": safe_ratio(clicks, impressions),
        "cpc": safe_ratio(spend, clicks),
        "cpm": safe_ratio(spend * 1000 if spend is not None else None, impressions),
        "cpa": safe_ratio(spend, sums["conversions"]),
        "post_click_cvr": None,
        "order_created_roas": total_roas([r for r in rows if isinstance(r, dict)],
                                         "order_created_attributed_sales", "spend"),
    }


def _distinct(rows: list, key: str) -> list[str]:
    return sorted({r[key] for r in rows if isinstance(r, dict) and isinstance(r.get(key), str) and r[key]})


def _attribution(click: int, view: int, basis: str) -> dict:
    return {"click_window_days": click, "view_window_days": view, "time_basis": basis or "ad_event_time",
            "source": "attribution parameters of the request (API defaults when omitted: 30 / 1 / ad_event_time)"}


def _dates(start_date: str, end_date: str, requested_tz: str, returned: list[str]) -> dict:
    """date_range in the timezone the API returned (metadata.timezone), else the one sent, else unknown."""
    if len(returned) == 1:
        tz, source = returned[0], "api (metadata.timezone)"
    elif requested_tz:
        tz, source = requested_tz, "request (not confirmed by the response)"
    else:
        tz, source = None, "unknown"
    info = date_range(start_date, end_date, tz, settle_days=SETTLE_DAYS, note=SETTLING_NOTE)
    info["timezone_reported"] = len(returned) == 1
    info["timezone_source"] = source
    if len(returned) > 1:
        info["timezones_returned"] = returned
    if tz is None:
        info["timezone"] = ACCOUNT_TZ
    return info


def _check_window(click: Any, view: Any, basis: Any) -> dict | None:
    if not _is_int_in(click, (7, 14, 30)):
        return _invalid("invalid_attribution", "attribution_window_days must be 7, 14 or 30")
    if not _is_int_in(view, (0, 1)):
        return _invalid("invalid_attribution", "view_through_attribution_window_days must be 0 or 1")
    if basis not in ("", "ad_event_time", "conversion_time"):
        return _invalid("invalid_attribution", "attribution_time_basis must be ad_event_time or conversion_time")
    return None


def _check_timezone(timezone: str) -> dict | None:
    if timezone and not _TIMEZONE.fullmatch(timezone):
        return _invalid("invalid_timezone", "timezone must be an IANA name such as Europe/Paris, or UTC")
    return None


def _encode_filters(filters: list) -> list[str] | dict:
    """filters[] as documented: IN with a non-empty list of strings; GREATER_THAN / LESS_THAN with a number."""
    if len(filters) > MAX_FILTERS:
        return _invalid("invalid_filters", f"at most {MAX_FILTERS} filters")
    encoded = []
    for item in filters:
        ok = (isinstance(item, dict) and set(item) == {"field", "operator", "value"}
              and isinstance(item["field"], str) and bool(_FIELD.fullmatch(item["field"]))
              and item["operator"] in _OPERATORS)
        if ok and item["operator"] == "IN":
            value = item["value"]
            ok = (isinstance(value, list) and 1 <= len(value) <= 100
                  and all(isinstance(v, str) and 0 < len(v) <= 256 for v in value))
        elif ok:
            ok = isinstance(item["value"], (int, float)) and not isinstance(item["value"], bool)
        if not ok:
            return _invalid("invalid_filters", 'each filter is {"field": canonical name, "operator": "IN" with a '
                                               'list of strings, or "GREATER_THAN"/"LESS_THAN" with a number}')
        encoded.append(json.dumps({"field": item["field"], "operator": item["operator"], "value": item["value"]},
                                  separators=(",", ":")))
    return encoded


def _encode_sort(sort: list) -> list[str] | dict:
    if len(sort) > MAX_SORTS:
        return _invalid("invalid_sort", f"at most {MAX_SORTS} sort criteria")
    encoded = []
    for item in sort:
        if (not isinstance(item, dict) or set(item) - {"field", "direction"} or "field" not in item
                or not isinstance(item["field"], str) or not _FIELD.fullmatch(item["field"])
                or item.get("direction", "desc") not in ("asc", "desc")):
            return _invalid("invalid_sort", 'each sort is {"field": canonical name, "direction": "asc" or "desc"}')
        encoded.append(json.dumps({"field": item["field"], "direction": item.get("direction", "desc")},
                                  separators=(",", ":")))
    return encoded


def _paginate(fetch: Callable[[dict, int], dict], page_size: int, max_rows: int, after: str, before: str) -> dict:
    """Read pages with after=<last_id> while has_more and fewer than max_rows rows (at most MAX_PAGES pages); the
    last page asks only for the rows still needed, so next_after continues exactly after the last row shown (if the
    API sends more rows than asked, the extra rows are dropped and next_after is the id of the last row kept). A page
    with has_more but no rows or the same last_id stops with stop_reason "no_progress". With `before`, a single page
    backwards. `complete` is true only for a full read from the start (no after/before). Never retries."""
    rows: list = []
    pages = 0
    page_error = None
    first_id = last_id = None
    has_more = False
    stop = "last_page"
    cursor = {"before": before} if before else ({"after": after} if after else {})
    while True:
        size = min(page_size, max_rows - len(rows))
        resp = fetch(cursor, size)
        if "error" in resp:
            if pages == 0:
                return {"error_response": resp}
            page_error, stop = resp, "error"
            break
        pages += 1
        data = resp.get("data") if isinstance(resp.get("data"), list) else []
        previous = cursor.get("after")
        if first_id is None:
            first_id = resp.get("first_id")
        last_id = resp.get("last_id")
        has_more = resp.get("has_more") is True
        if len(data) > size:
            # The API sent more rows than asked: keep `size` rows and continue after the last row kept (its id is
            # the cursor), so no row is skipped.
            data = data[:size]
            has_more = True
            kept_id = data[-1].get("id") if isinstance(data[-1], dict) else None
            last_id = kept_id if isinstance(kept_id, str) and _PAGE_ID.fullmatch(kept_id) else None
            if last_id is None and not before:
                rows.extend(data)
                page_error = _invalid("api_error", "the API returned more rows than limit and the last row kept has "
                                                   "no id to continue from; paging stopped")
                stop = "error"
                break
        rows.extend(data)
        if has_more and not before and (not data or (previous is not None and last_id == previous)):
            page_error = _invalid("api_error", "has_more is true but the page made no progress (no rows or the same "
                                               "last_id); paging stopped")
            stop = "no_progress"
            break
        if before:
            stop = "before_single_page"
            break
        if not has_more:
            break
        if len(rows) >= max_rows:
            stop = "max_rows"
            break
        if pages >= MAX_PAGES:
            stop = "max_pages"
            break
        if not isinstance(last_id, str) or not _PAGE_ID.fullmatch(last_id):
            page_error = _invalid("api_error", "has_more is true but last_id is missing or unexpected; paging stopped")
            stop = "error"
            break
        cursor = {"after": last_id}
    next_after = (last_id if has_more and not before and stop != "no_progress" and isinstance(last_id, str)
                  else None)
    out = {"rows": rows, "row_count": len(rows), "has_more": has_more, "truncated": has_more,
           "next_after": next_after, "first_id": first_id, "last_id": last_id, "pages_read": pages,
           "max_pages": MAX_PAGES, "stop_reason": stop,
           "complete": not has_more and page_error is None and not before and not after}
    if page_error is not None:
        out["page_error"] = page_error
    return out


def _insights(url: str, start_date: str, end_date: str, *, timezone: str, time_granularity: str,
              aggregation_level: str, fields: list, segment: str, filters: list, sort: list,
              include_zero_impression_items: bool, attribution_window_days: int,
              view_through_attribution_window_days: int, attribution_time_basis: str, page_size: int,
              max_rows: int, after: str, before: str) -> dict:
    try:
        validate_dates(start_date, end_date)
    except ValueError as exc:
        return _invalid("invalid_dates", str(exc))
    problem = (_check_timezone(timezone)
               or _check_window(attribution_window_days, view_through_attribution_window_days,
                                attribution_time_basis))
    if problem:
        return problem
    if time_granularity not in ("none", "daily", "monthly", "hourly"):
        return _invalid("invalid_time_granularity", "time_granularity must be hourly, daily, monthly or none")
    if segment not in ("", "product", "country", "device", "platform"):
        return _invalid("invalid_segment", "segment must be product, country, device or platform")
    if segment and time_granularity == "hourly":
        return _invalid("invalid_time_granularity", "with a segment, time_granularity must be none, daily or monthly")
    if segment and include_zero_impression_items:
        return _invalid("invalid_argument", "include_zero_impression_items works only without a segment")
    if aggregation_level and aggregation_level not in _AGG_LEVELS:
        return _invalid("invalid_aggregation_level", "aggregation_level must be ad_account, campaign, ad_group or ad")
    if not fields or len(fields) > MAX_FIELDS or not all(isinstance(f, str) and _FIELD.fullmatch(f) for f in fields):
        return _invalid("invalid_fields", f"1 to {MAX_FIELDS} canonical field names (a-z 0-9 _, dotted, e.g. "
                                          "campaign.name)")
    for name, value, top in (("page_size", page_size, MAX_PAGE_SIZE), ("max_rows", max_rows, MAX_ROWS)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= top:
            return _invalid("invalid_limit", f"{name} must be 1 to {top}")
    if after and before:
        return _invalid("invalid_cursor", "give after or before, not both")
    for value in (after, before):
        if value and not _PAGE_ID.fullmatch(value):
            return _invalid("invalid_cursor", "after/before must be a last_id/first_id returned by a previous call")
    encoded_filters = _encode_filters(list(filters))
    if isinstance(encoded_filters, dict):
        return encoded_filters
    encoded_sort = _encode_sort(list(sort))
    if isinstance(encoded_sort, dict):
        return encoded_sort

    headers = _headers()  # once for every page of this read
    time_range: dict[str, str] = {"type": "date_range", "since": start_date, "until": end_date}
    if timezone:
        time_range["timezone"] = timezone
    params: dict[str, Any] = {
        "time_ranges[]": [json.dumps(time_range, separators=(",", ":"))],
        "time_granularity": time_granularity,
        "fields[]": list(fields),
        "attribution_window_days": attribution_window_days,
        "view_through_attribution_window_days": view_through_attribution_window_days,
    }
    if attribution_time_basis:
        params["attribution_time_basis"] = attribution_time_basis
    if aggregation_level:
        params["aggregation_level"] = aggregation_level
    if segment:
        params["segments[]"] = [segment]
    if encoded_filters:
        params["filters[]"] = encoded_filters
    if encoded_sort:
        params["sort[]"] = encoded_sort
    if include_zero_impression_items:
        params["includes[]"] = ["zero_impression_items"]

    def fetch(cursor: dict, size: int) -> dict:
        return CLIENT.call("GET", url, headers=headers, params={**params, "limit": size, **cursor})

    pages = _paginate(fetch, page_size, max_rows, after, before)
    if "error_response" in pages:
        return pages["error_response"]
    out = dict(pages)
    currencies = _distinct(pages["rows"], "order_created_attributed_sales_currency")
    out["totals"] = _totals(pages["rows"])
    out["totals_complete"] = pages["complete"]
    out["totals_note"] = TOTALS_NOTE
    out["currency"] = currencies[0] if len(currencies) == 1 else None
    if out["currency"] is None:
        out["currency_note"] = f"several currencies in the rows: {currencies}" if currencies else CURRENCY_NOTE
    out["units"] = UNITS_NOTE
    out["metrics"] = {f: METRICS[f] for f in fields if f in METRICS}
    out["date_range"] = _dates(start_date, end_date, timezone, _distinct(pages["rows"], "timezone"))
    out["timezone_reported"] = out["date_range"]["timezone_reported"]
    out["attribution"] = _attribution(attribution_window_days, view_through_attribution_window_days,
                                      attribution_time_basis)
    out["null_means"] = NULL_MEANS
    return out


# ------------------------------------------------------------------ tools
@guard("current state of the ad account")
@_contained
def openai_ads_account() -> dict:
    """OpenAI Ads (ChatGPT ads) ad account of the configured OPENAI_ADS_API_KEY (GET /v1/ad_account): only id,
    name, status, currency and timezone when present; every other key is dropped and listed in raw_fields_dropped.
    Use it as a connection test before openai_ads_insights. Cached 300 s ("cached": true)."""
    resp = CLIENT.call("GET", f"{BASE_URL}/ad_account", headers=_headers(), cache=True)
    if "error" in resp:
        return resp
    cached = resp.pop("cached", False)
    return {"ad_account": {k: resp[k] for k in ACCOUNT_FIELDS if k in resp},
            "raw_fields_dropped": sorted(str(k) for k in resp if k not in ACCOUNT_FIELDS), "cached": cached}


@guard(FRESHNESS)
@_contained
def openai_ads_insights(
    start_date: Annotated[str, Field(description="First day, YYYY-MM-DD")],
    end_date: Annotated[str, Field(description="Last day, YYYY-MM-DD (inclusive)")],
    level: Annotated[Literal["account", "campaign", "ad_group", "ad"],
                     Field(description="Which endpoint: account = the whole ad account; campaign, ad_group or "
                                       "ad = one entity given by entity_id")] = "account",
    entity_id: Annotated[str, Field(description="Id of the campaign, ad group or ad (letters, digits, _ and -); "
                                                "empty for level account")] = "",
    timezone: Annotated[str, Field(description="Optional IANA timezone of the day boundaries; empty uses the ad "
                                               "account timezone (keep empty for conversion or sales metrics, "
                                               "which require whole days in the account timezone)")] = "",
    time_granularity: Annotated[InsightsGranularity,
                                Field(description="none: one row per entity for the period; daily, monthly or "
                                                  "hourly (hourly: about 30 days of history, not with a "
                                                  "segment)")] = "none",
    aggregation_level: Annotated[Literal["", "ad_account", "campaign", "ad_group", "ad"],
                                 Field(description="Row entity inside the endpoint scope: ad_account, campaign, "
                                                   "ad_group or ad; empty uses the endpoint level")] = "",
    fields: Annotated[list[str] | None,
                      Field(description="Canonical field names, e.g. impressions, clicks, spend, ctr, cpc, cpm, "
                                        "conversions, cpa, post_click_cvr, order_created_attributed_sales, "
                                        "order_created_attributed_sales_currency, order_created_roas, campaign.id, "
                                        "campaign.name, metadata.readable_time, metadata.timezone; empty for "
                                        "these metrics plus metadata.timezone")] = None,
    segment: Annotated[Segment, Field(description="Optional single segment: product, country, device or "
                                                  "platform (request segment fields such as country.clicks)")] = "",
    filters: Annotated[list[dict[str, Any]] | None,
                       Field(description='Optional filters, combined with AND: {"field": "campaign.id", "operator": '
                                         '"IN", "value": ["cmpn_1"]} or {"field": "ad.clicks", "operator": '
                                         '"GREATER_THAN" or "LESS_THAN", "value": 10} (at most 10)')] = None,
    sort: Annotated[list[dict[str, str]] | None,
                    Field(description='Optional sort criteria in order: [{"field": "spend", "direction": "desc"}] '
                                      '(at most 5)')] = None,
    include_zero_impression_items: Annotated[bool, Field(description="True: also return entities with zero "
                                                                     "impressions (only without a segment)")] = False,
    attribution_window_days: Annotated[ClickWindow, Field(description="Click attribution window in days: 7, 14 "
                                                                      "or 30")] = 30,
    view_through_attribution_window_days: Annotated[ViewWindow, Field(description="View-through attribution "
                                                                                  "window in days: 0 or 1")] = 1,
    attribution_time_basis: Annotated[TimeBasis, Field(description="ad_event_time (API default) or "
                                                                   "conversion_time; empty sends nothing")] = "",
    page_size: Annotated[int, Field(description="Rows per API page (1 to 2000)", ge=1, le=MAX_PAGE_SIZE)] = 500,
    max_rows: Annotated[int, Field(description="Maximum rows returned by this call (1 to 6000), over at most 5 "
                                               "pages", ge=1, le=MAX_ROWS)] = 1000,
    after: Annotated[str, Field(description="next_after (last_id) of a previous call to read the next rows; "
                                            "empty to start")] = "",
    before: Annotated[str, Field(description="first_id of a previous call to read the rows before it (one page); "
                                             "not with after")] = "",
) -> dict:
    """OpenAI Ads (ChatGPT ads) performance over a date range: impressions, clicks, spend, ctr, conversions and
    attributed sales for the ad account or one campaign, ad group or ad. Returns "rows" as reported (flat keys such as
    campaign_id; null kept, null_means "non calculable"), recomputed "totals" (roas = sum of sales / sum of spend),
    "currency" (from order_created_attributed_sales_currency, else None), "date_range" in the timezone returned by
    the API, "next_after" while has_more (reads up to max_rows rows over at most 5 pages), units (spend in major
    currency units, ctr ratio) and "attribution". No automatic retry."""
    if level == "account":
        if entity_id:
            return _invalid("invalid_entity_id", "entity_id must be empty for level account")
        url = f"{BASE_URL}/ad_account/insights"
    elif level in _LEVEL_PATH:
        if not isinstance(entity_id, str) or not _ENTITY_ID.fullmatch(entity_id):
            return _invalid("invalid_entity_id", "entity_id is required for this level and may only contain "
                                                 "letters, digits, _ and - (1 to 128 characters)")
        url = f"{BASE_URL}/{_LEVEL_PATH[level]}/{entity_id}/insights"
    else:
        return _invalid("invalid_level", "level must be account, campaign, ad_group or ad")
    out = _insights(url, start_date, end_date, timezone=timezone, time_granularity=time_granularity,
                    aggregation_level=aggregation_level,
                    fields=list(fields) if fields is not None else list(DEFAULT_FIELDS), segment=segment,
                    filters=list(filters or ()), sort=list(sort or ()),
                    include_zero_impression_items=include_zero_impression_items,
                    attribution_window_days=attribution_window_days,
                    view_through_attribution_window_days=view_through_attribution_window_days,
                    attribution_time_basis=attribution_time_basis, page_size=page_size, max_rows=max_rows,
                    after=after, before=before)
    if "error" not in out:
        out["level"] = level
        out["entity_id"] = entity_id or None
    return out


def _unix_range(start_date: str, end_date: str, timezone: str) -> tuple[int, int]:
    """Midnight of start_date to midnight after end_date (end exclusive) in `timezone`, as unix seconds."""
    from zoneinfo import ZoneInfo

    tz = datetime.timezone.utc if timezone == "UTC" else ZoneInfo(timezone)
    start = datetime.datetime.combine(datetime.date.fromisoformat(start_date), datetime.time(), tzinfo=tz)
    end = datetime.datetime.combine(datetime.date.fromisoformat(end_date) + datetime.timedelta(days=1),
                                    datetime.time(), tzinfo=tz)
    return int(start.timestamp()), int(end.timestamp())


def _keep(item: Any, allowed: tuple, dropped: set) -> Any:
    if not isinstance(item, dict):
        return item
    dropped.update(str(k) for k in item if k not in allowed)
    return {k: item[k] for k in allowed if k in item}


@guard(FRESHNESS)
@_contained
def openai_ads_conversions(
    start_date: Annotated[str, Field(description="First day, YYYY-MM-DD (whole days, 365 days at most)")],
    end_date: Annotated[str, Field(description="Last day, YYYY-MM-DD (inclusive)")],
    account_timezone: Annotated[str, Field(description="IANA timezone of the ad account, e.g. Europe/Paris (the "
                                                       "timezone column of openai_ads_insights): the API counts "
                                                       "whole days in it")],
    aggregation_level: Annotated[ConversionLevel, Field(description="Required row entity: campaign, ad_group or "
                                                                    "ad")],
    entity_ids: Annotated[list[str] | None,
                          Field(description="Ids of the entities of aggregation_level (1 to 100). Required when "
                                            "group_by_entity is true; must be empty when it is false")] = None,
    group_by_entity: Annotated[bool, Field(description="True (API default): one row per entity of entity_ids; "
                                                       "false: one row for the whole ad account (no "
                                                       "entity_ids)")] = True,
    time_granularity: Annotated[Literal["none", "daily"], Field(description="none: period totals; daily: one row "
                                                                            "per day")] = "none",
    breakdown: Annotated[Literal["", "country", "device"], Field(description="Optional breakdown: country or "
                                                                             "device; empty for none")] = "",
    attribution_window_days: Annotated[ClickWindow, Field(description="Click attribution window in days: 7, 14 "
                                                                      "or 30")] = 30,
    view_through_attribution_window_days: Annotated[ViewWindow, Field(description="View-through attribution "
                                                                                  "window in days: 0 or 1")] = 1,
    attribution_time_basis: Annotated[TimeBasis, Field(description="ad_event_time (API default) or "
                                                                   "conversion_time; empty sends nothing")] = "",
    include_attributed_events: Annotated[bool, Field(description="True: add per-event aggregates (event_name, "
                                                                 "counts, value); may expose order or user "
                                                                 "identifiers, so only aggregate fields are "
                                                                 "kept")] = False,
    event_names: Annotated[list[str] | None,
                           Field(description="Optional exact event names for the event details (requires "
                                             "include_attributed_events; here at most 20 names of 64 characters "
                                             "among letters, digits, _ . : -, stricter than the API)")] = None,
    include_zero_rows: Annotated[bool, Field(description="True (API default): keep rows without "
                                                         "conversions")] = True,
) -> dict:
    """OpenAI Ads (ChatGPT ads) conversion insights (POST /v1/conversions/insights, a read query): goal conversions
    (click-through and view-through) and attributed order value per campaign, ad group or ad (entity_ids), or for the
    whole account (group_by_entity false), over whole days in the account timezone. Returns "rows" (null kept),
    recomputed "totals", "currency" (account_currency), "attribution" and "date_range". No pagination: more than
    2000 rows answers too_many_rows (fewer entities, shorter period or fewer event_names). Warning: attributed event
    details may contain order or user identifiers; only documented aggregate fields are kept."""
    entity_ids, event_names = list(entity_ids or ()), list(event_names or ())
    try:
        validate_dates(start_date, end_date)
    except ValueError as exc:
        return _invalid("invalid_dates", str(exc))
    if aggregation_level not in _CONVERSION_LEVELS:
        return _invalid("invalid_argument", "aggregation_level is required: campaign, ad_group or ad")
    if not account_timezone:
        return _invalid("invalid_timezone", "account_timezone is required: the API counts whole days in the ad "
                                            "account timezone (e.g. Europe/Paris)")
    problem = (_check_timezone(account_timezone)
               or _check_window(attribution_window_days, view_through_attribution_window_days,
                                attribution_time_basis))
    if problem:
        return problem
    days = (datetime.date.fromisoformat(end_date) - datetime.date.fromisoformat(start_date)).days + 1
    if days > MAX_CONVERSION_DAYS:
        return _invalid("invalid_dates", f"at most {MAX_CONVERSION_DAYS} days per request, got {days}")
    if group_by_entity and not entity_ids:
        return _invalid("invalid_argument", "entity_ids is required (1 to 100 ids) when group_by_entity is true; "
                                            "set group_by_entity to false for the whole account")
    if not group_by_entity and entity_ids:
        return _invalid("invalid_argument", "entity_ids must be empty when group_by_entity is false")
    if len(entity_ids) > MAX_ENTITY_IDS or not all(isinstance(i, str) and _ENTITY_ID.fullmatch(i)
                                                   for i in entity_ids):
        return _invalid("invalid_entity_id", f"at most {MAX_ENTITY_IDS} ids made of letters, digits, _ and -")
    if event_names and not include_attributed_events:
        return _invalid("invalid_argument", "event_names requires include_attributed_events")
    if len(event_names) > MAX_EVENT_NAMES or not all(isinstance(e, str) and _EVENT_NAME.fullmatch(e)
                                                     for e in event_names):
        return _invalid("invalid_event_names", f"at most {MAX_EVENT_NAMES} names of up to 64 characters (letters, "
                                               "digits, _ . : -); stricter than the API")
    try:
        start, end = _unix_range(start_date, end_date, account_timezone)
    except Exception:  # unknown zone or no tz database
        return _invalid("invalid_timezone", f"unknown timezone {account_timezone!r}")
    body: dict[str, Any] = {
        "aggregation_level": aggregation_level,
        "time_granularity": time_granularity,
        "time_ranges": [json.dumps({"type": "unix_range", "start": str(start), "end": str(end)},
                                   separators=(",", ":"))],
        "group_by_entity": group_by_entity,
        "attribution_window_days": attribution_window_days,
        "view_through_attribution_window_days": view_through_attribution_window_days,
        "include_zero_rows": include_zero_rows,
    }
    if entity_ids:
        body["entity_ids"] = entity_ids
    if breakdown:
        body["breakdown"] = breakdown
    if attribution_time_basis:
        body["attribution_time_basis"] = attribution_time_basis
    if include_attributed_events:
        body["include"] = ["attributed_events"]
    if event_names:
        body["event_names"] = event_names
    resp = CLIENT.call("POST", f"{BASE_URL}/conversions/insights", headers=_headers(), json=body)
    if "error" in resp:
        return resp
    dropped: set = set()
    rows = []
    for raw in resp.get("data") if isinstance(resp.get("data"), list) else []:
        row = _keep(raw, CONVERSION_ROW_FIELDS, dropped)
        if isinstance(row, dict) and isinstance(row.get("attributed_events"), list):
            row["attributed_events"] = [_keep(e, EVENT_FIELDS, dropped) for e in row["attributed_events"]]
        rows.append(row)
    currency = resp.get("account_currency")
    out: dict[str, Any] = {
        "rows": rows,
        "row_count": len(rows),
        "count": resp.get("count"),
        "totals": {m: _sum(rows, m) for m in CONVERSION_ADDITIVE},
        "totals_note": "sums of the returned summary rows (nulls left out); daily rows and breakdowns partition "
                       "each entity, so the sums are the totals of the entities requested",
        "currency": currency if isinstance(currency, str) and currency else None,
        "metrics": CONVERSION_METRICS,
        "unix_range": {"start": start, "end": end, "end_exclusive": True},
        "date_range": _dates(start_date, end_date, account_timezone, []),
        "timezone_reported": False,
        "attribution": _attribution(attribution_window_days, view_through_attribution_window_days,
                                    attribution_time_basis),
        "null_means": NULL_MEANS,
    }
    if dropped:
        out["raw_fields_dropped"] = sorted(dropped)
    if include_attributed_events:
        out["warning"] = EVENTS_WARNING
    if out["currency"] is None:
        out["currency_note"] = "account_currency absent from the answer; never guessed"
    return out


TOOLS = [
    ("OpenAI Ads account", openai_ads_account),
    ("OpenAI Ads insights", openai_ads_insights),
    ("OpenAI Ads conversions", openai_ads_conversions),
]


def register(mcp: Any) -> list[str]:
    """Register the OpenAI Ads tools on a FastMCP server (READ_ONLY); returns their names."""
    return register_tools(mcp, TOOLS)


# ------------------------------------------------------------------ summary for ads_overview
def _summary(start_date: str, end_date: str) -> dict:
    out = _insights(f"{BASE_URL}/ad_account/insights", start_date, end_date, timezone="", time_granularity="none",
                    aggregation_level="ad_account", fields=list(DEFAULT_FIELDS), segment="", filters=[], sort=[],
                    include_zero_impression_items=False, attribution_window_days=30,
                    view_through_attribution_window_days=1, attribution_time_basis="", page_size=MAX_PAGE_SIZE,
                    max_rows=MAX_ROWS, after="", before="")
    if "error" in out:
        return out
    totals = dict(out["totals"])
    if not out["rows"]:  # no activity in the period: zero, not unknown (ads_overview can then add the spends)
        totals.update({"spend": 0.0, "impressions": 0.0, "clicks": 0.0, "conversions": 0.0,
                       "order_created_attributed_sales": 0.0})
    result = {
        "platform": PLATFORM,
        "spend": totals["spend"],
        "impressions": totals["impressions"],
        "clicks": totals["clicks"],
        "conversions": totals["conversions"],
        "conversion_value": totals["order_created_attributed_sales"],
        "roas": totals["order_created_roas"],
        "currency": out["currency"],
        "attribution": out["attribution"],
        "date_range": out["date_range"],
        "timezone_reported": out["timezone_reported"],
        "units": UNITS_NOTE,
        "totals_complete": out["totals_complete"],
        "null_means": NULL_MEANS,
    }
    if not out["rows"]:
        result["no_activity"] = True
    if result["currency"] is None and not _distinct(out["rows"], "order_created_attributed_sales_currency"):
        currency, note, error = _account_currency()
        result["currency"] = currency
        result["currency_source"] = "ad_account" if currency else None
        if note:
            result["currency_note"] = note
        if error:
            result["currency_error"] = error
    elif result["currency"] is not None:
        result["currency_source"] = "order_created_attributed_sales_currency"
    else:
        result["currency_note"] = out["currency_note"]  # several currencies in the rows: never picked
    return result


def _account_currency() -> tuple[str | None, str | None, dict | None]:
    """Best effort: currency of GET /v1/ad_account (cached 300 s), from its 'currency' or 'account_currency' field.
    Returns (currency, note, error); never raises and never guesses."""
    try:
        resp = CLIENT.call("GET", f"{BASE_URL}/ad_account", headers=_headers(), cache=True)
    except NotConfigured:
        raise
    except Exception as exc:  # the totals must still be returned
        return None, CURRENCY_NOTE, {"error": "internal_error", "detail": redact(f"{type(exc).__name__}: {exc}")[:300]}
    if "error" in resp:
        return None, f"{CURRENCY_NOTE}; the ad account lookup failed (see currency_error)", resp
    for key in ("currency", "account_currency"):
        value = resp.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip(), None, None
    return None, f"{CURRENCY_NOTE}; GET /v1/ad_account has no currency field either", None


@guard(FRESHNESS)
def summary(start_date: str, end_date: str) -> dict:
    """Normalised account totals for ads_overview, from GET /v1/ad_account/insights (aggregation ad_account,
    granularity none, account timezone, default attribution 30 d click / 1 d view / ad_event_time): spend (major
    currency units), impressions, clicks, conversions, conversion_value (order_created_attributed_sales), roas
    (recomputed), attribution and date_range (timezone from metadata.timezone). currency comes from
    order_created_attributed_sales_currency in the rows; when the rows carry none (e.g. a period without activity),
    from the 'currency' or 'account_currency' field of GET /v1/ad_account (best effort, cached 300 s; a failure only
    adds currency_note and currency_error). Never guessed: None with currency_note when no source has it. A period
    without rows gives zeros for spend, impressions, clicks, conversions and conversion_value, with
    no_activity: true. Never raises: errors are dicts."""
    try:
        result = _summary(start_date, end_date)
    except NotConfigured:
        raise  # guard turns it into not_configured
    except InvalidInput as exc:
        return exc.as_dict()
    except Exception as exc:  # summary must never break ads_overview
        return _internal_error(exc)
    if not isinstance(result, dict):
        return {"error": "internal_error", "platform": PLATFORM, "fix": INTERNAL_FIX,
                "detail": f"summary computed {type(result).__name__}, expected dict"}
    return result

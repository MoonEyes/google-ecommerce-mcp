"""Meta Ads (Facebook / Instagram) read-only connector: ad accounts and the Insights report.

Every request goes through ads_common.AdsClient and its allow-list: GET only, host pinned to graph.facebook.com,
three paths (/me/adaccounts, /act_<id>, /act_<id>/insights). The access token (META_ACCESS_TOKEN, or the keyring
entry meta-access-token) travels only in the Authorization: Bearer header, never in the URL. Pagination follows
paging.cursors.after only: the paging.next URL returned by Meta is never requested."""

from __future__ import annotations

import functools
import json
import re
from typing import Annotated, Any, Callable, Literal

from pydantic import Field

from .ads_common import (AdsClient, AllowList, cap, date_range, guard, redact, register_tools, roas,
                         safe_ratio, total_roas, validate_dates)
from .ads_config import AdsSettings
from .config import NotConfigured

PLATFORM = "meta"
GRAPH = "https://graph.facebook.com"
MAX_PAGES = 5
MAX_LIMIT = 500
SETTLE_DAYS = 3
SETTLE_NOTE = ("Meta keeps attributing conversions to recent days (late events, view-through windows); compare "
               "complete days only")

# Every request this connector may send. GET only: nothing here creates, updates or deletes.
ALLOWED_ENDPOINTS = [
    ("GET", r"https://graph\.facebook\.com/v[0-9]+\.[0-9]+/me/adaccounts"),
    ("GET", r"https://graph\.facebook\.com/v[0-9]+\.[0-9]+/act_[0-9]+/insights"),
    ("GET", r"https://graph\.facebook\.com/v[0-9]+\.[0-9]+/act_[0-9]+"),
]
ALLOW_LIST = AllowList(ALLOWED_ENDPOINTS)
# Secrets are taken from the Authorization header of each call (AdsClient masks them in every output).
CLIENT = AdsClient(PLATFORM, ALLOW_LIST)

ACCOUNT_FIELDS = "id,account_id,name,account_status,currency,timezone_name"
INSIGHT_FIELDS = ["spend", "impressions", "clicks", "reach", "frequency", "cpc", "cpm", "actions", "action_values",
                  "purchase_roas", "account_currency"]
LEVEL_FIELDS = {
    "account": ["account_id", "account_name"],
    "campaign": ["campaign_id", "campaign_name"],
    "adset": ["campaign_id", "campaign_name", "adset_id", "adset_name"],
    "ad": ["campaign_id", "campaign_name", "adset_id", "adset_name", "ad_id", "ad_name"],
}
PURCHASE_TYPES = ("omni_purchase", "purchase")
DEFAULT_ATTRIBUTION = ["7d_click", "1d_view"]

Level = Literal["account", "campaign", "adset", "ad"]
Breakdown = Literal["age", "gender", "country", "region", "publisher_platform", "platform_position",
                    "device_platform", "impression_device"]
AttributionWindow = Literal["1d_click", "7d_click", "28d_click", "1d_view", "1d_ev"]
DatePreset = Literal["", "today", "yesterday", "this_month", "last_month", "this_quarter", "last_quarter",
                     "this_year", "last_year", "last_3d", "last_7d", "last_14d", "last_28d", "last_30d", "last_90d"]
BREAKDOWNS = frozenset(Breakdown.__args__)                 # type: ignore[attr-defined]
ATTRIBUTION_WINDOWS = frozenset(AttributionWindow.__args__)  # type: ignore[attr-defined]
DATE_PRESETS = frozenset(DatePreset.__args__)              # type: ignore[attr-defined]

ACCOUNT_STATUS = {1: "ACTIVE", 2: "DISABLED", 3: "UNSETTLED", 7: "PENDING_RISK_REVIEW", 8: "PENDING_SETTLEMENT",
                  9: "IN_GRACE_PERIOD", 100: "PENDING_CLOSURE", 101: "CLOSED", 201: "ANY_ACTIVE", 202: "ANY_CLOSED"}

METRICS = {
    "spend": {"unit": "account currency", "definition": "amount spent in the period, as reported by Meta"},
    "impressions": {"unit": "count", "definition": "times the ads were on screen"},
    "clicks": {"unit": "count", "definition": "all clicks (Meta 'clicks (all)', not only link clicks)"},
    "reach": {"unit": "people", "definition": "unique accounts reached; not additive across rows (totals: null "
                                              "when several rows)"},
    "frequency": {"unit": "ratio", "definition": "impressions / reach, as reported by Meta"},
    "ctr": {"unit": "ratio 0..1", "definition": "clicks / impressions recomputed here (Meta reports a percent)"},
    "cpc": {"unit": "account currency", "definition": "spend / clicks"},
    "cpm": {"unit": "account currency", "definition": "spend / impressions * 1000"},
    "purchases": {"unit": "count", "definition": "purchases attributed by Meta (action omni_purchase, else "
                                                 "purchase; see purchase_action_type_used); 0 when a row has "
                                                 "spend and no purchase action; null when several attribution "
                                                 "windows are requested (see purchases_by_window)"},
    "purchase_value": {"unit": "account currency", "definition": "value of those purchases (action_values); 0 "
                                                                 "when a row has spend and no purchase value"},
    "purchases_by_window": {"unit": "count", "definition": "purchases per requested attribution window, read "
                                                           "from Meta's per-window keys, never added together"},
    "purchase_value_by_window": {"unit": "account currency", "definition": "purchase value per requested "
                                                                           "attribution window, never added"},
    "roas": {"unit": "ratio", "definition": "purchase_value / spend, computed here; totals = sum of values / "
                                            "sum of spends over every row with spend"},
    "cpa": {"unit": "account currency", "definition": "spend / purchases"},
    "meta_reported_roas": {"unit": "ratio", "definition": "purchase_roas exactly as returned by Meta (raw list)"},
}

# [0-9], not \d: \d also matches non-ASCII digits.
NOT_MEASURED_NOTE = ("aucun évènement d'achat remonté : pixel/CAPI non configuré ou aucun achat ? non mesurable "
                     "(no purchase / omni_purchase entry in any row: purchases, purchase_value and roas are null)")
_VERSION = re.compile(r"v[0-9]+\.[0-9]+")
_ACCOUNT = re.compile(r"[0-9]+")

__all__ = ["ALLOWED_ENDPOINTS", "CLIENT", "TOOLS", "meta_ad_accounts", "meta_insights", "register", "summary"]


# ------------------------------------------------------------------ helpers
def _settings() -> AdsSettings:
    """Read once per tool call (the token is then reused for every page: one keyring read per call)."""
    return AdsSettings.from_env()


def _context() -> tuple[AdsSettings, str] | dict:
    """(settings, token), or a structured error when the settings cannot be read (broken keyring backend...).
    Raises NotConfigured when no token is set (turned into not_configured by guard)."""
    try:
        settings = _settings()
    except NotConfigured:
        raise
    except Exception as exc:  # never crash a tool on a broken keyring or environment
        return {"error": "not_configured", "platform": PLATFORM,
                "detail": f"could not read the Meta settings ({type(exc).__name__})",
                "fix": "set META_ACCESS_TOKEN in the MCP server env, or repair the OS keyring"}
    return settings, _token(settings)


def _token(settings: AdsSettings) -> str:
    if not settings.meta_access_token:
        raise NotConfigured("META_ACCESS_TOKEN")
    return settings.meta_access_token


class InvalidInput(ValueError):
    """A value that cannot be put in a Graph URL; returned as {"error": code, ...}, never sent."""

    def __init__(self, code: str, detail: str, fix: str) -> None:
        super().__init__(detail)
        self.code, self.detail, self.fix = code, detail, fix

    def as_dict(self) -> dict:
        return {"error": self.code, "platform": PLATFORM, "detail": self.detail, "fix": self.fix}


def _safe_message(exc: BaseException) -> str:
    return redact(f"{type(exc).__name__}: {exc}")[:300]


def _contained(fn: Callable[..., dict]) -> Callable[..., dict]:
    """Inside guard() (same as google_ads._contained): InvalidInput becomes its error dict and any unexpected
    exception a structured internal_error with secrets masked, instead of crashing the tool. NotConfigured is
    left to guard()."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict:
        try:
            return fn(*args, **kwargs)
        except NotConfigured:
            raise
        except InvalidInput as exc:
            return exc.as_dict()
        except Exception as exc:
            return {"error": "internal_error", "platform": PLATFORM, "detail": _safe_message(exc),
                    "fix": "unexpected failure in the Meta Ads connector; retry, and report it if it persists"}

    return wrapper


def _base(settings: AdsSettings) -> str:
    """https://graph.facebook.com/<version>; raises InvalidInput (invalid_api_version) when META_API_VERSION is
    malformed (same code as google_ads)."""
    version = settings.meta_api_version
    if not _VERSION.fullmatch(version or ""):
        raise InvalidInput("invalid_api_version", f"META_API_VERSION must look like v26.0, got {version!r}",
                           "set META_API_VERSION to a Graph API version such as v26.0, or unset it")
    return f"{GRAPH}/{version}"


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _invalid(detail: str) -> dict:
    return {"error": "invalid_argument", "platform": PLATFORM, "detail": detail}


def _account(account_id: str, settings: AdsSettings) -> str:
    """'act_123', '123' or '' (configured account) -> '123'; raises ValueError / NotConfigured."""
    raw = (account_id or "").strip() or (settings.meta_ad_account_id or "")
    if not raw:
        raise NotConfigured("META_AD_ACCOUNT_ID")
    if raw.lower().startswith("act_"):
        raw = raw[4:]
    if not _ACCOUNT.fullmatch(raw):
        raise ValueError(f"account_id must be digits (optionally prefixed act_), got {account_id or raw!r}")
    return raw


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    number = _num(value)
    return int(number) if number is not None else None


def _entries(items: Any) -> dict[str, dict]:
    """Meta [{action_type, value, <window>: ...}] -> {action_type: entry}."""
    return {str(item["action_type"]): item for item in (items if isinstance(items, list) else [])
            if isinstance(item, dict) and item.get("action_type")}


def _action_map(entries: dict[str, dict]) -> dict[str, float]:
    """{action_type: Meta 'value'} for context (the default attribution, or as Meta reports it)."""
    out = {name: _num(entry.get("value")) for name, entry in entries.items()}
    return {name: value for name, value in out.items() if value is not None}


def _purchase_type(actions: dict, values: dict) -> str | None:
    for name in PURCHASE_TYPES:
        if name in actions or name in values:
            return name
    return None


def _measure(entries: dict[str, dict], used: str | None, key: str, zero_if_absent: bool) -> float | None:
    """The purchase figure under one key: 'value' (default attribution) or the requested window key, never a
    fallback from a window key to 'value'. Absent means 0 only when purchases are measured somewhere in the
    data and the row has spend (zero_if_absent); otherwise unknown (None)."""
    entry = entries.get(used) if used else None
    number = _num(entry.get(key)) if entry is not None else None
    if number is not None:
        return number
    return 0.0 if zero_if_absent else None


def _has_purchase_entry(raw: dict) -> bool:
    return _purchase_type(_entries(raw.get("actions")), _entries(raw.get("action_values"))) is not None


def _paged(url: str, headers: dict, params: dict, cache: bool = False) -> dict:
    """Read up to MAX_PAGES pages following paging.cursors.after (never paging.next). Returns
    {data, pages_read, complete, cached} or the error dict of the failing page."""
    rows: list = []
    pages, cached_pages, complete = 0, 0, False
    query = dict(params)
    while pages < MAX_PAGES:
        page = CLIENT.call("GET", url, headers=headers, params=query, cache=cache)
        if "error" in page:
            return page
        pages += 1
        cached_pages += 1 if page.get("cached") else 0
        rows.extend(page.get("data") or [])
        paging = page.get("paging") if isinstance(page.get("paging"), dict) else {}
        cursors = paging.get("cursors") if isinstance(paging.get("cursors"), dict) else {}
        after = cursors.get("after")
        # Meta omits paging.next on the last page; its URL is never requested, only its presence is read.
        if not after or "next" not in paging:
            complete = True
            break
        query = {**params, "after": str(after)}
    return {"data": rows, "pages_read": pages, "complete": complete, "cached": pages > 0 and cached_pages == pages}


def _row(raw: dict, level: str, breakdowns: list[str], windows: list[str], measured: bool) -> dict:
    action_entries, value_entries = _entries(raw.get("actions")), _entries(raw.get("action_values"))
    actions, values = _action_map(action_entries), _action_map(value_entries)
    used = _purchase_type(action_entries, value_entries)
    spend, impressions, clicks = _num(raw.get("spend")), _int(raw.get("impressions")), _int(raw.get("clicks"))
    # Zero is only meaningful when purchases are measured: some row of the result carries a purchase entry.
    zero = measured and spend is not None
    row: dict[str, Any] = {key: raw.get(key) for key in LEVEL_FIELDS[level] if key in raw}
    row.update({key: raw.get(key) for key in breakdowns if key in raw})
    row.update({
        "date_start": raw.get("date_start"), "date_stop": raw.get("date_stop"),
        "spend": spend, "impressions": impressions, "clicks": clicks,
        "reach": _int(raw.get("reach")), "frequency": _num(raw.get("frequency")),
        "ctr": safe_ratio(clicks, impressions),
        "cpc": _num(raw.get("cpc")) if raw.get("cpc") is not None else safe_ratio(spend, clicks),
        "cpm": _num(raw.get("cpm")),
    })
    if len(windows) > 1:
        # Windows overlap (1d_click is inside 7d_click): each is read from its own key, never added up.
        by_count = {w: _measure(action_entries, used, w, zero) for w in windows}
        by_value = {w: _measure(value_entries, used, w, zero) for w in windows}
        purchases = purchase_value = None
        row.update({"purchases_by_window": by_count, "purchase_value_by_window": by_value,
                    "roas_by_window": {w: roas(by_value[w], spend) for w in windows}})
    else:
        key = windows[0] if windows else "value"
        purchases = _measure(action_entries, used, key, zero)
        purchase_value = _measure(value_entries, used, key, zero)
    row.update({
        "purchases": purchases, "purchase_value": purchase_value,
        "purchase_action_type_used": used,
        "roas": roas(purchase_value, spend),
        "cpa": safe_ratio(spend, purchases) if purchases else None,
        "meta_reported_roas": raw.get("purchase_roas"),
        "currency": raw.get("account_currency"),
        "actions": actions, "action_values": values,
    })
    return row


def _sum(rows: list[dict], key: str) -> float | None:
    numbers = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
    return round(sum(numbers), 6) if numbers else None


def _pooled_roas(rows: list[dict], value_key: str) -> float | None:
    """Sum of values / sum of spends over every row with spend (values of those rows are 0.0, never None)."""
    spent = [r for r in rows if isinstance(r.get("spend"), (int, float))]
    return total_roas(spent, value_key, "spend")


def _no_activity(rows: list[dict]) -> bool:
    """No row, or rows without any spend nor impression: nothing happened, so 0 purchases is a fact."""
    return not rows or (not _sum(rows, "spend") and not _sum(rows, "impressions"))


def _totals(rows: list[dict], windows: list[str]) -> dict:
    spend, impressions, clicks = _sum(rows, "spend"), _sum(rows, "impressions"), _sum(rows, "clicks")
    purchases, value = _sum(rows, "purchases"), _sum(rows, "purchase_value")
    idle = _no_activity(rows)
    if idle:
        purchases, value = purchases or 0.0, value or 0.0
    single = len(rows) == 1
    totals: dict[str, Any] = {
        "spend": spend if spend is not None else 0.0,
        "impressions": int(impressions) if impressions is not None else 0,
        "clicks": int(clicks) if clicks is not None else 0,
        "reach": rows[0].get("reach") if single else None,
        "frequency": rows[0].get("frequency") if single else None,
        "ctr": safe_ratio(clicks, impressions),
        "cpc": safe_ratio(spend, clicks),
        "cpm": safe_ratio((spend or 0) * 1000, impressions) if spend is not None else None,
        "purchases": purchases,
        "purchase_value": value,
        "roas": _pooled_roas(rows, "purchase_value"),
        "cpa": safe_ratio(spend, purchases) if purchases else None,
        "no_activity": idle,
    }
    if len(windows) > 1:
        flat = [{"spend": r.get("spend"),
                 **{f"c:{w}": (r.get("purchases_by_window") or {}).get(w) for w in windows},
                 **{f"v:{w}": (r.get("purchase_value_by_window") or {}).get(w) for w in windows}} for r in rows]
        totals.update({
            "purchases": None, "purchase_value": None, "roas": None, "cpa": None,
            "purchases_by_window": {w: _sum(flat, f"c:{w}") if not idle else 0.0 for w in windows},
            "purchase_value_by_window": {w: _sum(flat, f"v:{w}") if not idle else 0.0 for w in windows},
            "roas_by_window": {w: _pooled_roas(flat, f"v:{w}") for w in windows},
        })
    return totals


def _attribution(windows: list[str]) -> dict:
    if len(windows) > 1:
        return {"windows": list(windows), "default_used": False, "single_total": False,
                "note": "several attribution windows requested: they overlap, so no single purchases / "
                        "purchase_value / roas is given; read purchases_by_window, purchase_value_by_window and "
                        "roas_by_window (one value per window, never added together)"}
    if windows:
        return {"windows": list(windows), "default_used": False, "single_total": True,
                "note": f"purchases and purchase_value use the {windows[0]} window only"}
    return {"windows": list(DEFAULT_ATTRIBUTION), "default_used": True, "single_total": True,
            "note": "Meta default attribution applied: 7-day click + 1-day view (the account's setting can differ)"}


# ------------------------------------------------------------------ tools
@guard("current state; account list cached 5 minutes")
@_contained
def meta_ad_accounts() -> dict:
    """List the Meta ad accounts the access token can read (id act_..., name, status, currency, timezone).

    Use it to find the account id for META_AD_ACCOUNT_ID or for the account_id argument of meta_insights."""
    context = _context()
    if isinstance(context, dict):
        return context
    settings, token = context
    base = _base(settings)
    result = _paged(f"{base}/me/adaccounts", _headers(token), {"fields": ACCOUNT_FIELDS, "limit": 100},
                    cache=True)
    if "error" in result:
        return result
    accounts = []
    for item in result["data"]:
        if not isinstance(item, dict):
            continue
        status = _int(item.get("account_status"))
        accounts.append({"id": item.get("id"), "account_id": item.get("account_id") or
                         str(item.get("id", "")).replace("act_", ""), "name": item.get("name"),
                         "account_status": status, "account_status_label": ACCOUNT_STATUS.get(status),
                         "currency": item.get("currency"), "timezone_name": item.get("timezone_name")})
    return {"accounts": accounts, "count": len(accounts), "complete": result["complete"],
            "configured_account_id": settings.meta_ad_account_id, "api_version": settings.meta_api_version,
            "cached": result["cached"]}


@guard("Meta Insights; the last 3 days can still change (late attributed conversions)")
@_contained
def meta_insights(
    level: Annotated[Level, Field(description="Row level: account (one row per period), campaign, adset or ad")]
    = "account",
    start_date: Annotated[str, Field(description="YYYY-MM-DD, first day in the ad account's timezone; leave empty "
                                     "with end_date to use date_preset")] = "",
    end_date: Annotated[str, Field(description="YYYY-MM-DD, last day included; leave empty with start_date to use "
                                   "date_preset")] = "",
    date_preset: Annotated[DatePreset, Field(description="Meta relative period (e.g. last_7d, last_month), "
                                             "exclusive with start_date/end_date; all three empty means last_28d")]
    = "",
    breakdowns: Annotated[tuple[Breakdown, ...], Field(description="Optional extra split of each row, from a "
                                                       "fixed list: age, gender, country, region, "
                                                       "publisher_platform, platform_position, device_platform, "
                                                       "impression_device")] = (),
    attribution_windows: Annotated[tuple[AttributionWindow, ...], Field(
        description="Optional attribution windows for purchases (e.g. 7d_click); empty means Meta's default "
                    "7-day click + 1-day view; several windows give one figure per window, no single total")] = (),
    time_increment: Annotated[Literal["all_days", "1"], Field(description="all_days: one row for the whole "
                                                              "period; 1: one row per day")] = "all_days",
    limit: Annotated[int, Field(description="Maximum rows returned (1 to 500), highest spend first "
                                "(daily rows too); totals cover up to 5 pages of 500 rows",
                                ge=1, le=MAX_LIMIT)] = 100,
    account_id: Annotated[str, Field(description="Ad account id (digits or act_<digits>); empty means "
                                     "META_AD_ACCOUNT_ID")] = "",
) -> dict:
    """Meta Ads performance (spend, impressions, clicks, reach, purchases, purchase value, ROAS) by level.

    ctr is recomputed as a ratio 0..1, ROAS = purchase value / spend (totals: sum of values / sum of spends),
    Meta's own purchase_roas is kept raw in meta_reported_roas. Currency is the ad account's, never converted."""
    context = _context()
    if isinstance(context, dict):
        return context
    settings, token = context
    try:
        account = _account(account_id, settings)
    except ValueError as exc:
        return _invalid(str(exc))
    base = _base(settings)
    breakdowns = list(breakdowns or [])
    windows = list(dict.fromkeys(attribution_windows or ()))
    bad = [b for b in breakdowns if b not in BREAKDOWNS] + [w for w in windows if w not in ATTRIBUTION_WINDOWS]
    if bad:
        return _invalid(f"unsupported breakdowns or attribution windows: {bad}")
    if level not in LEVEL_FIELDS or time_increment not in ("all_days", "1") or date_preset not in DATE_PRESETS:
        return _invalid("level, time_increment or date_preset is not one of the allowed values")
    if date_preset and (start_date or end_date):
        return _invalid("date_preset and start_date/end_date are mutually exclusive: give one or the other")
    if bool(start_date) != bool(end_date):
        return _invalid("give both start_date and end_date (YYYY-MM-DD), or neither")
    preset_defaulted = not (date_preset or start_date)
    preset = date_preset or ("last_28d" if preset_defaulted else "")
    if start_date:
        try:
            validate_dates(start_date, end_date)
        except ValueError as exc:
            return _invalid(str(exc))

    headers = _headers(token)
    info = CLIENT.call("GET", f"{base}/act_{account}", headers=headers,
                       params={"fields": "currency,timezone_name"}, cache=True)
    if "error" in info:
        return info

    max_rows = cap(limit, MAX_LIMIT)
    params: dict[str, Any] = {"level": level, "fields": ",".join(LEVEL_FIELDS[level] + INSIGHT_FIELDS),
                              "time_increment": time_increment, "limit": MAX_LIMIT}
    if preset:
        params["date_preset"] = preset
    else:
        params["time_range"] = json.dumps({"since": start_date, "until": end_date}, separators=(",", ":"))
    if breakdowns:
        params["breakdowns"] = ",".join(breakdowns)
    if windows:
        params["action_attribution_windows"] = json.dumps(windows, separators=(",", ":"))

    # Pages of 500 rows (up to 5) so that totals cover as many rows as possible; `limit` only trims the rows.
    result = _paged(f"{base}/act_{account}/insights", headers, params)
    if "error" in result:
        return result
    raw_rows = [r for r in result["data"] if isinstance(r, dict)]
    measured = any(_has_purchase_entry(r) for r in raw_rows)
    all_rows = [_row(r, level, breakdowns, windows, measured) for r in raw_rows]
    # Highest spend first in every mode (daily rows too), so trimming to `limit` drops the cheapest rows; the
    # sort is stable, so equal spends keep Meta's (chronological) order.
    all_rows.sort(key=lambda r: r["spend"] or 0.0, reverse=True)
    rows = all_rows[:max_rows]
    rows_omitted = len(all_rows) - len(rows)
    used = sorted({r["purchase_action_type_used"] for r in all_rows if r["purchase_action_type_used"]})
    currency = info.get("currency") or next((r["currency"] for r in rows if r.get("currency")), None)
    timezone = info.get("timezone_name")
    if start_date:
        period = date_range(start_date, end_date, timezone, settle_days=SETTLE_DAYS, note=SETTLE_NOTE)
    else:
        starts = sorted(r["date_start"] for r in all_rows if r.get("date_start"))
        stops = sorted(r["date_stop"] for r in all_rows if r.get("date_stop"))
        period = (date_range(starts[0], stops[-1], timezone, settle_days=SETTLE_DAYS, note=SETTLE_NOTE)
                  if starts and stops else {"timezone": timezone or "unknown"})
        period["date_preset"] = preset
        if preset_defaulted:
            period["date_preset_defaulted"] = True
    return {
        "account_id": f"act_{account}", "level": level, "breakdowns": breakdowns,
        "rows": rows, "row_count": len(rows), "rows_read": len(all_rows),
        "rows_order": "spend descending (also for daily rows: sort by date_start for a time series)",
        "truncated": rows_omitted > 0, "rows_omitted": rows_omitted,
        "totals": _totals(all_rows, windows),
        "totals_complete": result["complete"],
        "totals_note": None if result["complete"] else
        f"more rows exist beyond {MAX_PAGES} pages: totals cover the rows read only; narrow the request",
        "pages_read": result["pages_read"],
        "purchase_action_type_used": used[0] if len(used) == 1 else (used or None),
        "currency": currency,
        "date_range": period,
        "attribution": _attribution(windows),
        "purchase_measurement": {"measured": measured,
                                 "note": None if measured or _no_activity(all_rows) else NOT_MEASURED_NOTE},
        "metrics": METRICS,
    }


TOOLS = [("Meta ad accounts", meta_ad_accounts), ("Meta Ads insights", meta_insights)]


def register(mcp: Any) -> list[str]:
    """Register the Meta Ads tools on a FastMCP server (READ_ONLY annotations); returns their names."""
    return register_tools(mcp, TOOLS)


def summary(start_date: str, end_date: str) -> dict:
    """Account totals for ads_overview: spend, impressions, clicks, conversions (purchases), conversion_value,
    roas, currency, attribution, date_range; or the error dict ({"error": ...}, e.g. not_configured).
    Never raises: an unexpected failure becomes {"error": "internal_error"} with secrets masked."""
    try:
        result = meta_insights(level="account", start_date=start_date, end_date=end_date, breakdowns=(),
                               attribution_windows=(), time_increment="all_days", limit=MAX_LIMIT)
        if "error" in result:
            return {"platform": PLATFORM, **result}
        totals = result["totals"]
        return {
            "platform": PLATFORM,
            "spend": totals["spend"], "impressions": totals["impressions"], "clicks": totals["clicks"],
            "conversions": totals["purchases"], "conversion_value": totals["purchase_value"],
            "roas": totals["roas"], "currency": result["currency"],
            "attribution": result["attribution"], "date_range": result["date_range"],
            "conversion_definition": "Meta purchases (omni_purchase, else purchase), as attributed by Meta",
            "no_activity": totals["no_activity"],
            "purchase_measurement": result["purchase_measurement"],
            "totals_complete": result["totals_complete"],
            "fetched_at": result.get("fetched_at"), "freshness": result.get("freshness"),
        }
    except Exception as exc:  # the overview must never break because of one platform
        return {"platform": PLATFORM, "error": "internal_error",
                "detail": redact(f"{type(exc).__name__}: {exc}"[:300]),
                "fix": "report this bug; the other platforms are unaffected"}

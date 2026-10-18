"""Cross-platform ads overview, read-only: Google Ads, Meta and OpenAI Ads side by side.

ads_overview calls the summary(start_date, end_date) of each connector (google_ads, meta_ads, openai_ads) and
never sends an HTTP request itself. A platform that is not configured gets a not_configured block and a platform
that fails gets its error block; the others are still returned (partial result).

Calculation rules: spends are added up only when every platform that returned data reports the same, known
currency (no currency conversion, ever); otherwise combined is null with the reason. A platform without activity
(spend 0, nothing converted) adds zeros and needs no currency. The combined ROAS is the sum
of conversion values divided by the sum of spends, never the mean of the platform ROAS. Conversions are attributed
independently by each platform with different windows and overlap: their sum is not deduplicated, which the
mandatory warning states in every output."""

from __future__ import annotations

import copy
from types import ModuleType
from typing import Annotated, Any, Optional

from pydantic import Field

from . import clients, google_ads, meta_ads, openai_ads
from .clients import ClientError
from .ads_common import guard, redact, register_tools, roas, validate_dates
from .config import NotConfigured

WARNING = ("Les conversions de chaque plateforme sont attribuées indépendamment et se recoupent. "
           "La somme n'est pas dédupliquée et surestime les commandes réelles.")
FRESHNESS = ("each platform's own reporting delay; the most recent day and conversions still inside their "
             "attribution window can change")

# Platform key -> connector module. summary is looked up at call time (replaceable in tests).
PLATFORMS: tuple[tuple[str, ModuleType], ...] = (
    ("google_ads", google_ads),
    ("meta", meta_ads),
    ("openai_ads", openai_ads),
)

# Documented defaults, shown when a platform did not return its own attribution (not configured, error).
DEFAULT_ATTRIBUTION: dict[str, Any] = {
    "google_ads": {"basis": "réglage par action de conversion",
                   "note": "each conversion action has its own model and lookback window, set in Google Ads"},
    "meta": {"windows": ["7d_click", "1d_view"],
             "note": "Meta default: 7-day click + 1-day view (the account's setting can differ)"},
    "openai_ads": {"click_window_days": 30, "view_window_days": 1,
                   "note": "OpenAI Ads documented default: 30 days after a click, 1 day after a view"},
}

_BLOCK_FIELDS = ("spend", "impressions", "clicks", "conversions", "conversion_value", "roas", "currency",
                 "attribution", "date_range", "totals_complete")
_EXTRA_FIELDS = ("no_activity", "currency_note", "account_info_error", "attribution_note", "conversion_definition",
                 "customer_id", "units", "timezone_reported", "timezone_note", "null_means")
_ACTIVITY_FIELDS = ("spend", "impressions", "clicks")
_CONVERSION_FIELDS = ("conversions", "conversion_value")


def _attribution(platform: str, value: Any) -> dict:
    """Attribution always as a dict: a string becomes {"basis": <string>}, None the documented default."""
    if isinstance(value, dict):
        return value
    if value is None:
        return copy.deepcopy(DEFAULT_ATTRIBUTION[platform])
    return {"basis": value if isinstance(value, str) else str(value)}


def _call_summary(module: ModuleType, platform: str, start_date: str, end_date: str,
                  client_id: str | None = None) -> dict:
    """One platform's summary as a dict, never raising (NotConfigured and crashes become error dicts).
    client_id None = legacy mode: summary(start, end) exactly as before; else summary(..., client_id=<that client>)."""
    try:
        result = (module.summary(start_date, end_date) if client_id is None
                  else module.summary(start_date, end_date, client_id=client_id))
    except NotConfigured as exc:
        return {"platform": platform, "error": "not_configured", "detail": redact(str(exc))}
    except Exception as exc:  # one broken connector must not hide the others
        return {"platform": platform, "error": "internal_error",
                "detail": redact(f"{type(exc).__name__}: {exc}")[:500]}
    if not isinstance(result, dict):
        return {"platform": platform, "error": "internal_error",
                "detail": f"summary returned {type(result).__name__}, expected dict"}
    return result


def _block(platform: str, result: dict) -> dict:
    """Normalise a summary into a side-by-side block with a status: ok, not_configured or error."""
    if "error" in result:
        status = "not_configured" if result.get("error") == "not_configured" else "error"
        block = {k: v for k, v in result.items() if k not in ("fetched_at", "freshness")}
        block.update({"platform": platform, "status": status,
                      "attribution": copy.deepcopy(DEFAULT_ATTRIBUTION[platform]),
                      "attribution_source": "documented default"})
        return _clean_currency(block) if "currency" in block else block
    block: dict[str, Any] = {"platform": platform, "status": "ok"}
    for key in _BLOCK_FIELDS:
        block[key] = result.get(key)
    block["roas_reported_by_platform"] = block.pop("roas")
    block["attribution_source"] = "documented default" if block["attribution"] is None else "platform"
    block["attribution"] = _attribution(platform, block["attribution"])
    for key in _EXTRA_FIELDS:
        if key in result:
            block[key] = result[key]
    return _clean_currency(block)


def _clean_currency(block: dict) -> dict:
    """Keep currency only if it is a non-empty string; otherwise None, with a currency_note when one was given."""
    raw = block.get("currency")
    if isinstance(raw, str) and raw.strip():
        return block
    block["currency"] = None
    if raw is not None:
        prior = block.get("currency_note")
        block["currency_note"] = (f"currency ignored: {type(raw).__name__} is not a non-empty string"
                                  + (f"; {prior}" if isinstance(prior, str) and prior else ""))
    return block


def _is_zero(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0


def _without_activity(block: dict) -> bool:
    """An ok block with nothing spent and nothing converted: no_activity flagged by the platform (or every
    activity metric at zero, as Google Ads answers an empty account), spend == 0 and no conversion value.
    Such a block adds nothing to any sum, so its currency (often unknown on an empty account) is not required."""
    if not _is_zero(block.get("spend")):
        return False
    if not all(block.get(k) is None or _is_zero(block.get(k)) for k in _CONVERSION_FIELDS):
        return False
    return block.get("no_activity") is True or all(_is_zero(block.get(k)) for k in _ACTIVITY_FIELDS)


def _sum(values: list[Any]) -> float | None:
    """Sum of numbers; None as soon as one value is missing (a partial sum would mislead)."""
    if any(v is None or isinstance(v, bool) or not isinstance(v, (int, float)) for v in values):
        return None
    return round(sum(values), 6)


def _combined(blocks: list[dict]) -> tuple[dict | None, str | None]:
    """Totals across the ok blocks, or (None, reason). Same known currency required for the blocks with activity
    (a block without activity adds zeros and needs no currency); ROAS pooled, never averaged."""
    ok = [b for b in blocks if b["status"] == "ok"]
    if not ok:
        return None, "no platform returned data"
    idle = [b for b in ok if _without_activity(b)]
    active = [b for b in ok if not _without_activity(b)]
    priced = active or [b for b in idle if b.get("currency")]
    if not priced:
        return None, "no activity on any platform"
    unknown = [b["platform"] for b in active if not b.get("currency")]
    if unknown:
        return None, f"currency unknown for {', '.join(unknown)}: spends are not added up without a currency"
    currencies = sorted({b["currency"] for b in priced})
    if len(currencies) > 1:
        by_platform = ", ".join(f"{b['platform']}={b['currency']}" for b in priced)
        return None, f"different currencies ({by_platform}): no currency conversion, spends are not added up"

    def total(key: str) -> float | None:
        # Idle blocks count as zero (a None there means "nothing happened", not "unknown").
        return _sum([b.get(key) for b in active] + [b.get(key) or 0 for b in idle])

    spend = total("spend")
    if spend is None:
        missing = [b["platform"] for b in active if not isinstance(b.get("spend"), (int, float))]
        return None, f"spend missing for {', '.join(missing)}"
    value = total("conversion_value")
    combined: dict[str, Any] = {
        "currency": currencies[0],
        "platforms_included": [b["platform"] for b in ok],
        "platforms_without_activity": [b["platform"] for b in idle],
        "spend": spend,
        "impressions": total("impressions"),
        "clicks": total("clicks"),
        "conversions_sum_not_deduplicated": total("conversions"),
        "conversion_value_sum_not_deduplicated": value,
        "roas": roas(value, spend) if value is not None else None,
        "roas_definition": "sum of conversion values / sum of spends (never the mean of the platform ROAS); "
                           "overstated because the conversion values overlap",
        "totals_complete": all(b.get("totals_complete") is not False for b in ok),
    }
    if value is None:
        combined["roas_reason"] = "conversion value missing for at least one platform"
    return combined, None


@guard(FRESHNESS)
def ads_overview(
    start_date: Annotated[str, Field(description="First day, YYYY-MM-DD (each platform counts in its own "
                                                 "account timezone)")],
    end_date: Annotated[str, Field(description="Last day, YYYY-MM-DD, on or after start_date")],
    client_id: Annotated[Optional[str], Field(description="Client id from clients_list; empty uses the only "
                                                          "client. Only that client's platforms are added up")] = None,
) -> dict:
    """Google Ads, Meta and OpenAI Ads totals side by side for one period: spend, impressions, clicks, conversions
    and conversion value as reported by each platform, the platform's own ROAS, currency, attribution windows and
    date range. A platform not configured or failing gets its own block without blocking the others (partial).
    combined adds spends only when every currency is the same (else null with combined_reason; a platform without
    activity adds zeros and needs no currency) and computes ROAS
    as total value / total spend. Conversions overlap between platforms: their sum is not deduplicated (warning).
    Multi-client: one client only (client_id), and only the platforms that client has; never two clients mixed."""
    try:
        validate_dates(start_date, end_date)
    except ValueError as exc:
        return {"error": "invalid_dates", "detail": str(exc), "warning": WARNING}
    try:
        ctx = clients.resolve(client_id)
    except ClientError as exc:
        return {**exc.to_dict(), "warning": WARNING}
    try:
        return _overview(start_date, end_date, None if ctx.legacy else ctx)
    except Exception as exc:  # never lose the mandatory warning, whatever a connector answered
        return {"error": "internal_error", "warning": WARNING,
                "detail": redact(f"{type(exc).__name__}: {exc}")[:500],
                "fix": "unexpected answer from an ads connector; query each platform's own tool to find which"}


def _not_configured_for(platform: str, client_id: str) -> dict:
    return {"platform": platform, "error": "not_configured",
            "detail": f"client {client_id!r} has no {platform} account in the registry",
            "fix": f"google-ecommerce-mcp client add {client_id} --replace (asks the missing ids)"}


def _overview(start_date: str, end_date: str, ctx: Any = None) -> dict:
    """ctx None = legacy mode (unchanged). Otherwise every summary gets ctx.client_id, and a platform the client
    does not have is not called at all (not_configured block)."""
    if ctx is None:
        results = [(name, _call_summary(module, name, start_date, end_date)) for name, module in PLATFORMS]
    else:
        mine = set(ctx.platforms())
        results = [(name, _call_summary(module, name, start_date, end_date, ctx.client_id) if name in mine
                    else _not_configured_for(name, ctx.client_id)) for name, module in PLATFORMS]
    if ctx is not None:  # a block stamped with another client is never added up
        results = [(name, {"platform": name, "error": "internal_error",
                           "detail": "summary answered for another client; ignored"})
                   if result.get("client_id") not in (None, ctx.client_id) else (name, result)
                   for name, result in results]
    blocks = [_block(name, result) for name, result in results]
    combined, reason = _combined(blocks)
    out: dict[str, Any] = {
        "warning": WARNING,
        "requested_range": {"start_date": start_date, "end_date": end_date},
        "platforms": {b["platform"]: b for b in blocks},
        "attribution_side_by_side": {b["platform"]: b["attribution"] for b in blocks},
        "combined": combined,
        "partial": any(b["status"] != "ok" for b in blocks),
        "not_configured": [b["platform"] for b in blocks if b["status"] == "not_configured"],
        "errors": [b["platform"] for b in blocks if b["status"] == "error"],
        "platforms_without_activity": [b["platform"] for b in blocks
                                       if b["status"] == "ok" and _without_activity(b)],
        "notes": ["each platform reports in its own account timezone and attribution windows; compare with care",
                  "conversions and conversion values are as declared by each platform, not orders"],
    }
    if reason is not None:
        out["combined_reason"] = reason
    if ctx is not None:
        out["client_id"] = ctx.client_id
    return out


TOOLS = [
    ("Ads overview (all platforms)", ads_overview),
]


def register(mcp: Any) -> list[str]:
    """Register ads_overview on a FastMCP server (READ_ONLY annotations); returns its name."""
    return register_tools(mcp, TOOLS)

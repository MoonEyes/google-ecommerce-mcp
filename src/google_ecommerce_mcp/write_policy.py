"""Write policy: the locks every plan passes, at plan time AND again at apply time (write_core).

Locks (each refusal is {"error", "detail", "fix", "op_id"}):
  switches     GEM_WRITE=1 (process), write.<platform>.enabled (client), account in account_allow AND owned by
               the client (ctx.owns), op platform == plan platform
  size         1..max_ops_per_plan ops (hard ceiling 100), unique op_ids
  shape        risk in RISKS, method POST/PATCH/PUT/DELETE, https URL without query string nor dot segments,
               rollback "auto"|"none"
  binding      the op's account must be the account the request really targets: when the platform puts the
               account in the path (Google Ads customers/<id>, Meta act_<id>, OpenAI Ads accounts/<id>...) it
               must be exactly that id; otherwise the handler's `before` snapshot must carry it
               (account_id / customer / ad_account). Any other account id in the URL, body, params or snapshot
               is refused.
  payload      body AND params are inspected (JSON objects AND form strings "a=b&c=d" encoded in strings
               included). Every status must be a known pure-ASCII string (a non-ASCII value, even one that
               upper-cases to PAUSED like "pauſed", is status_unknown); budgets must be strict integers. Status
               keys are status, *_status / *status (configured_status, effective_status, status_option,
               adset_status...), state, and the boolean enabled / is_active / active / paused.
  mask         an updateMask / update_mask / fieldMask / field_mask / paths key must hold a plain string of
               field paths ("status,name"): a list, an object ({"paths": [...]}), a JSON string or a token that
               is not a field path is refused (update_mask_not_string), so no mask escapes the other locks.
  fields       ALLOW-LIST: every key of body/params (dotted path, list indices dropped, e.g.
               "operations.update.status") and every updateMask field must be registered for (platform, action)
               with register_field_allowlist (field_not_allowed); no allow-list registered = refused
               (no_field_allowlist, fail closed) unless _TEST_PERMISSIVE (tests only). Registration itself
               refuses (ValueError): a path naming money (names_money: each segment is split into WORDS,
               camelCase / snake_case / kebab-case / digits, and a whole word of MONEY_WORDS - budget, amount,
               spend, cap, limit, cost, bid, price, value, micros, cpc, cpm, cpa, cpv, roas, revenue, fee,
               charge, pay, payment, tcpa, troas, margin, markup... - or an unambiguous substring such as
               "dailybudget" names money; "caption", "landscape", "limited", "forbidden" do not) unless its leaf
               is a DAILY_BUDGET_KEYS field or the EXACT path is listed in money_ok (kept in the registry);
               bidding / lifetime-budget fields; a ".*" subtree whose prefix has fewer than 3 segments
               (the policy cannot know every child a platform accepts, so subtrees stay narrow); a second,
               different list for the same (platform, action) (replace=True: tests only, _TEST_PERMISSIVE);
               anything after freeze_allowlists() (registry_frozen). At check time a field reached only
               through a ".*" subtree must not name money either (field_not_allowed). The registry is private
               (_FIELD_ALLOWLIST): field_allowlist() / field_money_ok() / golden_allowlists() read copies.
  override     a method / _method / http_method / override / relative_url / batch key in body/params is refused
               (method_override_in_payload): the policy checks op.method and op.url, nothing else.
  creation     action "create*", a `create` key in body/params, or (Meta / OpenAI Ads) a POST to a collection
               URL, whatever the builder's label: every created entity needs an explicit PAUSED status, at the
               FIRST level of body or params (or directly in a Google Ads create / update object); a nested
               PAUSED (tracking_specs[0].status) does not count. Meta POST .../copies also needs
               status_option PAUSED (a copy otherwise inherits the source's status).
  activation   any other op setting ENABLED/ACTIVE: allow_activate, and the op must be flagged financial
  references   budget / bidding-strategy references (campaignBudget, budget_id, shared_budget...) and, outside
               a creation, re-parenting ids (campaign_id, adset_id, ad_group_id...) change what is spent: the op
               must be flagged financial (financial_undeclared)
  delete       DELETE / remove / delete / status REMOVED|DELETED|ARCHIVED: allow_delete; Google Ads remove also
               needs the "gem-managed" label on the entity's `before` snapshot
  irreversible risk "irreversible": allow_irreversible; pii: allow_pii, and personal-data keys (PII_KEYS:
               substrings email, phone, hashed, userdata, ipaddress, firstname, lastname, zip, postal,
               externalid, gclid, fbc, fbp, address...; exact em, ph, fn, ln...) in body/params require the op
               to be flagged pii
  bidding      bid / bidding-strategy / target CPA-ROAS fields are not supported (refused): known keys and
               prefixes, any whole BID_WORDS word (maxCpc, enhanced_cpc, fixedCpm, max_bid, tcpa...)
  budget       daily budget fields (micros; Meta daily_budget in minor units of the account currency, converted
               with its decimals; a currency of unknown decimals is refused): the op must be
               financial, max_daily_budget_micros and cap_currency set, account currency (from `before` only)
               known and equal, value <= cap, and for an update value <= baseline * (1 + max_increase_pct/100),
               the baseline being the lowest of the fresh `before` and the journaled `before` values of the
               last 24 h. The plan total is <= cap * max_ops_per_plan (and <= the optional
               max_plan_budget_micros, read with getattr: clients.py does not accept that field in
               clients.json today, so in production only the cap * max_ops_per_plan ceiling applies).
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from typing import TYPE_CHECKING, Any, Callable, Iterable, Sequence
from urllib.parse import parse_qsl, unquote, urlsplit

from .write_journal import sha256_hex

if TYPE_CHECKING:  # contracts of clients.py / write_core.py, not imported at run time
    from .clients import ClientContext, WritePolicy
    from .write_core import WriteOp

__all__ = ["RISKS", "WRITE_ENV", "LIVE_ENV", "GEM_LABEL", "HARD_MAX_OPS", "write_enabled", "live_test",
           "policy_hash", "violation", "force_paused", "is_create", "is_remove", "activates", "budget_values",
           "currency_of", "has_gem_label", "check_ops", "pii_keys", "is_pii_key", "minor_unit_multiplier",
           "MINOR_UNIT_DECIMALS", "PII_KEYS", "PII_EXACT_KEYS", "register_field_allowlist",
           "field_allowlist", "field_money_ok", "freeze_allowlists", "allowlists_frozen", "METHOD_OVERRIDE_KEYS",
           "BUDGET_REFERENCE_KEYS", "REPARENT_KEYS", "golden_allowlists", "MONEY_WORDS", "MONEY_TERMS",
           "BID_WORDS", "field_words", "names_money", "MASK_KEYS"]

RISKS = ("low", "medium", "high", "irreversible")
WRITE_ENV = "GEM_WRITE"
LIVE_ENV = "GEM_LIVE_TEST"
GEM_LABEL = "gem-managed"
HARD_MAX_OPS = 100
WRITE_METHODS = ("POST", "PATCH", "PUT", "DELETE")
STATUS_KEYS = frozenset({"status", "configured_status"})
ACTIVE_VALUES = frozenset({"ENABLED", "ACTIVE"})
PAUSED_VALUES = frozenset({"PAUSED"})
REMOVE_VALUES = frozenset({"REMOVED", "DELETED", "ARCHIVED"})
RISKY_VALUES = ACTIVE_VALUES | REMOVE_VALUES
KNOWN_STATUS_VALUES = ACTIVE_VALUES | PAUSED_VALUES | REMOVE_VALUES
# Daily budget field -> multiplier to micros; None = minor units of the account currency (Meta daily_budget),
# converted with the currency exponent (MINOR_UNIT_DECIMALS).
DAILY_BUDGET_KEYS: dict[str, int | None] = {"amountMicros": 1, "amount_micros": 1, "daily_budget_micros": 1,
                                            "dailyBudgetMicros": 1, "budget_micros": 1, "daily_budget": None}
# Decimals of the minor unit, per currency (Meta "offset": 1 = 0 decimals, 100 = 2). Misfiling a currency as
# 0-decimal only over-counts (stricter), so the Meta offset-1 currencies are listed there. Any currency not
# listed is refused for minor-unit budgets (currency_unit_unknown).
_ZERO_DECIMALS = frozenset({"JPY", "KRW", "VND", "CLP", "ISK", "UGX", "XAF", "XOF", "PYG", "RWF", "BIF", "DJF",
                            "GNF", "KMF", "VUV", "XPF", "COP", "CRC", "HUF", "IDR", "TWD"})
_THREE_DECIMALS = frozenset({"BHD", "KWD", "OMR", "JOD", "TND", "IQD", "LYD"})
_TWO_DECIMALS = frozenset({"EUR", "USD", "GBP", "CHF", "CAD", "AUD", "NZD", "SEK", "NOK", "DKK", "PLN", "CZK",
                           "RON", "BGN", "MXN", "BRL", "ARS", "PEN", "UYU", "BOB", "GTQ", "HNL", "NIO", "DOP",
                           "ZAR", "INR", "SGD", "HKD", "MYR", "THB", "PHP", "CNY", "ILS", "TRY", "AED", "SAR",
                           "QAR", "EGP", "NGN", "KES", "MAD", "PKR", "BDT", "LKR", "UAH", "RSD", "GEL", "KZT",
                           "DZD", "MOP"})
MINOR_UNIT_DECIMALS = {**{c: 0 for c in _ZERO_DECIMALS}, **{c: 2 for c in _TWO_DECIMALS},
                       **{c: 3 for c in _THREE_DECIMALS}}
_DEFAULT_MINOR_MULT = 10_000  # only while the currency is unknown, which is itself refused
# Personal data: the single canonical list (write_core imports it). A key is personal data when its normalized
# form (lower case, no _ / -) CONTAINS one of PII_KEYS, or EQUALS one of PII_EXACT_KEYS (Meta user_data short
# keys, too short to match as substrings).
PII_KEYS = ("email", "phone", "hashed", "userdata", "useridentifier", "ipaddress", "firstname", "lastname",
            "zip", "postal", "externalid", "gclid", "gbraid", "wbraid", "fbc", "fbp", "address", "useragent",
            "dateofbirth", "birthdate")
PII_EXACT_KEYS = ("em", "ph", "fn", "ln", "db", "ge", "ct", "st", "zp", "dob")
# Keys that would let the payload pick another HTTP method or request than op.method / op.url.
METHOD_OVERRIDE_KEYS = frozenset({"method", "_method", "http_method", "httpMethod", "override",
                                  "method_override", "x-http-method-override", "relative_url", "batch"})
# References to a budget or a bidding strategy: always financial.
BUDGET_REFERENCE_KEYS = frozenset({"campaignBudget", "campaign_budget", "budget", "budget_id", "budgetId",
                                   "shared_budget", "sharedBudget", "shared_budget_id", "bidding_strategy",
                                   "biddingStrategy", "bidding_strategy_id", "portfolio_bidding_strategy"})
# Parent ids: on an existing entity, changing them moves it under another budget: financial.
REPARENT_KEYS = frozenset({"campaign", "campaign_id", "campaignId", "adset", "adset_id", "adsetId", "ad_set_id",
                           "adGroup", "ad_group", "ad_group_id", "adGroupId"})


@dataclasses.dataclass(frozen=True)
class _Allowlist:
    """One registered allow-list: normalized paths, and the exact paths exempted from the money rule."""

    fields: frozenset[str]
    money_ok: frozenset[str]


# Field allow-list per (platform, action): normalized dotted paths ("operations.update.status"), a trailing
# ".*" allows a whole subtree. PRIVATE: filled only through register_field_allowlist, read through
# field_allowlist() / golden_allowlists() (copies); freeze_allowlists() closes it.
_FIELD_ALLOWLIST: dict[tuple[str, str], _Allowlist] = {}
_FROZEN = False
_MAX_FIELD_DEPTH = 4
# A ".*" subtree needs at least this many prefix segments ("operations.update.*" is too wide).
_MIN_SUBTREE_SEGMENTS = 3
# WORDS that name money or bids. A field path segment is split into words (camelCase, snake_case, kebab-case,
# digits): "maxCpc" -> max, cpc; "daily_spend_cap" -> daily, spend, cap; "caption" -> caption (no money). A
# segment names money when one of its WHOLE words (or that word's plural "...s") is in MONEY_WORDS. Never
# allow-listed, except a DAILY_BUDGET_KEYS leaf (bounded by the budget locks) or an exact path listed in
# register_field_allowlist(..., money_ok=...).
MONEY_WORDS = frozenset({"budget", "amount", "spend", "spending", "cap", "limit", "cost", "bid", "bidding",
                         "price", "value", "micros", "cpc", "cpm", "cpa", "cpv", "roas", "revenue", "fee",
                         "charge", "pay", "payment", "tcpa", "troas", "margin", "markup"})
MONEY_TERMS = tuple(sorted(MONEY_WORDS))  # compatibility alias (sorted words)
# Substrings of a normalized segment (lower case, no _ -) that name money even when the segment is not split
# into words ("dailybudget", "maxcpc"). Only long or unambiguous ones: never cap / limit / bid / pay / fee /
# value / charge, which hide in ordinary words (caption, limited, forbidden, display, feed).
_MONEY_SUBSTRINGS = ("budget", "amount", "spend", "cost", "price", "micros", "bidding", "revenue", "payment",
                     "cpc", "cpm", "cpv", "tcpa", "troas", "roas")
# Words naming a bid: a payload field holding one is refused (bidding_not_supported), whole words only.
BID_WORDS = frozenset({"bid", "bidding", "cpc", "cpm", "cpa", "cpv", "roas", "tcpa", "troas"})
_BID_SUBSTRINGS = ("bidding", "bidamount", "bidmicros", "bidstrategy", "bidmodifier", "bidadjust", "maxbid",
                   "cpc", "cpm", "cpv", "tcpa", "troas", "roas", "targetcpa")
_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
# Keys holding a field mask: their value must be a plain string of field paths.
MASK_KEYS = frozenset({"updateMask", "update_mask", "fieldMask", "field_mask", "paths"})
_MASK_TOKEN_RE = re.compile(r"[A-Za-z0-9_.-]+")
# Tests only: True (or a collection of actions / (platform, action) pairs) lets an op WITHOUT a registered
# allow-list through. Never set it in production code.
_TEST_PERMISSIVE: bool | frozenset = False
# Collections a POST creates an entity in (last path segment), per platform.
_CREATE_COLLECTIONS = {
    "meta": frozenset({"campaigns", "adsets", "ads", "adcreatives", "copies", "adimages", "advideos",
                       "customaudiences", "adlabels", "adrules_library"}),
    "openai_ads": frozenset({"campaigns", "ad_groups", "adgroups", "ads", "creatives"}),
}
UNSUPPORTED_BUDGET_KEYS = frozenset({"lifetime_budget", "lifetime_budget_micros", "totalAmountMicros",
                                     "spend_cap", "spend_cap_micros"})
# Bidding fields: no cap covers them, so they are refused (bidding_not_supported).
BIDDING_KEYS = frozenset({"cpc_bid_micros", "cpcBidMicros", "cpm_bid_micros", "cpmBidMicros", "cpv_bid_micros",
                          "cpvBidMicros", "targetCpaMicros", "target_cpa_micros", "targetRoas", "target_roas",
                          "bid_amount", "bid_strategy", "bid_constraints", "bidding_strategy",
                          "biddingStrategy", "manualCpc", "maximizeConversions", "maximizeConversionValue"})
CURRENCY_KEYS = ("currency", "currency_code", "currencyCode", "account_currency")
_LOWER_STATUS_PLATFORMS = frozenset({"openai_ads"})
_MAX_NODES = 200_000
_MAX_JSON_STRING = 1_000_000


def _nk(key: Any) -> str:
    """Normalized key: lower case, without _ and - (amountMicros == amount_micros == AMOUNT-MICROS)."""
    return str(key).lower().replace("_", "").replace("-", "")


_STATUS_NK = frozenset({"status", "configuredstatus", "effectivestatus", "statusoption", "adsetstatus",
                        "adstatus", "campaignstatus", "executionstatus", "deliverystatus", "state"})
# Boolean status keys: value True means (ENABLED, PAUSED)[0 or 1]; "paused" is inverted.
_BOOL_STATUS_NK = {"enabled": ("ENABLED", "PAUSED"), "isenabled": ("ENABLED", "PAUSED"),
                   "active": ("ENABLED", "PAUSED"), "isactive": ("ENABLED", "PAUSED"),
                   "paused": ("PAUSED", "ENABLED"), "ispaused": ("PAUSED", "ENABLED")}
_BUDGET_NK = {_nk(k): mult for k, mult in DAILY_BUDGET_KEYS.items()}
_PII_PARTS = tuple(_nk(k) for k in PII_KEYS)
_PII_EXACT = frozenset(_nk(k) for k in PII_EXACT_KEYS)
_OVERRIDE_NK = frozenset(_nk(k) for k in METHOD_OVERRIDE_KEYS)
_BUDGET_REF_NK = frozenset(_nk(k) for k in BUDGET_REFERENCE_KEYS)
_REPARENT_NK = frozenset(_nk(k) for k in REPARENT_KEYS)
_UNSUPPORTED_NK = frozenset(_nk(k) for k in UNSUPPORTED_BUDGET_KEYS)
_UNSUPPORTED_PREFIXES = ("lifetimebudget", "lifetimespend", "totalamount", "spendcap", "dailyspendlimit")
_BIDDING_NK = frozenset(_nk(k) for k in BIDDING_KEYS)
_BIDDING_PREFIXES = ("bid", "targetcpa", "targetroas", "targetspend", "targetimpressionshare", "targetcpm",
                     "targetcpv", "maximizeconversion", "manualcp", "percentcpc", "commission",
                     "roasaveragefloor", "biddingstrategy")
_ACCOUNT_FIELD_NK = frozenset({"accountid", "customer", "customerid", "adaccount", "adaccountid"})
_MASK_NK = frozenset(_nk(k) for k in MASK_KEYS)
# Platforms that put the account in the URL path: regex capturing the raw account id.
_ACCOUNT_REFS = {
    "google_ads": re.compile(r"customers/([0-9][0-9-]*)"),
    "meta": re.compile(r"(?<![A-Za-z0-9])act_([0-9]+)", re.IGNORECASE),
    "openai_ads": re.compile(r"(?:ad_accounts|accounts)/([A-Za-z0-9_-]+)"),
    "merchant": re.compile(r"accounts/([0-9]+)"),
    "ga4": re.compile(r"properties/([0-9]+)"),
}


class PayloadError(ValueError):
    """The payload is too large to inspect (fail closed)."""


def write_enabled() -> bool:
    """True only when GEM_WRITE is exactly "1" (read at each call)."""
    return (os.environ.get(WRITE_ENV) or "").strip() == "1"


def live_test() -> bool:
    """True when GEM_LIVE_TEST=1: apply then requires a sandbox client."""
    return (os.environ.get(LIVE_ENV) or "").strip() == "1"


def policy_hash(policy: Any) -> str:
    """SHA-256 of the canonical form of a WritePolicy (a changed policy invalidates outstanding plans)."""
    data = dataclasses.asdict(policy) if dataclasses.is_dataclass(policy) else dict(vars(policy))
    return sha256_hex(data)


def violation(code: str, detail: str, fix: str, op_id: str | None = None) -> dict:
    out = {"error": code, "detail": detail, "fix": fix}
    if op_id is not None:
        out["op_id"] = op_id
    return out


# ------------------------------------------------------------------ payload inspection
_FORM_PAIR = r"[A-Za-z0-9_.\[\]%~-]+=[^&=\s]+"
_FORM_RE = re.compile(rf"{_FORM_PAIR}(?:&{_FORM_PAIR})*")


def _decode_form(text: str) -> Any:
    """'a=b&c=d' -> {"a": "b", "c": "d"} (a list of one-key dicts when a key repeats, so every value is seen);
    None when text is not such a string (base64 padding "abc==" and URLs do not match)."""
    if not _FORM_RE.fullmatch(text):
        return None
    try:
        pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    if len({k for k, _ in pairs}) != len(pairs):
        return [{k: v} for k, v in pairs]
    return dict(pairs)


def _decode(value: Any) -> Any:
    """A str holding a JSON object/array, or a form string 'a=b&c=d' (Meta form params, batch bodies), is
    inspected as that object."""
    if isinstance(value, str) and len(value) <= _MAX_JSON_STRING:
        text = value.strip()
        if text[:1] in ("{", "["):
            try:
                parsed = json.loads(text)
            except (ValueError, RecursionError):
                return value
            if isinstance(parsed, (dict, list)):
                return parsed
        elif "=" in text:
            form = _decode_form(text)
            if form is not None:
                return form
    return value


def _walk(value: Any, path: tuple = ()) -> Iterable[tuple[tuple, str, Any]]:
    """(parent path, key, value) for every key of every dict in value (iterative, JSON strings decoded)."""
    stack: list[tuple[tuple, Any]] = [(path, value)]
    seen = 0
    while stack:
        here, item = stack.pop()
        item = _decode(item)
        seen += 1
        if seen > _MAX_NODES:
            raise PayloadError(f"more than {_MAX_NODES} nodes")
        if isinstance(item, dict):
            entries = list(item.items())
            for key, child in entries:
                yield here, key, child
            stack.extend((here + (key,), child) for key, child in reversed(entries))
        elif isinstance(item, (list, tuple)):
            stack.extend((here + (index,), child) for index, child in reversed(list(enumerate(item))))


def _strings(value: Any) -> Iterable[str]:
    """Every string (keys excluded) found in value, JSON strings decoded."""
    stack = [value]
    seen = 0
    while stack:
        item = stack.pop()
        seen += 1
        if seen > _MAX_NODES:
            raise PayloadError(f"more than {_MAX_NODES} nodes")
        if isinstance(item, str):
            yield item
        decoded = _decode(item)
        if isinstance(decoded, dict):
            stack.extend(decoded.values())
        elif isinstance(decoded, (list, tuple)):
            stack.extend(decoded)


def _payload(op: WriteOp) -> tuple[Any, Any]:
    return op.body, getattr(op, "params", None)


def _is_status_key(key: Any) -> bool:
    """status, state, any *status key (status_option, adset_status, effectiveStatus...), and the boolean
    enabled / is_active / active / paused keys."""
    if not isinstance(key, str):
        return False
    n = _nk(key)
    return n in _STATUS_NK or n.endswith("status") or n in _BOOL_STATUS_NK


def _status_value(key: str, value: Any) -> str | None:
    """The status a key/value pair sets, upper-cased (booleans mapped to ENABLED / PAUSED); None when it is
    not parseable."""
    pair = _BOOL_STATUS_NK.get(_nk(key))
    if pair is not None:
        if isinstance(value, bool):
            return pair[0] if value else pair[1]
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return pair[0] if value.strip().lower() == "true" else pair[1]
    if isinstance(value, str) and value.strip():
        text = value.strip()
        upper = text.upper()
        if not (text.isascii() and upper.isascii()):
            # "pauſed".upper() == "PAUSED": a non-ASCII value never counts as a known status (status_unknown)
            return f"<non-ascii {_safe_repr(text)}>"
        return upper
    return None


def _status_scan(*objs: Any) -> tuple[list[str], list[str]]:
    """(upper-cased statuses, descriptions of unparseable ones) found in objs."""
    values: list[str] = []
    bad: list[str] = []
    for obj in objs:
        for _, key, value in _walk(obj):
            if not _is_status_key(key):
                continue
            status = _status_value(key, value)
            if status is not None:
                values.append(status)
            else:
                bad.append(f"{key}={type(value).__name__}")
    return values, bad


def _direct_statuses(obj: Any) -> list[str]:
    """Statuses set by the FIRST-level keys of obj only (a nested PAUSED does not make an entity paused)."""
    obj = _decode(obj)
    if not isinstance(obj, dict):
        return []
    return [s for k, v in obj.items() if _is_status_key(k) and (s := _status_value(k, v)) is not None]


def _top_paused(op: WriteOp) -> bool:
    """A flat creation is paused only by a first-level PAUSED status of body or params (Google Ads: also one
    set directly in an `update` object)."""
    payload = _payload(op)
    if any(s in PAUSED_VALUES for obj in payload for s in _direct_statuses(obj)):
        return True
    if op.platform == "google_ads":
        updates = [v for obj in payload for _, k, v in _walk(obj) if isinstance(k, str) and _nk(k) == "update"]
        return any(s in PAUSED_VALUES for u in updates for s in _direct_statuses(u))
    return False


def _is_meta_copy(op: WriteOp) -> bool:
    if op.platform != "meta" or op.method.upper() != "POST":
        return False
    path = unquote(unquote(urlsplit(op.url).path)).rstrip("/")
    return path.rsplit("/", 1)[-1].lower() == "copies"


def _copy_paused(op: WriteOp) -> bool:
    """Meta copies inherit the source status unless status_option is PAUSED (first level of body/params)."""
    for obj in _payload(op):
        obj = _decode(obj)
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(k, str) and _nk(k) == "statusoption" and _status_value(k, v) == "PAUSED":
                    return True
    return False


def _statuses(body: Any) -> list[str]:
    return _status_scan(body)[0]


def force_paused(body: dict | None, platform: str, *, copy: bool = False) -> dict:
    """Copy of body where every status is PAUSED (paused for OpenAI Ads; False for enabled / active, True for
    paused); every Google Ads `create` object gets one, and a flat body without a first-level status gets a
    top-level one. copy=True (Meta POST .../copies) also sets status_option PAUSED. For plan builders
    (creation = paused)."""
    value = "paused" if platform in _LOWER_STATUS_PLATFORMS else "PAUSED"

    def paused_value(key: str) -> Any:
        pair = _BOOL_STATUS_NK.get(_nk(key))
        return value if pair is None else pair[0] == "PAUSED"

    def fix(item: Any) -> Any:
        if isinstance(item, dict):
            out = {k: (paused_value(k) if _is_status_key(k) else fix(v)) for k, v in item.items()}
            for k, v in out.items():
                if _nk(k) == "create" and isinstance(v, dict) and not any(_is_status_key(x) for x in v):
                    v["status"] = value
            return out
        if isinstance(item, list):
            return [fix(v) for v in item]
        return item

    out = fix(dict(body or {}))
    if not _creates(out) and not any(_is_status_key(k) for k in out):
        out["status"] = value
    if copy:
        out["status_option"] = "PAUSED"
    return out


def _create_values(*objs: Any) -> list[Any]:
    """Values of every `create` key (any case) in objs, except None."""
    return [v for obj in objs for _, k, v in _walk(obj) if isinstance(k, str) and _nk(k) == "create"
            and v is not None]


def _creates(*objs: Any) -> list[dict]:
    """Every created entity: a `create` dict, each item of a `create` list; any other `create` value counts as
    an entity without status ({}), so it fails the PAUSED check."""
    out: list[dict] = []
    for value in _create_values(*objs):
        value = _decode(value)
        items = value if isinstance(value, (list, tuple)) else [value]
        out.extend(item if isinstance(item, dict) else {} for item in items)
    return out


def _posts_to_collection(op: WriteOp) -> bool:
    collections = _CREATE_COLLECTIONS.get(op.platform)
    if not collections or op.method.upper() != "POST":
        return False
    path = unquote(unquote(urlsplit(op.url).path)).rstrip("/")
    return path.rsplit("/", 1)[-1].lower() in collections


def is_create(op: WriteOp) -> bool:
    """A creation whatever the builder's label: action create*, a `create` key in body/params (Google Ads
    mutate), or (Meta / OpenAI Ads) a POST to a collection URL (/act_X/campaigns, /v1/ad_groups...)."""
    if str(op.action).lower().startswith("create"):
        return True
    try:
        if _create_values(*_payload(op)):
            return True
    except PayloadError:
        return True  # cannot prove it is not one: the strictest rule applies (check_ops refuses it anyway)
    return _posts_to_collection(op)


def is_pii_key(key: Any) -> bool:
    """True when key names personal data: its normalized form contains one of PII_KEYS or equals one of
    PII_EXACT_KEYS."""
    if not isinstance(key, str):
        return False
    n = _nk(key)
    return n in _PII_EXACT or any(part in n for part in _PII_PARTS)


def pii_keys(op: WriteOp) -> list[str]:
    """Personal-data keys (is_pii_key) found in body/params, updateMask fields included."""
    return sorted({k for k in _keys(*_payload(op)) if is_pii_key(k)})


def is_remove(op: WriteOp) -> bool:
    """DELETE, a remove/delete action, a `remove` key anywhere in body/params, or a REMOVED / DELETED /
    ARCHIVED status."""
    action = op.action.lower()
    if op.method.upper() == "DELETE" or action in ("remove", "delete", "archive") or action.startswith(
            ("remove_", "delete_", "archive_")):
        return True
    payload = _payload(op)
    if any(_nk(k) == "remove" for obj in payload for _, k, _ in _walk(obj)):
        return True
    return any(s in REMOVE_VALUES for s in _status_scan(*payload)[0])


def activates(op: WriteOp) -> bool:
    return any(s in ACTIVE_VALUES for s in _status_scan(*_payload(op))[0])


_INT_RE = re.compile(r"[0-9]+")
_MAX_INT = 10 ** 30  # far above any real budget; bigger ints cannot even be printed in a refusal (4300 digits)


def _strict_int(value: Any) -> int | None:
    """int (not bool, >= 0) or a str of ASCII digits; anything else (floats, 5e7, 5.5, bool, None) is None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value < _MAX_INT else None
    if isinstance(value, str) and _INT_RE.fullmatch(value.strip()):
        try:
            parsed = int(value.strip())
        except ValueError:  # over sys.get_int_max_str_digits() (4300) digits: unparseable, refused
            return None
        return parsed if parsed < _MAX_INT else None
    return None


def _safe_repr(value: Any) -> str:
    """repr for a refusal message: never raises (ints over 4300 digits cannot be converted to str)."""
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) >= _MAX_INT:
        return f"<int of about {value.bit_length() * 0.30103:.0f} digits>"
    try:
        return repr(value)[:80]
    except (ValueError, RecursionError):
        return f"<{type(value).__name__}>"


def minor_unit_multiplier(currency: str | None) -> int | None:
    """Micros per minor unit of currency (EUR 10 000, JPY 1 000 000, KWD 1 000); None when unknown."""
    decimals = MINOR_UNIT_DECIMALS.get((currency or "").strip().upper())
    return None if decimals is None else 10 ** (6 - decimals)


def _budget_scan(*objs: Any, minor: int = _DEFAULT_MINOR_MULT) -> tuple[list[int], list[str]]:
    """(daily budgets in micros, keys whose value is not a strict integer) found in objs; minor-unit fields
    are multiplied by `minor`."""
    values: list[int] = []
    bad: list[str] = []
    for obj in objs:
        for _, key, value in _walk(obj):
            if not (isinstance(key, str) and _nk(key) in _BUDGET_NK):
                continue
            mult = _BUDGET_NK[_nk(key)] or minor
            parsed = _strict_int(value)
            if parsed is None:
                bad.append(f"{key}={_safe_repr(value)}"[:80])
            else:
                values.append(parsed * mult)
    return values, bad


def _has_minor_units(*objs: Any) -> bool:
    return any(isinstance(k, str) and _BUDGET_NK.get(_nk(k), 1) is None for obj in objs for _, k, _ in _walk(obj))


def budget_values(obj: Any, currency: str | None = None) -> list[int]:
    """Daily budgets found in obj, in micros (only strict integers; check_ops refuses the others). Minor units
    use currency, else the currency the snapshot obj carries, else 2 decimals."""
    mult = minor_unit_multiplier(currency or _currency_in(obj)) or _DEFAULT_MINOR_MULT
    return _budget_scan(obj, minor=mult)[0]


def _is_mask_key(key: Any) -> bool:
    return isinstance(key, str) and _nk(key) in _MASK_NK


def _mask_tokens(value: Any) -> list[str] | None:
    """Field paths of a mask value ("status,campaign.name"); None when the value is not a plain string of
    field paths (a list, an object, a JSON string, a token with other characters)."""
    if not isinstance(value, str) or not isinstance(_decode(value), str):
        return None
    tokens = [t for t in re.split(r"[,\s]+", value) if t]
    if not all(_MASK_TOKEN_RE.fullmatch(t) for t in tokens):
        return None
    return tokens


def _bad_masks(*objs: Any) -> list[str]:
    """Mask keys (MASK_KEYS) whose value is not a plain string of field paths."""
    return sorted({f"{key}={type(value).__name__}" for obj in objs for _, key, value in _walk(obj)
                   if _is_mask_key(key) and _mask_tokens(value) is None})


def _mask_fields(*objs: Any) -> list[str]:
    fields: list[str] = []
    for obj in objs:
        for _, key, value in _walk(obj):
            if _is_mask_key(key):
                for token in _mask_tokens(value) or ():
                    fields.extend(p for p in token.split(".") if p)
    return fields


def _keys(*objs: Any) -> list[str]:
    return [k for obj in objs for _, k, _ in _walk(obj) if isinstance(k, str)] + _mask_fields(*objs)


def _is_unsupported(key: str) -> bool:
    n = _nk(key)
    return n in _UNSUPPORTED_NK or n.startswith(_UNSUPPORTED_PREFIXES)


def field_words(segment: Any) -> list[str]:
    """Lower-case words of one field segment: camelCase, snake_case, kebab-case and digits split
    ("maxCpc" -> ["max", "cpc"], "daily_spend_cap" -> ["daily", "spend", "cap"], "caption" -> ["caption"])."""
    return [w.lower() for w in _WORD_RE.findall(str(segment))]


def _word_in(words: Iterable[str], vocabulary: frozenset[str]) -> bool:
    """A whole word (or its plural "...s") is in vocabulary."""
    return any(w in vocabulary or (len(w) > 3 and w.endswith("s") and w[:-1] in vocabulary) for w in words)


def _is_bidding(key: str) -> bool:
    """Bid field: a known bidding key or prefix, a whole BID_WORDS word (maxCpc, enhanced_cpc, fixedCpm,
    max_bid), an unambiguous bid substring (maxbid, cpcbidmicros), or a segment ending in "bid(s)". A word that
    only contains "bid" (forbidden) is not a bid."""
    n = _nk(key)
    return (n in _BIDDING_NK or n.startswith(_BIDDING_PREFIXES) or _word_in(field_words(key), BID_WORDS)
            or any(s in n for s in _BID_SUBSTRINGS) or n.endswith(("bid", "bids")))


def _unsupported_budget(*objs: Any) -> list[str]:
    return sorted({k for k in _keys(*objs) if _is_unsupported(k)})


def _bidding(*objs: Any) -> list[str]:
    return sorted({k for k in _keys(*objs) if _is_bidding(k) and not _is_unsupported(k)})


def currency_of(op: WriteOp) -> str | None:
    """Account currency from the op's `before` snapshot only (never from the body the builder wrote)."""
    return _currency_in(op.before)


def _currency_in(obj: Any) -> str | None:
    for _, key, value in _walk(obj):
        if key in CURRENCY_KEYS and isinstance(value, str) and value.strip():
            return value.strip().upper()
    return None


def has_gem_label(before: Any) -> bool:
    """True when the snapshot carries the gem-managed label (labels: names or {"name": ...})."""
    if not isinstance(before, dict):
        return False
    if before.get("gem_managed") is True:
        return True
    labels = before.get("labels") or []
    if not isinstance(labels, (list, tuple)):
        return False
    names = {(lab.get("name") if isinstance(lab, dict) else lab) for lab in labels}
    return GEM_LABEL in names


# ------------------------------------------------------------------ accounts
_ID_PREFIXES = ("act_", "customers/", "properties/", "accounts/")


def _norm_account(platform: str, value: str) -> str:
    """Account id in the registry's normalized form (clients.py rules when available: act_, dashes,
    properties/...), so "act_1" and "1", "123-456-7890" and "1234567890" compare equal."""
    try:
        from . import clients
        normalize = getattr(clients, "_normalize_any", None)
        family = getattr(clients, "WRITE_PLATFORMS", {}).get(platform, platform)
        if normalize is not None:
            norm = normalize(family, value)
            if norm is not None:
                return str(norm)
    except Exception:
        pass
    text = value.strip()
    for prefix in _ID_PREFIXES:
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
    return text.replace("-", "") if text.replace("-", "").isdigit() else text


def _refs(platform: str, texts: Iterable[str]) -> set[str]:
    pattern = _ACCOUNT_REFS.get(platform)
    if pattern is None:
        return set()
    return {_norm_account(platform, m) for text in texts for m in pattern.findall(text)}


def _field_accounts(platform: str, obj: Any, top_only: bool) -> set[str]:
    """Normalized values of account_id / customer / ad_account fields (top level of obj, or anywhere)."""
    found: set[str] = set()
    if top_only:
        items: Iterable[tuple[Any, Any]] = obj.items() if isinstance(obj, dict) else ()
    else:
        items = ((k, v) for _, k, v in _walk(obj))
    for key, value in items:
        if not (isinstance(key, str) and _nk(key) in _ACCOUNT_FIELD_NK):
            continue
        if isinstance(value, dict):
            value = value.get("id", value.get("resourceName"))
        if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
            continue
        found.add(_norm_account(platform, str(value)))
    return found


def _url_problems(url: str) -> list[tuple[str, str, str]]:
    out = []
    head = url.split("#", 1)[0]
    if "?" in head or urlsplit(url).query:
        out.append(("query_in_url", "the URL carries a query string, which the policy does not inspect.",
                    "Put every parameter in op.params (inspected), never in the URL."))
    path = urlsplit(url).path
    segments = path.split("/") + unquote(unquote(path)).replace("\\", "/").split("/")
    if any(seg in (".", "..") for seg in segments) or "\\" in path:
        out.append(("bad_url", "the URL path has dot segments or backslashes.", "Fix the plan builder."))
    return out


def _binding(platform: str, op: WriteOp) -> list[tuple[str, str, str]]:
    """The op's account must be the account the request targets (URL path, else the `before` snapshot)."""
    expected = _norm_account(platform, str(op.account_id))
    path = urlsplit(op.url).path
    in_path = _refs(platform, (path, unquote(path), unquote(unquote(path))))
    body, params = _payload(op)
    elsewhere = _refs(platform, [s for obj in (body, params, op.before) for s in _strings(obj)])
    elsewhere |= _field_accounts(platform, body, top_only=False) | _field_accounts(platform, params, top_only=False)
    elsewhere |= _field_accounts(platform, op.before, top_only=False)
    foreign = sorted((in_path | elsewhere) - {expected})
    if foreign:
        return [("account_not_bound", f"the request references account(s) {foreign}, not the plan's account "
                 f"{op.account_id}.", "One account per op: build the op for the account it really targets.")]
    if in_path:
        return []
    if op.before is None:
        return [("before_unknown", f"the URL does not name account {op.account_id}, and there is no snapshot "
                 "to bind the entity to it.", "Retry; the handler must snapshot the entity (with its account).")]
    bound = _field_accounts(platform, op.before, top_only=True) | _refs(platform, _strings(op.before))
    if expected not in bound:
        return [("account_not_bound", f"neither the URL nor the snapshot ties the entity to account "
                 f"{op.account_id}.", "The handler's snapshot must carry the entity's account_id (or customer / "
                 "ad_account).")]
    return []


# ------------------------------------------------------------------ field allow-list
def _norm_path(path: str) -> str:
    segs = [seg.strip() for seg in str(path).split(".")]
    if not path or any(not seg for seg in segs) or "*" in segs[:-1]:
        raise ValueError(f"bad field path {path!r}")
    base = segs[:-1] if segs[-1] == "*" else segs
    if not base or len(base) > _MAX_FIELD_DEPTH:
        raise ValueError(f"field path {path!r}: 1 to {_MAX_FIELD_DEPTH} segments (deeper: use a '.*' subtree)")
    return ".".join([_nk(s) for s in base] + (["*"] if segs[-1] == "*" else []))


def names_money(segments: Iterable[Any]) -> bool:
    """True when RAW field segments (original spelling, so camelCase can be split) name money or a bid: a
    whole word of a segment (field_words, plural included) in MONEY_WORDS, an unambiguous money substring of
    the normalized segment (_MONEY_SUBSTRINGS: dailybudget, maxcpc), or a bidding / lifetime-budget field.
    "caption", "landscape", "limited", "forbidden" do not name money. A DAILY_BUDGET_KEYS leaf is bounded by
    the budget locks, so it is left out of the test (only as the LAST segment)."""
    segs = [str(s) for s in segments]
    if segs and _nk(segs[-1]) in _BUDGET_NK:
        segs = segs[:-1]
    for seg in segs:
        n = _nk(seg)
        if (_word_in(field_words(seg), MONEY_WORDS) or any(t in n for t in _MONEY_SUBSTRINGS)
                or _is_bidding(seg) or _is_unsupported(seg)):
            return True
    return False


_names_money = names_money  # internal name kept for the callers below


def _raw_segments(path: str) -> list[str]:
    """Segments of a field path as written (stripped), without a trailing '*'."""
    segs = [seg.strip() for seg in str(path).split(".")]
    return segs[:-1] if segs and segs[-1] == "*" else segs


def _registration_problem(raw: str, entry: str, exempt: bool) -> str | None:
    segs = entry.split(".")
    subtree = segs[-1] == "*"
    base = segs[:-1] if subtree else segs
    if subtree and len(base) < _MIN_SUBTREE_SEGMENTS:
        return (f"subtree {entry!r}: a '.*' needs at least {_MIN_SUBTREE_SEGMENTS} prefix segments (the policy "
                "cannot know every child field a platform accepts; list the fields one by one)")
    if not exempt and names_money(_raw_segments(raw)):
        return (f"field {raw!r} names money (a word of {sorted(MONEY_WORDS)}, bidding or lifetime budget): only "
                f"the daily budget fields {sorted(DAILY_BUDGET_KEYS)} (as the leaf of a path) or an exact path "
                "listed in money_ok can be allow-listed")
    return None


def register_field_allowlist(platform: str, action: str, fields: Iterable[str], *,
                             money_ok: Iterable[str] = (), replace: bool = False) -> frozenset[str]:
    """Allow-list the body/params fields an op of (platform, action) may carry: dotted paths, list indices
    dropped ("name", "operations.update.status", "operations.updateMask"), at most 4 segments;
    "a.b.c.*" allows a whole subtree (prefix of 3 segments at least). Parent containers of a listed path are
    implied. Returns the normalized set.

    money_ok: EXACT paths of `fields` allowed to name money ("custom_data.value", "price.amountMicros"); the
    exemption covers that path only (not a child, not a subtree) and is kept in the registry
    (field_money_ok, golden_allowlists). The other locks (bidding_not_supported, budget, financial) still
    apply at check time.

    Raises ValueError for a bad path, a path naming money (names_money) other than a DAILY_BUDGET_KEYS leaf and
    not in money_ok, a money_ok path that is a subtree or not in fields, a too-wide subtree, a second,
    DIFFERENT list (fields or money_ok) for the same (platform, action) (the same list again is a no-op), any
    change after freeze_allowlists() ("registry_frozen"), or replace=True outside tests (_TEST_PERMISSIVE)."""
    if isinstance(fields, str) or isinstance(money_ok, str):
        raise TypeError("fields and money_ok must be iterables of paths, not a str")
    if replace and not _TEST_PERMISSIVE:
        raise ValueError(f"({platform}, {action}): replace=True is for tests only (_TEST_PERMISSIVE)")
    pairs = [(str(f), _norm_path(f)) for f in fields]
    normalized = frozenset(n for _, n in pairs)
    exempt = frozenset(_norm_path(p) for p in money_ok)
    problems = sorted({f"money_ok {p!r}: an exemption names one exact path, not a subtree" for p in exempt
                       if p.endswith(".*")} | {f"money_ok {p!r} is not one of the listed fields" for p in exempt
                                               if p not in normalized})
    problems += sorted({p for raw, n in pairs if (p := _registration_problem(raw, n, n in exempt))})
    if problems:
        raise ValueError(f"({platform}, {action}): " + "; ".join(problems))
    key = (str(platform), str(action).lower())
    entry = _Allowlist(normalized, exempt)
    existing = _FIELD_ALLOWLIST.get(key)
    if existing == entry:
        return existing.fields
    if _FROZEN:
        raise ValueError(f"registry_frozen: ({platform}, {action}) cannot be registered or changed after "
                         "freeze_allowlists()")
    if existing is not None and not replace:
        raise ValueError(f"({platform}, {action}) already has a different field allow-list "
                         f"{sorted(existing.fields)} (money_ok {sorted(existing.money_ok)}); refusing to replace "
                         "it silently")
    _FIELD_ALLOWLIST[key] = entry
    return normalized


def freeze_allowlists() -> None:
    """Close the registry: from now on any new or changed allow-list raises ValueError("registry_frozen...")
    (re-registering the identical list stays a no-op). Call it once every platform module is imported."""
    global _FROZEN
    _FROZEN = True


def allowlists_frozen() -> bool:
    """True once freeze_allowlists() was called."""
    return _FROZEN


def _unfreeze_for_tests() -> None:
    """Tests only: reopen the registry closed by freeze_allowlists()."""
    global _FROZEN
    _FROZEN = False


def field_allowlist(platform: str, action: str) -> frozenset[str] | None:
    """The registered allow-list of (platform, action) (immutable), None when there is none."""
    entry = _FIELD_ALLOWLIST.get((str(platform), str(action).lower()))
    return None if entry is None else entry.fields


def field_money_ok(platform: str, action: str) -> frozenset[str] | None:
    """The exact paths of (platform, action) exempted from the money rule, None when nothing is registered."""
    entry = _FIELD_ALLOWLIST.get((str(platform), str(action).lower()))
    return None if entry is None else entry.money_ok


def golden_allowlists() -> dict[str, dict[str, dict[str, list[str]]]]:
    """Sorted snapshot (a fresh copy) of the registry,
    {platform: {action: {"fields": [normalized paths], "money_ok": [exempted exact paths]}}} (JSON-ready, for
    a golden test: any change of an allow-list or of a money exemption shows up as a diff)."""
    out: dict[str, dict[str, dict[str, list[str]]]] = {}
    for (platform, action), entry in sorted(_FIELD_ALLOWLIST.items()):
        out.setdefault(platform, {})[action] = {"fields": sorted(entry.fields), "money_ok": sorted(entry.money_ok)}
    return out


def _permissive(platform: str, action: str) -> bool:
    flag = _TEST_PERMISSIVE
    if isinstance(flag, bool):
        return flag
    try:
        return action in flag or (platform, action) in flag
    except TypeError:
        return False


def _path_allowed(segs: list[str], raw: list[str], is_container: bool, entries: frozenset[str]) -> bool:
    """Exact path, parent container of an entry, or inside a '.*' subtree (where no segment past the prefix
    may name money: a subtree never opens an unknown amount). segs are normalized, raw the same segments as
    sent (for the word split)."""
    for entry in entries:
        es = entry.split(".")
        if es[-1] == "*":
            base = es[:-1]
            if segs[:len(base)] == base and not names_money(raw[len(base):]):
                return True
            if is_container and len(segs) < len(base) and base[:len(segs)] == segs:
                return True
        elif segs == es or (is_container and len(segs) < len(es) and es[:len(segs)] == segs):
            return True
    return False


def _mask_allowed(raw: list[str], entries: frozenset[str]) -> bool:
    """An updateMask field (raw segments, relative to the updated object) is allowed when it ends an
    allow-listed path, or enters a '.*' subtree (past its prefix, nothing naming money)."""
    segs = [_nk(s) for s in raw]
    if not segs:
        return False
    for entry in entries:
        es = entry.split(".")
        if es[-1] == "*":
            base = es[:-1]
            if any(len(base) >= i and base[len(base) - i:] == segs[:i] and not names_money(raw[i:])
                   for i in range(1, len(segs) + 1)):
                return True
        elif len(segs) <= len(es) and es[len(es) - len(segs):] == segs:
            return True
    return False


def _field_violations(op: WriteOp) -> list[tuple[str, str, str]]:
    action = str(op.action).lower()
    entries = field_allowlist(op.platform, action)
    if entries is None:
        if _permissive(op.platform, action):
            return []
        return [("no_field_allowlist", f"no field allow-list is registered for ({op.platform}, {op.action}), so "
                 "the payload cannot be checked.", "The platform module must call "
                 "write_policy.register_field_allowlist for every action it builds.")]
    refused: list[str] = []
    for obj in _payload(op):
        for parent, key, value in _walk(obj):
            raw = [str(p) for p in parent if not isinstance(p, int)] + [str(key)]
            segs = [_nk(p) for p in raw]
            decoded = _decode(value)
            if not _path_allowed(segs, raw, isinstance(decoded, (dict, list, tuple)), entries):
                refused.append(".".join(raw))
            if _is_mask_key(key):  # a non-string mask is refused by update_mask_not_string
                for field in _mask_tokens(value) or ():
                    if not _mask_allowed([s for s in field.split(".") if s], entries):
                        refused.append(f"{key}:{field}")
    if not refused:
        return []
    names = sorted(set(refused))
    return [("field_not_allowed", f"fields {names[:5]} are not in the allow-list of ({op.platform}, {op.action}).",
             "Remove them from the plan, or (code change) add them to the action's register_field_allowlist.")]


# ------------------------------------------------------------------ overrides and references
def _overrides(*objs: Any) -> list[str]:
    return sorted({k for obj in objs for _, k, _ in _walk(obj) if isinstance(k, str) and _nk(k) in _OVERRIDE_NK})


def _financial_refs(op: WriteOp) -> list[str]:
    """Budget references (any op; body/params keys and updateMask fields) and parent ids (outside a creation;
    body/params keys only: a mask path starts with the updated resource's own name, e.g. campaign.status)."""
    payload = _payload(op)
    found = set()
    for key in _keys(*payload):
        n = _nk(key)
        if n in _BUDGET_NK or _is_unsupported(key) or _is_bidding(key):
            continue  # amounts: the budget locks handle them; bidding fields are refused outright
        if n in _BUDGET_REF_NK or "budget" in n:
            found.add(key)
    if not is_create(op):
        found |= {k for obj in payload for _, k, _ in _walk(obj) if isinstance(k, str) and _nk(k) in _REPARENT_NK}
    return sorted(found)


# ------------------------------------------------------------------ the check
def _switches(ctx: ClientContext, platform: str, policy: WritePolicy) -> list[dict]:
    if not write_enabled():
        return [violation("write_disabled", "Server environment: GEM_WRITE is not 1, so every write is refused.",
                          "Set GEM_WRITE=1 in the MCP server environment, restart it, then plan again.")]
    if not policy.enabled:
        return [violation("write_not_enabled", f"clients.json, client {ctx.client_id!r}: "
                          f"write.{platform}.enabled is false.",
                          f"Set write.{platform}.enabled to true for this client (and account_allow), then "
                          "plan again.")]
    return []


def _op_shape(ctx: ClientContext, platform: str, op: WriteOp, policy: WritePolicy) -> list[dict]:
    out: list[dict] = []
    oid = op.op_id

    def add(code: str, detail: str, fix: str) -> None:
        out.append(violation(code, f"op {oid}: {detail}", fix, oid))

    if op.platform != platform:
        add("platform_mismatch", f"platform {op.platform!r} in a {platform!r} plan.", "One platform per plan.")
    if op.risk not in RISKS:
        add("bad_risk", f"risk {op.risk!r} is not one of {RISKS}.", "Fix the plan builder.")
    if op.method.upper() not in WRITE_METHODS:
        add("bad_method", f"method {op.method!r} is not a write method.", "Reads do not need a plan.")
    if not op.url.startswith("https://"):
        add("bad_url", "URL is not https.", "Fix the plan builder.")
    else:
        for code, detail, fix in _url_problems(op.url):
            add(code, detail, fix)
    if op.rollback not in ("auto", "none"):
        add("bad_rollback", f"rollback {op.rollback!r} is not auto|none.", "Fix the plan builder.")
    account = str(op.account_id)
    allowed = {_norm_account(platform, str(a)) for a in (policy.account_allow or ())}
    if _norm_account(platform, account) not in allowed:
        add("account_not_allowed", f"account {account} is not in write.{platform}.account_allow.",
            f"Add {account} to write.{platform}.account_allow of client {ctx.client_id!r} in clients.json.")
    elif not ctx.owns(platform, account):
        add("account_not_owned", f"account {account} does not belong to client {ctx.client_id!r}.",
            "Use the client that owns this account, or fix its accounts in clients.json.")
    elif not out:
        for code, detail, fix in _binding(platform, op):
            add(code, detail, fix)
    return out


def _op_locks(platform: str, op: WriteOp, policy: WritePolicy,
              baseline: Callable[[WriteOp], int | None] | None) -> list[dict]:
    out: list[dict] = []
    oid = op.op_id

    def add(code: str, detail: str, fix: str) -> None:
        out.append(violation(code, f"op {oid}: {detail}", fix, oid))

    payload = _payload(op)
    bad_masks = _bad_masks(*payload)
    if bad_masks:
        add("update_mask_not_string", f"masks {bad_masks[:5]} are not a plain string of field paths, so the "
            "fields they change cannot be checked.", "Send the mask as one string: \"status,name\" "
            "(comma-separated field paths), never a list, an object or a JSON string.")
    statuses, bad_statuses = _status_scan(*payload)
    if bad_statuses:
        add("status_unparseable", f"status values {bad_statuses[:5]} are not strings.",
            "Send each status as a plain string (PAUSED, ENABLED...).")
    unknown = sorted({s for s in statuses if s not in KNOWN_STATUS_VALUES})
    if unknown:
        add("status_unknown", f"status values {unknown[:5]} are not known "
            f"({sorted(KNOWN_STATUS_VALUES)}).", "Use PAUSED / ENABLED|ACTIVE / REMOVED|DELETED|ARCHIVED.")
    if is_create(op):
        creates = _creates(*payload)
        create_statuses = [_status_scan({k: v for k, v in c.items() if _is_status_key(k)})[0] for c in creates]
        if any(s in RISKY_VALUES for s in statuses):
            add("status_not_paused", "a creation sets an active or removed status.",
                "Create it PAUSED (write_policy.force_paused), then activate with a separate set_status plan.")
        elif (creates and not all(cs and all(s in PAUSED_VALUES for s in cs) for cs in create_statuses)) or (
                not creates and not _top_paused(op)):
            add("status_missing", "a creation has no explicit PAUSED status at the first level of body/params "
                "(on every created entity; a nested PAUSED does not count).",
                "Build the body with write_policy.force_paused.")
        elif _is_meta_copy(op) and not _copy_paused(op):
            add("status_missing", "a Meta copy inherits the source's status unless status_option is PAUSED.",
                "Send status_option=PAUSED (write_policy.force_paused(body, 'meta', copy=True)).")
    elif any(s in ACTIVE_VALUES for s in statuses):
        if not policy.allow_activate:
            add("activation_not_allowed", "activating spends money and allow_activate is false.",
                f"Activate it in the platform's interface, or set write.{platform}.allow_activate to true.")
        if not op.financial:
            add("financial_flag_missing", "an activation must be flagged financial.", "Fix the plan builder.")
    if is_remove(op):
        if not policy.allow_delete:
            add("delete_not_allowed", "deletion/removal and allow_delete is false.",
                f"Pause or archive instead, or set write.{platform}.allow_delete to true.")
        elif platform == "google_ads" and not has_gem_label(op.before):
            add("label_missing", f"Google Ads remove on an entity without the {GEM_LABEL!r} label.",
                f"Only entities labelled {GEM_LABEL!r} (created by this server) can be removed; pause it instead.")
    if op.risk == "irreversible" and not policy.allow_irreversible:
        add("irreversible_not_allowed", "irreversible operation and allow_irreversible is false.",
            f"Set write.{platform}.allow_irreversible to true if you really want this.")
    personal = pii_keys(op)
    if personal and not op.pii:
        add("pii_undeclared", f"fields {personal[:5]} carry personal data but the op is not flagged pii.",
            "Fix the plan builder: set pii=True on every op that sends personal data.")
    if op.pii and not policy.allow_pii:
        add("pii_not_allowed", "sends personal data and allow_pii is false.",
            f"Set write.{platform}.allow_pii to true once consent is documented.")
    unsupported = _unsupported_budget(*payload)
    if unsupported:
        add("budget_kind_unsupported", f"fields {unsupported} (lifetime/total/spend cap) are not supported.",
            "Change daily budgets only, or do it in the platform's interface.")
    bidding = _bidding(*payload)
    if bidding:
        add("bidding_not_supported", f"fields {bidding[:5]} change bids or the bidding strategy, which no cap "
            "bounds.", "Change bids in the platform's interface.")
    overrides = _overrides(*payload)
    if overrides:
        add("method_override_in_payload", f"fields {overrides[:5]} could change the HTTP method or request "
            "the policy checked (op.method, op.url).", "Send one plain request per op: no method / batch / "
            "relative_url field in body or params.")
    refs = _financial_refs(op)
    if refs and not op.financial:
        add("financial_undeclared", f"fields {refs[:5]} reference a budget, a bidding strategy or move the "
            "entity under another parent, which changes what is spent, but the op is not flagged financial.",
            "Fix the plan builder: set financial=True on this op.")
    for code, detail, fix in _field_violations(op):
        add(code, detail, fix)
    out.extend(_budget_locks(platform, op, policy, baseline))
    return out


def _budget_locks(platform: str, op: WriteOp, policy: WritePolicy,
                  baseline: Callable[[WriteOp], int | None] | None) -> list[dict]:
    currency = currency_of(op)
    minor = minor_unit_multiplier(currency)
    values, bad = _budget_scan(*_payload(op), minor=minor or _DEFAULT_MINOR_MULT)
    if not values and not bad:
        return []
    oid = op.op_id
    out: list[dict] = []

    def add(code: str, detail: str, fix: str) -> None:
        out.append(violation(code, f"op {oid}: {detail}", fix, oid))

    if bad:
        add("budget_unparseable", f"budget values {bad[:5]} are not whole numbers.",
            "Send budgets as integers (micros, or minor units for Meta daily_budget).")
    if not op.financial:
        add("financial_flag_missing", "a budget change must be flagged financial.", "Fix the plan builder.")
    cap = policy.max_daily_budget_micros
    if cap is None:
        add("budget_cap_missing", f"write.{platform}.max_daily_budget_micros is not set.",
            f"Set write.{platform}.max_daily_budget_micros (and cap_currency) for this client.")
        return out
    if not policy.cap_currency:
        add("cap_currency_missing", f"write.{platform}.cap_currency is not set.",
            f"Set write.{platform}.cap_currency to the account currency (e.g. EUR).")
    elif currency is None:
        add("currency_unknown", "the account currency is unknown (it is read from the snapshot only).",
            "The handler snapshot must carry the account currency (before.currency).")
    elif currency != policy.cap_currency.upper():
        add("currency_mismatch", f"account currency {currency} differs from cap_currency {policy.cap_currency}.",
            "Set cap_currency to the account currency and review max_daily_budget_micros.")
    if currency is not None and minor is None and _has_minor_units(*_payload(op), op.before):
        add("currency_unit_unknown", f"the minor unit of {currency} (decimals) is not known, so a budget in "
            "minor units cannot be converted to micros.", "Change this budget in the platform's interface, or "
            "add the currency to write_policy.MINOR_UNIT_DECIMALS.")
    if not values:
        return out
    new = max(values)
    if new > cap:
        add("budget_over_cap", f"daily budget {new} micros is over the cap {cap} micros.",
            "Lower the budget, or raise max_daily_budget_micros for this client.")
    if not is_create(op):
        olds, bad_olds = _budget_scan(op.before, minor=minor or _DEFAULT_MINOR_MULT)
        if not olds or bad_olds:
            add("before_unknown", "the current budget could not be read, so the increase cannot be bounded.",
                "Retry; the plan builder must snapshot the current budget.")
        else:
            base = min(olds)
            earlier = baseline(op) if baseline else None
            if earlier is not None:
                base = min(base, earlier)
            limit = base * (1 + float(policy.max_increase_pct) / 100.0)
            if new > limit:
                add("increase_too_large", f"budget {base} -> {new} micros is over +{policy.max_increase_pct}% "
                    "in 24 h.", f"Raise it by at most {policy.max_increase_pct}% (<= {int(limit)} micros), or "
                    "wait 24 h.")
    return out


def _plan_budget(platform: str, ops: Sequence[WriteOp], policy: WritePolicy, limit: int) -> list[dict]:
    """Aggregate cap: sum of every budget of the plan <= cap * max_ops_per_plan (and the optional
    max_plan_budget_micros)."""
    cap = policy.max_daily_budget_micros
    if cap is None:
        return []
    total = 0
    for op in ops:
        try:
            minor = minor_unit_multiplier(currency_of(op)) or _DEFAULT_MINOR_MULT
            total += sum(_budget_scan(*_payload(op), minor=minor)[0])
        except PayloadError:
            continue  # refused per op already
    ceiling = int(cap) * max(limit, 1)
    extra = getattr(policy, "max_plan_budget_micros", None)
    if isinstance(extra, int) and not isinstance(extra, bool) and extra >= 0:
        ceiling = min(ceiling, extra)
    if total > ceiling:
        return [violation("plan_budget_over_cap", f"Plan: budgets total {total} micros, over {ceiling} micros.",
                          f"Split the change, or lower the budgets (write.{platform}.max_daily_budget_micros x "
                          "max_ops_per_plan, or max_plan_budget_micros).")]
    return []


def check_ops(ctx: ClientContext, platform: str, ops: Sequence[WriteOp], policy: WritePolicy, *,
              baseline: Callable[[WriteOp], int | None] | None = None) -> list[dict]:
    """Every policy refusal for these ops (empty list = allowed). Run at plan time and again at apply time
    with fresh `before` snapshots."""
    switches = _switches(ctx, platform, policy)
    if switches:
        return switches
    out: list[dict] = []
    limit = min(int(policy.max_ops_per_plan or 0), HARD_MAX_OPS)
    if not ops:
        out.append(violation("empty_plan", "Plan: no operation.", "Nothing to do."))
    elif len(ops) > limit:
        out.append(violation("too_many_ops", f"Plan: {len(ops)} ops, max_ops_per_plan is {limit}.",
                             "Split the change into several plans, or raise max_ops_per_plan."))
    ids = [op.op_id for op in ops]
    if len(set(ids)) != len(ids):
        out.append(violation("duplicate_op_id", "Plan: two ops share an op_id.", "Fix the plan builder."))
    for op in ops:
        try:
            out.extend(_op_shape(ctx, platform, op, policy))
            out.extend(_op_locks(platform, op, policy, baseline))
        except PayloadError as exc:
            out.append(violation("payload_too_large", f"op {op.op_id}: the payload cannot be inspected ({exc}).",
                                 "Split the op into smaller ones.", op.op_id))
    out.extend(_plan_budget(platform, ops, policy, limit))
    return out

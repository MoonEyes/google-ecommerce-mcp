"""Merchant API v1: product attribute changes through the write path (plan_* -> google_apply).

Write plan (write token, scope content; the read token is never used here):
    merchant_plan_product_patch  PATCH products/v1/accounts/{a}/productInputs/{id}?dataSource=&updateMask=
The update mask is ALWAYS explicit and built from the fields the caller lists: Google deletes an attribute that
is listed in the mask without a value in the body, so a mask field without a value is refused (never sent), and
so is an attribute given without being listed. Supported attributes (top level of productAttributes):
    title, description   low risk
    link, availability   medium (availability: IN_STOCK, OUT_OF_STOCK, PREORDER, BACKORDER); a link leaving the
                         registrable domain of the product's current link (or set when there is none) is HIGH
                         risk, so it needs the out-of-band human approval (checked again at apply)
    price                high and financial: an out-of-band human approval is needed; the price is bounded like a
                         budget by write_policy (write.merchant.max_daily_budget_micros caps the new price,
                         cap_currency must be the product's currency, max_increase_pct caps a raise over 24 h),
                         may not fall below 1/10 of the current price (snapshot, plan and apply), its
                         currency can never change, and the product must already have a price
The data source must be an API data source of the client's account (input "API"; a file, UI or autofeed source
cannot be patched through the API): by default the product's primary data source, or the one named.

No native dry-run: dry_run is local and the diff is computed here, against the snapshot = the processed product
(products.get, read with the write token) and its data source (dataSources.get). execute re-checks everything on
the snapshot taken at apply time and refuses when a listed attribute changed since the plan. Merchant Center
applies a change with up to 30 minutes of delay (the processed product shows the old values meanwhile).

Rollback ("auto" when every listed attribute had a value before): a "product_restore" PATCH of the same mask with
the values of the snapshot, refused when the product no longer shows what the patch wrote (someone else's edit is
never overwritten; right after an apply, wait for Merchant Center to process it). An attribute that had no value
before cannot be restored (that would be a deletion): rollback "none".

Deliberately ABSENT in v1: productInputs:insert (it REPLACES the whole product and moves it between data sources),
product deletion (irreversible), local and regional inventories, custom attributes, sale price, and every other
attribute. Account of an op = the client's Merchant account id (clients.json merchant.account_id), in the path.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Annotated, Any, Optional
from urllib.parse import urlsplit

from pydantic import Field

from . import google_write as gw
from . import merchant_tools as mt
from .write_core import PlatformHandler, WriteOp

PLATFORM = "merchant"
MAPI = mt.MAPI
_A = r"https://merchantapi\.googleapis\.com"
_SEG = r"[A-Za-z0-9_~.-]+"
SNAPSHOT_RULES = [("GET", _A + r"/products/v1/accounts/[0-9]+/products/" + _SEG),
                  ("GET", _A + r"/datasources/v1/accounts/[0-9]+/dataSources/[0-9]+")]
WRITE_RULES = [("PATCH", _A + r"/products/v1/accounts/[0-9]+/productInputs/" + _SEG, None, "write")]
WRITE = gw.google_write_client(PLATFORM, WRITE_RULES, read_rules=SNAPSHOT_RULES)

PATCH = "product_patch"
RESTORE = "product_restore"
# attribute -> risk
ATTRIBUTES: dict[str, str] = {"title": "low", "description": "low", "link": "medium", "availability": "medium",
                              "price": "high"}
AVAILABILITY = ("IN_STOCK", "OUT_OF_STOCK", "PREORDER", "BACKORDER")
_PA = "productAttributes"
_FIELDS = ("name", "dataSource", "updateMask", *(f"{_PA}.{a}" for a in ATTRIBUTES),
           f"{_PA}.price.amountMicros", f"{_PA}.price.currencyCode")
_MONEY_OK = (f"{_PA}.price", f"{_PA}.price.amountMicros", f"{_PA}.price.currencyCode")
ALLOWLISTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {PATCH: (_FIELDS, _MONEY_OK),
                                                                   RESTORE: (_FIELDS, _MONEY_OK)}
TOOLS = ("merchant_plan_product_patch",)
VOLUNTARILY_ABSENT = ("productInputs:insert", "productInputs.delete", "localInventories", "regionalInventories",
                      "customAttributes", "salePrice")
LIMITS = {"title": 150, "description": 5000, "link": 2000}
PRICE_RATIO = 10
LATENCY_NOTE = "Merchant Center applies product changes with up to 30 minutes of delay"
_URL_RX = re.compile(re.escape(MAPI) + r"/products/v1/accounts/([0-9]+)/productInputs/(" + _SEG + ")")
_DS_RX = re.compile(r"accounts/([0-9]+)/dataSources/([0-9]+)")
_RANK = ("low", "medium", "high")

CLIENT_ID = Annotated[Optional[str], Field(description="Client id from clients_list. Required when several clients "
                                                       "are registered; empty uses the only client")]


# ------------------------------------------------------------------ helpers
def _segment_id(segment: str) -> str | None:
    try:
        return mt.product_id(*mt.parse_product_id(segment))
    except ValueError:
        return None


def _parse(url: Any) -> tuple[str, str] | None:
    """(account id, product path segment) of a productInputs URL, else None."""
    match = _URL_RX.fullmatch(url) if isinstance(url, str) else None
    return (match.group(1), match.group(2)) if match else None


def _attr_name(field: Any) -> str:
    name = str(field or "").strip()
    return name[len(_PA) + 1:] if name.startswith(_PA + ".") else name


def _mask_fields(params: Any) -> list[str]:
    mask = (params or {}).get("updateMask") if isinstance(params, dict) else None
    if not isinstance(mask, str):
        return []
    return [t for t in mask.split(",") if t]


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, (dict, list))
                                                                                 and not value)


def _normalize_price(value: Any) -> dict:
    """{"amountMicros": "<digits>", "currencyCode": "EUR"} from {amount_micros|amountMicros, currency_code|
    currencyCode}. Raises ValueError (amounts in micros only: 12.99 EUR = 12990000)."""
    if not isinstance(value, dict):
        raise ValueError("price: an object {amount_micros, currency_code} (12.99 EUR = 12990000 micros)")
    unknown = set(value) - {"amount_micros", "amountMicros", "currency_code", "currencyCode"}
    if unknown:
        raise ValueError(f"price: unknown fields {sorted(unknown)}")
    micros = value.get("amountMicros", value.get("amount_micros"))
    currency = value.get("currencyCode", value.get("currency_code"))
    if isinstance(micros, bool) or not (isinstance(micros, int) or (isinstance(micros, str)
                                                                    and micros.strip().isdigit())):
        raise ValueError("price.amount_micros: a whole number of micros (12.99 EUR = 12990000)")
    micros = int(str(micros).strip())
    if micros <= 0 or micros >= 10 ** 15:
        raise ValueError("price.amount_micros: greater than 0 and below 10^15")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Za-z]{3}", currency.strip()):
        raise ValueError("price.currency_code: a 3-letter ISO code such as EUR")
    return {"amountMicros": str(micros), "currencyCode": currency.strip().upper()}


def _value_problem(name: str, value: Any) -> str | None:
    if name in ("title", "description"):
        if not isinstance(value, str) or not value.strip() or len(value) > LIMITS[name]:
            return f"{name}: a non-empty text of {LIMITS[name]} characters at most"
    elif name == "link":
        if not isinstance(value, str) or len(value) > LIMITS["link"] or re.search(r"\s", value):
            return f"link: an http(s) URL of {LIMITS['link']} characters at most, without spaces"
        try:
            parts = urlsplit(value)
        except ValueError:
            return "link: cannot be parsed"
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            return "link: an http(s) URL with a host and no user information"
    elif name == "availability":
        if value not in AVAILABILITY:
            return f"availability: one of {AVAILABILITY}"
    elif name == "price":
        try:
            if _normalize_price(value) != value:
                return "price: send {amountMicros: '<digits>', currencyCode: 'EUR'}"
        except ValueError as exc:
            return str(exc)
    else:
        return f"{name}: not an attribute this server changes (supported: {sorted(ATTRIBUTES)})"
    return None


def _same(name: str, old: Any, new: Any) -> bool:
    if name == "price" and isinstance(old, dict) and isinstance(new, dict):
        return (str(old.get("amountMicros")) == str(new.get("amountMicros"))
                and str(old.get("currencyCode")) == str(new.get("currencyCode")))
    return old == new


def _risk(fields: list[str], link_off_site: bool = False) -> str:
    risks = [ATTRIBUTES.get(f, "medium") for f in fields] + (["high"] if link_off_site else [])
    return max(risks, key=_RANK.index, default="low")


# Two-level public suffixes common for shops (co.uk, com.au...): the registrable domain is then 3 labels. An
# approximation of the Public Suffix List, on the safe side: an unknown layout compares more labels, never fewer.
_SECOND_LEVEL = frozenset({"co", "com", "net", "org", "gov", "edu", "ac", "ltd", "plc", "ne", "or", "gouv", "asso"})


def registrable_domain(url: Any) -> str | None:
    """The registrable domain of an http(s) URL (shop.acme.co.uk -> acme.co.uk, www.acme.fr -> acme.fr), or None.
    IP literals and single-label hosts are returned whole."""
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".") if isinstance(url, str) else ""
    except ValueError:
        return None
    if not host:
        return None
    labels = host.split(".")
    if len(labels) < 2 or host.replace(".", "").isdigit():
        return host
    keep = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL else 2
    return ".".join(labels[-keep:])


def link_off_site(new_link: Any, current_link: Any) -> bool:
    """True when the new link leaves the registrable domain of the product's current link (or there is none)."""
    current = registrable_domain(current_link)
    return current is None or registrable_domain(new_link) != current


def price_ratio_problem(new: Any, old: Any) -> str | None:
    """Why a new price (micros) is too LOW against the current one: 0, or below 1/PRICE_RATIO of it (a units /
    cents / micros slip), or None. The upper side is the policy's (max_increase_pct over 24 h and the cap)."""
    try:
        new_micros, old_micros = int(str(new.get("amountMicros"))), int(str(old.get("amountMicros")))
    except (AttributeError, TypeError, ValueError):
        return "price: the current or the new amount cannot be read"
    if new_micros <= 0:
        return "price: must be greater than 0"
    if old_micros > 0 and new_micros * PRICE_RATIO < old_micros:
        return (f"price: {new_micros} micros is less than 1/{PRICE_RATIO} of the current {old_micros} micros "
                "(units, cents and micros mixed up?)")
    return None


def _supported(attrs: Any) -> dict:
    """The supported attributes of a processed product (price reduced to amountMicros + currencyCode)."""
    attrs = attrs if isinstance(attrs, dict) else {}
    out: dict[str, Any] = {}
    for name in ATTRIBUTES:
        value = attrs.get(name)
        if _empty(value):
            continue
        if name == "price":
            if not isinstance(value, dict) or "amountMicros" not in value:
                continue
            value = {"amountMicros": str(value.get("amountMicros")), "currencyCode": value.get("currencyCode")}
        out[name] = value
    return out


# ------------------------------------------------------------------ handler
def _read_product(ctx: Any, account: str, segment: str) -> dict:
    product = gw.write_call(WRITE, ctx, "GET", f"{MAPI}/products/v1/accounts/{account}/products/{segment}")
    if "error" in product:
        raise gw.SnapshotError.from_result(product, f"product {_segment_id(segment) or segment}")
    return product


def _snapshot(ctx: Any, op: WriteOp) -> dict:
    """The processed product (supported attributes) and its target data source, read with the write token after
    the binding check. Raises gw.SnapshotError."""
    parts = _parse(op.url)
    if parts is None:
        raise gw.SnapshotError("bad_url", "the op URL is not a Merchant productInputs URL")
    account, segment = parts
    gw.require_bound(ctx, PLATFORM, account, op)  # before any request: never read another client's account
    pid = _segment_id(segment)
    if pid is None:
        raise gw.SnapshotError("bad_url", "the op URL does not hold a valid product id")
    source = str((op.params or {}).get("dataSource") or "")
    ds = _DS_RX.fullmatch(source)
    if ds is None or ds.group(1) != account:
        raise gw.SnapshotError("data_source_not_bound", f"data source {source[:120]!r} is not a data source of "
                               f"account {account}", "Name a data source of the client's own Merchant account.")
    product = _read_product(ctx, account, segment)
    found = gw.write_call(WRITE, ctx, "GET", f"{MAPI}/datasources/v1/{source}")
    if "error" in found:
        raise gw.SnapshotError.from_result(found, f"data source {source}")
    attrs = _supported(product.get("productAttributes"))
    before: dict[str, Any] = {}
    price = attrs.get("price")
    if isinstance(price, dict) and price.get("currencyCode"):
        before["currency"] = str(price["currencyCode"]).upper()
    lang, label, offer = product.get("contentLanguage"), product.get("feedLabel"), product.get("offerId")
    before.update({
        "product": product.get("name"),
        "product_id": mt.product_id(lang, label, offer) if lang and label and offer else None,
        "primary_data_source": product.get("dataSource"),
        "data_source": found.get("name") or source,
        "data_source_input": found.get("input"),
        "data_source_kind": "primary" if "primaryProductDataSource" in found else
        "supplemental" if "supplementalProductDataSource" in found else None,
        _PA: attrs,
    })
    return before


def _problems(ctx: Any, op: WriteOp) -> list[str]:
    """Every module lock on op and its snapshot op.before (the diff is computed against it)."""
    parts = _parse(op.url)
    if parts is None:
        return ["the URL is not a Merchant productInputs URL"]
    account, segment = parts
    problems: list[str] = []
    pid = _segment_id(segment)
    if account != str(op.account_id) or not ctx.owns(PLATFORM, account):
        problems.append(f"Merchant account {account} is not the op's own account of client {ctx.client_id!r}")
    if op.method.upper() != "PATCH" or op.action not in (PATCH, RESTORE):
        problems.append("a product change is a PATCH (product_patch or product_restore)")
    before = op.before if isinstance(op.before, dict) else {}
    if pid is None or before.get("product_id") != pid:
        problems.append("the snapshot is not the product the URL names")
    params = op.params if isinstance(op.params, dict) else {}
    if set(params) != {"dataSource", "updateMask"}:
        problems.append("the request needs exactly the dataSource and updateMask parameters")
    source = str(params.get("dataSource") or "")
    ds = _DS_RX.fullmatch(source)
    if ds is None or ds.group(1) != account or before.get("data_source") != source:
        problems.append(f"data source {source[:120]!r} is not the snapshot's data source of account {account}")
    if before.get("data_source_input") != "API":
        problems.append(f"data source {source[:120]!r} has input {before.get('data_source_input')!r}: only an API "
                        "data source can be patched through the API (change file, UI or autofeed sources in "
                        "Merchant Center)")
    tokens = _mask_fields(params)
    names = [_attr_name(t) for t in tokens]
    if not tokens:
        problems.append("the update mask is empty: list the attributes to change")
    if any(not t.startswith(_PA + ".") for t in tokens) or len(set(names)) != len(names):
        problems.append("every mask entry is productAttributes.<attribute>, each listed once")
    body = op.body if isinstance(op.body, dict) else {}
    if set(body) - {"name", _PA}:
        problems.append(f"the body holds fields outside name and {_PA}")
    if body.get("name") != f"accounts/{account}/productInputs/{segment}":
        problems.append("the body name is not the product the URL names")
    attrs = body.get(_PA) if isinstance(body.get(_PA), dict) else {}
    missing = [n for n in names if _empty(attrs.get(n))]
    if missing:
        problems.append(f"mask fields {missing} have no value in the body: Google would DELETE them; give a value "
                        "or remove them from the mask")
    extra = sorted(set(attrs) - set(names))
    if extra:
        problems.append(f"attributes {extra} are in the body but not in the update mask")
    current = before.get(_PA) if isinstance(before.get(_PA), dict) else {}
    for name in names:
        if name not in ATTRIBUTES:
            problems.append(f"{name}: not an attribute this server changes (supported: {sorted(ATTRIBUTES)})")
            continue
        if _empty(attrs.get(name)):
            continue
        problem = _value_problem(name, attrs.get(name))
        if problem:
            problems.append(problem)
        elif name == "price":
            old = current.get("price")
            if not isinstance(old, dict):
                problems.append("price: the product has no price yet; set the first price in its data source")
            elif str(old.get("currencyCode", "")).upper() != attrs["price"]["currencyCode"]:
                problems.append(f"price: currency {attrs['price']['currencyCode']} differs from the product's "
                                f"{old.get('currencyCode')}; the currency is never changed here")
            else:
                ratio = price_ratio_problem(attrs["price"], old)  # against the snapshot: plan AND apply time
                if ratio:
                    problems.append(ratio)
        elif name == "link" and link_off_site(attrs.get("link"), current.get("link")) and op.risk != "high":
            problems.append(f"link: {registrable_domain(attrs.get('link'))} is not the registrable domain of the "
                            f"product's current link ({registrable_domain(current.get('link'))}): such a change "
                            "is high risk and needs a human approval")
    if op.action == PATCH and names and not problems and all(_same(n, current.get(n), attrs.get(n)) for n in names):
        problems.append("no change: the product already has these values")
    if ("price" in names) != bool(op.financial):
        problems.append("a price change, and only a price change, is flagged financial")
    return problems


def _expected_now(op: WriteOp) -> dict | None:
    """What the listed attributes must show at apply time: the plan-time values (patch: its inverse body) or what
    the change being undone wrote (restore: its inverse body). None when unknown."""
    inverse = op.inverse if isinstance(op.inverse, WriteOp) else None
    body = inverse.body if inverse is not None and isinstance(inverse.body, dict) else None
    attrs = body.get(_PA) if body is not None else None
    return attrs if isinstance(attrs, dict) else None


def _drift(op: WriteOp) -> str | None:
    expected = _expected_now(op)
    if expected is None:
        return "a restore must name the change it undoes" if op.action == RESTORE else None
    current = (op.before or {}).get(_PA) or {}
    changed = sorted(n for n, v in expected.items() if not _same(n, current.get(n), v))
    if not changed:
        return None
    if op.action == RESTORE:
        return (f"attributes {changed} no longer show what the change being undone wrote (edited since, or Merchant "
                "Center has not processed it yet: up to 30 minutes); restoring would overwrite them")
    return f"attributes {changed} changed since the plan: plan again on the current values"


def _diff(op: WriteOp) -> list[dict]:
    current = (op.before or {}).get(_PA) or {}
    attrs = (op.body or {}).get(_PA) or {}
    return [{"field": n, "before": current.get(n), "after": attrs.get(n)} for n in map(_attr_name,
                                                                                        _mask_fields(op.params))]


def _dry_run(ctx: Any, op: WriteOp) -> dict:
    problems = _problems(ctx, op)
    drift = _drift(op)
    if drift:
        problems.append(drift)
    if problems:
        return gw.local_result(False, "; ".join(problems))
    before = op.before or {}
    return gw.local_result(True, {"checked": "mask, values, data source (input API) and diff against the current "
                                             "product (Merchant API has no dry-run)",
                                  "diff": _diff(op), "data_source": before.get("data_source"),
                                  "data_source_kind": before.get("data_source_kind"), "note": LATENCY_NOTE})


def _execute(ctx: Any, op: WriteOp, grant: Any) -> dict:
    problems = _problems(ctx, op)  # op.before is the snapshot taken at apply time
    drift = _drift(op)
    if drift:
        problems.append(drift)
    if problems:
        return {"error": "precondition_failed", "detail": "; ".join(problems),
                "fix": "Check the product with merchant_products, then plan again; nothing was sent."}
    result = gw.write_call(WRITE, ctx, op.method, op.url, params=op.params, json=op.body, grant=grant)
    if "error" in result:
        return result
    out = gw.trim(result, ("name", "offerId", "contentLanguage", "feedLabel"))
    out.update(changed=[_attr_name(t) for t in _mask_fields(op.params)], note=LATENCY_NOTE)
    return out


def _restore_op(op: WriteOp, before: dict | None) -> WriteOp | None:
    """The PATCH writing back, for the same mask, the values `before` shows; None when one of them had no value
    (restoring it would delete it). Its inverse is op, so the restore is refused if the product no longer shows
    what op wrote."""
    if op.action not in (PATCH, RESTORE) or not isinstance(before, dict) or not isinstance(op.body, dict):
        return None
    current = before.get(_PA) if isinstance(before.get(_PA), dict) else {}
    names = [_attr_name(t) for t in _mask_fields(op.params)]
    if not names or any(_empty(current.get(n)) for n in names):
        return None
    attrs = {n: current[n] for n in names}
    return WriteOp(op_id=f"{op.op_id}-rb", platform=PLATFORM, account_id=op.account_id, action=RESTORE,
                   method="PATCH", url=op.url, body={_PA: attrs, "name": op.body.get("name")},
                   params=dict(op.params or {}), risk=op.risk, financial="price" in names,
                   summary=f"Restore {', '.join(names)} of product {before.get('product_id')} as before the change",
                   inverse=dataclasses.replace(op, before=None, inverse=None), rollback="auto")


def _invert(op: WriteOp, before: dict | None) -> WriteOp | None:
    return _restore_op(op, before)


def _entity_key(ctx: Any, op: WriteOp) -> str:
    before = op.before if isinstance(op.before, dict) else {}
    parts = _parse(op.url) or ("?", "?")
    return f"merchant/{parts[0]}/product/{before.get('product_id')}"


HANDLER = PlatformHandler(dry_run=_dry_run, snapshot=_snapshot, execute=_execute, invert=_invert,
                          entity_key=_entity_key)


# ------------------------------------------------------------------ plan tool
def _error(ctx: Any, code: str, detail: str, fix: str) -> dict:
    return {"error": code, "detail": detail, "fix": fix, "client_id": ctx.client_id}


@gw.tool_guard()
def merchant_plan_product_patch(
    product_id: Annotated[str, Field(description="Product id contentLanguage~feedLabel~offerId, as "
                                                 "merchant_products returns it (e.g. en~FR~SKU123)")],
    update_mask: Annotated[list[str], Field(description="Attributes to change, each also given in "
                                                        "product_attributes: title, description, link, "
                                                        "availability, price")],
    product_attributes: Annotated[dict, Field(description="New values, e.g. {\"title\": \"...\", \"availability\": "
                                                          "\"OUT_OF_STOCK\", \"price\": {\"amount_micros\": "
                                                          "12990000, \"currency_code\": \"EUR\"}}")],
    data_source_id: Annotated[str, Field(description="Numeric id of the API data source to patch (empty: the "
                                                     "product's primary data source)")] = "",
    account_id: mt.ACCOUNT_ID = "",
    client_id: CLIENT_ID = None,
) -> dict:
    """PLAN (nothing is written): change some attributes of one product of the client's Merchant Center account,
    with an explicit update mask (an attribute listed without a value is refused: Google would delete it). Title
    and description are low risk, link and availability medium, price high and financial (needs a human
    approval; capped by write.merchant.max_daily_budget_micros, max_increase_pct and cap_currency). Only API data
    sources. Apply with google_apply after the user agreed; changes show after up to 30 minutes; rollback_plan
    restores the previous values. Product insert, delete and inventories are deliberately not offered."""
    ctx = gw.resolve(client_id)
    account = mt.account_of(ctx, account_id)
    blocked = gw.write_precheck(ctx, PLATFORM, account)
    if blocked:
        return blocked
    lang, label, offer = mt.parse_product_id(product_id)
    segment = mt.product_segment(lang, label, offer)
    if isinstance(update_mask, str) or not isinstance(update_mask, (list, tuple)):
        raise ValueError("update_mask: a list of attribute names, e.g. [\"title\"]")
    names = [_attr_name(f) for f in update_mask if str(f or "").strip()]
    if not names:
        return _error(ctx, "update_mask_empty", "update_mask lists no attribute.",
                      "List the attributes to change (title, description, link, availability, price); nothing was "
                      "planned.")
    if len(set(names)) != len(names):
        raise ValueError("update_mask: each attribute once")
    unknown = sorted(set(names) - set(ATTRIBUTES))
    if unknown:
        return _error(ctx, "field_not_supported", f"attributes {unknown} are not changed by this server.",
                      f"Supported: {sorted(ATTRIBUTES)}; change the others in Merchant Center or its data source.")
    if not isinstance(product_attributes, dict):
        raise ValueError("product_attributes: an object of attribute -> new value")
    given = {_attr_name(k): v for k, v in product_attributes.items()}
    missing = [n for n in names if _empty(given.get(n))]
    if missing:
        return _error(ctx, "mask_field_without_value", f"update_mask lists {missing} without a value in "
                      "product_attributes: Google would DELETE these attributes.",
                      "Give a value for each listed attribute, or remove it from update_mask; deleting an "
                      "attribute is not offered.")
    extra = sorted(set(given) - set(names))
    if extra:
        return _error(ctx, "field_not_in_mask", f"product_attributes gives {extra} without listing them in "
                      "update_mask (Google would ignore them).", "List every attribute you change in update_mask.")
    attrs: dict[str, Any] = {}
    for name in names:
        value = given[name]
        if name == "price":
            value = _normalize_price(value)
        elif isinstance(value, str):
            value = value.strip() if name != "description" else value
        problem = _value_problem(name, value)
        if problem:
            raise ValueError(problem)
        attrs[name] = value
    url = f"{MAPI}/products/v1/accounts/{account}/productInputs/{segment}"
    try:
        product = _read_product(ctx, account, segment)
    except gw.SnapshotError as exc:
        return {**exc.as_dict(), "client_id": ctx.client_id}
    if str(data_source_id or "").strip():
        if not str(data_source_id).strip().isdigit():
            raise ValueError("data_source_id: the numeric data source id (merchant_data_sources lists them)")
        source = f"accounts/{account}/dataSources/{str(data_source_id).strip()}"
    else:
        source = str(product.get("dataSource") or "")
    params = {"dataSource": source, "updateMask": ",".join(f"{_PA}.{n}" for n in names)}
    pid = mt.product_id(lang, label, offer)
    current_link = (product.get("productAttributes") or {}).get("link") \
        if isinstance(product.get("productAttributes"), dict) else None
    off_site = "link" in names and link_off_site(attrs["link"], current_link)
    op = WriteOp(op_id=f"{PATCH}-1", platform=PLATFORM, account_id=account, action=PATCH, method="PATCH", url=url,
                 body={_PA: attrs, "name": f"accounts/{account}/productInputs/{segment}"}, params=params,
                 risk=_risk(names, off_site), financial="price" in names,
                 summary=f"Change {', '.join(names)} of product {pid} (account {account})")
    try:
        before = _snapshot(ctx, op)
    except gw.SnapshotError as exc:
        return {**exc.as_dict(), "client_id": ctx.client_id}
    restore = _restore_op(op, before)
    op = dataclasses.replace(op, before=before, inverse=restore, rollback="auto" if restore else "none")
    problems = _problems(ctx, op)
    if problems:
        code = "no_change" if problems == ["no change: the product already has these values"] else "invalid_request"
        return _error(ctx, code, "; ".join(problems), "Nothing was planned; fix the request (merchant_products "
                      "shows the current values).")
    from . import write_core

    out = write_core.create_plan(ctx, PLATFORM, [op], title=f"Merchant: {', '.join(names)} of {pid[:80]}")
    if "error" not in out:
        out["diff"] = _diff(op)
        out["note"] = LATENCY_NOTE
        if not restore:
            out["rollback_note"] = "no rollback: an attribute listed had no value before (restoring would delete it)"
    return out


# ------------------------------------------------------------------ registration
def register_write() -> None:
    """Field allow-lists and the "merchant" PlatformHandler (idempotent: the same lists again are a no-op)."""
    from . import write_core, write_policy

    for action, (fields, money_ok) in ALLOWLISTS.items():
        write_policy.register_field_allowlist(PLATFORM, action, fields, money_ok=money_ok)
    write_core.register_handler(PLATFORM, HANDLER)


def register(mcp: Any) -> list[str]:
    """Register the Merchant plan tool (read-only hint: it only plans)."""
    register_write()
    return gw.register_tools(mcp, [("Plan a Merchant Center product change", merchant_plan_product_patch)],
                             gw.PLAN_ONLY)

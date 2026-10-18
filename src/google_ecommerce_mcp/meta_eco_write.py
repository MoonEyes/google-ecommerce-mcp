"""Meta ecosystem write path (v1): one catalog product item update, through the single plan -> apply circuit.

Tool:
    meta_eco_plan_catalog_item_update   plan POST /{product_item_id} changing price, sale_price (minor units of the
                                        catalog currency, e.g. cents), availability and / or inventory. Nothing is
                                        written: it returns a signed plan applied with meta_apply.

How it is bound and checked:
- account_id of the op = the client's meta.catalog_id (the policy family "meta_eco" owns pixel_id / catalog_id;
  account_allow defaults to them). The snapshot reads GET /{product_item_id}?fields=...,product_catalog{id} and
  returns account_id = the catalog Meta reports for the item: an item of another catalog is refused
  (id_not_owned at plan time, account_not_bound by the policy at apply time).
- before = complete snapshot of the fields this tool can change (price and sale_price parsed to minor units,
  currency, availability, inventory, retailer_id); the inverse op restores them (rollback "auto"). When a
  changed field has no readable previous value (a formatted price that cannot be parsed, no sale price), the op
  has rollback "none" and the plan says so.
- currency always comes from the snapshot, never from the caller. A price change is risk "high" and flagged
  financial (a wrong price is shown in ads and shops): the plan then needs the out-of-band human approval
  (google-ecommerce-mcp approve). Stock / availability only: risk "medium".
- dry_run is LOCAL (mode "local"): Meta has no confirmed validate_only flag on a product item update. It checks
  the fields and values, the currency, and refuses a price more than 10 times above or below the current one
  (cents vs units mistake).
- field allow-list (write_policy.register_field_allowlist, in register() only): price, sale_price (money_ok,
  exact paths), currency, availability, inventory. Write allow-list: POST https://graph.facebook.com/v26.0/<id>
  only, kind "write" (needs the WriteGrant of an applied plan). Token: the client's "meta" secret, Bearer header.

Hypotheses to confirm live (sandbox catalog): the item update accepts a JSON body with the Bearer header (if
Meta required access_token, it would go in the JSON body, never in the URL); ProductItem exposes product_catalog;
the formatted price read back ("12,34 EUR", "$12.34") is parsed with the currency's decimals.

Deliberately absent (P2, not in this version): Conversions API events (PII, irreversible: one invalid event
rejects the batch, sent events cannot be deleted, test_event_code does not isolate), custom audiences,
items_batch (asynchronous, no undo), feeds / uploads (replace mode deletes absent items; update_only=true would
be forced), product set creation, any deletion, Instagram / Page publication, pixel settings, Commerce Order
Management (removed by Meta on 2026-10-27)."""

from __future__ import annotations

import dataclasses
import re
from typing import Annotated, Any, Literal, Optional

from mcp.types import ToolAnnotations
from pydantic import Field

from . import meta_eco, write_core, write_policy
from .ads_common import AdsClient, WriteAllowList, guard
from .meta_eco import BASE, InvalidInput, clean, headers_for, resolve_client, token_for
from .write_core import PlatformHandler, WriteOp

PLATFORM = "meta_eco"
ACTION = "catalog_item_update"
FIELDS = ("price", "sale_price", "currency", "availability", "inventory")
MONEY_FIELDS = ("price", "sale_price")
AVAILABILITY = ("in stock", "out of stock", "preorder", "available for order", "discontinued")
MAX_INVENTORY = 1_000_000_000
MAX_PRICE_MINOR = 10 ** 12
PRICE_RATIO = 10
ITEM_FIELDS = "id,retailer_id,price,sale_price,currency,availability,inventory,product_catalog{id}"
OWNER_FIELDS = "id,product_catalog{id}"

WRITE_ENDPOINTS = [("POST", r"https://graph\.facebook\.com/v26\.0/[0-9]+", None, "write")]
WRITE_ALLOW_LIST = WriteAllowList(WRITE_ENDPOINTS, platform=PLATFORM)
CLIENT = AdsClient(PLATFORM, meta_eco.ALLOW_LIST, write_allow_list=WRITE_ALLOW_LIST)
PLAN_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=True)

Availability = Literal["in stock", "out of stock", "preorder", "available for order", "discontinued"]
_ITEM = re.compile(r"[0-9]{1,25}")
_NUMBER = re.compile(r"[0-9](?:[0-9.,'   ]*[0-9])?")

__all__ = ["WRITE_ENDPOINTS", "WRITE_ALLOW_LIST", "CLIENT", "HANDLER", "TOOLS", "parse_price", "snapshot",
           "dry_run", "execute", "invert", "entity_key", "build_op", "meta_eco_plan_catalog_item_update",
           "register", "register_policy"]


# ------------------------------------------------------------------ price parsing
def parse_price(text: Any, currency: str | None) -> int | None:
    """Meta's formatted price ("12,34 €", "$1,234.50", "1.234,50 EUR", "¥1,200") -> minor units with the decimals
    of currency (EUR 2, JPY 0, KWD 3); None when the currency is unknown or the text is ambiguous."""
    decimals = write_policy.MINOR_UNIT_DECIMALS.get((currency or "").strip().upper())
    if decimals is None or not isinstance(text, str):
        return None
    found = _NUMBER.findall(text)
    if len(found) != 1:
        return None
    number = re.sub(r"['   ]", "", found[0])
    seps = [i for i, ch in enumerate(number) if ch in ".,"]
    if not seps:
        return int(number) * 10 ** decimals
    last = seps[-1]
    tail = number[last + 1:]

    def grouped(head: str, sep: str | None) -> str | None:
        """head with its thousands separators (all `sep`, groups of 3) removed, else None."""
        if sep is None:
            return head if head.isdigit() else None
        groups = head.split(sep)
        if any(ch in ".," for g in groups for ch in g) or not groups[0] or len(groups[0]) > 3 \
                or any(len(g) != 3 for g in groups[1:]):
            return None
        return "".join(groups)

    if decimals and len(tail) == decimals:
        head = number[:last]
        others = {number[i] for i in seps[:-1]}
        if number[last] in others or len(others) > 1:
            return None
        whole = grouped(head, others.pop() if others else None)
        return None if whole is None else int(whole) * 10 ** decimals + int(tail)
    if len(tail) == 3 and decimals != 3:
        sep_chars = {number[i] for i in seps}
        whole = grouped(number, sep_chars.pop()) if len(sep_chars) == 1 else None
        return None if whole is None else int(whole) * 10 ** decimals
    return None


# ------------------------------------------------------------------ snapshot / handler
def _item_url(item_id: str) -> str:
    return f"{BASE}/{item_id}"


def _read_item(ctx: Any, url: str) -> tuple[dict | None, dict | None]:
    """(snapshot, None) or (None, error dict). The snapshot carries account_id = the item's catalog."""
    raw = CLIENT.call("GET", url, headers=headers_for(token_for(ctx)), params={"fields": ITEM_FIELDS})
    if "error" in raw:
        return None, raw
    catalog = raw.get("product_catalog") if isinstance(raw.get("product_catalog"), dict) else {}
    currency = raw.get("currency") if isinstance(raw.get("currency"), str) else None
    currency = currency.strip().upper() if currency and currency.strip() else None
    inventory = raw.get("inventory")
    snap: dict[str, Any] = {
        "product_item_id": str(raw.get("id") or url.rsplit("/", 1)[-1]),
        "retailer_id": clean(raw.get("retailer_id"), 100) if isinstance(raw.get("retailer_id"), str) else None,
        "currency": currency,
        "price": parse_price(raw.get("price"), currency),
        "price_text": clean(raw.get("price"), 40) if isinstance(raw.get("price"), str) else None,
        "sale_price": parse_price(raw.get("sale_price"), currency),
        "sale_price_text": clean(raw.get("sale_price"), 40) if isinstance(raw.get("sale_price"), str) else None,
        "availability": raw.get("availability") if raw.get("availability") in AVAILABILITY else None,
        "inventory": inventory if isinstance(inventory, int) and not isinstance(inventory, bool) else None,
    }
    if isinstance(catalog.get("id"), (str, int)) and str(catalog["id"]).isdigit():
        snap["account_id"] = str(catalog["id"])
    return snap, None


def _item_catalog(ctx: Any, url: str) -> tuple[str | None, dict | None]:
    """(catalog id of the item, None) or (None, error dict): ownership read FIRST, `id,product_catalog{id}` only,
    so nothing else of an item of another catalog is ever read."""
    raw = CLIENT.call("GET", url, headers=headers_for(token_for(ctx)), params={"fields": OWNER_FIELDS})
    if "error" in raw:
        return None, raw
    catalog = raw.get("product_catalog") if isinstance(raw.get("product_catalog"), dict) else {}
    found = catalog.get("id")
    if not isinstance(found, (str, int)) or isinstance(found, bool) or not str(found).isdigit() \
            or str(raw.get("id", "")) != url.rsplit("/", 1)[-1]:
        return None, {"error": "snapshot_invalid", "detail": "the answer carries no product_catalog / id"}
    return str(found), None


def snapshot(ctx: Any, op: WriteOp) -> dict | None:
    """Current state of the product item (None when it cannot be read: the policy then refuses). An item that is
    no longer in the op's catalog gets a bare snapshot without account_id (the policy refuses it as
    account_not_bound, without naming the other catalog) and its price / stock are not read."""
    catalog, error = _item_catalog(ctx, op.url)
    if error is not None:
        return None
    if catalog != str(op.account_id):
        return {"product_item_id": op.url.rsplit("/", 1)[-1], "catalog": "not_this_client"}
    snap, _ = _read_item(ctx, op.url)
    return snap


def _inverse_body(changed: dict, before: dict | None) -> dict | None:
    """Body restoring every changed field from before, or None when one of them has no known value."""
    if not isinstance(before, dict):
        return None
    body: dict[str, Any] = {}
    for key in changed:
        if key == "currency":
            continue
        if before.get(key) is None:
            return None
        body[key] = before[key]
    if any(k in body for k in MONEY_FIELDS):
        if not before.get("currency"):
            return None
        body["currency"] = before["currency"]
    return body or None


def _make_op(op_id: str, account: str, url: str, body: dict, before: dict | None, inverse: WriteOp | None,
             summary: str) -> WriteOp:
    money = any(k in body for k in MONEY_FIELDS)
    return WriteOp(op_id=op_id, platform=PLATFORM, account_id=account, action=ACTION, method="POST", url=url,
                   body=body, params=None, risk="high" if money else "medium", financial=money, pii=False,
                   summary=summary, before=before, inverse=inverse, rollback="auto" if inverse else "none")


def _describe(body: dict, before: dict | None) -> str:
    before = before or {}
    parts = [f"{k} {before.get(k)} -> {body[k]}" for k in FIELDS if k in body and k != "currency"]
    sku = before.get("retailer_id") or before.get("product_item_id")
    return clean(f"catalog item {sku}: " + ", ".join(parts), 200)


def invert(op: WriteOp, before: dict | None) -> WriteOp | None:
    """The op restoring `before` (the snapshot taken at apply) for the fields op changed; None if impossible."""
    body = _inverse_body(op.body or {}, before)
    if body is None:
        return None
    return _make_op(f"{op.op_id}-rb", op.account_id, op.url, body, None, None, _describe(body, op.body))


def entity_key(ctx: Any, op: WriteOp) -> str:
    item = (op.before or {}).get("product_item_id")
    return f"product_item:{item}" if isinstance(item, str) and item else ""


def _value_problem(key: str, value: Any, before: dict | None) -> str | None:
    if key in MONEY_FIELDS:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < MAX_PRICE_MINOR:
            return f"{key} must be a positive integer in minor units"
        old = (before or {}).get(key) or (before or {}).get("price")
        if isinstance(old, int) and old > 0 and not old / PRICE_RATIO <= value <= old * PRICE_RATIO:
            return (f"{key} {value} is more than {PRICE_RATIO} times away from the current {old} (minor units: "
                    "cents, not units?)")
    elif key == "inventory":
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_INVENTORY:
            return f"inventory must be an integer from 0 to {MAX_INVENTORY}"
    elif key == "availability":
        if value not in AVAILABILITY:
            return f"availability must be one of {list(AVAILABILITY)}"
    elif key == "currency":
        if not isinstance(value, str) or value != (before or {}).get("currency"):
            return "currency must be the catalog item's own currency (from the snapshot)"
    else:
        return f"field {key!r} is not supported"
    return None


def dry_run(ctx: Any, op: WriteOp) -> dict:
    """Local validation (no native validate_only for this request): fields, values, currency, price ratio."""
    body = op.body if isinstance(op.body, dict) else None
    if not body or op.params:
        return {"ok": False, "mode": "local", "detail": "the op must carry a JSON body and no params"}
    problems = [p for k, v in body.items() if (p := _value_problem(k, v, op.before))]
    if any(k in body for k in MONEY_FIELDS) and "currency" not in body:
        problems.append("a price change must carry the item's currency")
    if problems:
        return {"ok": False, "mode": "local", "detail": "; ".join(problems)}
    return {"ok": True, "mode": "local", "detail": "fields and values checked locally (Meta has no validate_only "
                                                   "for a product item update)"}


def execute(ctx: Any, op: WriteOp, grant: Any) -> dict:
    """POST the planned body with the WriteGrant of this op (AdsClient checks it), after re-checking every value
    against op.before, the FRESH snapshot taken at apply time: a price that moved in between (or a currency that
    changed) more than PRICE_RATIO times away from the planned one is refused, nothing is sent."""
    body = op.body if isinstance(op.body, dict) else {}
    problems = [p for k, v in body.items() if (p := _value_problem(k, v, op.before))]
    if not body or problems:
        return {"error": "precondition_failed", "platform": PLATFORM,
                "detail": "; ".join(problems) or "the op carries no body",
                "fix": "Read the item with meta_catalog_products and plan again; nothing was sent."}
    result = CLIENT.call("POST", op.url, headers=headers_for(token_for(ctx)), json=op.body, grant=grant)
    if "error" in result:
        return result
    if result.get("success") is False:
        return {"error": "api_error", "platform": PLATFORM, "detail": clean(result),
                "fix": "Meta did not apply the update; read the item with meta_catalog_products"}
    return {"ok": True, "result": clean(result)}


HANDLER = PlatformHandler(dry_run=dry_run, snapshot=snapshot, execute=execute, invert=invert,
                          entity_key=entity_key)


# ------------------------------------------------------------------ plan builder
def build_op(ctx: Any, product_item_id: str, changes: dict) -> WriteOp:
    """The WriteOp of an item update (raises InvalidInput / ClientError / NotConfigured)."""
    item = (product_item_id or "").strip()
    if not _ITEM.fullmatch(item):
        raise InvalidInput("invalid_argument", "product_item_id must be digits (the id from meta_catalog_products)",
                           "call meta_catalog_products and pass the item's id")
    catalog = ctx.account("meta", "catalog_id")
    changes = {k: v for k, v in changes.items() if v is not None}
    if not changes:
        raise InvalidInput("invalid_argument", "nothing to change: give price, sale_price, availability or inventory",
                           "pass at least one new value")
    url = _item_url(item)

    def failed(error: dict) -> InvalidInput:
        return InvalidInput(str(error.get("error", "api_error")),
                            f"reading product item {item} failed: {clean(error.get('detail'), 200)}",
                            str(error.get("fix") or "check the id with meta_catalog_products"))

    owner, error = _item_catalog(ctx, url)  # ownership first: nothing else of a foreign item is read
    if error is not None:
        raise failed(error)
    if owner != catalog:
        raise InvalidInput("entity_not_found_for_client", f"product item {item} is not in this client's catalog.",
                           "use an item listed by meta_catalog_products for this client")
    before, error = _read_item(ctx, url)
    if error is not None:
        raise failed(error)
    assert before is not None
    if before.get("account_id") != catalog:
        raise InvalidInput("entity_not_found_for_client", f"product item {item} is not in this client's catalog.",
                           "use an item listed by meta_catalog_products for this client")
    body: dict[str, Any] = {}
    for key in ("price", "sale_price", "availability", "inventory"):
        if key in changes:
            problem = _value_problem(key, changes[key], before)
            if problem:
                raise InvalidInput("invalid_argument", problem, "fix the value and plan again")
            if changes[key] != before.get(key):
                body[key] = changes[key]
    if not body:
        raise InvalidInput("no_change", f"product item {item} already has these values", "nothing to do")
    if any(k in body for k in MONEY_FIELDS):
        if not before.get("currency"):
            raise InvalidInput("currency_unknown", f"product item {item}: Meta returned no currency",
                               "change the price in Commerce Manager")
        body["currency"] = before["currency"]
    inverse_body = _inverse_body(body, before)
    inverse = (_make_op("op1-rb", catalog, url, inverse_body, None, None, _describe(inverse_body, body))
               if inverse_body else None)
    return _make_op("op1", catalog, url, body, before, inverse, _describe(body, before))


@guard("plan only: nothing was written")
@meta_eco._contained
def meta_eco_plan_catalog_item_update(
    product_item_id: Annotated[str, Field(description="Product item id (the id column of meta_catalog_products)")],
    price: Annotated[Optional[int], Field(description="New price in MINOR units of the item currency (cents: "
                                          "1299 = 12.99 EUR); empty keeps it")] = None,
    sale_price: Annotated[Optional[int], Field(description="New sale price in minor units; empty keeps it")] = None,
    availability: Annotated[Optional[Availability], Field(description="New availability; empty keeps it")] = None,
    inventory: Annotated[Optional[int], Field(description="New stock quantity (0 or more); empty keeps it")] = None,
    client_id: meta_eco._CLIENT_ARG = None,
    title: Annotated[str, Field(description="Short title of the change (shown in the plan)")] = "",
) -> dict:
    """PLAN (writes nothing): update one Meta catalog product item (price, sale price, availability, stock). Reads
    the item first (complete snapshot, rollback restores it), validates locally, and returns a signed plan with
    confirmation_token and confirm; apply it with meta_apply after the user agrees (a price change also needs
    `google-ecommerce-mcp approve`)."""
    ctx = resolve_client(client_id)
    op = build_op(ctx, product_item_id, {"price": price, "sale_price": sale_price, "availability": availability,
                                         "inventory": inventory})
    if op.rollback == "none":
        op = dataclasses.replace(op, summary=clean(op.summary + " (no automatic rollback: a previous value is "
                                                   "unknown)", 240))
    result = write_core.create_plan(ctx, PLATFORM, [op], title=title or op.summary)
    result.setdefault("client_id", ctx.client_id)
    return result


TOOLS = [("Plan a Meta catalog item update", meta_eco_plan_catalog_item_update)]


def register_policy() -> None:
    """Field allow-list and platform handler (idempotent: the same list again is a no-op)."""
    write_policy.register_field_allowlist(PLATFORM, ACTION, FIELDS, money_ok=MONEY_FIELDS)
    write_core.register_handler(PLATFORM, HANDLER)


def register(mcp: Any) -> list[str]:
    """Register the allow-list, the handler and the plan tool (readOnlyHint=True: it writes nothing)."""
    register_policy()
    names = []
    for title, fn in TOOLS:
        mcp.tool(title=title, annotations=PLAN_ONLY)(fn)
        names.append(fn.__name__)
    return names

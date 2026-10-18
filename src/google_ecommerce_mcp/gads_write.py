"""Google Ads writes (opt-in, per client): plan_* tools and the google_ads PlatformHandler of write_core.

Nothing here writes by itself. Each plan_* tool reads the entity (GAQL through the read client of google_ads),
builds WriteOps (one entity per op, account = the client's customer, exact URL
https://googleads.googleapis.com/v26/customers/<id>/<service>:mutate without query) and calls
write_core.create_plan, which checks the policy, dry-runs every op natively (validateOnly: true) and returns the
plan with its confirmation token. Only gads_apply (write_core) executes a plan, op by op, each request carrying the
WriteGrant that write_core signs.

Tools (all readOnlyHint=True: they never change anything on Google Ads):
    gads_plan_pause_entities   campaign / ad_group / ad -> status PAUSED (nothing else)
    gads_plan_budget_change    campaignBudgets amountMicros (daily budget), financial, currency of the account
    gads_plan_add_negatives    negative keywords on a campaign or a shared negative list (texts given by the
                               caller; search terms are never read here)
    gads_plan_create_campaign  one Search campaign, PAUSED, with its own (not shared) daily budget, atomically
No activation tool in v1: an activation only happens through rollback_plan of a pause, under allow_activate.
Never a remove, never a bid or bidding strategy field (write_policy refuses both anyway).

Requests: WRITE_ALLOW_LIST (built by register, never at import) holds two rules per mutate service
(googleAds:mutate, campaigns:mutate, campaignBudgets:mutate), host pinned, version v26, customer id of 10 digits,
no joker. kind="validate": the dry_run_validator requires validateOnly: true (a real bool). kind="write": no
validateOnly key; AdsClient then also requires the signed WriteGrant of the op. Both validators check the body
shape (operation kinds of each service, no remove).

Field allow-lists (write_policy.register_field_allowlist, called by register only): one per action, the exact
body paths below; money_ok lists the exact paths whose names hold "budget" (campaignBudgetOperation,
campaignBudget); amountMicros is a DAILY_BUDGET_KEYS leaf, bounded by the budget locks."""

from __future__ import annotations

import re
import threading
from typing import Annotated, Any, Literal, Optional

from mcp.types import ToolAnnotations
from pydantic import Field

from . import google_ads, write_core, write_policy
from .ads_common import AdsClient, WriteAllowList, guard, platform_dry_run_validator
from .write_core import PlatformHandler, WriteOp

__all__ = ["PLATFORM", "API_VERSION", "BASE", "TOOLS", "HANDLER", "ACTIONS", "FIELD_ALLOWLISTS", "MONEY_OK",
           "register", "build_write_allow_list", "write_client", "status_op", "budget_op", "negatives_op",
           "create_campaign_op", "snapshot", "dry_run", "execute", "invert", "entity_key"]

PLATFORM = "google_ads"
API_VERSION = "v26"  # writes are pinned to this version (GADS_API_VERSION only moves the reads)
BASE = f"https://{google_ads.HOST}/{API_VERSION}"
MAX_ENTITIES = 25
MAX_NEGATIVES = 50
MAX_BUDGET_MICROS = 10 ** 13
PLAN_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=True)
FRESHNESS = "plan only: nothing was changed on Google Ads (apply it with gads_apply)"

_CUSTOMER_RX = re.compile(r"[0-9]{10}")
_ID_RX = re.compile(r"[0-9]{1,19}")
_AD_RX = re.compile(r"([0-9]{1,19})~([0-9]{1,19})")
# customers/<10 digits>/<collection>/<id> (adGroupAds: <ad_group_id>~<ad_id>)
_RESOURCE_RX = re.compile(r"customers/([0-9]{10})/(campaigns|adGroups|adGroupAds|campaignBudgets|sharedSets)/"
                          r"([0-9]{1,19}(?:~[0-9]{1,19})?)")
_KIND_OF = {"campaigns": "campaign", "adGroups": "ad_group", "adGroupAds": "ad", "campaignBudgets": "campaign_budget",
            "sharedSets": "shared_set"}
_COLLECTION = {v: k for k, v in _KIND_OF.items()}
# GAQL resource read by the snapshot of each entity kind (shared sets are not on the reporting allow-list).
_GAQL_RESOURCE = {"campaign": "campaign", "ad_group": "ad_group", "ad": "ad_group_ad",
                  "campaign_budget": "campaign_budget"}
_MUTATE_KEY = {"ad_group": "adGroupOperation", "ad": "adGroupAdOperation"}
_STATUSES = ("ENABLED", "PAUSED")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ​-‏‪-‮⁦-⁩﻿]")

# ------------------------------------------------------------------ actions and their field allow-lists
_STATUS_FIELDS = (
    "operations.update.resourceName", "operations.update.status", "operations.updateMask",
    "mutateOperations.adGroupOperation.update.resourceName", "mutateOperations.adGroupOperation.update.status",
    "mutateOperations.adGroupOperation.updateMask",
    "mutateOperations.adGroupAdOperation.update.resourceName", "mutateOperations.adGroupAdOperation.update.status",
    "mutateOperations.adGroupAdOperation.updateMask",
)
_BUDGET_CREATE = "mutateOperations.campaignBudgetOperation.create"
_CAMPAIGN_CREATE = "mutateOperations.campaignOperation.create"
FIELD_ALLOWLISTS: dict[str, tuple[str, ...]] = {
    "pause": _STATUS_FIELDS,
    "set_status": _STATUS_FIELDS,  # rollback of a pause only (no tool builds it); activation needs allow_activate
    "budget_change": ("operations.update.resourceName", "operations.update.amountMicros", "operations.updateMask"),
    "add_negatives": (
        "mutateOperations.campaignCriterionOperation.create.campaign",
        "mutateOperations.campaignCriterionOperation.create.negative",
        "mutateOperations.campaignCriterionOperation.create.keyword.*",
        "mutateOperations.sharedCriterionOperation.create.sharedSet",
        "mutateOperations.sharedCriterionOperation.create.keyword.*",
    ),
    "create_campaign": (
        f"{_BUDGET_CREATE}.resourceName", f"{_BUDGET_CREATE}.name", f"{_BUDGET_CREATE}.amountMicros",
        f"{_BUDGET_CREATE}.deliveryMethod", f"{_BUDGET_CREATE}.explicitlyShared",
        f"{_CAMPAIGN_CREATE}.resourceName", f"{_CAMPAIGN_CREATE}.name", f"{_CAMPAIGN_CREATE}.status",
        f"{_CAMPAIGN_CREATE}.advertisingChannelType", f"{_CAMPAIGN_CREATE}.campaignBudget",
        f"{_CAMPAIGN_CREATE}.containsEuPoliticalAdvertising", f"{_CAMPAIGN_CREATE}.networkSettings.*",
    ),
}
# Exact paths naming money (a "budget" word) that are allowed: the budget entity created with the campaign and
# the campaign's reference to it. Their amounts stay bounded by the budget locks (cap, currency, financial).
MONEY_OK: dict[str, tuple[str, ...]] = {
    "create_campaign": (
        f"{_BUDGET_CREATE}.resourceName", f"{_BUDGET_CREATE}.name", f"{_BUDGET_CREATE}.amountMicros",
        f"{_BUDGET_CREATE}.deliveryMethod", f"{_BUDGET_CREATE}.explicitlyShared",
        f"{_CAMPAIGN_CREATE}.campaignBudget",
    ),
}
ACTIONS = tuple(FIELD_ALLOWLISTS)


def _register_allowlists() -> None:
    for action, fields in FIELD_ALLOWLISTS.items():
        write_policy.register_field_allowlist(PLATFORM, action, fields, money_ok=MONEY_OK.get(action, ()))


# ------------------------------------------------------------------ write allow-list and client
_MUTATE_SERVICES = ("googleAds", "campaigns", "campaignBudgets")
# googleAds:mutate: the operation kinds a body may hold; campaigns / campaignBudgets: update or create only.
_GOOGLE_ADS_OPERATIONS = frozenset({"campaignOperation", "adGroupOperation", "adGroupAdOperation",
                                    "campaignBudgetOperation", "campaignCriterionOperation",
                                    "sharedCriterionOperation"})
_SERVICE_OPERATION_KEYS = frozenset({"update", "updateMask", "create"})


def _pattern(service: str) -> str:
    return rf"https://googleads\.googleapis\.com/{API_VERSION}/customers/[0-9]{{10}}/{service}:mutate"


def _operation_ok(item: Any, allowed: frozenset) -> bool:
    if not isinstance(item, dict) or not item or not set(item) <= allowed:
        return False
    return "remove" not in item and ("update" in item or "create" in item) and not (
        "update" in item and "create" in item)


def _body_ok(service: str, *, dry_run: bool):
    """Body validator of one mutate service: {operations|mutateOperations: [1..MAX]} (+ validateOnly for a
    dry-run only), known operation kinds, update or create (never remove)."""
    list_key = "mutateOperations" if service == "googleAds" else "operations"
    extra = {"validateOnly"} if dry_run else set()

    def validate(body: Any) -> bool:
        if not isinstance(body, dict) or set(body) - extra != {list_key}:
            return False
        if not dry_run and "validateOnly" in body:
            return False
        items = body[list_key]
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_NEGATIVES + 1:
            return False
        if service != "googleAds":
            return all(_operation_ok(item, _SERVICE_OPERATION_KEYS) for item in items)
        for item in items:
            if not isinstance(item, dict) or len(item) != 1:
                return False
            (kind, operation), = item.items()
            if kind not in _GOOGLE_ADS_OPERATIONS or not _operation_ok(operation, _SERVICE_OPERATION_KEYS):
                return False
        return True

    return validate


def build_write_allow_list() -> WriteAllowList:
    """The Google Ads write allow-list: per service a validate rule (validateOnly: true required) and a write rule
    (no validateOnly; a WriteGrant required by AdsClient). No joker; host and version pinned."""
    rules = []
    for service in _MUTATE_SERVICES:
        rules.append(("POST", _pattern(service),
                      platform_dry_run_validator(PLATFORM, _body_ok(service, dry_run=True)), "validate"))
        rules.append(("POST", _pattern(service), _body_ok(service, dry_run=False), "write"))
    return WriteAllowList(rules, platform=PLATFORM)


_STATE: dict[str, Any] = {}
_LOCK = threading.Lock()


def write_client() -> AdsClient:
    """The AdsClient of the dry-runs and writes (read allow-list of google_ads + WRITE_ALLOW_LIST), built on
    first use (register() builds it; never at import). Its session is replaceable in tests."""
    with _LOCK:
        client = _STATE.get("client")
        if client is None:
            client = AdsClient(PLATFORM, google_ads.ALLOW_LIST, secrets=google_ads._secrets,
                               write_allow_list=build_write_allow_list())
            _STATE["client"] = client
        return client


# ------------------------------------------------------------------ small helpers
class PlanInputError(ValueError):
    """Bad tool input, returned as {"error", "detail", "fix"}; nothing was read nor planned."""

    def __init__(self, code: str, detail: str, fix: str) -> None:
        super().__init__(detail)
        self.code, self.detail, self.fix = code, detail, fix

    def as_dict(self) -> dict:
        return {"error": self.code, "platform": PLATFORM, "detail": self.detail, "fix": self.fix}


def _err(code: str, detail: str, fix: str, target: Any = None) -> dict:
    out = {"error": code, "platform": PLATFORM, "detail": detail, "fix": fix}
    return target.stamp(out) if target is not None else out


def _mutate_url(customer: str, service: str) -> str:
    return f"{BASE}/customers/{customer}/{service}:mutate"


def _resource(customer: str, kind: str, entity_id: str) -> str:
    return f"customers/{customer}/{_COLLECTION[kind]}/{entity_id}"


def _parse_resource(name: Any) -> tuple[str, str, str] | None:
    """(customer, kind, id) of a canonical resource name, else None."""
    match = _RESOURCE_RX.fullmatch(name) if isinstance(name, str) else None
    return (match.group(1), _KIND_OF[match.group(2)], match.group(3)) if match else None


def _entity_id(kind: str, value: Any) -> str:
    text = str(value if value is not None else "").strip()
    rx = _AD_RX if kind == "ad" else _ID_RX
    if not rx.fullmatch(text):
        shape = "<ad_group_id>~<ad_id> (digits)" if kind == "ad" else "digits"
        raise PlanInputError("invalid_id", f"{kind} id must be {shape}, got {text[:40]!r}",
                             "use the ids returned by gads_performance or gads_query")
    return text


def _clean_text(value: Any, name: str, limit: int) -> str:
    text = " ".join(str(value if value is not None else "").split())
    if not text or len(text) > limit or _CONTROL.search(text):
        raise PlanInputError("invalid_text", f"{name}: 1 to {limit} printable characters, got {text[:40]!r}",
                             f"pass a plain {name} without control characters")
    return text


def _status_of(body: Any) -> str | None:
    """The status set by a status-update body (operations or mutateOperations), else None."""
    for update in _updates(body):
        status = update.get("status")
        if isinstance(status, str):
            return status
    return None


def _updates(body: Any) -> list[dict]:
    if not isinstance(body, dict):
        return []
    out = []
    for item in body.get("operations") or []:
        if isinstance(item, dict) and isinstance(item.get("update"), dict):
            out.append(item["update"])
    for item in body.get("mutateOperations") or []:
        if isinstance(item, dict):
            for operation in item.values():
                if isinstance(operation, dict) and isinstance(operation.get("update"), dict):
                    out.append(operation["update"])
    return out


def _target_resource(op: WriteOp) -> str | None:
    """Canonical resource name the op updates (status or budget ops), else None."""
    names = [u.get("resourceName") for u in _updates(op.body)]
    return names[0] if len(names) == 1 and isinstance(names[0], str) else None


def _first_create(op: WriteOp, kind: str) -> dict | None:
    for item in (op.body or {}).get("mutateOperations") or []:
        if isinstance(item, dict) and isinstance(item.get(kind), dict) and isinstance(item[kind].get("create"), dict):
            return item[kind]["create"]
    return None


# ------------------------------------------------------------------ op builders
def status_op(customer: str, kind: str, entity_id: str, status: str, *, op_id: str, before: dict | None = None,
              inverse: WriteOp | None = None, action: str | None = None) -> WriteOp:
    """One status update: campaigns:mutate for a campaign, googleAds:mutate (adGroupOperation /
    adGroupAdOperation) for an ad group or an ad. PAUSED = action "pause" (low risk); ENABLED = "set_status",
    financial (it can spend), used by rollbacks only."""
    if status not in _STATUSES or kind not in ("campaign", "ad_group", "ad"):
        raise ValueError(f"status_op: {kind} -> {status} is not supported")
    name = _resource(customer, kind, entity_id)
    update = {"update": {"resourceName": name, "status": status}, "updateMask": "status"}
    if kind == "campaign":
        url, body = _mutate_url(customer, "campaigns"), {"operations": [update]}
    else:
        url, body = _mutate_url(customer, "googleAds"), {"mutateOperations": [{_MUTATE_KEY[kind]: update}]}
    pausing = status == "PAUSED"
    label = kind.replace("_", " ")
    return WriteOp(op_id=op_id, platform=PLATFORM, account_id=customer, action=action or ("pause" if pausing
                                                                                       else "set_status"),
                   method="POST", url=url, body=body, risk="low" if pausing else "medium",
                   financial=not pausing, summary=f"{'pause' if pausing else 're-enable'} {label} {entity_id}",
                   before=before, inverse=inverse, rollback="auto" if inverse is not None else "none")


def budget_op(customer: str, budget_id: str, amount_micros: int, *, op_id: str, before: dict | None = None,
              inverse: WriteOp | None = None) -> WriteOp:
    """campaignBudgets:mutate update of amountMicros (int64 sent as a string), financial."""
    name = _resource(customer, "campaign_budget", budget_id)
    old = (before or {}).get("amount_micros")
    risk = "high" if isinstance(old, int) and amount_micros > old else "medium"
    currency = (before or {}).get("currency") or "account currency"
    body = {"operations": [{"update": {"resourceName": name, "amountMicros": str(int(amount_micros))},
                            "updateMask": "amount_micros"}]}
    return WriteOp(op_id=op_id, platform=PLATFORM, account_id=customer, action="budget_change", method="POST",
                   url=_mutate_url(customer, "campaignBudgets"), body=body, risk=risk, financial=True,
                   summary=f"set daily budget {budget_id} to {amount_micros / 1_000_000:g} {currency}",
                   before=before, inverse=inverse, rollback="auto" if inverse is not None else "none")


def negatives_op(customer: str, keywords: list[str], match_type: str, *, op_id: str, campaign_id: str = "",
                 shared_set_id: str = "", before: dict | None = None) -> WriteOp:
    """One googleAds:mutate creating negative keywords on a campaign (campaignCriterionOperation, negative: true)
    or in a shared negative keyword list (sharedCriterionOperation). Atomic: all or none."""
    if bool(campaign_id) == bool(shared_set_id):
        raise ValueError("negatives_op: exactly one of campaign_id / shared_set_id")
    items = []
    for text in keywords:
        keyword = {"text": text, "matchType": match_type}
        if campaign_id:
            items.append({"campaignCriterionOperation": {"create": {
                "campaign": _resource(customer, "campaign", campaign_id), "negative": True, "keyword": keyword}}})
        else:
            items.append({"sharedCriterionOperation": {"create": {
                "sharedSet": _resource(customer, "shared_set", shared_set_id), "keyword": keyword}}})
    where = f"campaign {campaign_id}" if campaign_id else f"shared list {shared_set_id}"
    return WriteOp(op_id=op_id, platform=PLATFORM, account_id=customer, action="add_negatives", method="POST",
                   url=_mutate_url(customer, "googleAds"), body={"mutateOperations": items}, risk="low",
                   financial=False, summary=f"add {len(items)} negative keyword(s) ({match_type}) to {where}",
                   before=before, rollback="none")


def create_campaign_op(customer: str, name: str, amount_micros: int, *, op_id: str,
                       before: dict | None = None) -> WriteOp:
    """One atomic googleAds:mutate: a dedicated daily budget (explicitlyShared false, temporary id -1) and a
    Search campaign PAUSED using it (temporary id -2), Google Search only, no bidding field (Google applies its
    default), not EU political advertising. financial (it creates a budget)."""
    budget_name = f"customers/{customer}/campaignBudgets/-1"
    budget = {"resourceName": budget_name, "name": f"{name} - budget", "amountMicros": str(int(amount_micros)),
              "deliveryMethod": "STANDARD", "explicitlyShared": False}
    campaign = write_policy.force_paused({
        "resourceName": f"customers/{customer}/campaigns/-2", "name": name, "status": "PAUSED",
        "advertisingChannelType": "SEARCH", "campaignBudget": budget_name,
        "containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
        "networkSettings": {"targetGoogleSearch": True, "targetSearchNetwork": False,
                            "targetContentNetwork": False, "targetPartnerSearchNetwork": False}}, PLATFORM)
    body = {"mutateOperations": [{"campaignBudgetOperation": {"create": budget}},
                                 {"campaignOperation": {"create": campaign}}]}
    currency = (before or {}).get("currency") or "account currency"
    return WriteOp(op_id=op_id, platform=PLATFORM, account_id=customer, action="create_campaign", method="POST",
                   url=_mutate_url(customer, "googleAds"), body=body, risk="medium", financial=True,
                   summary=f"create Search campaign {name!r} PAUSED with a daily budget of "
                           f"{amount_micros / 1_000_000:g} {currency}", before=before, rollback="none")


# ------------------------------------------------------------------ reads (snapshots)
class SnapshotError(RuntimeError):
    """The current state could not be read (network, auth...): write_core refuses the op (snapshot_failed)."""


def _quote(resource_name: str) -> str:
    if not _RESOURCE_RX.fullmatch(resource_name):
        raise SnapshotError("not a canonical resource name")
    return f"'{resource_name}'"


def _query(target: google_ads.Target, query: str) -> list[dict]:
    resp = google_ads.run_gaql(target, query)
    if "error" in resp:
        raise SnapshotError(f"{resp.get('error')}: {str(resp.get('detail'))[:200]}")
    return resp["results"]


def _currency(row: dict) -> str | None:
    customer = row.get("customer") if isinstance(row.get("customer"), dict) else {}
    value = customer.get("currencyCode")
    return value if isinstance(value, str) and value else None


def _account_state(target: google_ads.Target) -> dict | None:
    rows = _query(target, "SELECT customer.id, customer.currency_code FROM customer LIMIT 1")
    if not rows:
        return None
    return {"account_id": target.customer, "currency": _currency(rows[0]), "resourceName":
            f"customers/{target.customer}", "entity": "customer"}


def read_entity(target: google_ads.Target, kind: str, resource_name: str) -> dict | None:
    """Minimal current state of one entity: {account_id, currency, status, resourceName, entity} (+
    amount_micros, explicitly_shared, reference_count for a budget; campaign_budget for a campaign). None when
    Google returns no row (unknown or removed id). Raises SnapshotError when the read fails."""
    resource = _GAQL_RESOURCE[kind]
    fields = [f"{resource}.resource_name", f"{resource}.status"]
    if kind == "campaign_budget":
        fields += [f"{resource}.amount_micros", f"{resource}.explicitly_shared", f"{resource}.reference_count"]
    if kind == "campaign":
        fields += [f"{resource}.campaign_budget", f"{resource}.advertising_channel_type"]
    query = (f"SELECT {', '.join(fields)}, customer.currency_code FROM {resource} "
             f"WHERE {resource}.resource_name = {_quote(resource_name)} LIMIT 1")
    rows = _query(target, query)
    if not rows:
        return None
    row = rows[0]
    camel = {"campaign": "campaign", "ad_group": "adGroup", "ad": "adGroupAd", "campaign_budget": "campaignBudget"}
    entity = row.get(camel[kind]) if isinstance(row.get(camel[kind]), dict) else {}
    if entity.get("resourceName") not in (None, resource_name):
        raise SnapshotError("Google returned another entity than the one asked")
    state: dict[str, Any] = {"account_id": target.customer, "currency": _currency(row),
                             "status": entity.get("status"), "resourceName": resource_name, "entity": kind}
    if kind == "campaign_budget":
        amount = entity.get("amountMicros")
        state["amount_micros"] = int(amount) if isinstance(amount, (str, int)) and str(amount).isdigit() else None
        # Same value under the body's field name, so the plan diff and the approval preview show "now: ..."
        state["amountMicros"] = state["amount_micros"]
        state["explicitly_shared"] = bool(entity.get("explicitlyShared", False))
        count = entity.get("referenceCount")
        state["reference_count"] = int(count) if str(count).isdigit() else None
    if kind == "campaign":
        budget = entity.get("campaignBudget")
        parsed = _parse_resource(budget)
        state["campaign_budget"] = budget if parsed and parsed[0] == target.customer else None
        state["channel"] = entity.get("advertisingChannelType")
    return state


def _target_of(ctx: Any, op: WriteOp) -> google_ads.Target:
    customer = str(op.account_id)
    if not _CUSTOMER_RX.fullmatch(customer) or not op.url.startswith(f"{BASE}/customers/{customer}/"):
        raise SnapshotError("the op's account and URL disagree")
    if not ctx.owns(PLATFORM, customer):
        raise SnapshotError("the account does not belong to this client")
    return google_ads.target_for(ctx, customer)


def snapshot(ctx: Any, op: WriteOp) -> dict | None:
    """PlatformHandler.snapshot: the entity the op updates (status / budget) or, for a creation, the account
    context (account_id, currency) plus the parent it attaches to. Raises SnapshotError on a failed read."""
    target = _target_of(ctx, op)
    name = _target_resource(op)
    parsed = _parse_resource(name)
    if parsed is not None and op.action in ("pause", "set_status", "budget_change"):
        if parsed[0] != target.customer:
            raise SnapshotError("the entity belongs to another account")
        return read_entity(target, parsed[1], name)  # type: ignore[arg-type]
    if op.action not in ("add_negatives", "create_campaign"):
        raise SnapshotError(f"no snapshot for action {op.action!r}")
    state = _account_state(target)
    if state is None:
        return None
    if op.action == "add_negatives":
        create = _first_create(op, "campaignCriterionOperation") or _first_create(op, "sharedCriterionOperation") \
            or {}
        parent = create.get("campaign") or create.get("sharedSet")
        parent_parsed = _parse_resource(parent)
        if parent_parsed is None or parent_parsed[0] != target.customer:
            raise SnapshotError("the negatives target no entity of this account")
        if parent_parsed[1] == "campaign":
            campaign = read_entity(target, "campaign", parent)
            if campaign is None:
                return None
            state["status"] = campaign.get("status")
        state["resourceName"] = parent
    return state


# ------------------------------------------------------------------ handler
def _messages(resp: dict) -> list[str]:
    """Up to 3 short messages of a Google Ads error answer (errors[].message), else the error code."""
    detail = resp.get("detail")
    found: list[str] = []
    if isinstance(detail, dict):
        for item in detail.get("details") or []:
            for error in (item.get("errors") or []) if isinstance(item, dict) else []:
                if isinstance(error, dict) and error.get("message"):
                    code = error.get("errorCode")
                    prefix = ""
                    if isinstance(code, dict) and code:
                        prefix = f"{next(iter(code.values()))}: "
                    found.append(f"{prefix}{error['message']}"[:200])
        if not found and detail.get("message"):
            found.append(str(detail["message"])[:200])
    return found[:3] or [str(resp.get("error"))]


def dry_run(ctx: Any, op: WriteOp) -> dict:
    """PlatformHandler.dry_run: the op's request with validateOnly: true (native; Google validates without
    executing and returns only errors)."""
    try:
        target = _target_of(ctx, op)
    except SnapshotError as exc:
        return {"ok": False, "mode": "native", "detail": str(exc)}
    body = {**(op.body or {}), "validateOnly": True}
    resp = write_client().call("POST", op.url, headers=google_ads.headers_for(target), json=body)
    if "error" in resp:
        return {"ok": False, "mode": "native", "detail": {"error": resp.get("error"), "messages": _messages(resp),
                                                          "reason": resp.get("reason")}}
    return {"ok": True, "mode": "native", "detail": "validateOnly: accepted by Google Ads (nothing executed)"}


def _resource_names(value: Any, out: list[str]) -> list[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "resourceName" and isinstance(item, str):
                out.append(item)
            else:
                _resource_names(item, out)
    elif isinstance(value, list):
        for item in value:
            _resource_names(item, out)
    return out


def execute(ctx: Any, op: WriteOp, grant: Any) -> dict:
    """PlatformHandler.execute: the exact request of the plan (no validateOnly) with its WriteGrant."""
    try:
        target = _target_of(ctx, op)
    except SnapshotError as exc:
        return {"error": "blocked", "detail": str(exc), "fix": "plan again for the client's own account"}
    resp = write_client().call("POST", op.url, headers=google_ads.headers_for(target), json=op.body, grant=grant)
    if "error" in resp:
        return {k: resp.get(k) for k in ("error", "reason", "detail", "fix", "http_status") if k in resp}
    return {"status": "ok", "resource_names": _resource_names(resp, [])[:MAX_NEGATIVES + 2]}


def invert(op: WriteOp, before: dict | None) -> WriteOp | None:
    """PlatformHandler.invert: the op restoring `before` (the snapshot taken at apply): the previous status of a
    paused / re-enabled entity, the previous daily budget. None for creations (the created campaign is PAUSED;
    remove it in the interface) and when there is nothing to restore."""
    before = before if isinstance(before, dict) else op.before
    if not isinstance(before, dict):
        return None
    name = _target_resource(op)
    parsed = _parse_resource(name)
    if parsed is None or parsed[0] != str(op.account_id):
        return None
    customer, kind, entity_id = parsed
    if op.action in ("pause", "set_status"):
        old, new = before.get("status"), _status_of(op.body)
        if old not in _STATUSES or old == new or kind not in ("campaign", "ad_group", "ad"):
            return None
        return status_op(customer, kind, entity_id, old, op_id=f"{op.op_id}-rb")
    if op.action == "budget_change":
        old = before.get("amount_micros")
        if not isinstance(old, int) or isinstance(old, bool) or old <= 0:
            return None
        return budget_op(customer, entity_id, old, op_id=f"{op.op_id}-rb")
    return None


def entity_key(ctx: Any, op: WriteOp) -> str | None:
    """PlatformHandler.entity_key: the resourceName of the trusted `before` snapshot (never the body)."""
    name = (op.before or {}).get("resourceName") if isinstance(op.before, dict) else None
    return name if isinstance(name, str) and name else None


HANDLER = PlatformHandler(dry_run=dry_run, snapshot=snapshot, execute=execute, invert=invert, entity_key=entity_key)


# ------------------------------------------------------------------ plan tools
CLIENT_ID = google_ads.CLIENT_ID
CUSTOMER_ID = Annotated[str, Field(description="Google Ads customer id (10 digits, dashes allowed); empty uses the "
                                               "client's own customer")]


def _resolve(client_id: str | None, customer_id: str) -> google_ads.Target:
    target = google_ads.resolve_target(client_id, customer_id)
    if not target.customer or not _CUSTOMER_RX.fullmatch(target.customer):
        raise PlanInputError("invalid_customer_id", "a Google Ads customer id has 10 digits",
                             "use the id shown by gads_accounts")
    return target


def _precheck(target: google_ads.Target) -> dict | None:
    """The switches, before any read: GEM_WRITE, write.google_ads.enabled, the customer in account_allow."""
    ctx = target.ctx
    policy = ctx.policy(PLATFORM)
    switches = [v for v in write_policy.check_ops(ctx, PLATFORM, (), policy) if v["error"] != "empty_plan"]
    if switches:
        return target.stamp({**switches[0], "platform": PLATFORM})
    allowed = {str(a).replace("-", "") for a in policy.account_allow or ()}
    if target.customer not in allowed:
        return _err("account_not_allowed", f"customer {target.customer} is not in write.{PLATFORM}.account_allow "
                    f"of client {ctx.client_id!r}.", "add it with `google-ecommerce-mcp client enable-write "
                    f"{ctx.client_id} google_ads`, or plan for an allowed customer", target)
    return None


def _plan(target: google_ads.Target, ops: list[WriteOp], title: str, notes: list[str] | None = None) -> dict:
    result = write_core.create_plan(target.ctx, PLATFORM, ops, title=title)
    if notes:
        result["notes"] = notes
    return target.stamp(result)


def _tool(fn):
    """Errors of the plan tools as data: PlanInputError, plus google_ads._contained (InvalidInput, ClientError,
    internal_error) and guard (not_configured, fetched_at / freshness)."""
    def inner(*args: Any, **kwargs: Any) -> dict:
        try:
            return fn(*args, **kwargs)
        except PlanInputError as exc:
            return exc.as_dict()
        except SnapshotError as exc:
            return {"error": "read_failed", "platform": PLATFORM, "detail": str(exc)[:300],
                    "fix": "check the account access with gads_accounts / gads_query, then plan again"}

    inner.__name__, inner.__doc__ = fn.__name__, fn.__doc__
    inner.__annotations__ = fn.__annotations__
    inner.__wrapped__ = fn  # type: ignore[attr-defined]
    return guard(FRESHNESS)(google_ads._contained(inner))


def _gads_plan_pause_entities(level: str, ids: list[str], customer_id: str = "",
                              client_id: Optional[str] = None) -> dict:
    """Plan pausing Google Ads campaigns, ad groups or ads (status PAUSED, nothing else). Each entity is read
    first (current status kept for the rollback) and dry-run by Google (validateOnly). Ad ids are
    "<ad_group_id>~<ad_id>". Returns the plan (diff, risks, confirmation_token, confirm): show it to the user,
    then gads_apply. A paused entity stops spending; rollback_plan re-enables it only if allow_activate is set."""
    if level not in ("campaign", "ad_group", "ad"):
        raise PlanInputError("invalid_level", f"level must be campaign, ad_group or ad, got {level!r}",
                             "use campaign, ad_group or ad")
    if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_ENTITIES:
        raise PlanInputError("invalid_ids", f"ids: 1 to {MAX_ENTITIES} ids", "split the change into several plans")
    wanted = list(dict.fromkeys(_entity_id(level, i) for i in ids))
    target = _resolve(client_id, customer_id)
    refused = _precheck(target)
    if refused:
        return refused
    ops, notes = [], []
    for number, entity_id in enumerate(wanted, start=1):
        name = _resource(target.customer, level, entity_id)
        state = read_entity(target, level, name)
        if state is None:
            return _err("not_found", f"{level} {entity_id} was not found in customer {target.customer}.",
                        "check the id with gads_performance or gads_query", target)
        if state.get("status") == "PAUSED":
            notes.append(f"{level} {entity_id} is already PAUSED: left out")
            continue
        if state.get("status") not in _STATUSES:
            return _err("not_pausable", f"{level} {entity_id} has status {state.get('status')!r}.",
                        "only ENABLED entities can be paused", target)
        inverse = status_op(target.customer, level, entity_id, "ENABLED", op_id=f"op{number}-rb")
        ops.append(status_op(target.customer, level, entity_id, "PAUSED", op_id=f"op{number}", before=state,
                             inverse=inverse))
    if not ops:
        return target.stamp({"status": "nothing_to_do", "platform": PLATFORM, "notes": notes})
    return _plan(target, ops, f"Pause {len(ops)} {level.replace('_', ' ')}(s) in {target.customer}", notes)


def _gads_plan_budget_change(daily_budget_micros: int, budget_id: str = "", campaign_id: str = "",
                             customer_id: str = "", client_id: Optional[str] = None) -> dict:
    """Plan a new daily amount for one Google Ads campaign budget (amountMicros, in micros of the ACCOUNT
    currency: 12.50 EUR = 12500000). Give budget_id, or campaign_id to change the budget that campaign uses. The
    current budget and currency are read first; the policy caps it (max_daily_budget_micros in cap_currency, +20 %
    per 24 h at most) and a human approval is required before gads_apply (financial). A shared budget changes
    every campaign using it (shown in the plan)."""
    if isinstance(daily_budget_micros, bool) or not isinstance(daily_budget_micros, int) \
            or not 0 < daily_budget_micros <= MAX_BUDGET_MICROS:
        raise PlanInputError("invalid_amount", "daily_budget_micros must be a positive whole number of micros",
                             "12.50 in the account currency = 12500000")
    if bool(str(budget_id or "").strip()) == bool(str(campaign_id or "").strip()):
        raise PlanInputError("invalid_ids", "give exactly one of budget_id or campaign_id",
                             "budget_id from gads_query (campaign_budget.id), or the campaign_id")
    target = _resolve(client_id, customer_id)
    refused = _precheck(target)
    if refused:
        return refused
    if str(campaign_id or "").strip():
        cid = _entity_id("campaign", campaign_id)
        campaign = read_entity(target, "campaign", _resource(target.customer, "campaign", cid))
        parsed = _parse_resource((campaign or {}).get("campaign_budget"))
        if campaign is None or parsed is None:
            return _err("not_found", f"campaign {cid} or its budget was not found in customer {target.customer}.",
                        "check the id with gads_performance", target)
        bid = parsed[2]
    else:
        bid = _entity_id("campaign_budget", budget_id)
    state = read_entity(target, "campaign_budget", _resource(target.customer, "campaign_budget", bid))
    if state is None or not isinstance(state.get("amount_micros"), int):
        return _err("not_found", f"campaign budget {bid} was not found in customer {target.customer}.",
                    "check the id with gads_query (campaign_budget.id)", target)
    if state["amount_micros"] == daily_budget_micros:
        return target.stamp({"status": "nothing_to_do", "platform": PLATFORM,
                             "notes": [f"budget {bid} is already {daily_budget_micros} micros"]})
    notes = []
    if state.get("explicitly_shared") or (state.get("reference_count") or 0) > 1:
        notes.append(f"budget {bid} is shared by {state.get('reference_count') or 'several'} campaigns: all of "
                     "them change")
    inverse = budget_op(target.customer, bid, state["amount_micros"], op_id="op1-rb")
    op = budget_op(target.customer, bid, daily_budget_micros, op_id="op1", before=state, inverse=inverse)
    return _plan(target, [op], f"Daily budget {bid}: {state['amount_micros']} -> {daily_budget_micros} micros "
                 f"({state.get('currency') or 'currency unknown'})", notes)


def _keywords(values: Any) -> list[str]:
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_NEGATIVES:
        raise PlanInputError("invalid_keywords", f"keywords: 1 to {MAX_NEGATIVES} texts",
                             "split the list into several plans")
    out = []
    for value in values:
        text = _clean_text(value, "keyword", 80)
        if len(text.split()) > 10:
            raise PlanInputError("invalid_keywords", f"keyword {text[:40]!r} has more than 10 words",
                                 "Google Ads keywords hold at most 10 words")
        if text.lower() not in {k.lower() for k in out}:
            out.append(text)
    return out


def _gads_plan_add_negatives(keywords: list[str], match_type: str = "EXACT", campaign_id: str = "",
                             shared_set_id: str = "", customer_id: str = "", client_id: Optional[str] = None) -> dict:
    """Plan adding negative keywords (1 to 50 texts given by you, match type EXACT, PHRASE or BROAD) to one
    campaign (campaign_id) or to one shared negative keyword list (shared_set_id), in one atomic request,
    dry-run by Google first. Negatives only stop ads from showing (no spend increase). Rollback is manual (remove
    them in the interface)."""
    if match_type not in ("EXACT", "PHRASE", "BROAD"):
        raise PlanInputError("invalid_match_type", f"match_type must be EXACT, PHRASE or BROAD, got {match_type!r}",
                             "use EXACT, PHRASE or BROAD")
    texts = _keywords(keywords)
    if bool(str(campaign_id or "").strip()) == bool(str(shared_set_id or "").strip()):
        raise PlanInputError("invalid_ids", "give exactly one of campaign_id or shared_set_id",
                             "campaign_id from gads_performance, or the id of a shared negative keyword list")
    campaign = _entity_id("campaign", campaign_id) if str(campaign_id or "").strip() else ""
    shared = _entity_id("shared_set", shared_set_id) if str(shared_set_id or "").strip() else ""
    target = _resolve(client_id, customer_id)
    refused = _precheck(target)
    if refused:
        return refused
    if campaign and read_entity(target, "campaign", _resource(target.customer, "campaign", campaign)) is None:
        return _err("not_found", f"campaign {campaign} was not found in customer {target.customer}.",
                    "check the id with gads_performance", target)
    op = negatives_op(target.customer, texts, match_type, op_id="op1", campaign_id=campaign, shared_set_id=shared)
    where = f"campaign {campaign}" if campaign else f"shared list {shared}"
    return _plan(target, [op], f"Add {len(texts)} negative keyword(s) to {where}")


def _gads_plan_create_campaign(name: str, daily_budget_micros: int, customer_id: str = "",
                               client_id: Optional[str] = None) -> dict:
    """Plan creating one Google Ads Search campaign, PAUSED, with its own daily budget (amountMicros in micros
    of the account currency; not shared), Google Search network only, in one atomic request dry-run by Google.
    No ad group, keyword, ad nor bidding setting is created (none is allowed here). It stays PAUSED: activate it
    in the Google Ads interface once it is complete. Financial: human approval before gads_apply."""
    title = _clean_text(name, "name", 200)
    if isinstance(daily_budget_micros, bool) or not isinstance(daily_budget_micros, int) \
            or not 0 < daily_budget_micros <= MAX_BUDGET_MICROS:
        raise PlanInputError("invalid_amount", "daily_budget_micros must be a positive whole number of micros",
                             "12.50 in the account currency = 12500000")
    target = _resolve(client_id, customer_id)
    refused = _precheck(target)
    if refused:
        return refused
    state = _account_state(target)
    if state is None:
        return _err("not_found", f"customer {target.customer} could not be read.", "check gads_accounts", target)
    op = create_campaign_op(target.customer, title, daily_budget_micros, op_id="op1", before=state)
    return _plan(target, [op], f"Create Search campaign {title!r} (PAUSED)",
                 ["created PAUSED: nothing spends until someone enables it in Google Ads",
                  "rollback is manual: remove the campaign in the interface"])


def _annotated(fn, annotations: dict) -> Any:
    fn.__annotations__ = {**annotations, "return": dict}
    return fn


_LEVEL = Annotated[Literal["campaign", "ad_group", "ad"], Field(description="What to pause")]
_IDS = Annotated[list[str], Field(description="Ids to pause (1 to 25): campaign ids, ad group ids, or "
                                              "'<ad_group_id>~<ad_id>' for ads")]
_AMOUNT = Annotated[int, Field(description="New daily budget in micros of the account currency (12.50 = "
                                           "12500000)", gt=0)]
_BUDGET_ID = Annotated[str, Field(description="Campaign budget id (campaign_budget.id); or give campaign_id")]
_CAMPAIGN_ID = Annotated[str, Field(description="Campaign id")]
_SHARED_ID = Annotated[str, Field(description="Shared negative keyword list id (shared_set.id); or give "
                                              "campaign_id")]
_KEYWORDS = Annotated[list[str], Field(description="Negative keyword texts (1 to 50, at most 10 words each)")]
_MATCH = Annotated[Literal["EXACT", "PHRASE", "BROAD"], Field(description="Match type of every negative")]
_NAME = Annotated[str, Field(description="Campaign name (1 to 200 characters)")]

gads_plan_pause_entities = _tool(_annotated(_gads_plan_pause_entities, {
    "level": _LEVEL, "ids": _IDS, "customer_id": CUSTOMER_ID, "client_id": CLIENT_ID}))
gads_plan_pause_entities.__name__ = "gads_plan_pause_entities"
gads_plan_budget_change = _tool(_annotated(_gads_plan_budget_change, {
    "daily_budget_micros": _AMOUNT, "budget_id": _BUDGET_ID, "campaign_id": _CAMPAIGN_ID,
    "customer_id": CUSTOMER_ID, "client_id": CLIENT_ID}))
gads_plan_budget_change.__name__ = "gads_plan_budget_change"
gads_plan_add_negatives = _tool(_annotated(_gads_plan_add_negatives, {
    "keywords": _KEYWORDS, "match_type": _MATCH, "campaign_id": _CAMPAIGN_ID, "shared_set_id": _SHARED_ID,
    "customer_id": CUSTOMER_ID, "client_id": CLIENT_ID}))
gads_plan_add_negatives.__name__ = "gads_plan_add_negatives"
gads_plan_create_campaign = _tool(_annotated(_gads_plan_create_campaign, {
    "name": _NAME, "daily_budget_micros": _AMOUNT, "customer_id": CUSTOMER_ID, "client_id": CLIENT_ID}))
gads_plan_create_campaign.__name__ = "gads_plan_create_campaign"

TOOLS = [
    ("Plan: pause Google Ads entities", gads_plan_pause_entities),
    ("Plan: change a Google Ads daily budget", gads_plan_budget_change),
    ("Plan: add Google Ads negative keywords", gads_plan_add_negatives),
    ("Plan: create a paused Google Ads Search campaign", gads_plan_create_campaign),
]


def register(mcp: Any) -> list[str]:
    """Register the field allow-lists (write_policy), the write client, the google_ads handler (write_core) and
    the four plan tools (readOnlyHint=True); returns the tool names. Called once by registry.register_all;
    calling it again re-registers identical lists (no-op)."""
    _register_allowlists()
    write_client()
    write_core.register_handler(PLATFORM, HANDLER)
    names = []
    for title, fn in TOOLS:
        mcp.tool(name=fn.__name__, title=title, annotations=PLAN_ONLY)(fn)
        names.append(fn.__name__)
    return names

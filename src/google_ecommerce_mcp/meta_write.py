"""Meta Ads writes (Marketing API core) through the single write path: plan -> dry-run -> apply.

Plan tools (readOnlyHint=True: they read and dry-run, never write) build write_core.WriteOp lists and call
write_core.create_plan; meta_apply (write_core) applies them. P0: meta_plan_pause (status PAUSED),
meta_plan_budget_change (daily_budget in MINOR units of the account currency, financial). P1:
meta_plan_create_campaign / _adset / _ad (status PAUSED at the first level of the body, special_ad_categories
explicit). Not in v1: activation, spend_cap, lifetime budgets, bids, deletion, copies.

Requests (WRITE_CLIENT, host graph.facebook.com and API version v26.0 pinned, no joker):
  read      GET /v26.0/<id> and GET /v26.0/act_<id> (snapshots: ownership first with id,account_id, then
            status, daily_budget, currency)
  validate  POST /v26.0/act_<id>/(campaigns|adsets|ads) only, with execution_options=["validate_only"] in the
            JSON body (ads_common.platform_dry_run_validator("meta")): native dry-run of CREATIONS
  write     POST /v26.0/act_<id>/(campaigns|adsets|ads) and POST /v26.0/<id>, only with the WriteGrant of an
            applied plan (body digest checked)
Updates (pause, budget, restore: POST /<id>) have NO validate rule and are dry-run LOCALLY (nothing is sent at
plan time): validate_only is not documented on updates, and a Graph that ignored it would write without grant.
URLs never carry a query string; params are empty; the token travels ONLY in "Authorization: Bearer".

UNVERIFIED (to test live, PLAN_FULLCONTROL section 5 point 5): Graph accepts the Bearer header on these POSTs
and a JSON body (execution_options as a JSON list). The planned fallback "access_token in the JSON body" is NOT
wired: write_core signs the body of the plan, so a token added at send time would break the grant digest (and
would land in the journal digest); it needs a write_core change, decided in lot D.

Policy (write_policy, checked at plan AND at apply): field allow-list per action (registered in register(),
never at import), creations PAUSED, account binding (URL act_<id>, else the snapshot's account_id; an entity of
another account is refused as entity_not_found_for_client before its state is read), daily
budget caps in micros converted from the account currency's minor unit (JPY 0 decimals, EUR 2, KWD 3),
+max_increase_pct over 24 h, human approval for financial ops. The snapshot of a creation also reads the
parents named in the body (campaign_id, adset_id, creative.creative_id): a parent of another account makes the
plan refused (account_not_bound)."""

from __future__ import annotations

import dataclasses
import re
from typing import Annotated, Any, Literal, Optional

from pydantic import Field

from . import clients, write_core, write_policy
from .ads_common import AdsClient, AllowList, WriteAllowList, platform_dry_run_validator
from .clients import ClientError
from .meta_ads import GRAPH, minor_to_units
from .write_core import PlatformHandler, WriteOp

__all__ = ["PLATFORM", "WRITE_RULES", "SNAPSHOT_ENDPOINTS", "WRITE_CLIENT", "WRITE_ALLOW_LIST", "FIELD_ALLOWLISTS",
           "MONEY_OK", "HANDLER", "TOOLS", "register", "snapshot", "dry_run", "execute", "invert", "entity_key"]

PLATFORM = "meta"
# Graph API version pinned (as meta_eco): the field lists and the validate_only semantics below are those of
# v26.0; META_API_VERSION is not read by the write path.
API_VERSION = "v26.0"
_V = r"https://graph\.facebook\.com/v26\.0"
_COLLECTION = _V + r"/act_[0-9]+/(campaigns|adsets|ads)"
_NODE = _V + r"/[0-9]+"
_NODE_RX = re.compile(_NODE)
# A write body never carries the dry-run flag nor a token (the Bearer header does).
_FORBIDDEN_WRITE_KEYS = frozenset({"execution_options", "access_token", "appsecret_proof"})


def _plain_write_body(body: Any) -> bool:
    return isinstance(body, dict) and not (set(body) & _FORBIDDEN_WRITE_KEYS)


# validate_only is documented for creations only: an update (POST /<id>) has NO validate rule, so no update can
# ever leave without the WriteGrant of an applied plan (a Graph that ignored the flag would otherwise write at
# plan time). Updates are dry-run locally (dry_run below).
WRITE_RULES = [
    ("POST", _COLLECTION, platform_dry_run_validator("meta"), "validate"),
    ("POST", _COLLECTION, _plain_write_body, "write"),
    ("POST", _NODE, _plain_write_body, "write"),
]
SNAPSHOT_ENDPOINTS = [("GET", _NODE), ("GET", _V + r"/act_[0-9]+")]
WRITE_ALLOW_LIST = WriteAllowList(WRITE_RULES, platform=PLATFORM)
WRITE_CLIENT = AdsClient(PLATFORM, AllowList(SNAPSHOT_ENDPOINTS), write_allow_list=WRITE_ALLOW_LIST)

LEVELS = ("campaign", "adset", "ad")
Level = Literal["campaign", "adset", "ad"]
BudgetLevel = Literal["campaign", "adset"]
_SNAPSHOT_FIELDS = {
    "campaign": "id,account_id,status,effective_status,daily_budget,lifetime_budget,objective",
    "adset": "id,account_id,campaign_id,status,effective_status,daily_budget,lifetime_budget",
    "ad": "id,account_id,campaign_id,adset_id,status,effective_status",
}
_PARENT_KEYS = (("campaign_id", "campaign"), ("adset_id", "adset"))

# Field allow-list per action (write_policy.register_field_allowlist, in register() only).
FIELD_ALLOWLISTS: dict[str, tuple[str, ...]] = {
    "pause_campaign": ("status",), "pause_adset": ("status",), "pause_ad": ("status",),
    # rollback of a pause: puts back the status read before (ACTIVE = activation: allow_activate + financial)
    "restore_status_campaign": ("status",), "restore_status_adset": ("status",), "restore_status_ad": ("status",),
    "budget_campaign": ("daily_budget",), "budget_adset": ("daily_budget",),
    "create_campaign": ("name", "objective", "status", "special_ad_categories", "special_ad_category_country",
                        "daily_budget", "is_adset_budget_sharing_enabled"),
    "create_adset": ("name", "campaign_id", "status", "daily_budget", "billing_event", "optimization_goal",
                     "start_time", "targeting.geo_locations.countries", "targeting.age_min", "targeting.age_max",
                     "targeting.targeting_automation.advantage_audience", "promoted_object.pixel_id",
                     "promoted_object.custom_event_type"),
    "create_ad": ("name", "adset_id", "status", "creative.creative_id"),
}
# Exact paths naming money that are not a daily budget: a boolean (budget sharing between ad sets).
MONEY_OK: dict[str, tuple[str, ...]] = {"create_campaign": ("is_adset_budget_sharing_enabled",)}

Objective = Literal["OUTCOME_SALES", "OUTCOME_TRAFFIC", "OUTCOME_LEADS", "OUTCOME_ENGAGEMENT",
                    "OUTCOME_AWARENESS", "OUTCOME_APP_PROMOTION"]
SpecialAdCategory = Literal["NONE", "EMPLOYMENT", "HOUSING", "FINANCIAL_PRODUCTS_SERVICES",
                            "ISSUES_ELECTIONS_POLITICS"]
BillingEvent = Literal["IMPRESSIONS", "LINK_CLICKS", "THRUPLAY"]
OptimizationGoal = Literal["OFFSITE_CONVERSIONS", "LINK_CLICKS", "LANDING_PAGE_VIEWS", "IMPRESSIONS", "REACH",
                           "VALUE", "THRUPLAY", "POST_ENGAGEMENT", "LEAD_GENERATION"]
CustomEventType = Literal["", "PURCHASE", "ADD_TO_CART", "INITIATED_CHECKOUT", "ADD_PAYMENT_INFO",
                          "COMPLETE_REGISTRATION", "LEAD", "CONTENT_VIEW", "SEARCH"]
_GOALS = frozenset(OptimizationGoal.__args__)  # type: ignore[attr-defined]
_BILLING = frozenset(BillingEvent.__args__)  # type: ignore[attr-defined]
_ID = re.compile(r"[0-9]{1,25}")
_COUNTRY = re.compile(r"[A-Z]{2}")
_ISO_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(:[0-9]{2})?([+-][0-9]{2}:?[0-9]{2}|Z)")
_NEEDS_BEFORE = frozenset({"before_unknown", "currency_unknown", "label_missing", "account_not_bound"})
MAX_ENTITIES = 25
MAX_NAME = 400


class PlanInputError(ValueError):
    """A plan argument refused before anything is read or planned."""

    def __init__(self, code: str, detail: str, fix: str) -> None:
        super().__init__(detail)
        self.code, self.detail, self.fix = code, detail, fix


def _err(code: str, detail: str, fix: str, client_id: str | None = None, **extra: Any) -> dict:
    out = {"error": code, "platform": PLATFORM, "detail": detail, "fix": fix, **extra}
    if client_id:
        out["client_id"] = client_id
    return out


# ------------------------------------------------------------------ requests
def _base() -> str:
    return f"{GRAPH}/{API_VERSION}"


def _headers(ctx: Any) -> dict:
    """Authorization: Bearer <the client's own Meta token>; raises PlanInputError when none is stored."""
    token = ctx.secret("meta")
    if not token:
        raise PlanInputError("not_configured", f"client {ctx.client_id!r}: no Meta access token (secret 'meta').",
                             f"google-ecommerce-mcp client set-secret {ctx.client_id} meta (a token with "
                             "ads_management)")
    return {"Authorization": f"Bearer {token}"}


def _digits(value: Any) -> str | None:
    text = str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    if text.lower().startswith("act_"):
        text = text[4:]
    return text if _ID.fullmatch(text) else None


def _minor_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit() and value.strip().isascii():
        return int(value.strip())
    return None


def _read(ctx: Any, url: str, fields: str) -> dict:
    """One GET of the snapshot list (never cached: the state must be fresh)."""
    return WRITE_CLIENT.call("GET", url, headers=_headers(ctx), params={"fields": fields})


def _level_of(action: str) -> str | None:
    for level in ("adset", "ad", "campaign"):  # "adset" before "ad"
        if action.endswith("_" + level):
            return level
    return None


def _account_state(ctx: Any, account: str) -> dict:
    """{"account_id", "currency"} of act_<account>, or the error dict."""
    info = _read(ctx, f"{_base()}/act_{account}", "account_id,currency")
    if "error" in info:
        return info
    return {"account_id": _digits(info.get("account_id")) or account, "currency": info.get("currency")}


def read_entity(ctx: Any, level: str, entity_id: str, account: str) -> dict:
    """Trusted state of a campaign / ad set / ad: {"id", "level", "account_id", "status", "effective_status",
    "daily_budget"?, "lifetime_budget"?, "campaign_id"?, "adset_id"?, "currency"?}; or an error dict.

    Ownership first: only `id,account_id` is read, and an entity of another ad account (an agency token may
    see several) gets the generic entity_not_found_for_client error: no status, budget nor owner account id of
    the other client is ever read into the answer."""
    probe = _read(ctx, f"{_base()}/{entity_id}", "id,account_id")
    if "error" in probe:
        return probe
    owner = _digits(probe.get("account_id"))
    if owner is None or str(probe.get("id", "")) != entity_id:
        return _err("snapshot_invalid", f"Meta {level} {entity_id}: the answer carries no account_id / id.",
                    f"check that {entity_id} is a {level} id (meta_campaigns / meta_adsets / meta_ads_list)")
    if owner != account:
        return _not_for_client(level, entity_id)
    raw = _read(ctx, f"{_base()}/{entity_id}", _SNAPSHOT_FIELDS[level])
    if "error" in raw:
        return raw
    if _digits(raw.get("account_id")) != account or str(raw.get("id", "")) != entity_id:
        return _not_for_client(level, entity_id)
    state: dict[str, Any] = {"id": entity_id, "level": level, "account_id": owner,
                             "status": raw.get("status"), "effective_status": raw.get("effective_status")}
    for key in ("campaign_id", "adset_id"):
        if raw.get(key) is not None:
            state[key] = str(raw[key])
    for key in ("daily_budget", "lifetime_budget"):
        amount = _minor_int(raw.get(key))
        if amount:  # Meta answers "0" or nothing when the budget is set at the other level
            state[key] = amount
    info = _account_state(ctx, account)
    if "error" in info:
        return info
    state["currency"] = info.get("currency")
    return state


def _not_for_client(level: str, entity_id: str) -> dict:
    """Generic refusal of an entity outside the client's ad account (nothing about its owner)."""
    return _err("entity_not_found_for_client", f"Meta {level} {entity_id} is not in this client's ad account.",
                "take the id from meta_campaigns / meta_adsets / meta_ads_list of this client")


def _parents(op: WriteOp) -> list[tuple[str, str]]:
    body = op.body if isinstance(op.body, dict) else {}
    found = [(level, str(body[key])) for key, level in _PARENT_KEYS if body.get(key) is not None]
    creative = body.get("creative")
    if isinstance(creative, dict) and creative.get("creative_id") is not None:
        found.append(("creative", str(creative["creative_id"])))
    return found


def snapshot(ctx: Any, op: WriteOp) -> dict | None:
    """PlatformHandler.snapshot: the entity's state (pause / budget / restore), or, for a creation, the account
    context {"account_id", "currency", "level": "account", "parents": [{"level", "id", "account_id"}]}. None
    when Meta cannot be read (write_core then refuses: before_unknown)."""
    account = _digits(op.account_id)
    if account is None:
        return None
    if write_policy.is_create(op):
        state = _account_state(ctx, account)
        if "error" in state:
            return None
        parents = []
        for level, parent_id in _parents(op):
            if _digits(parent_id) is None:
                return None
            raw = _read(ctx, f"{_base()}/{parent_id}", "id,account_id")
            owner = _digits(raw.get("account_id")) if "error" not in raw else None
            if owner is None:
                return None
            parents.append({"level": level, "id": parent_id, "account_id": owner})
        return {**state, "level": "account", "parents": parents}
    level = _level_of(op.action)
    entity = op.url.rstrip("/").rsplit("/", 1)[-1]
    if level is None or _digits(entity) is None or entity.startswith("act_"):
        return None
    state = read_entity(ctx, level, entity, account)
    return None if "error" in state else state


def _local_dry_run(op: WriteOp) -> dict:
    """Dry-run of an update (pause, budget, restore): LOCAL, nothing is sent. validate_only is not documented
    on POST /<id>, and a Graph that ignored it would apply the change at plan time without any WriteGrant."""
    problems: list[str] = []
    level = _level_of(op.action)
    before = op.before if isinstance(op.before, dict) else {}
    entity = str(op.url).rsplit("/", 1)[-1]
    body = op.body if isinstance(op.body, dict) else None
    if op.method.upper() != "POST" or not _NODE_RX.fullmatch(str(op.url)):
        problems.append("an update is POST https://graph.facebook.com/v26.0/<numeric id>")
    if body is None or not _plain_write_body(body) or op.params:
        problems.append("the op must carry a plain JSON body (no execution_options, no token) and no params")
    if level is None or before.get("level") != level or before.get("id") != entity:
        problems.append("the snapshot does not name the entity of the URL")
    if body is not None and op.action.startswith(("pause_", "restore_status_")):
        if set(body) != {"status"} or body.get("status") not in ("ACTIVE", "PAUSED"):
            problems.append("a status change sets only status to PAUSED (or ACTIVE for a restore)")
        if before.get("status") not in ("ACTIVE", "PAUSED"):
            problems.append(f"the entity has status {before.get('status')!r} (only ACTIVE / PAUSED change here)")
    elif body is not None and op.action.startswith("budget_"):
        if set(body) != {"daily_budget"} or not _minor_int(body.get("daily_budget")):
            problems.append("a budget change sets only a positive daily_budget in minor units")
        if before.get("lifetime_budget") or not before.get("daily_budget"):
            problems.append("the entity has no daily budget to change")
    elif body is not None:
        problems.append(f"unknown Meta update action {op.action!r}")
    if problems:
        return {"ok": False, "mode": "local", "detail": "; ".join(problems)}
    return {"ok": True, "mode": "local", "detail": {"checked": "URL, fields, values and the fresh snapshot "
                                                    "locally (validate_only is not used on updates)"}}


def dry_run(ctx: Any, op: WriteOp) -> dict:
    """PlatformHandler.dry_run. Creations (POST act_<id>/campaigns|adsets|ads): the same POST with
    execution_options=["validate_only"] (native, documented for creations; nothing runs). Updates (POST /<id>):
    local checks only, nothing is sent (_local_dry_run)."""
    if not write_policy.is_create(op):
        return _local_dry_run(op)
    body = {**(op.body or {}), "execution_options": ["validate_only"]}
    try:
        headers = _headers(ctx)
    except PlanInputError as exc:
        return {"ok": False, "mode": "native", "detail": exc.detail}
    result = WRITE_CLIENT.call(op.method, op.url, headers=headers, json=body, params=op.params)
    if "error" in result:
        return {"ok": False, "mode": "native", "detail": result}
    if result.get("success") is False:
        return {"ok": False, "mode": "native", "detail": {"success": False}}
    return {"ok": True, "mode": "native", "detail": {"validate_only": True, "success": result.get("success", True)}}


def execute(ctx: Any, op: WriteOp, grant: Any) -> dict:
    """PlatformHandler.execute: the exact request of the plan, with its WriteGrant (AdsClient checks it)."""
    try:
        headers = _headers(ctx)
    except PlanInputError as exc:
        return {"error": exc.code, "detail": exc.detail, "fix": exc.fix}
    result = WRITE_CLIENT.call(op.method, op.url, headers=headers, json=op.body, params=op.params, grant=grant)
    if "error" in result:
        return result
    out: dict[str, Any] = {"ok": True}
    if result.get("id") is not None:
        out["id"] = str(result["id"])
    if "success" in result:
        out["success"] = result["success"]
    return out


def _status_op(op: WriteOp, level: str, status: str) -> WriteOp:
    activates = status == "ACTIVE"
    return WriteOp(op_id=f"{op.op_id}-undo", platform=PLATFORM, account_id=op.account_id,
                   action=f"restore_status_{level}", method="POST", url=op.url, body={"status": status},
                   params=None, risk="high" if activates else "medium", financial=activates,
                   summary=f"Restore the status of {level} {op.url.rsplit('/', 1)[-1]} to {status}"
                           + (" (re-activates spending)" if activates else ""), rollback="none")


def invert(op: WriteOp, before: dict | None) -> WriteOp | None:
    """PlatformHandler.invert: the op putting back what `before` (snapshot taken at apply) held. Creations:
    None (the entity was created PAUSED; archive it in Ads Manager if unwanted)."""
    level = _level_of(op.action)
    if level is None or not isinstance(before, dict) or write_policy.is_create(op):
        return None
    if op.action.startswith(("pause_", "restore_status_")):
        status = before.get("status")
        return _status_op(op, level, status) if status in ("ACTIVE", "PAUSED") else None
    if op.action.startswith("budget_"):
        old = _minor_int(before.get("daily_budget"))
        if not old:
            return None
        return WriteOp(op_id=f"{op.op_id}-undo", platform=PLATFORM, account_id=op.account_id, action=op.action,
                       method="POST", url=op.url, body={"daily_budget": old}, params=None, risk="high",
                       financial=True, summary=f"Restore the daily budget of {level} "
                       f"{op.url.rsplit('/', 1)[-1]} to {old} (minor units)", rollback="none")
    return None


def entity_key(ctx: Any, op: WriteOp) -> str:
    """PlatformHandler.entity_key: "<level>:<id>" from the trusted snapshot (never from the body)."""
    before = op.before if isinstance(op.before, dict) else {}
    level, entity = before.get("level"), before.get("id")
    if level in LEVELS and isinstance(entity, str) and _ID.fullmatch(entity):
        return f"{level}:{entity}"
    return ""


HANDLER = PlatformHandler(dry_run=dry_run, snapshot=snapshot, execute=execute, invert=invert,
                          entity_key=entity_key)


# ------------------------------------------------------------------ plan builders
def _client(client_id: str | None) -> tuple[Any, str, str]:
    """(ctx, account digits, base URL); raises ClientError / PlanInputError."""
    ctx = clients.resolve(client_id)
    raw = ctx.account("meta", "ad_account_id")
    account = _digits(raw)
    if account is None:
        raise PlanInputError("invalid_account", f"client {ctx.client_id!r}: meta.ad_account_id {raw!r} is not "
                             "act_<digits>.", f"google-ecommerce-mcp client add {ctx.client_id} --replace")
    return ctx, account, _base()


def _entity(value: str, what: str) -> str:
    text = (value or "").strip()
    if not _ID.fullmatch(text):
        raise PlanInputError("invalid_argument", f"{what} must be a numeric Meta id (ASCII digits), got "
                             f"{value!r}"[:200], "take the id from meta_campaigns / meta_adsets / meta_ads_list")
    return text


def _name(value: str) -> str:
    text = (value or "").strip()
    if not text or len(text) > MAX_NAME or any(ord(c) < 32 for c in text):
        raise PlanInputError("invalid_argument", f"name must be 1 to {MAX_NAME} printable characters.",
                             "give a short, plain name")
    return text


def _budget(value: Any, what: str = "daily_budget_minor") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PlanInputError("invalid_argument", f"{what} must be a positive whole number of MINOR units of the "
                             f"account currency (cents for EUR, yen for JPY), got {value!r}"[:200],
                             "e.g. 5000 = 50.00 EUR, or 5000 JPY for a JPY account")
    return value


def _blocked_early(ctx: Any, ops: list[WriteOp]) -> bool:
    """True when the policy already refuses these ops without any snapshot (switches, allow-list, fields...):
    then nothing is read and create_plan returns the refusal."""
    policy = ctx.policy(PLATFORM)
    return any(v.get("error") not in _NEEDS_BEFORE for v in write_policy.check_ops(ctx, PLATFORM, ops, policy))


def _money(minor: int | None, currency: str | None) -> str:
    units = minor_to_units(minor, currency) if minor is not None else None
    if minor is None:
        return "none"
    return f"{minor} minor units" + (f" = {units:g} {currency}" if units is not None else " (currency unknown)")


def _plan(ctx: Any, ops: list[WriteOp], title: str) -> dict:
    out = write_core.create_plan(ctx, PLATFORM, ops, title=title)
    out.setdefault("client_id", ctx.client_id)
    return out


def _run(build: Any, client_id: str | None) -> dict:
    """Run a plan builder; ClientError / PlanInputError / unexpected errors become data, never exceptions."""
    try:
        return build()
    except ClientError as exc:
        return _err(exc.code, exc.detail, exc.fix, client_id)
    except PlanInputError as exc:
        return _err(exc.code, exc.detail, exc.fix, client_id)
    except Exception as exc:
        return _err("internal_error", f"meta_write: unexpected {type(exc).__name__}.",
                    "retry; if it persists, report it with the audit_log of this client", client_id)


def _plan_pause(level: str, entity_ids: Any, client_id: str | None) -> dict:
    if level not in LEVELS:
        raise PlanInputError("invalid_argument", f"level must be one of {LEVELS}.", "campaign, adset or ad")
    ids = list(dict.fromkeys(_entity(e, "entity_ids[]") for e in (entity_ids or ())))
    if not 1 <= len(ids) <= MAX_ENTITIES:
        raise PlanInputError("invalid_argument", f"give 1 to {MAX_ENTITIES} entity ids.", "split the list")
    ctx, account, base = _client(client_id)
    ops = [WriteOp(op_id=f"pause-{level}-{eid}", platform=PLATFORM, account_id=f"act_{account}",
                   action=f"pause_{level}", method="POST", url=f"{base}/{eid}", body={"status": "PAUSED"},
                   params=None, risk="medium", financial=False, summary=f"Pause {level} {eid}", rollback="auto")
           for eid in ids]
    if _blocked_early(ctx, ops):
        return _plan(ctx, ops, f"Pause {len(ops)} Meta {level}(s)")
    planned = []
    for op in ops:
        eid = op.url.rsplit("/", 1)[-1]
        state = read_entity(ctx, level, eid, account)
        if "error" in state:
            return {**state, "client_id": ctx.client_id, "entity_id": eid}
        if state.get("status") not in ("ACTIVE", "PAUSED"):
            return _err("entity_not_pausable", f"{level} {eid} has status {state.get('status')!r}.",
                        "only ACTIVE or PAUSED entities can be paused (archived / deleted ones stay as they are)",
                        ctx.client_id)
        note = " (already PAUSED)" if state.get("status") == "PAUSED" else ""
        planned.append(dataclasses.replace(op, before=state, inverse=invert(op, state),
                                           summary=f"Pause {level} {eid}{note}"))
    return _plan(ctx, planned, f"Pause {len(planned)} Meta {level}(s)")


def _plan_budget(level: str, entity_id: str, daily_budget_minor: Any, client_id: str | None) -> dict:
    if level not in ("campaign", "adset"):
        raise PlanInputError("invalid_argument", "level must be campaign or adset.", "ads have no budget")
    eid = _entity(entity_id, "entity_id")
    new = _budget(daily_budget_minor)
    ctx, account, base = _client(client_id)
    op = WriteOp(op_id=f"budget-{level}-{eid}", platform=PLATFORM, account_id=f"act_{account}",
                 action=f"budget_{level}", method="POST", url=f"{base}/{eid}", body={"daily_budget": new},
                 params=None, risk="high", financial=True, summary=f"Daily budget of {level} {eid} -> {new} (minor "
                 "units)", rollback="auto")
    if _blocked_early(ctx, [op]):
        return _plan(ctx, [op], f"Daily budget of Meta {level} {eid}")
    state = read_entity(ctx, level, eid, account)
    if "error" in state:
        return {**state, "client_id": ctx.client_id, "entity_id": eid}
    if state.get("lifetime_budget"):
        return _err("lifetime_budget_not_supported", f"{level} {eid} uses a lifetime budget.",
                    "only daily budgets can be changed here; change it in Ads Manager", ctx.client_id)
    if not state.get("daily_budget"):
        other = "its ad sets" if level == "campaign" else "its campaign (Advantage campaign budget)"
        return _err("no_daily_budget", f"{level} {eid} has no daily budget: it is set on {other}.",
                    f"plan the change on {other} instead", ctx.client_id)
    currency = state.get("currency")
    summary = (f"Daily budget of {level} {eid}: {_money(state['daily_budget'], currency)} -> "
               f"{_money(new, currency)}")
    op = dataclasses.replace(op, before=state, inverse=invert(op, state), summary=summary)
    return _plan(ctx, [op], f"Daily budget of Meta {level} {eid}")


def _create_op(account: str, base: str, level: str, body: dict, financial: bool, summary: str) -> WriteOp:
    edge = {"campaign": "campaigns", "adset": "adsets", "ad": "ads"}[level]
    return WriteOp(op_id=f"create-{level}", platform=PLATFORM, account_id=f"act_{account}",
                   action=f"create_{level}", method="POST", url=f"{base}/act_{account}/{edge}",
                   body=write_policy.force_paused(body, PLATFORM), params=None, risk="medium",
                   financial=financial, summary=summary + " (created PAUSED; activate it in Ads Manager)",
                   rollback="none")


def _countries(values: Any, what: str) -> list[str]:
    out = list(dict.fromkeys(str(v).strip().upper() for v in (values or ())))
    if any(not _COUNTRY.fullmatch(c) for c in out) or len(out) > 50:
        raise PlanInputError("invalid_argument", f"{what}: ISO 3166 alpha-2 codes (FR, BE...), at most 50.",
                             "e.g. [\"FR\"]")
    return out


def _plan_create_campaign(name: str, objective: str, special_ad_categories: Any,
                          special_ad_category_country: Any, daily_budget_minor: Any,
                          adset_budget_sharing: Any, client_id: str | None) -> dict:
    cats = list(dict.fromkeys(special_ad_categories or ()))
    allowed = set(SpecialAdCategory.__args__)  # type: ignore[attr-defined]
    if any(c not in allowed for c in cats) or ("NONE" in cats and len(cats) > 1):
        raise PlanInputError("invalid_argument", f"special_ad_categories must be [] / [\"NONE\"] or from "
                             f"{sorted(allowed - {'NONE'})}.", "declare the categories explicitly")
    if objective not in Objective.__args__:  # type: ignore[attr-defined]
        raise PlanInputError("invalid_argument", f"objective {objective!r} is not supported.", "OUTCOME_SALES...")
    body: dict[str, Any] = {"name": _name(name), "objective": objective, "special_ad_categories": cats}
    countries = _countries(special_ad_category_country, "special_ad_category_country")
    if countries:
        body["special_ad_category_country"] = countries
    financial = False
    if daily_budget_minor is not None:
        body["daily_budget"] = _budget(daily_budget_minor)
        financial = True
    if adset_budget_sharing is not None:
        if not isinstance(adset_budget_sharing, bool):
            raise PlanInputError("invalid_argument", "adset_budget_sharing must be true or false.", "or omit it")
        body["is_adset_budget_sharing_enabled"] = adset_budget_sharing
        financial = True
    ctx, account, base = _client(client_id)
    budget = f", daily budget {body['daily_budget']} minor units" if "daily_budget" in body else ""
    op = _create_op(account, base, "campaign", body, financial,
                    f"Create campaign {body['name']!r} ({objective}, special_ad_categories={cats}{budget})")
    return _plan(ctx, [op], f"Create Meta campaign {body['name']}")


def _plan_create_adset(name: str, campaign_id: str, optimization_goal: str, billing_event: str,
                       countries: Any, advantage_audience: Any, age_min: Any, age_max: Any,
                       daily_budget_minor: Any, start_time: str, pixel_id: str, custom_event_type: str,
                       client_id: str | None) -> dict:
    if optimization_goal not in _GOALS or billing_event not in _BILLING:
        raise PlanInputError("invalid_argument", "optimization_goal or billing_event is not supported.",
                             "see the tool's allowed values")
    if advantage_audience not in (0, 1) or isinstance(advantage_audience, bool):
        raise PlanInputError("invalid_argument", "advantage_audience must be 0 or 1 (explicit, required by "
                             "Graph v26 for these ad sets).", "0 = your targeting only, 1 = Advantage+ audience")
    ages = (age_min, age_max)
    if any(isinstance(a, bool) or not isinstance(a, int) for a in ages) or not 13 <= age_min <= age_max <= 65:
        raise PlanInputError("invalid_argument", "age_min / age_max: whole numbers, 13 <= age_min <= age_max "
                             "<= 65.", "e.g. 18 and 65")
    geo = _countries(countries, "countries")
    if not geo:
        raise PlanInputError("invalid_argument", "countries: at least one ISO code.", "e.g. [\"FR\"]")
    targeting = {"geo_locations": {"countries": geo}, "age_min": age_min, "age_max": age_max,
                 "targeting_automation": {"advantage_audience": advantage_audience}}
    body: dict[str, Any] = {"name": _name(name), "campaign_id": _entity(campaign_id, "campaign_id"),
                            "optimization_goal": optimization_goal, "billing_event": billing_event,
                            "targeting": targeting}
    if start_time:
        if not _ISO_TIME.fullmatch(start_time.strip()):
            raise PlanInputError("invalid_argument", "start_time must be ISO 8601 with offset "
                                 "(2026-11-01T08:00:00+01:00).", "or leave it empty")
        body["start_time"] = start_time.strip()
    ctx, account, base = _client(client_id)
    if pixel_id or custom_event_type:
        pixel = _entity(pixel_id, "pixel_id")
        if not ctx.legacy:
            ctx.require_owned("meta_eco", pixel)
        if custom_event_type not in CustomEventType.__args__ or not custom_event_type:  # type: ignore[attr-defined]
            raise PlanInputError("invalid_argument", "custom_event_type is required with pixel_id (PURCHASE...).",
                                 "give both, or neither")
        body["promoted_object"] = {"pixel_id": pixel, "custom_event_type": custom_event_type}
    financial = False
    if daily_budget_minor is not None:
        body["daily_budget"] = _budget(daily_budget_minor)
        financial = True
    budget = f", daily budget {body['daily_budget']} minor units" if "daily_budget" in body else ""
    op = _create_op(account, base, "adset", body, financial,
                    f"Create ad set {body['name']!r} in campaign {body['campaign_id']} ({optimization_goal}, "
                    f"{', '.join(geo)}{budget})")
    return _plan(ctx, [op], f"Create Meta ad set {body['name']}")


def _plan_create_ad(name: str, adset_id: str, creative_id: str, client_id: str | None) -> dict:
    body = {"name": _name(name), "adset_id": _entity(adset_id, "adset_id"),
            "creative": {"creative_id": _entity(creative_id, "creative_id")}}
    ctx, account, base = _client(client_id)
    op = _create_op(account, base, "ad", body, False,
                    f"Create ad {body['name']!r} in ad set {body['adset_id']} with creative {creative_id}")
    return _plan(ctx, [op], f"Create Meta ad {body['name']}")


# ------------------------------------------------------------------ MCP tools
_CLIENT = Annotated[Optional[str], Field(description="Client id from clients_list; empty uses the only client")]
_NAME = Annotated[str, Field(description="Name of the new entity (plain text)")]
_BUDGET_OPT = Annotated[Optional[int], Field(description="Optional daily budget in MINOR units of the account "
                                             "currency (5000 = 50.00 EUR; 5000 = 5000 JPY); capped by the client's "
                                             "write policy")]


def meta_plan_pause(
    level: Annotated[Level, Field(description="Entity level: campaign, adset or ad")],
    entity_ids: Annotated[tuple[str, ...], Field(description="1 to 25 numeric ids of that level (from "
                                                 "meta_campaigns / meta_adsets / meta_ads_list)")],
    client_id: _CLIENT = None,
) -> dict:
    """PLAN (nothing is written): pause Meta campaigns, ad sets or ads (status PAUSED). Reads each entity
    (account first, then status), checks the change locally (nothing is sent to Meta), returns the signed
    plan; apply it with meta_apply after the user agrees. Rollback restores the previous status."""
    return _run(lambda: _plan_pause(level, entity_ids, client_id), client_id)


def meta_plan_budget_change(
    level: Annotated[BudgetLevel, Field(description="campaign (Advantage campaign budget) or adset")],
    entity_id: Annotated[str, Field(description="Numeric id of the campaign or ad set")],
    daily_budget_minor: Annotated[int, Field(description="New daily budget in MINOR units of the account "
                                             "currency: 5000 = 50.00 EUR, 5000 = 5000 JPY (0 decimals)")],
    client_id: _CLIENT = None,
) -> dict:
    """PLAN (nothing is written): change the daily budget of a Meta campaign or ad set. Financial: capped by the
    client's policy (max daily budget in its currency, +20 % in 24 h) and applied only after a human approval
    out of band (`google-ecommerce-mcp approve`). Rollback restores the previous budget."""
    return _run(lambda: _plan_budget(level, entity_id, daily_budget_minor, client_id), client_id)


def meta_plan_create_campaign(
    name: _NAME,
    objective: Annotated[Objective, Field(description="Campaign objective (ODAX), e.g. OUTCOME_SALES")],
    special_ad_categories: Annotated[tuple[SpecialAdCategory, ...], Field(
        description="Explicit special ad categories: [] or [\"NONE\"] when none apply; HOUSING, EMPLOYMENT, "
                    "FINANCIAL_PRODUCTS_SERVICES, ISSUES_ELECTIONS_POLITICS otherwise")],
    special_ad_category_country: Annotated[tuple[str, ...], Field(
        description="ISO countries of the special ad categories (only when categories are declared)")] = (),
    daily_budget_minor: _BUDGET_OPT = None,
    adset_budget_sharing: Annotated[Optional[bool], Field(
        description="Optional is_adset_budget_sharing_enabled flag (campaigns without a campaign budget)")] = None,
    client_id: _CLIENT = None,
) -> dict:
    """PLAN (nothing is written): create a Meta campaign, always PAUSED. Dry-run with validate_only; apply with
    meta_apply; activation stays in Ads Manager. No automatic rollback (archive it in Ads Manager)."""
    return _run(lambda: _plan_create_campaign(name, objective, special_ad_categories, special_ad_category_country,
                                              daily_budget_minor, adset_budget_sharing, client_id), client_id)


def meta_plan_create_adset(
    name: _NAME,
    campaign_id: Annotated[str, Field(description="Numeric id of the parent campaign (same ad account)")],
    optimization_goal: Annotated[OptimizationGoal, Field(description="Optimization goal, e.g. "
                                                         "OFFSITE_CONVERSIONS")],
    billing_event: Annotated[BillingEvent, Field(description="Billing event, usually IMPRESSIONS")],
    countries: Annotated[tuple[str, ...], Field(description="Target countries, ISO alpha-2 (FR, BE...)")],
    advantage_audience: Annotated[int, Field(description="targeting_automation.advantage_audience, explicit: 0 "
                                             "(your targeting only) or 1 (Advantage+ audience)", ge=0, le=1)],
    age_min: Annotated[int, Field(description="Minimum age (13-65)", ge=13, le=65)] = 18,
    age_max: Annotated[int, Field(description="Maximum age (13-65)", ge=13, le=65)] = 65,
    daily_budget_minor: _BUDGET_OPT = None,
    start_time: Annotated[str, Field(description="Optional ISO 8601 start with offset; empty = Meta default")] = "",
    pixel_id: Annotated[str, Field(description="Optional pixel id of the client (promoted_object)")] = "",
    custom_event_type: Annotated[CustomEventType, Field(description="Conversion event with pixel_id, e.g. "
                                                        "PURCHASE")] = "",
    client_id: _CLIENT = None,
) -> dict:
    """PLAN (nothing is written): create a Meta ad set, always PAUSED, in a campaign of the client's account
    (checked). No bid fields (bid caps are refused by the policy). Dry-run with validate_only; apply with
    meta_apply. No automatic rollback."""
    return _run(lambda: _plan_create_adset(name, campaign_id, optimization_goal, billing_event, countries,
                                           advantage_audience, age_min, age_max, daily_budget_minor, start_time,
                                           pixel_id, custom_event_type, client_id), client_id)


def meta_plan_create_ad(
    name: _NAME,
    adset_id: Annotated[str, Field(description="Numeric id of the parent ad set (same ad account)")],
    creative_id: Annotated[str, Field(description="Numeric id of an existing ad creative of the account")],
    client_id: _CLIENT = None,
) -> dict:
    """PLAN (nothing is written): create a Meta ad, always PAUSED, from an existing creative, in an ad set of the
    client's account (both checked). Dry-run with validate_only; apply with meta_apply. No automatic rollback."""
    return _run(lambda: _plan_create_ad(name, adset_id, creative_id, client_id), client_id)


TOOLS = [("Plan a Meta pause", meta_plan_pause), ("Plan a Meta budget change", meta_plan_budget_change),
         ("Plan a Meta campaign", meta_plan_create_campaign), ("Plan a Meta ad set", meta_plan_create_adset),
         ("Plan a Meta ad", meta_plan_create_ad)]


def _install() -> None:
    """Field allow-lists + handler (idempotent: re-registering the same lists is a no-op)."""
    for action, fields in FIELD_ALLOWLISTS.items():
        write_policy.register_field_allowlist(PLATFORM, action, fields, money_ok=MONEY_OK.get(action, ()))
    write_core.register_handler(PLATFORM, HANDLER)


def register(mcp: Any) -> list[str]:
    """Register the Meta plan tools (readOnlyHint=True: they plan and dry-run, nothing is written), the Meta
    field allow-lists and the "meta" write handler. meta_apply itself comes from write_core."""
    from mcp.types import ToolAnnotations

    _install()
    plan_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=True)
    names = []
    for title, fn in TOOLS:
        mcp.tool(title=title, annotations=plan_only)(fn)
        names.append(fn.__name__)
    return names

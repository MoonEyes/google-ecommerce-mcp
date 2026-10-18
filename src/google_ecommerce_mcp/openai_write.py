"""OpenAI Ads (ChatGPT ads) writes, through the single write path of write_core (plan -> local dry-run -> signed
token -> human approval when needed -> openai_ads_apply -> journal).

Key: an OpenAI Ads API key does NOT distinguish reading from writing (no read-only key is documented, and each key
is scoped to one ad account): the key that reads can also create, change, activate and archive. Only the policy of
this server (GEM_WRITE=1, write.openai_ads.enabled, account_allow, caps, approvals) stands between a plan and a
write. Keep the key dedicated and revoke it when unused.

Endpoints (developers.openai.com/ads/api-reference/campaigns, ad-groups, ads; read 2026-10-17). Only these are on
WRITE_ALLOW_LIST, all kind="write" (a signed WriteGrant from write_core.apply_plan is required):
    POST /v1/(campaigns|ad_groups|ads)/<id>/pause    no body                       action "pause"
    POST /v1/(campaigns|ad_groups|ads)/<id>          {"status": "active"|"paused"} action "set_status" (only built
                                                                                   as the rollback of a pause)
    POST /v1/campaigns/<id>                          {"budget": {"daily_spend_limit_micros": n}}  "budget_change"
    POST /v1/campaigns                               paused campaign, explicit start_time, daily budget
                                                                                   action "create_campaign"
Never sent: /activate (an activation is only the rollback of a pause, sent as {"status": "active"} so the policy
sees it: allow_activate, financial, human approval), /archive (irreversible), the account endpoints, the Bulk API.

Dry-run: the unit endpoints have NO validate_only (only the Bulk API has one), so there is no kind="validate" rule
and the dry-run of every op is LOCAL ("mode": "local"): the op must match WRITE_ALLOW_LIST with its exact body and
its snapshot must allow it; nothing is sent.

Budgets: the policy (write_policy) must be able to cap `daily_spend_limit_micros` (a DAILY_BUDGET_KEYS field, not
an unsupported budget kind) before openai_ads_plan_budget_change and openai_ads_plan_create_campaign are registered;
until then (budget_supported() False) only openai_ads_plan_pause exists. Lifetime budgets are never written (a
campaign cannot switch back from daily to lifetime: the change would not be reversible).

Snapshot (`before`): GET /v1/ad_account (account_id = its id, currency = its currency_code) and, for an existing
entity, GET /v1/<collection>/<id> (level, id, status and, for a campaign, its budget). The URL never names the ad
account, so the policy binds every op to the account through this snapshot: a key answering for another account
than the op's is refused (account_not_bound)."""

from __future__ import annotations

import dataclasses
import re
from typing import Annotated, Any, Literal, Optional

from pydantic import Field

from . import openai_ads, write_core, write_policy
from .ads_common import AdsClient, WriteAllowList
from .config import NotConfigured
from .write_core import PlatformHandler, WriteOp

PLATFORM = "openai_ads"
BASE_URL = openai_ads.BASE_URL
_HOST_RX = openai_ads._HOST_RX
_ID_RX = r"[A-Za-z0-9_-]+"
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}", re.ASCII)
LEVELS = {"campaign": "campaigns", "ad_group": "ad_groups", "ad": "ads"}
_LEVEL_OF = {v: k for k, v in LEVELS.items()}
_ENTITY_URL = re.compile(re.escape(BASE_URL) + r"/(campaigns|ad_groups|ads)/([A-Za-z0-9_-]{1,128})(/pause)?")
BUDGET_KEY = "daily_spend_limit_micros"
LIFETIME_KEY = "lifetime_spend_limit_micros"
MIN_UNIX, MAX_UNIX = 946684800, 4102444800  # documented range of start_time / end_time
MAX_ENTITIES = 25
LOCAL_DRY_RUN = ("local check only, nothing sent: the OpenAI Ads unit endpoints have no validate_only (only the "
                 "Bulk API has one); method, URL and exact body match the write allow-list")
KEY_NOTE = ("an OpenAI Ads API key does not distinguish reading from writing: the same key can change spending; "
            "only this server's write policy and approvals guard it")


# ------------------------------------------------------------------ write allow-list (confirmed endpoints only)
def _no_body(body: Any) -> bool:
    return body is None


def _status_body(body: Any) -> bool:
    return isinstance(body, dict) and set(body) == {"status"} and body["status"] in ("active", "paused")


def _micros(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < 10 ** 15


def _unix(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and MIN_UNIX <= value <= MAX_UNIX


def _budget_body(body: Any) -> bool:
    return (isinstance(body, dict) and set(body) == {"budget"} and isinstance(body["budget"], dict)
            and set(body["budget"]) == {BUDGET_KEY} and _micros(body["budget"][BUDGET_KEY]))


def _campaign_update_body(body: Any) -> bool:
    return _status_body(body) or _budget_body(body)


_CREATE_KEYS = frozenset({"name", "description", "status", "start_time", "end_time", "budget"})


def _create_body(body: Any) -> bool:
    """POST /v1/campaigns: paused, explicit start_time, daily budget only, no targeting / bidding / mode."""
    if not isinstance(body, dict) or not {"name", "status", "start_time", "budget"} <= set(body) <= _CREATE_KEYS:
        return False
    budget = body["budget"]
    return (body["status"] == "paused" and isinstance(body["name"], str) and _unix(body["start_time"])
            and ("end_time" not in body or (_unix(body["end_time"]) and body["end_time"] > body["start_time"]))
            and ("description" not in body or isinstance(body["description"], str))
            and isinstance(budget, dict) and set(budget) == {BUDGET_KEY} and _micros(budget[BUDGET_KEY]))


WRITE_ALLOW_LIST = WriteAllowList([
    ("POST", _HOST_RX + r"/campaigns/" + _ID_RX + r"/pause", _no_body, "write"),
    ("POST", _HOST_RX + r"/ad_groups/" + _ID_RX + r"/pause", _no_body, "write"),
    ("POST", _HOST_RX + r"/ads/" + _ID_RX + r"/pause", _no_body, "write"),
    ("POST", _HOST_RX + r"/campaigns/" + _ID_RX, _campaign_update_body, "write"),
    ("POST", _HOST_RX + r"/ad_groups/" + _ID_RX, _status_body, "write"),
    ("POST", _HOST_RX + r"/ads/" + _ID_RX, _status_body, "write"),
    ("POST", _HOST_RX + r"/campaigns", _create_body, "write"),
], platform=PLATFORM)

# Snapshots (GET, on the read allow-list) and writes (grant required) share this client; never cached.
WRITE_CLIENT = AdsClient(PLATFORM, openai_ads.ALLOWED_ENDPOINTS, secrets=None, write_allow_list=WRITE_ALLOW_LIST)

# Field allow-lists per action (registered in register() only). money_ok: the exact daily budget path.
FIELD_ALLOWLISTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "pause": ((), ()),
    "set_status": (("status",), ()),
}
BUDGET_FIELD_ALLOWLISTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "budget_change": ((f"budget.{BUDGET_KEY}",), (f"budget.{BUDGET_KEY}",)),
    "create_campaign": (("name", "description", "status", "start_time", "end_time", f"budget.{BUDGET_KEY}"),
                        (f"budget.{BUDGET_KEY}",)),
}


def budget_supported() -> bool:
    """True only when write_policy caps daily_spend_limit_micros like any daily budget (a DAILY_BUDGET_KEYS field
    in micros, not an unsupported budget kind). Fail closed: any doubt is False."""
    try:
        keys = getattr(write_policy, "DAILY_BUDGET_KEYS", {})
        nk = BUDGET_KEY.lower().replace("_", "")
        return (keys.get(BUDGET_KEY) == 1 and getattr(write_policy, "_BUDGET_NK", {}).get(nk) == 1
                and not write_policy._is_unsupported(BUDGET_KEY))
    except Exception:
        return False


# ------------------------------------------------------------------ errors
def _err(code: str, detail: str, fix: str, ctx: Any = None, **extra: Any) -> dict:
    out = {"error": code, "platform": PLATFORM, "detail": detail, "fix": fix, **extra}
    if ctx is not None:
        out["client_id"] = ctx.client_id
    return out


class SnapshotError(RuntimeError):
    """Reading the current state failed; carries the platform error dict."""

    def __init__(self, error: dict) -> None:
        super().__init__(str(error.get("error", "snapshot_failed")))
        self.error = error


# ------------------------------------------------------------------ handler
def _entity(url: str) -> tuple[str, str] | None:
    """(collection, id) of an entity URL (…/<collection>/<id> or …/<collection>/<id>/pause), else None."""
    match = _ENTITY_URL.fullmatch(url) if isinstance(url, str) else None
    return (match.group(1), match.group(2)) if match else None


def _get(ctx: Any, url: str) -> dict:
    resp = WRITE_CLIENT.call("GET", url, headers=openai_ads.headers_for(ctx))
    if not isinstance(resp, dict) or "error" in resp:
        raise SnapshotError(resp if isinstance(resp, dict) else {"error": "bad_answer"})
    return resp


def read_state(ctx: Any, collection: str | None = None, entity_id: str | None = None) -> dict:
    """The trusted snapshot: {"account_id", "currency"} of the key's ad account and, for an entity, its "level",
    "id", "status" and (campaign) "budget" (daily / lifetime micros only). Raises SnapshotError."""
    account = _get(ctx, f"{BASE_URL}/ad_account")
    before: dict[str, Any] = {"account_id": account.get("id") if isinstance(account.get("id"), str) else None,
                              "currency": openai_ads.account_currency_of(account)}
    if collection is None:
        return before
    entity = _get(ctx, f"{BASE_URL}/{collection}/{entity_id}")
    if entity.get("id") != entity_id:
        raise SnapshotError({"error": "entity_mismatch", "detail": "the answer is not the entity asked for"})
    before.update(level=_LEVEL_OF[collection], id=entity_id,
                  status=entity.get("status") if isinstance(entity.get("status"), str) else None)
    budget = entity.get("budget")
    if collection == "campaigns" and isinstance(budget, dict):
        before["budget"] = {k: budget[k] for k in (BUDGET_KEY, LIFETIME_KEY) if k in budget}
    return before


def snapshot(ctx: Any, op: WriteOp) -> dict | None:
    if write_policy.is_create(op):
        return read_state(ctx)
    found = _entity(op.url)
    if found is None:
        return None
    return read_state(ctx, *found)


def dry_run(ctx: Any, op: WriteOp) -> dict:
    """LOCAL validation (no request): exact method + URL + body on WRITE_ALLOW_LIST, no params, entity state."""
    problems = []
    if WRITE_ALLOW_LIST.match(op.method, op.url, op.body) != "write":
        problems.append("method, URL or body is not on the OpenAI Ads write allow-list")
    if op.params is not None:
        problems.append("query parameters are never sent on a write")
    status = (op.before or {}).get("status")
    if not write_policy.is_create(op) and status == "archived":
        problems.append("the entity is archived (archiving is irreversible)")
    if problems:
        return {"ok": False, "mode": "local", "detail": "; ".join(problems)}
    return {"ok": True, "mode": "local", "detail": LOCAL_DRY_RUN}


def execute(ctx: Any, op: WriteOp, grant: Any) -> dict:
    resp = WRITE_CLIENT.call(op.method, op.url, headers=openai_ads.headers_for(ctx), json=op.body, params=op.params,
                             grant=grant)
    if not isinstance(resp, dict):
        return {"error": "bad_answer", "detail": "non-dict answer"}
    if "error" in resp:
        return resp
    return {k: resp[k] for k in ("id", "object", "status", "updated_at") if k in resp}


def _status_op(op_id: str, account: str, collection: str, entity_id: str, status: str) -> WriteOp:
    active = status == "active"
    level = _LEVEL_OF[collection]
    return WriteOp(op_id=op_id, platform=PLATFORM, account_id=account, action="set_status", method="POST",
                   url=f"{BASE_URL}/{collection}/{entity_id}", body={"status": status},
                   risk="high" if active else "low", financial=active,
                   summary=f"{'reactivate (spends money)' if active else 'pause'} {level} {entity_id}",
                   rollback="auto")


def _pause_op(op_id: str, account: str, collection: str, entity_id: str) -> WriteOp:
    return WriteOp(op_id=op_id, platform=PLATFORM, account_id=account, action="pause", method="POST",
                   url=f"{BASE_URL}/{collection}/{entity_id}/pause", body=None, risk="low", financial=False,
                   summary=f"pause {_LEVEL_OF[collection]} {entity_id}", rollback="auto")


def _budget_op(op_id: str, account: str, campaign_id: str, micros: int) -> WriteOp:
    return WriteOp(op_id=op_id, platform=PLATFORM, account_id=account, action="budget_change", method="POST",
                   url=f"{BASE_URL}/campaigns/{campaign_id}", body={"budget": {BUDGET_KEY: micros}},
                   risk="medium", financial=True,
                   summary=f"set the daily budget of campaign {campaign_id} to {micros} micros", rollback="auto")


def _daily(before: Any) -> int | None:
    budget = before.get("budget") if isinstance(before, dict) else None
    value = budget.get(BUDGET_KEY) if isinstance(budget, dict) else None
    return value if _micros(value) else None


def invert(op: WriteOp, before: dict | None) -> WriteOp | None:
    """Inverse from the snapshot taken at apply: pause -> set_status active (only if it was active), set_status
    active -> pause, set_status paused -> set_status active (only if it was active), budget_change -> the old daily
    budget. A creation has no automatic inverse (it was created paused)."""
    found = _entity(op.url)
    if found is None or not isinstance(before, dict):
        return None
    collection, entity_id = found
    inv_id = f"{op.op_id}_undo"
    if op.action == "pause" or (op.action == "set_status" and (op.body or {}).get("status") == "paused"):
        if before.get("status") != "active":
            return None
        return _status_op(inv_id, op.account_id, collection, entity_id, "active")
    if op.action == "set_status" and (op.body or {}).get("status") == "active":
        return _pause_op(inv_id, op.account_id, collection, entity_id) if before.get("status") != "active" else None
    if op.action == "budget_change" and collection == "campaigns":
        old = _daily(before)
        return _budget_op(inv_id, op.account_id, entity_id, old) if old is not None else None
    return None


def entity_key(ctx: Any, op: WriteOp) -> str:
    """Stable entity id for the 24 h budget baseline, from the trusted snapshot only ('' for a creation)."""
    before = op.before if isinstance(op.before, dict) else {}
    level, entity_id = before.get("level"), before.get("id")
    return f"{level}/{entity_id}" if isinstance(level, str) and isinstance(entity_id, str) else ""


HANDLER = PlatformHandler(dry_run=dry_run, snapshot=snapshot, execute=execute, invert=invert,
                          entity_key=entity_key)


# ------------------------------------------------------------------ plan builders
_NEEDS_BEFORE = frozenset({"before_unknown", "currency_unknown", "label_missing", "account_not_bound"})


def _context(client_id: str | None) -> tuple[Any, str | None, dict | None]:
    """(ctx, OpenAI Ads account id of the client, None) or (ctx|None, None, error dict)."""
    try:
        ctx = openai_ads.resolve_client(client_id)
    except Exception as exc:
        return None, None, openai_ads._client_error(exc) or _err(
            "client_error", f"client resolution failed ({type(exc).__name__})", "check clients.json (clients_list)")
    if ctx is None:
        return None, None, _err("platform_not_configured", "no client registry module", "reinstall the package")
    try:
        account = ctx.account(PLATFORM, "account_id")
    except Exception as exc:
        return ctx, None, openai_ads._client_error(exc) or _err(
            "platform_not_configured", "openai_ads.account_id is not set", "google-ecommerce-mcp client add "
            "<client> --replace", ctx)
    return ctx, account, None


def _early_refusal(ctx: Any, ops: list[WriteOp], title: str) -> dict | None:
    """The plan's refusal when a lock that needs no network read already refuses it (GEM_WRITE, enabled,
    account_allow, ownership...): no snapshot is read for a plan that cannot pass."""
    violations = [v for v in write_policy.check_ops(ctx, PLATFORM, ops, ctx.policy(PLATFORM))
                  if v.get("error") not in _NEEDS_BEFORE]
    return write_core.create_plan(ctx, PLATFORM, ops, title=title) if violations else None


def _snapshot_error(ctx: Any, exc: Exception, what: str) -> dict:
    error = getattr(exc, "error", None)
    if isinstance(error, dict) and error.get("error"):
        return {**error, "client_id": ctx.client_id, "detail": f"{what}: {error.get('detail', error['error'])}"}
    if isinstance(exc, NotConfigured):
        return _err("not_configured", str(exc), "store the client's ChatGPT Ads key", ctx)
    return _err("snapshot_failed", f"{what}: reading the current state failed ({type(exc).__name__})",
                "retry; check the access with openai_ads_account", ctx)


def _ids(values: Any, name: str) -> list[str] | dict:
    items = list(values or [])
    if not 1 <= len(items) <= MAX_ENTITIES:
        return _err("invalid_argument", f"{name}: 1 to {MAX_ENTITIES} ids", "split the change into several plans")
    if not all(isinstance(i, str) and _ID.fullmatch(i) for i in items):
        return _err("invalid_entity_id", f"{name}: ids of letters, digits, _ and - only", "use the ids returned by "
                    "openai_ads_campaigns / openai_ads_ad_groups / openai_ads_ads")
    if len(set(items)) != len(items):
        return _err("invalid_argument", f"{name}: the same id twice", "list each id once")
    return items


def plan_pause(level: str, entity_ids: Any, client_id: str | None = None) -> dict:
    if level not in LEVELS:
        return _err("invalid_level", "level must be campaign, ad_group or ad", "pass one of the three levels")
    ids = _ids(entity_ids, "entity_ids")
    if isinstance(ids, dict):
        return ids
    ctx, account, error = _context(client_id)
    if error:
        return error
    collection = LEVELS[level]
    title = f"Pause {len(ids)} OpenAI Ads {level}(s)"
    draft = [_pause_op(f"op{n}", account, collection, entity_id) for n, entity_id in enumerate(ids, start=1)]
    refused = _early_refusal(ctx, draft, title)
    if refused is not None:
        return refused
    ops, skipped = [], []
    for op, entity_id in zip(draft, ids):
        try:
            before = read_state(ctx, collection, entity_id)
        except Exception as exc:
            return _snapshot_error(ctx, exc, f"{level} {entity_id}")
        status = before.get("status")
        if status == "archived":
            return _err("entity_archived", f"{level} {entity_id} is archived (irreversible): it cannot be paused",
                        "remove it from the list", ctx)
        if status == "paused":
            skipped.append(entity_id)
            continue
        inverse = _status_op(f"{op.op_id}_undo", account, collection, entity_id, "active") \
            if status == "active" else None
        ops.append(dataclasses.replace(op, before=before, inverse=inverse,
                                       rollback="auto" if inverse is not None else "none"))
    if not ops:
        return _err("nothing_to_do", f"every {level} listed is already paused ({', '.join(skipped)})",
                    "nothing to pause", ctx, already_paused=skipped)
    out = write_core.create_plan(ctx, PLATFORM, ops, title=title)
    if skipped and isinstance(out, dict):
        out["already_paused"] = skipped
    return out


def plan_budget_change(campaign_id: str, daily_budget_micros: Any, client_id: str | None = None) -> dict:
    if not budget_supported():
        return _err("budget_kind_unsupported", "write_policy cannot cap daily_spend_limit_micros yet",
                    "change the budget in the ChatGPT Ads interface")
    if not isinstance(campaign_id, str) or not _ID.fullmatch(campaign_id):
        return _err("invalid_entity_id", "campaign_id: letters, digits, _ and - only", "use openai_ads_campaigns")
    if not _micros(daily_budget_micros):
        return _err("invalid_budget", "daily_budget_micros must be a positive integer (micros: 1000000 = 1 unit)",
                    "pass the new daily budget in micros of the account currency")
    ctx, account, error = _context(client_id)
    if error:
        return error
    title = f"Daily budget of OpenAI Ads campaign {campaign_id}"
    op = _budget_op("op1", account, campaign_id, daily_budget_micros)
    refused = _early_refusal(ctx, [op], title)
    if refused is not None:
        return refused
    try:
        before = read_state(ctx, "campaigns", campaign_id)
    except Exception as exc:
        return _snapshot_error(ctx, exc, f"campaign {campaign_id}")
    old = _daily(before)
    if old is None:
        return _err("budget_kind_unsupported", f"campaign {campaign_id} has no daily budget (lifetime budget): "
                    "switching to a daily budget cannot be undone", "change it in the ChatGPT Ads interface", ctx)
    if before.get("status") == "archived":
        return _err("entity_archived", f"campaign {campaign_id} is archived", "nothing to change", ctx)
    op = dataclasses.replace(op, before=before, inverse=_budget_op("op1_undo", account, campaign_id, old))
    return write_core.create_plan(ctx, PLATFORM, [op], title=title)


def plan_create_campaign(name: str, daily_budget_micros: Any, start_time: Any, end_time: Any = None,
                         description: str = "", client_id: str | None = None) -> dict:
    if not budget_supported():
        return _err("budget_kind_unsupported", "write_policy cannot cap daily_spend_limit_micros yet",
                    "create the campaign in the ChatGPT Ads interface")
    if not isinstance(name, str) or not 3 <= len(name) <= 1000 or not name.strip():
        return _err("invalid_name", "name: 3 to 1000 characters with a non-space character", "fix the name")
    if not isinstance(description, str) or len(description) > 1000:
        return _err("invalid_description", "description: text of 1000 characters at most", "shorten it")
    if not _micros(daily_budget_micros):
        return _err("invalid_budget", "daily_budget_micros must be a positive integer (micros: 1000000 = 1 unit)",
                    "pass the daily budget in micros of the account currency")
    if not _unix(start_time):
        return _err("invalid_start_time", f"start_time is required: unix seconds between {MIN_UNIX} and {MAX_UNIX} "
                    "(without it, a campaign delivers as soon as it is active)", "pass an explicit start_time")
    if end_time is not None and not (_unix(end_time) and end_time > start_time):
        return _err("invalid_end_time", "end_time: unix seconds after start_time", "fix end_time or omit it")
    ctx, account, error = _context(client_id)
    if error:
        return error
    body: dict[str, Any] = {"name": name, "start_time": start_time, "budget": {BUDGET_KEY: daily_budget_micros}}
    if end_time is not None:
        body["end_time"] = end_time
    if description:
        body["description"] = description
    body = write_policy.force_paused(body, PLATFORM)  # explicit "status": "paused"
    op = WriteOp(op_id="op1", platform=PLATFORM, account_id=account, action="create_campaign", method="POST",
                 url=f"{BASE_URL}/campaigns", body=body, risk="medium", financial=True,
                 summary=f"create a PAUSED campaign with a daily budget of {daily_budget_micros} micros",
                 rollback="none")
    return write_core.create_plan(ctx, PLATFORM, [op], title="Create a paused OpenAI Ads campaign")


# ------------------------------------------------------------------ MCP tools
_CLIENT = Annotated[Optional[str], Field(description="Client id from clients_list; empty uses the only client")]


def _safe(fn: Any, client_id: str | None) -> dict:
    try:
        return fn()
    except NotConfigured as exc:
        return {"error": "not_configured", "platform": PLATFORM, "detail": str(exc), "client_id": client_id}
    except Exception as exc:
        return openai_ads._client_error(exc) or _err(
            "internal_error", f"openai_write: unexpected {type(exc).__name__}", "retry; report it if it persists")


def openai_ads_plan_pause(
    level: Annotated[Literal["campaign", "ad_group", "ad"], Field(description="What to pause: campaign, ad_group "
                                                                              "or ad")],
    entity_ids: Annotated[list[str], Field(description="Ids of the entities to pause (1 to 25), from "
                                                       "openai_ads_campaigns / openai_ads_ad_groups / openai_ads_ads")],
    client_id: _CLIENT = None,
) -> dict:
    """PLAN (nothing is written): pause OpenAI Ads (ChatGPT ads) campaigns, ad groups or ads. Reads each entity
    (archived ones are refused, already paused ones are skipped), checks the client's write policy, runs a LOCAL
    dry-run (the unit endpoints have no validate_only) and returns the plan with confirmation_token and confirm;
    apply it with openai_ads_apply after the user agreed. Rollback reactivates what was active (that rollback
    spends money: allow_activate and a human approval). Note: an OpenAI Ads key cannot be read-only; only this
    server's policy guards writes."""
    return _safe(lambda: plan_pause(level, entity_ids, client_id), client_id)


def openai_ads_plan_budget_change(
    campaign_id: Annotated[str, Field(description="Campaign id (from openai_ads_campaigns); it must have a daily "
                                                  "budget")],
    daily_budget_micros: Annotated[int, Field(description="New daily budget in micros of the account currency "
                                                          "(1000000 = 1 unit); capped by the client's policy")],
    client_id: _CLIENT = None,
) -> dict:
    """PLAN (nothing is written): set the daily budget (daily_spend_limit_micros) of one OpenAI Ads campaign.
    Financial: the client's cap, currency and +20 %/24 h locks apply and the user must approve it out of band
    (google-ecommerce-mcp approve). Lifetime budgets are refused. LOCAL dry-run only. Apply with openai_ads_apply;
    rollback restores the previous daily budget. An OpenAI Ads key cannot be read-only."""
    return _safe(lambda: plan_budget_change(campaign_id, daily_budget_micros, client_id), client_id)


def openai_ads_plan_create_campaign(
    name: Annotated[str, Field(description="Campaign name, 3 to 1000 characters")],
    daily_budget_micros: Annotated[int, Field(description="Daily budget in micros of the account currency "
                                                          "(1000000 = 1 unit); capped by the client's policy")],
    start_time: Annotated[int, Field(description="Explicit start, unix seconds (required: without it a campaign "
                                                 "delivers as soon as it is active)")],
    end_time: Annotated[Optional[int], Field(description="Optional end, unix seconds after start_time")] = None,
    description: Annotated[str, Field(description="Optional description (1000 characters at most)")] = "",
    client_id: _CLIENT = None,
) -> dict:
    """PLAN (nothing is written): create one OpenAI Ads campaign, always with an explicit status "paused" and an
    explicit start_time, and a daily budget (no targeting, no bidding, no product feed). Financial: cap and
    currency locks plus an out-of-band human approval. LOCAL dry-run only. Apply with openai_ads_apply; activate it
    in the ChatGPT Ads interface once its ad groups and ads are ready. An OpenAI Ads key cannot be read-only."""
    return _safe(lambda: plan_create_campaign(name, daily_budget_micros, start_time, end_time, description,
                                              client_id), client_id)


TOOLS = [("Plan an OpenAI Ads pause", openai_ads_plan_pause)]
BUDGET_TOOLS = [("Plan an OpenAI Ads budget change", openai_ads_plan_budget_change),
                ("Plan an OpenAI Ads campaign (paused)", openai_ads_plan_create_campaign)]


def register(mcp: Any) -> list[str]:
    """Register the handler, the field allow-lists and the plan_* tools (readOnlyHint=True: a plan writes
    nothing). The budget tools and their allow-lists exist only when budget_supported()."""
    from mcp.types import ToolAnnotations

    write_core.register_handler(PLATFORM, HANDLER)
    lists = dict(FIELD_ALLOWLISTS)
    tools = list(TOOLS)
    if budget_supported():
        lists.update(BUDGET_FIELD_ALLOWLISTS)
        tools += BUDGET_TOOLS
    for action, (fields, money_ok) in lists.items():
        write_policy.register_field_allowlist(PLATFORM, action, fields, money_ok=money_ok)
    plan_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=True)
    names = []
    for title, fn in tools:
        mcp.tool(title=title, annotations=plan_only)(fn)
        names.append(fn.__name__)
    return names

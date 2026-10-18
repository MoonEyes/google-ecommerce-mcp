"""Google Tag Manager API v2: workspace reads, and workspace / tag / trigger / variable changes through the write
path. Nothing written here ever reaches the live site.

Read (read token, tagmanager.readonly): gtm_workspace_status, gtm_get_tag, gtm_get_trigger, gtm_get_variable,
gtm_versions.
Write plans (write token, tagmanager.edit.containers; plan_* -> google_apply):
    gtm_plan_workspace_create  POST {container}/workspaces            a dedicated workspace, marked [gem-managed]
    gtm_plan_tag_upsert        POST {workspace}/tags, or PUT {workspace}/tags/{id} with ?fingerprint= (required)
    gtm_plan_trigger_upsert    same for triggers
    gtm_plan_variable_upsert   same for variables
    gtm_plan_quick_preview     POST {workspace}:quick_preview (tagmanager.edit.containerversions): compiles the
                               workspace into a throw-away preview version. It changes nothing (no workspace
                               change, no version, nothing live), so it is answered directly: no plan to apply.
Deliberately ABSENT in v1: create_version (it deletes the workspace and moves the container base), publish
(live for every visitor; scope tagmanager.publish is never requested), every DELETE, revert, sync,
resolve_conflict, bulk_update. Publishing stays a human action in the GTM interface.

Locks of this module, on top of write_policy (checked by the builder, by dry_run and again by execute on the
snapshot taken at apply time):
- the Default Workspace is never written: refused when its name is "Default Workspace", when it is the only
  workspace of the container, or when it has the lowest workspace id and is not marked [gem-managed];
- the account of an op is the client's GTM account_id, the container its configured container_id (numeric
  ids in clients.json: gtm.account_id, gtm.container_id); the URL, the op and the snapshot must agree;
- an update is a full PUT of the entity: every field of the current entity must be one this module can send
  back (else refused, nothing is lost silently), and the fingerprint read by gtm_get_* is required and must
  still be current;
- unpausing a paused tag is refused (do it in GTM); custom HTML tags and custom JavaScript variables are
  "high" risk (they run code on the site once someone publishes): their plans need the out-of-band human
  approval (write_core.approval_required);
- gtm_get_* show the entity cleaned of control / bidi / invisible characters, strings capped at READ_TEXT_LIMIT,
  and parameters whose key names a secret / token / key / password masked as "[secret]"; a plan that sends
  "[secret]" back is refused (it would overwrite the real value).
Rollback: an update is undone by a "<kind>_restore" PUT of the entity as it was before the apply, refused when
the entity changed since (someone else's edit is never overwritten); creations have no automatic rollback
(abandon the workspace in GTM, or delete the entity there).

Handler "gtm": snapshot = the container's workspaces (+ the entity for an update); dry_run = the locks above,
then quick_preview of the target workspace when the token allows it ("native": compiler and sync status of the
workspace as it is before the change), else "local".
"""

from __future__ import annotations

import dataclasses
import json
import re
from typing import Annotated, Any, Optional

from pydantic import Field

from . import google_write as gw
from .write_core import PlatformHandler, WriteOp
from .write_core import _clean as _clean_text
from .write_core import _clean_tree

PLATFORM = "gtm"
GTM_API = "https://tagmanager.googleapis.com/tagmanager/v2"
_A = r"https://tagmanager\.googleapis\.com/tagmanager/v2/accounts"
_C = _A + r"/[0-9]+/containers/[0-9]+"
_W = _C + r"/workspaces/[0-9]+"
_K = r"(tags|triggers|variables)"

READ_RULES = [("GET", _A), ("GET", _A + r"/[0-9]+/containers"), ("GET", _C + "/workspaces"), ("GET", _W),
              ("GET", _W + "/status"), ("GET", _W + "/" + _K + r"/[0-9]+"), ("GET", _C + "/version_headers"),
              ("GET", _C + "/versions:live")]
# Write client: reads for the snapshots, the side-effect-free preview, and the three write shapes.
SNAPSHOT_RULES = [("GET", _C + "/workspaces"), ("GET", _W + "/" + _K + r"/[0-9]+"),
                  ("POST", _W + ":quick_preview")]
WRITE_RULES = [("POST", _C + "/workspaces", None, "write"), ("POST", _W + "/" + _K, None, "write"),
               ("PUT", _W + "/" + _K + r"/[0-9]+", None, "write")]

READ = gw.google_read_client(PLATFORM, READ_RULES)
WRITE = gw.google_write_client(PLATFORM, WRITE_RULES, read_rules=SNAPSHOT_RULES)

DEFAULT_WORKSPACE = "default workspace"
GEM_MARKER = "[gem-managed]"
OUTPUT_ONLY = frozenset({"path", "accountId", "containerId", "workspaceId", "tagId", "triggerId", "variableId",
                         "fingerprint", "tagManagerUrl"})
_URL_RX = re.compile(re.escape(GTM_API) + r"/accounts/([0-9]+)/containers/([0-9]+)/workspaces"
                     r"(?:/([0-9]+)(?:/(tags|triggers|variables)(?:/([0-9]+))?)?)?")
_DIGITS = re.compile(r"[0-9]{1,20}")
# Read tools: a parameter whose key names a secret is shown as SECRET_MASK; every string is capped.
SECRET_MASK = "[secret]"
_SECRET_NAME = re.compile(r"secret|token|key|password", re.IGNORECASE)
READ_TEXT_LIMIT = 4000
VOLUNTARILY_ABSENT = ("create_version", "publish", "delete", "revert", "sync", "resolve_conflict", "bulk_update")


def _param(prefix: str) -> list[str]:
    """Allow-list paths of a GTM Parameter field (type, key, value, isWeakReference, and nested list / map
    items), at most 4 segments deep."""
    out: list[str] = []
    for sub in ("", ".list", ".map", ".list.map", ".list.list"):
        base = prefix + sub
        if base.count(".") + 2 > 4:
            continue
        out.extend(f"{base}.{leaf}" for leaf in ("type", "key", "value", "isWeakReference"))
    return out


_TRIGGER_PARAMS = ("eventName", "waitForTags", "checkValidation", "waitForTagsTimeout", "uniqueTriggerId",
                   "selector", "interval", "intervalSeconds", "maxTimerLengthSeconds", "visibilitySelector",
                   "visiblePercentageMin", "visiblePercentageMax", "continuousTimeMinMilliseconds",
                   "totalTimeMinMilliseconds", "verticalScrollPercentageList", "horizontalScrollPercentageList")
TAG_FIELDS = tuple(["name", "type", "notes", "parentFolderId", "paused", "firingTriggerId", "blockingTriggerId",
                    "tagFiringOption", "scheduleStartMs", "scheduleEndMs", "liveOnly",
                    "monitoringMetadataTagNameKey", "setupTag.tagName", "setupTag.stopOnSetupFailure",
                    "teardownTag.tagName", "teardownTag.stopTeardownOnFailure"]
                   + _param("parameter") + _param("priority") + _param("monitoringMetadata"))
TRIGGER_FIELDS = tuple(["name", "type", "notes", "parentFolderId"] + _param("parameter")
                       + [p for f in ("filter", "customEventFilter", "autoEventFilter")
                          for p in [f"{f}.type", *_param(f"{f}.parameter")]]
                       + [p for f in _TRIGGER_PARAMS for p in _param(f)])
VARIABLE_FIELDS = tuple(["name", "type", "notes", "parentFolderId", "scheduleStartMs", "scheduleEndMs",
                         "enablingTriggerId", "disablingTriggerId"] + _param("parameter"))
WORKSPACE_FIELDS = ("name", "description")
KINDS: dict[str, dict[str, Any]] = {
    "tag": {"plural": "tags", "id": "tagId", "fields": TAG_FIELDS, "key": "tag"},
    "trigger": {"plural": "triggers", "id": "triggerId", "fields": TRIGGER_FIELDS, "key": "trigger"},
    "variable": {"plural": "variables", "id": "variableId", "fields": VARIABLE_FIELDS, "key": "variable"},
}
_PLURAL_TO_KIND = {v["plural"]: k for k, v in KINDS.items()}


def _money_ok(fields: tuple[str, ...]) -> tuple[str, ...]:
    """Parameter "value" leaves (a GTM parameter value is a string, never an amount the policy could cap)."""
    return tuple(f for f in fields if f.rsplit(".", 1)[-1] == "value")


def _allowlists() -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    out: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {"workspace_create": (WORKSPACE_FIELDS, ())}
    for kind, spec in KINDS.items():
        fields = spec["fields"]
        out[f"{kind}_create"] = (fields, _money_ok(fields))
        updated = tuple(fields) + ("fingerprint",)  # ?fingerprint= travels in op.params
        out[f"{kind}_update"] = (updated, _money_ok(updated))
        out[f"{kind}_restore"] = (updated, _money_ok(updated))
    return out


ALLOWLISTS = _allowlists()
TOOLS = ("gtm_workspace_status", "gtm_get_tag", "gtm_get_trigger", "gtm_get_variable", "gtm_versions",
         "gtm_plan_workspace_create", "gtm_plan_tag_upsert", "gtm_plan_trigger_upsert", "gtm_plan_variable_upsert",
         "gtm_plan_quick_preview")

CLIENT_ID = Annotated[Optional[str], Field(description="Client id from clients_list. Required when several clients "
                                                       "are registered; empty uses the only client")]
WORKSPACE_ID = Annotated[str, Field(description="Numeric GTM workspace id (gtm_workspace_status lists them); "
                                                "never the Default Workspace for a change")]
FINGERPRINT = Annotated[str, Field(description="Update only: the fingerprint returned by gtm_get_tag / "
                                               "gtm_get_trigger / gtm_get_variable (required; refused when stale)")]


# ------------------------------------------------------------------ helpers
def _base(account_id: str, container_id: str) -> str:
    return f"{GTM_API}/accounts/{account_id}/containers/{container_id}"


def _digits(value: Any, what: str) -> str:
    text = str(value or "").strip()
    if not _DIGITS.fullmatch(text):
        raise ValueError(f"{what} must be a numeric GTM id, got {value!r}")
    return text


def _is_default_name(name: Any) -> bool:
    return isinstance(name, str) and name.strip().casefold() == DEFAULT_WORKSPACE


def _workspaces(raw: list) -> list[dict]:
    """Workspace list of the snapshot (renamed keys, so the plan diff never mixes them with the body) with the
    Default Workspace flag."""
    items = [w for w in raw if isinstance(w, dict) and _DIGITS.fullmatch(str(w.get("workspaceId", "")))]
    lowest = min((int(w["workspaceId"]) for w in items), default=None)
    out = []
    for w in items:
        gem = GEM_MARKER in str(w.get("description") or "")
        default = (_is_default_name(w.get("name")) or len(items) == 1
                   or (int(w["workspaceId"]) == lowest and not gem))
        out.append({"workspaceId": str(w["workspaceId"]), "workspace_name": str(w.get("name") or ""),
                    "gem_managed": gem, "is_default": default})
    return out


def _write_target(ctx: Any) -> tuple[str, str]:
    """(GTM account id, container id) of the client for writes: both numeric ids must be in clients.json."""
    account = _digits(ctx.account("gtm", "account_id"), "clients.json gtm.account_id")
    container = _digits(ctx.account("gtm", "container_id"), "clients.json gtm.container_id")
    return account, container


def _read_target(ctx: Any) -> tuple[tuple[str, str] | None, dict | None]:
    """(account id, container id) for the read tools: the numeric ids when configured, else found by listing the
    accounts and containers visible to the read token (public id GTM-XXXX in legacy mode)."""
    ids = ctx.accounts("gtm") if callable(getattr(ctx, "accounts", None)) else {}
    account, container, public = ids.get("account_id"), ids.get("container_id"), ids.get("public_id")
    if account and container and _DIGITS.fullmatch(str(account)) and _DIGITS.fullmatch(str(container)):
        return (str(account), str(container)), None
    if not (container or public):
        ctx.account("gtm", "container_id")  # raises platform_not_configured with its fix
    accounts = gw.read_call(READ, ctx, "GET", f"{GTM_API}/accounts")
    if "error" in accounts:
        return None, accounts
    for item in accounts.get("account", []):
        acc = str(item.get("accountId", ""))
        if account and acc != str(account):
            continue  # never list the containers of another GTM account
        listed = gw.read_call(READ, ctx, "GET", f"{GTM_API}/accounts/{acc}/containers")
        if "error" in listed:
            return None, listed
        for c in listed.get("container", []):
            if (public and c.get("publicId") == public) or (container and str(c.get("containerId")) == str(container)):
                return (acc, str(c.get("containerId"))), None
    return None, {"error": "container_not_found", "detail": f"{public or container} is not visible to the "
                  "authorized Google account", "fix": "check gtm.public_id / gtm.container_id of the client"}


def _clean_entity(entity: dict) -> dict:
    return {k: v for k, v in entity.items() if k != "tagManagerUrl"}


def _mask_secrets(value: Any) -> tuple[Any, list[str]]:
    """(copy of a GTM entity where every parameter whose key names a secret / token / key / password has its
    value, list and map replaced by SECRET_MASK, masked parameter keys)."""
    masked: list[str] = []

    def walk(item: Any) -> Any:
        if isinstance(item, list):
            return [walk(v) for v in item]
        if not isinstance(item, dict):
            return item
        name = item.get("key")
        # a parameter named exactly "key" is the label column of a table row (handled below), not a secret
        if isinstance(name, str) and name.lower() != "key" and _SECRET_NAME.search(name):
            masked.append(name)
            return {k: (SECRET_MASK if k in ("value", "list", "map") else walk(v)) for k, v in item.items()}
        entries = item.get("map")
        if isinstance(entries, list):  # a name / value row of a table: {key: name, value: "api_key"}, {key: value}
            label = next((e.get("value") for e in entries if isinstance(e, dict)
                          and e.get("key") in ("name", "key", "parameter", "fieldName")), None)
            if isinstance(label, str) and _SECRET_NAME.search(label):
                masked.append(label)
                rows = [{**e, "value": SECRET_MASK} if isinstance(e, dict) and e.get("key") == "value" else walk(e)
                        for e in entries]
                return {**{k: walk(v) for k, v in item.items() if k != "map"}, "map": rows}
        return {k: walk(v) for k, v in item.items()}

    return walk(value), masked


def _readable(entity: dict) -> tuple[dict, list[str], bool]:
    """What a read tool shows of an entity: secrets masked, every string (keys included) without control / bidi /
    invisible characters (write_core._clean_tree) and capped at READ_TEXT_LIMIT. (entity, masked, truncated)."""
    shown, masked = _mask_secrets(_clean_entity(entity))
    cleaned = _clean_tree(shown, READ_TEXT_LIMIT)
    truncated = json.dumps(cleaned, sort_keys=True, default=str) != json.dumps(
        _clean_tree(shown, 10 ** 9), sort_keys=True, default=str)
    return cleaned, masked, truncated


def _has_mask(value: Any) -> bool:
    if isinstance(value, str):
        return value == SECRET_MASK
    if isinstance(value, dict):
        return any(_has_mask(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_mask(v) for v in value)
    return False


def _roundtrip(kind: str, entity: dict) -> tuple[dict, list[str]]:
    """(body a PUT can send back, top-level fields this module cannot send back) of a current entity. Output-only
    fields are dropped, so are the defaults paused=false and consentSettings {consentStatus: notSet}."""
    allowed = {f.split(".", 1)[0] for f in KINDS[kind]["fields"]}
    body: dict[str, Any] = {}
    unknown: list[str] = []
    for key, value in entity.items():
        if key in OUTPUT_ONLY:
            continue
        if key == "paused" and value is False:
            continue
        if key == "consentSettings" and isinstance(value, dict) and set(value) <= {"consentStatus"} and \
                str(value.get("consentStatus", "notSet")).replace("_", "").lower() == "notset":
            continue
        if key in allowed:
            body[key] = value
        else:
            unknown.append(key)
    return body, sorted(unknown)


def _canon(value: Any) -> Any:
    """Comparable form: lists of parameters sorted by key, dict keys sorted."""
    if isinstance(value, dict):
        return {k: _canon(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        items = [_canon(v) for v in value]
        if all(isinstance(i, dict) and "key" in i for i in items):
            return sorted(items, key=lambda i: json.dumps(i, sort_keys=True, default=str))
        return items
    return value


def _matches(current: dict, expected: dict) -> bool:
    """Every field of expected has the same value in current (parameters compared in any order)."""
    return all(_canon(current.get(k)) == _canon(v) for k, v in expected.items())


def _risk(kind: str, *types: Any) -> str:
    risky = {"tag": "html", "variable": "jsm"}.get(kind)
    return "high" if risky and risky in {str(t).lower() for t in types if t} else "medium"


def _parse(url: str) -> tuple[str, str, str | None, str | None, str | None] | None:
    match = _URL_RX.fullmatch(url) if isinstance(url, str) else None
    return match.groups() if match else None  # type: ignore[return-value]


# ------------------------------------------------------------------ read tools
@gw.tool_guard("current workspace state, which may differ from the live version")
def gtm_workspace_status(workspace_id: Annotated[str, Field(description="Numeric workspace id; empty lists every "
                                                                        "workspace (5 at most)")] = "",
                         client_id: CLIENT_ID = None) -> dict:
    """Workspaces of the client's GTM container (id, name, whether it is the Default Workspace, whether this
    server created it) and, for each one, its pending changes (added / updated / deleted tags, triggers,
    variables) and merge conflicts. Read only; use a non-default workspace for any change."""
    ctx = gw.resolve(client_id)
    ids, error = _read_target(ctx)
    if error:
        return {**error, "client_id": ctx.client_id}
    assert ids is not None
    base = _base(*ids)
    wanted = _digits(workspace_id, "workspace_id") if str(workspace_id or "").strip() else None
    listed = gw.read_call(READ, ctx, "GET", f"{base}/workspaces")
    if "error" in listed:
        return {**listed, "client_id": ctx.client_id}
    raw = {str(w.get("workspaceId")): w for w in listed.get("workspace", []) if isinstance(w, dict)}
    marked = _workspaces(list(raw.values()))
    targets = [w for w in marked if wanted is None or w["workspaceId"] == wanted][:5]
    if wanted and not targets:
        return {"error": "workspace_not_found", "detail": f"workspace {wanted} is not in the container",
                "fix": "call gtm_workspace_status without workspace_id to list them", "client_id": ctx.client_id}
    out = []
    errors = []
    for w in targets:
        entry = {"workspace_id": w["workspaceId"], "name": w["workspace_name"],
                 "description": raw[w["workspaceId"]].get("description", ""), "is_default": w["is_default"],
                 "gem_managed": w["gem_managed"]}
        status = gw.read_call(READ, ctx, "GET", f"{base}/workspaces/{w['workspaceId']}/status")
        if "error" in status:
            errors.append({"step": f"status of workspace {w['workspaceId']}", **status})
        else:
            changes = []
            for change in status.get("workspaceChange", []):
                kind = next((k for k in ("tag", "trigger", "variable", "folder", "builtInVariable", "client",
                                         "zone", "customTemplate", "transformation", "gtagConfig") if k in change),
                            "other")
                item = change.get(kind) if isinstance(change.get(kind), dict) else {}
                changes.append({"change": change.get("changeStatus"), "kind": kind, "name": item.get("name"),
                                "id": item.get(f"{kind}Id")})
            entry.update(changes=changes, merge_conflicts=len(status.get("mergeConflict", [])))
        out.append(entry)
    result = {"client_id": ctx.client_id, "container": {"account_id": ids[0], "container_id": ids[1]},
              "workspaces": out, "count": len(marked),
              "untrusted_text": gw.untrusted("workspaces[].name", "workspaces[].description",
                                             "workspaces[].changes[].name")}
    if errors:
        result.update(partial=True, partial_errors=errors)
    return result


def _get_entity(kind: str, entity_id: str, workspace_id: str, client_id: str | None) -> dict:
    ctx = gw.resolve(client_id)
    eid = _digits(entity_id, f"{kind}_id")
    ws = _digits(workspace_id, "workspace_id")
    ids, error = _read_target(ctx)
    if error:
        return {**error, "client_id": ctx.client_id}
    assert ids is not None
    url = f"{_base(*ids)}/workspaces/{ws}/{KINDS[kind]['plural']}/{eid}"
    entity = gw.read_call(READ, ctx, "GET", url)
    if "error" in entity:
        return {**entity, "client_id": ctx.client_id}
    _, unknown = _roundtrip(kind, entity)
    shown, masked, truncated = _readable(entity)
    fingerprint = entity.get("fingerprint")
    out = {"client_id": ctx.client_id, "workspace_id": ws, kind: shown,
           "fingerprint": _clean_text(fingerprint, 64) if fingerprint is not None else None,
           "updatable": not unknown, "fields_not_supported_for_update": [_clean_text(u, 100) for u in unknown],
           "untrusted_text": gw.untrusted(f"{kind}.name", f"{kind}.notes", f"{kind}.parameter")}
    if masked:
        out["secrets_masked"] = sorted({_clean_text(m, 100) for m in masked})
    if masked or truncated:
        out["copy_note"] = (f"values shown as {SECRET_MASK!r} are hidden and long texts are cut at "
                            f"{READ_TEXT_LIMIT} characters: never send them back; an update keeps every field you "
                            "do not pass")
    return out


@gw.tool_guard("current workspace state, which may differ from the live version")
def gtm_get_tag(tag_id: Annotated[str, Field(description="Numeric tag id")], workspace_id: WORKSPACE_ID,
                client_id: CLIENT_ID = None) -> dict:
    """One tag of a workspace in full (type, parameters, triggers, paused) with its fingerprint, which
    gtm_plan_tag_upsert needs to update it. Read only."""
    return _get_entity("tag", tag_id, workspace_id, client_id)


@gw.tool_guard("current workspace state, which may differ from the live version")
def gtm_get_trigger(trigger_id: Annotated[str, Field(description="Numeric trigger id")], workspace_id: WORKSPACE_ID,
                    client_id: CLIENT_ID = None) -> dict:
    """One trigger of a workspace in full (type, filters) with its fingerprint (needed to update it). Read only."""
    return _get_entity("trigger", trigger_id, workspace_id, client_id)


@gw.tool_guard("current workspace state, which may differ from the live version")
def gtm_get_variable(variable_id: Annotated[str, Field(description="Numeric variable id")],
                     workspace_id: WORKSPACE_ID, client_id: CLIENT_ID = None) -> dict:
    """One variable of a workspace in full (type, parameters) with its fingerprint (needed to update it). Read
    only."""
    return _get_entity("variable", variable_id, workspace_id, client_id)


@gw.tool_guard("current state of the published versions")
def gtm_versions(limit: Annotated[int, Field(description="Maximum number of versions to return")] = 20,
                 client_id: CLIENT_ID = None) -> dict:
    """Container versions of the client's GTM container, newest first (id, name, number of tags, triggers and
    variables) and which one is live. Read only: publishing is not offered."""
    ctx = gw.resolve(client_id)
    ids, error = _read_target(ctx)
    if error:
        return {**error, "client_id": ctx.client_id}
    assert ids is not None
    base = _base(*ids)
    headers = gw.read_call(READ, ctx, "GET", f"{base}/version_headers")
    if "error" in headers:
        return {**headers, "client_id": ctx.client_id}
    items = [h for h in headers.get("containerVersionHeader", []) if isinstance(h, dict)]
    items.sort(key=lambda h: int(h["containerVersionId"]) if str(h.get("containerVersionId", "")).isdigit() else -1,
               reverse=True)
    versions = [{"version_id": h.get("containerVersionId"), "name": h.get("name"), "deleted": bool(h.get("deleted")),
                 "tags": h.get("numTags"), "triggers": h.get("numTriggers"), "variables": h.get("numVariables")}
                for h in items[:max(1, min(int(limit), 100))]]
    result = {"client_id": ctx.client_id, "container": {"account_id": ids[0], "container_id": ids[1]},
              "versions": versions, "count": len(items),
              "untrusted_text": gw.untrusted("versions[].name", "live_version.name")}
    live = gw.read_call(READ, ctx, "GET", f"{base}/versions:live")
    if "error" in live:
        result.update(partial=True, partial_errors=[{"step": "live version", **live}])
    else:
        result["live_version"] = {"version_id": live.get("containerVersionId"), "name": live.get("name")}
    return result


# ------------------------------------------------------------------ handler
def _snapshot(ctx: Any, op: WriteOp) -> dict:
    """The container's workspaces (+ the entity of an update), read with the write token. The account and
    container come from Google's answer, never from the op. Raises gw.SnapshotError."""
    parts = _parse(op.url)
    if parts is None:
        raise gw.SnapshotError("bad_url", "the op URL is not a GTM workspace / entity URL")
    account, container, workspace, plural, entity_id = parts
    configured = ctx.accounts("gtm") if callable(getattr(ctx, "accounts", None)) else {}
    if account != str(op.account_id) or account != str(configured.get("account_id")) \
            or container != str(configured.get("container_id")):  # before any request: never read elsewhere
        raise gw.SnapshotError("account_not_bound", f"the URL names GTM account {account} / container {container}, "
                               f"not the op's account and the configured container of client {ctx.client_id!r}",
                               "One container per op: the client's gtm.account_id / gtm.container_id.")
    listed = gw.write_call(WRITE, ctx, "GET", f"{_base(account, container)}/workspaces")
    if "error" in listed:
        raise gw.SnapshotError.from_result(listed, f"workspaces of container {container}")
    raw = [w for w in listed.get("workspace", []) if isinstance(w, dict)]
    if not raw:
        raise gw.SnapshotError("container_not_found", f"container {container} lists no workspace",
                               "Check gtm.account_id / gtm.container_id of the client.")
    before: dict[str, Any] = {"accountId": str(raw[0].get("accountId", "")),
                              "containerId": str(raw[0].get("containerId", "")), "workspaces": _workspaces(raw)}
    if workspace is not None:
        target = next((w for w in before["workspaces"] if w["workspaceId"] == workspace), None)
        if target is None:
            raise gw.SnapshotError("workspace_not_found", f"workspace {workspace} is not in container {container}",
                                   "Create one with gtm_plan_workspace_create, or pick one in gtm_workspace_status.")
        before["workspace"] = target
    if entity_id is not None:
        entity = gw.write_call(WRITE, ctx, "GET", op.url)
        if "error" in entity:
            raise gw.SnapshotError.from_result(entity, f"{plural} {entity_id} of workspace {workspace}")
        before["entity"] = _clean_entity(entity)
    return before


def _preflight(ctx: Any, op: WriteOp) -> list[str]:
    """The module locks, on op.before (the trusted snapshot). Empty list = go on."""
    parts = _parse(op.url)
    if parts is None:
        return ["the URL is not a GTM workspace / entity URL"]
    account, container, workspace, plural, entity_id = parts
    configured = ctx.accounts("gtm") if callable(getattr(ctx, "accounts", None)) else {}
    problems: list[str] = []
    if account != str(op.account_id) or account != str(configured.get("account_id")):
        problems.append(f"GTM account {account} is not the client's account of this op")
    if container != str(configured.get("container_id")):
        problems.append(f"container {container} is not the client's configured gtm.container_id")
    before = op.before if isinstance(op.before, dict) else {}
    if str(before.get("accountId")) != account or str(before.get("containerId")) != container:
        problems.append("the snapshot names another GTM account or container than the URL")
    body = op.body if isinstance(op.body, dict) else {}
    action = str(op.action)
    if action == "workspace_create":
        name = body.get("name")
        if op.method.upper() != "POST" or workspace is not None:
            problems.append("a workspace is created by POST on the container's workspaces")
        if not isinstance(name, str) or not name.strip():
            problems.append("the workspace needs a name")
        elif _is_default_name(name):
            problems.append("a workspace may not be named Default Workspace")
        elif any(w.get("workspace_name", "").casefold() == name.strip().casefold()
                 for w in before.get("workspaces") or []):
            problems.append(f"a workspace named {name!r} already exists")
        return problems
    target = before.get("workspace") if isinstance(before.get("workspace"), dict) else None
    if workspace is None or target is None or target.get("workspaceId") != workspace:
        problems.append(f"workspace {workspace} is not in the snapshot")
    elif target.get("is_default"):
        problems.append(f"workspace {workspace} ({target.get('workspace_name')!r}) is the Default Workspace, which "
                        "this server never writes: create a dedicated one with gtm_plan_workspace_create")
    kind, _, verb = action.partition("_")
    if kind not in KINDS or KINDS[kind]["plural"] != plural:
        return problems + [f"action {action!r} does not match the URL ({plural})"]
    entity = before.get("entity") if isinstance(before.get("entity"), dict) else None
    if verb == "create":
        if op.method.upper() != "POST" or entity_id is not None:
            problems.append(f"a {kind} is created by POST on the workspace's {plural}")
        if not body.get("name") or not body.get("type"):
            problems.append(f"a new {kind} needs a name and a type")
    elif verb in ("update", "restore"):
        if op.method.upper() != "PUT" or entity_id is None or entity is None:
            problems.append(f"an update is a PUT of an existing {kind} read in the snapshot")
        elif verb == "update":
            fingerprint = (op.params or {}).get("fingerprint")
            if not fingerprint:
                problems.append("an update needs the fingerprint of the entity (read it with gtm_get_" + kind + ")")
            elif str(entity.get("fingerprint")) != str(fingerprint):
                problems.append(f"the {kind} changed since it was read (fingerprint {fingerprint} is now "
                                f"{entity.get('fingerprint')}): read it again and plan again")
        else:
            expected = op.inverse.body if isinstance(op.inverse, WriteOp) and isinstance(op.inverse.body, dict) \
                else None
            current, _ = _roundtrip(kind, entity)
            if expected is None:
                problems.append("a restore must name the change it undoes")
            elif not _matches(current, expected):
                problems.append(f"the {kind} changed since the plan being undone was applied; restoring it would "
                                "overwrite that change: edit it by hand in GTM")
        if entity is not None and str(entity.get(KINDS[kind]["id"])) != str(entity_id):
            problems.append(f"the snapshot is not {kind} {entity_id}")
    else:
        problems.append(f"unknown GTM action {action!r}")
    return problems


def _ws_url(op: WriteOp) -> str | None:
    parts = _parse(op.url)
    if parts is None or parts[2] is None:
        return None
    return f"{_base(parts[0], parts[1])}/workspaces/{parts[2]}"


def _preview(ctx: Any, ws_url: str) -> dict:
    return gw.write_call(WRITE, ctx, "POST", f"{ws_url}:quick_preview")


def _preview_detail(result: dict) -> dict:
    version = result.get("containerVersion") if isinstance(result.get("containerVersion"), dict) else {}
    sync = result.get("syncStatus") if isinstance(result.get("syncStatus"), dict) else {}
    return {"compiler_error": bool(result.get("compilerError", False)),
            "merge_conflict": bool(sync.get("mergeConflict", False)), "sync_error": bool(sync.get("syncError", False)),
            "tags": len(version.get("tag", []) or []), "triggers": len(version.get("trigger", []) or []),
            "variables": len(version.get("variable", []) or [])}


def _dry_run(ctx: Any, op: WriteOp) -> dict:
    problems = _preflight(ctx, op)
    if problems:
        return gw.local_result(False, "; ".join(problems))
    ws_url = _ws_url(op)
    if ws_url is None:  # workspace creation: nothing to preview yet
        return gw.local_result(True, {"checked": "name, container and account against the current workspaces"})
    preview = _preview(ctx, ws_url)
    if "error" in preview:
        if preview.get("error") == "not_available":
            return gw.local_result(False, f"the workspace cannot be previewed: {preview.get('detail')}")
        return gw.local_result(True, {"checked": "workspace, fingerprint and fields (local)",
                                      "preview_unavailable": preview.get("error")})
    detail = _preview_detail(preview)
    detail["note"] = "quick_preview of the workspace as it is now, before this change; nothing goes live"
    return gw.native_result(True, detail)


def _execute(ctx: Any, op: WriteOp, grant: Any) -> dict:
    problems = _preflight(ctx, op)  # op.before is the snapshot taken at apply time
    if problems:
        return {"error": "precondition_failed", "detail": "; ".join(problems),
                "fix": "Check the workspace with gtm_workspace_status / gtm_get_*, then plan again; nothing was sent."}
    result = gw.write_call(WRITE, ctx, op.method, op.url, params=op.params, json=op.body, grant=grant)
    if "error" in result:
        return result
    return gw.trim(result, ("path", "name", "workspaceId", "tagId", "triggerId", "variableId", "fingerprint"))


def _restore_op(op: WriteOp, before: dict | None) -> WriteOp | None:
    """The PUT putting the entity back as `before` shows it (None when impossible). Its inverse is op, so the
    restore is refused if the entity no longer is what op wrote."""
    kind, _, verb = str(op.action).partition("_")
    if kind not in KINDS or verb not in ("update", "restore") or not isinstance(before, dict):
        return None
    entity = before.get("entity")
    if not isinstance(entity, dict):
        return None
    body, unknown = _roundtrip(kind, entity)
    if unknown:
        return None
    name = body.get("name") or entity.get(KINDS[kind]["id"])
    return WriteOp(op_id=f"{op.op_id}-rb", platform=PLATFORM, account_id=op.account_id, action=f"{kind}_restore",
                   method="PUT", url=op.url, body=body, params=None,
                   risk=max(_risk(kind, body.get("type")), op.risk, key=("low", "medium", "high").index)
                   if op.risk in ("low", "medium", "high") else op.risk,
                   financial=False, summary=f"Restore {kind} {name!r} as it was before the change",
                   inverse=dataclasses.replace(op, before=None, inverse=None), rollback="auto")


def _invert(op: WriteOp, before: dict | None) -> WriteOp | None:
    return _restore_op(op, before)


def _entity_key(ctx: Any, op: WriteOp) -> str:
    before = op.before if isinstance(op.before, dict) else {}
    kind = str(op.action).partition("_")[0]
    entity = before.get("entity") if isinstance(before.get("entity"), dict) else {}
    workspace = (before.get("workspace") or {}).get("workspaceId") if isinstance(before.get("workspace"), dict) \
        else None
    ident = entity.get(KINDS[kind]["id"]) if kind in KINDS else None
    ident = ident or f"new:{(op.body or {}).get('name')}"
    return f"gtm/{before.get('accountId')}/{before.get('containerId')}/{workspace}/{kind}/{ident}"


HANDLER = PlatformHandler(dry_run=_dry_run, snapshot=_snapshot, execute=_execute, invert=_invert,
                          entity_key=_entity_key)


# ------------------------------------------------------------------ plan tools
def _error(ctx: Any, code: str, detail: str, fix: str) -> dict:
    return {"error": code, "detail": detail, "fix": fix, "client_id": ctx.client_id}


def _plan_op(ctx: Any, op: WriteOp, title: str) -> dict:
    """Snapshot, module locks, then write_core.create_plan (policy, dry-run, signed plan)."""
    from . import write_core

    try:
        before = _snapshot(ctx, op)
    except gw.SnapshotError as exc:
        return {**exc.as_dict(), "client_id": ctx.client_id}
    op = dataclasses.replace(op, before=before)
    problems = _preflight(ctx, op)
    if problems:
        code = "default_workspace_refused" if any("Default Workspace" in p for p in problems) else "invalid_request"
        return _error(ctx, code, "; ".join(problems), "Nothing was planned; fix the request (gtm_workspace_status "
                      "and gtm_get_* show the current state).")
    return write_core.create_plan(ctx, PLATFORM, [op], title=title)


@gw.tool_guard()
def gtm_plan_workspace_create(
    name: Annotated[str, Field(description="Name of the new workspace (unique in the container)")],
    description: Annotated[str, Field(description="Optional description; [gem-managed] is appended")] = "",
    client_id: CLIENT_ID = None,
) -> dict:
    """PLAN (nothing is written): create a dedicated workspace in the client's GTM container, where the tag,
    trigger and variable plans can then work (the Default Workspace is never written). Low risk: a workspace
    changes nothing live. Apply with google_apply after the user agreed. No automatic rollback."""
    ctx = gw.resolve(client_id)
    account, container = _write_target(ctx)
    blocked = gw.write_precheck(ctx, PLATFORM, account)
    if blocked:
        return blocked
    name = str(name or "").strip()
    if not name or len(name) > 100:
        raise ValueError("name: 1 to 100 characters")
    desc = f"{str(description or '').strip()} {GEM_MARKER}".strip()[:500]
    op = WriteOp(op_id="workspace_create-1", platform=PLATFORM, account_id=account, action="workspace_create",
                 method="POST", url=f"{_base(account, container)}/workspaces", body={"name": name, "description": desc},
                 risk="low", financial=False, rollback="none",
                 summary=f"Create GTM workspace {name!r} in container {container} (nothing goes live)")
    return _plan_op(ctx, op, f"GTM: new workspace {name}")


def _plan_upsert(kind: str, client_id: str | None, workspace_id: str, entity: Any, entity_id: str,
                 fingerprint: str) -> dict:
    ctx = gw.resolve(client_id)
    spec = KINDS[kind]
    account, container = _write_target(ctx)
    blocked = gw.write_precheck(ctx, PLATFORM, account)
    if blocked:
        return blocked
    ws = _digits(workspace_id, "workspace_id")
    eid = _digits(entity_id, f"{kind}_id") if str(entity_id or "").strip() else None
    if not isinstance(entity, dict) or not entity:
        raise ValueError(f"{kind} must be an object with the GTM {kind} fields (name, type, parameter...)")
    expected_ids = {"accountId": account, "containerId": container, "workspaceId": ws, spec["id"]: eid}
    mismatched = sorted(k for k, v in expected_ids.items() if k in entity and str(entity[k]) != str(v))
    if mismatched:
        return _error(ctx, "id_mismatch", f"{kind} fields {mismatched} name another account, container, workspace "
                      f"or {kind} than the request.", "Pass the ids of the entity you change, or drop those fields.")
    wanted = {k: v for k, v in entity.items() if k not in OUTPUT_ONLY}
    if _has_mask(wanted):
        return _error(ctx, "masked_value_sent_back", f"the {kind} holds {SECRET_MASK!r}: a value the read tools "
                      "hide; sending it would overwrite the real secret.",
                      "Pass only the fields you change (an update keeps the others), or the real value.")
    allowed = {f.split(".", 1)[0] for f in spec["fields"]}
    refused = sorted(set(wanted) - allowed)
    if refused:
        return _error(ctx, "field_not_allowed", f"{kind} fields {refused} are not supported by this server.",
                      f"Remove them; supported fields: {sorted(allowed)}. Edit the others in the GTM interface.")
    base = f"{_base(account, container)}/workspaces/{ws}/{spec['plural']}"
    url = f"{base}/{eid}" if eid else base
    probe = WriteOp(op_id=f"{kind}-probe", platform=PLATFORM, account_id=account, action=f"{kind}_create",
                    method="POST", url=url, body=None)
    try:
        before = _snapshot(ctx, probe)
    except gw.SnapshotError as exc:
        return {**exc.as_dict(), "client_id": ctx.client_id}
    target = before.get("workspace") or {}
    if target.get("is_default"):
        return _error(ctx, "default_workspace_refused", f"workspace {ws} ({target.get('workspace_name')!r}) is the "
                      "Default Workspace, which this server never writes.",
                      "Create a dedicated workspace with gtm_plan_workspace_create, then plan in it.")
    ws_name = target.get("workspace_name")
    if eid is None:
        body = dict(wanted)
        if body.get("paused") is False:
            body.pop("paused")
        name = body.get("name")
        op = WriteOp(op_id=f"{kind}_create-1", platform=PLATFORM, account_id=account, action=f"{kind}_create",
                     method="POST", url=url, body=body, risk=_risk(kind, body.get("type")), financial=False,
                     rollback="none", before=before,
                     summary=f"Create {kind} {name!r} in workspace {ws_name!r} (#{ws}); nothing goes live until "
                             "someone publishes a version in GTM")
        return _finish(ctx, op, f"GTM: new {kind} {name}")
    current = before.get("entity") or {}
    if not str(fingerprint or "").strip():
        return _error(ctx, "fingerprint_required", f"updating {kind} {eid} needs its current fingerprint.",
                      f"Read it with gtm_get_{kind}({kind}_id={eid!r}, workspace_id={ws!r}) and pass its "
                      "fingerprint.")
    if str(current.get("fingerprint")) != str(fingerprint).strip():
        return _error(ctx, "fingerprint_stale", f"{kind} {eid} changed since it was read (fingerprint "
                      f"{str(fingerprint).strip()} is now {current.get('fingerprint')}).",
                      f"Read it again with gtm_get_{kind} and plan again on the current version.")
    kept, unknown = _roundtrip(kind, current)
    if unknown:
        return _error(ctx, "not_updatable", f"{kind} {eid} has fields this server cannot send back {unknown}: a "
                      "full update would erase them.", "Edit this entity in the GTM interface.")
    body = {**kept, **wanted}
    if body.get("paused") is False:
        if kept.get("paused") is True:
            return _error(ctx, "unpause_refused", f"{kind} {eid} is paused; unpausing it lets it fire once a "
                          "version is published.", "Unpause it in the GTM interface.")
        body.pop("paused")
    if _canon(body) == _canon(kept):
        return _error(ctx, "no_change", f"{kind} {eid} already has these values.", "Nothing to plan.")
    name = body.get("name")
    restore = _restore_op(WriteOp(op_id=f"{kind}_update-1", platform=PLATFORM, account_id=account,
                                  action=f"{kind}_update", method="PUT", url=url, body=body,
                                  params={"fingerprint": str(fingerprint).strip()}), before)
    op = WriteOp(op_id=f"{kind}_update-1", platform=PLATFORM, account_id=account, action=f"{kind}_update",
                 method="PUT", url=url, body=body, params={"fingerprint": str(fingerprint).strip()},
                 risk=_risk(kind, body.get("type"), current.get("type")), financial=False, before=before,
                 inverse=restore, rollback="auto" if restore else "none",
                 summary=f"Update {kind} {name!r} (#{eid}) in workspace {ws_name!r} (#{ws}); nothing goes live "
                         "until someone publishes a version in GTM")
    return _finish(ctx, op, f"GTM: update {kind} {name}")


def _finish(ctx: Any, op: WriteOp, title: str) -> dict:
    from . import write_core

    problems = _preflight(ctx, op)
    if problems:
        return _error(ctx, "invalid_request", "; ".join(problems), "Nothing was planned; fix the request.")
    out = write_core.create_plan(ctx, PLATFORM, [op], title=title)
    if "error" not in out and op.risk == "high":
        out["warning"] = (f"{op.action.partition('_')[0]} of type {(op.body or {}).get('type')!r} runs code on "
                          "the site once a version with it is published: review it with the user")
    return out


ENTITY_DOC = ("The {kind} as a GTM API object: name, type, parameter [{{type, key, value}}]{extra}. For an update "
              "pass only the fields to change (the others are kept); output fields (path, ids, fingerprint) may be "
              "left in and must match")


@gw.tool_guard()
def gtm_plan_tag_upsert(
    workspace_id: WORKSPACE_ID,
    tag: Annotated[dict, Field(description=ENTITY_DOC.format(kind="tag", extra=", firingTriggerId, "
                                                             "blockingTriggerId, paused, notes"))],
    tag_id: Annotated[str, Field(description="Numeric id of the tag to update; empty creates a new tag")] = "",
    fingerprint: FINGERPRINT = "",
    client_id: CLIENT_ID = None,
) -> dict:
    """PLAN (nothing is written): create or update a tag in a non-default workspace of the client's GTM
    container. An update needs the fingerprint from gtm_get_tag. Nothing goes live: publishing stays in the GTM
    interface. Custom HTML tags are high risk. Apply with google_apply after the user agreed; an update can be
    undone with rollback_plan."""
    return _plan_upsert("tag", client_id, workspace_id, tag, tag_id, fingerprint)


@gw.tool_guard()
def gtm_plan_trigger_upsert(
    workspace_id: WORKSPACE_ID,
    trigger: Annotated[dict, Field(description=ENTITY_DOC.format(kind="trigger", extra=", filter, "
                                                                 "customEventFilter, autoEventFilter"))],
    trigger_id: Annotated[str, Field(description="Numeric id of the trigger to update; empty creates one")] = "",
    fingerprint: FINGERPRINT = "",
    client_id: CLIENT_ID = None,
) -> dict:
    """PLAN (nothing is written): create or update a trigger in a non-default workspace of the client's GTM
    container (fingerprint from gtm_get_trigger for an update). Apply with google_apply after the user agreed."""
    return _plan_upsert("trigger", client_id, workspace_id, trigger, trigger_id, fingerprint)


@gw.tool_guard()
def gtm_plan_variable_upsert(
    workspace_id: WORKSPACE_ID,
    variable: Annotated[dict, Field(description=ENTITY_DOC.format(kind="variable", extra=", notes"))],
    variable_id: Annotated[str, Field(description="Numeric id of the variable to update; empty creates one")] = "",
    fingerprint: FINGERPRINT = "",
    client_id: CLIENT_ID = None,
) -> dict:
    """PLAN (nothing is written): create or update a variable in a non-default workspace of the client's GTM
    container (fingerprint from gtm_get_variable for an update). Custom JavaScript variables are high risk.
    Apply with google_apply after the user agreed."""
    return _plan_upsert("variable", client_id, workspace_id, variable, variable_id, fingerprint)


@gw.tool_guard("quick preview compiled now")
def gtm_plan_quick_preview(workspace_id: WORKSPACE_ID, client_id: CLIENT_ID = None) -> dict:
    """Preview step of a GTM plan: compile a workspace into a throw-away preview version and report compiler
    errors, merge conflicts and what it holds. It changes nothing (no version is created, nothing goes live), so
    there is nothing to apply. Uses the write token (scope tagmanager.edit.containerversions) and needs writes
    enabled for the client."""
    ctx = gw.resolve(client_id)
    account, container = _write_target(ctx)
    blocked = gw.write_precheck(ctx, PLATFORM, account)
    if blocked:
        return blocked
    ws = _digits(workspace_id, "workspace_id")
    ws_url = f"{_base(account, container)}/workspaces/{ws}"
    probe = WriteOp(op_id="preview", platform=PLATFORM, account_id=account, action="tag_create", method="POST",
                    url=f"{ws_url}/tags")
    try:
        before = _snapshot(ctx, probe)
    except gw.SnapshotError as exc:
        return {**exc.as_dict(), "client_id": ctx.client_id}
    preview = _preview(ctx, ws_url)
    if "error" in preview:
        return {**preview, "client_id": ctx.client_id}
    target = before.get("workspace") or {}
    return {"client_id": ctx.client_id, "workspace_id": ws, "workspace": target.get("workspace_name"),
            "is_default": bool(target.get("is_default")), "preview": _preview_detail(preview),
            "note": "nothing was written; publishing is done in the GTM interface",
            "untrusted_text": gw.untrusted("workspace")}


# ------------------------------------------------------------------ registration
def register_write() -> None:
    """Field allow-lists and the "gtm" PlatformHandler (idempotent: the same lists again are a no-op)."""
    from . import write_core, write_policy

    for action, (fields, money_ok) in ALLOWLISTS.items():
        write_policy.register_field_allowlist(PLATFORM, action, fields, money_ok=money_ok)
    write_core.register_handler(PLATFORM, HANDLER)


def register(mcp: Any) -> list[str]:
    """Register the GTM read tools (read-only) and plan tools (read-only hint: they only plan or preview)."""
    register_write()
    names = gw.register_tools(mcp, [("GTM workspace status", gtm_workspace_status), ("GTM tag", gtm_get_tag),
                                    ("GTM trigger", gtm_get_trigger), ("GTM variable", gtm_get_variable),
                                    ("GTM versions", gtm_versions)], gw.READ_ONLY)
    names += gw.register_tools(mcp, [("Plan a GTM workspace", gtm_plan_workspace_create),
                                     ("Plan a GTM tag change", gtm_plan_tag_upsert),
                                     ("Plan a GTM trigger change", gtm_plan_trigger_upsert),
                                     ("Plan a GTM variable change", gtm_plan_variable_upsert),
                                     ("Preview a GTM workspace", gtm_plan_quick_preview)], gw.PLAN_ONLY)
    return names

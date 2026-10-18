"""Offline tests of the OpenAI Ads (ChatGPT ads) write module: plan -> local dry-run -> apply on a fake server,
refusals (write allow-list, policy, foreign account), two clients, rollback, budget tools gated on the policy.
No network (ads_fakes.no_network is autouse), no real keyring, no real registry nor state folder."""

from __future__ import annotations

import asyncio
import copy
import inspect
import json

import pytest

from ads_fakes import FakeResponse, no_keyring, no_network  # noqa: F401  (fixtures)
from clients_fakes import ACME, BETA, write_registry
from google_ecommerce_mcp import ads_common, clients, openai_ads, openai_write, write_core, write_policy
from google_ecommerce_mcp.write_core import WriteOp
from google_ecommerce_mcp.write_journal import Journal

BASE = "https://api.ads.openai.com/v1"
# Built at run time: no literal secret in the repository.
KEY_ACME = "sk-" + "acme" + "W" * 30
KEY_BETA = "sk-" + "beta" + "X" * 30
POLICY = {"enabled": True, "max_daily_budget_micros": 50_000_000, "cap_currency": "EUR", "allow_activate": True}


class FakeOpenAI:
    """In-memory OpenAI Ads API, one ad account per key (as documented). Records (method, url, kwargs)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.accounts = {KEY_ACME: {"id": "acct_acme", "name": "Acme", "currency_code": "EUR"},
                         KEY_BETA: {"id": "acct_beta", "name": "Beta", "currency_code": "EUR"}}
        self.entities = {
            KEY_ACME: {("campaigns", "cmpn_1"): {"id": "cmpn_1", "name": "Spring", "status": "active",
                                                 "budget": {"daily_spend_limit_micros": 10_000_000}},
                       ("campaigns", "cmpn_2"): {"id": "cmpn_2", "name": "Old", "status": "paused",
                                                 "budget": {"lifetime_spend_limit_micros": 25_000_000}},
                       ("campaigns", "cmpn_3"): {"id": "cmpn_3", "name": "Gone", "status": "archived"},
                       ("ad_groups", "adgrp_1"): {"id": "adgrp_1", "name": "US", "status": "active"},
                       ("ads", "ad_1"): {"id": "ad_1", "name": "Card", "status": "active"}},
            KEY_BETA: {("campaigns", "cmpn_9"): {"id": "cmpn_9", "name": "Beta", "status": "active",
                                                 "budget": {"daily_spend_limit_micros": 5_000_000}}}}
        self.created = 0

    @property
    def writes(self) -> list[tuple[str, str, dict]]:
        return [c for c in self.calls if c[0] == "POST"]

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        key = kwargs["headers"]["Authorization"].split(" ", 1)[1]
        path = url[len(BASE):].strip("/").split("/")
        store = self.entities.setdefault(key, {})
        if method == "GET" and path == ["ad_account"]:
            return FakeResponse(200, self.accounts[key])
        if method == "POST" and path == ["campaigns"]:
            self.created += 1
            entity = {"id": f"cmpn_new{self.created}", **copy.deepcopy(kwargs["json"])}
            store[("campaigns", entity["id"])] = entity
            return FakeResponse(200, entity)
        entity = store.get(tuple(path[:2])) if len(path) >= 2 else None
        if entity is None:
            return FakeResponse(404, {"error": {"message": "not found"}})
        if method == "GET" and len(path) == 2:
            return FakeResponse(200, copy.deepcopy(entity))
        if method == "POST" and len(path) == 3 and path[2] == "pause":
            entity["status"] = "paused"
            return FakeResponse(200, copy.deepcopy(entity))
        if method == "POST" and len(path) == 2:
            body = kwargs.get("json") or {}
            if "budget" in body:
                entity["budget"] = dict(body["budget"])
            if "status" in body:
                entity["status"] = body["status"]
            return FakeResponse(200, copy.deepcopy(entity))
        return FakeResponse(404, {"error": {"message": "unexpected"}})


def registry(path, acme_policy=None, beta_policy=None):
    acme = {**ACME, "write": {"openai_ads": {**POLICY, "account_allow": ["acct_acme"], **(acme_policy or {})}}}
    beta = {**BETA, "write": {"openai_ads": {**POLICY, "account_allow": ["acct_beta"], **(beta_policy or {})}}}
    write_registry(path, {"acme": acme, "beta": beta})


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch, no_keyring):  # noqa: F811  (fixture)
    for name in ("OPENAI_ADS_API_KEY", "GEM_CLIENT", "GEM_LIVE_TEST"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "clients.json"
    registry(path)
    monkeypatch.setenv("GEM_CLIENTS_FILE", str(path))
    monkeypatch.setenv("GEM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GEM_WRITE", "1")
    monkeypatch.setenv("GEM_WRITE_HMAC_KEY", "ab" * 32)
    monkeypatch.setenv("GEM_SECRET_ACME_OPENAI", KEY_ACME)
    monkeypatch.setenv("GEM_SECRET_BETA_OPENAI", KEY_BETA)
    monkeypatch.setattr(write_core, "journal", Journal())
    monkeypatch.setattr(write_core, "_HANDLERS", {})
    # the real field allow-lists (no _TEST_PERMISSIVE), in a private copy of the registry, never frozen
    monkeypatch.setattr(write_policy, "_FIELD_ALLOWLIST", dict(write_policy._FIELD_ALLOWLIST))
    monkeypatch.setattr(write_policy, "_FROZEN", False)
    monkeypatch.setattr(write_policy, "_TEST_PERMISSIVE", False, raising=False)
    monkeypatch.setattr(openai_write.WRITE_CLIENT, "BLOCKED_ATTEMPTS", [])
    return path


@pytest.fixture
def api(monkeypatch):
    fake = FakeOpenAI()
    monkeypatch.setattr(openai_write.WRITE_CLIENT, "session", fake)
    return fake


@pytest.fixture
def mcp():
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("test-openai-write")
    names = openai_write.register(server)
    write_core.register_apply_tools(server)
    server.names = names
    return server


def ctx(client_id="acme"):
    return clients.resolve(client_id)


def apply(plan, client_id="acme", **kw):
    return write_core._apply_tool("openai_ads_apply", client_id, plan["plan_id"],
                                  kw.get("token", plan["confirmation_token"]), kw.get("confirm", plan["confirm"]))


def keys_sent(api):
    return {c[2]["headers"]["Authorization"] for c in api.calls}


# ------------------------------------------------------------------ registration
def test_register_declares_plan_tools_read_only_with_client_id(mcp):
    # write_policy caps daily_spend_limit_micros (a DAILY_BUDGET_KEYS field): the budget tools are registered
    assert mcp.names == ["openai_ads_plan_pause", "openai_ads_plan_budget_change", "openai_ads_plan_create_campaign"]
    assert openai_write.budget_supported() is True
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    plan = tools["openai_ads_plan_pause"]
    assert plan.annotations.readOnlyHint is True and plan.annotations.destructiveHint is False
    assert "client_id" in plan.inputSchema["properties"] and "client_id" not in plan.inputSchema["required"]
    assert tools["openai_ads_apply"].annotations.readOnlyHint is False
    assert "read-only" in plan.description and "LOCAL" in plan.description
    assert write_core.get_handler("openai_ads") is openai_write.HANDLER
    assert write_policy.field_allowlist("openai_ads", "pause") == frozenset()
    assert write_policy.field_allowlist("openai_ads", "set_status") == frozenset({"status"})
    assert write_policy.field_allowlist("openai_ads", "budget_change") == frozenset({"budget.dailyspendlimitmicros"})


def test_register_twice_is_a_no_op(mcp):
    from mcp.server.fastmcp import FastMCP

    assert openai_write.register(FastMCP("again")) == mcp.names


def test_importing_does_not_register_allowlists_nor_handler(monkeypatch):
    import importlib

    monkeypatch.setattr(write_policy, "_FIELD_ALLOWLIST", {})
    importlib.reload(openai_write)
    assert write_policy.golden_allowlists() == {} and write_core.get_handler("openai_ads") is None


def test_key_is_documented_as_not_read_only():
    for text in (openai_write.__doc__, openai_ads.__doc__, openai_write.openai_ads_plan_pause.__doc__):
        assert "read-only" in text or "reading from writing" in text


# ------------------------------------------------------------------ write allow-list
def test_write_allow_list_has_only_confirmed_write_rules_and_no_validate_rule():
    rules = openai_write.WRITE_ALLOW_LIST.rules
    assert {kind for _, _, kind in rules} == {"write"}  # no native dry-run on the unit endpoints
    assert {m for m, _, _ in rules} == {"POST"}
    text = " ".join(p for _, p, _ in rules)
    for word in ("archive", "activate", "bulk", "spend_limit", "ad_account", "audiences", "upload", "delete"):
        assert word not in text
    assert openai_write.WRITE_ALLOW_LIST.hosts == {"api.ads.openai.com"}
    assert openai_write.WRITE_ALLOW_LIST.is_allowed("POST", f"{BASE}/campaigns/c/pause") is False


@pytest.mark.parametrize("method, url, body, reason", [
    ("POST", f"{BASE}/campaigns/cmpn_1/archive", None, "not_on_write_allow_list"),
    ("POST", f"{BASE}/campaigns/cmpn_1/activate", None, "not_on_write_allow_list"),
    ("POST", f"{BASE}/ad_account/pause", None, "not_on_write_allow_list"),
    ("POST", f"{BASE}/bulk_mutation_jobs", {"validate_only": True}, "not_on_write_allow_list"),
    ("POST", f"{BASE}/campaigns/cmpn_1/pause", {"status": "paused"}, "not_on_write_allow_list"),
    ("POST", f"{BASE}/campaigns/cmpn_1", {"status": "archived"}, "not_on_write_allow_list"),
    ("POST", f"{BASE}/campaigns/cmpn_1", {"status": "active", "name": "x"}, "not_on_write_allow_list"),
    ("POST", f"{BASE}/campaigns", {"name": "x", "status": "active", "start_time": 2000000000,
                                   "budget": {"daily_spend_limit_micros": 1}}, "not_on_write_allow_list"),
    ("POST", f"{BASE}/campaigns", {"name": "x", "status": "paused",
                                   "budget": {"daily_spend_limit_micros": 1}}, "not_on_write_allow_list"),
    ("DELETE", f"{BASE}/campaigns/cmpn_1", None, "not_on_write_allow_list"),
    ("POST", f"{BASE}/campaigns/cmpn_1/pause", None, "grant_missing"),
])
def test_requests_off_the_write_allow_list_or_without_grant_are_blocked(api, method, url, body, reason):
    out = openai_write.WRITE_CLIENT.call(method, url, json=body, headers={"Authorization": f"Bearer {KEY_ACME}"})
    assert out["error"] == "blocked" and out["reason"] == reason and api.calls == []
    assert KEY_ACME not in json.dumps(out)


# ------------------------------------------------------------------ plan -> apply
def test_plan_then_apply_pauses_a_campaign_with_a_local_dry_run(api, mcp):
    plan = openai_write.openai_ads_plan_pause("campaign", ["cmpn_1"], client_id="acme")
    assert "error" not in plan, plan
    assert plan["client_id"] == "acme" and plan["apply_tool"] == "openai_ads_apply"
    assert plan["human_approval_required"] is False and plan["risks"]["financial"] is False
    assert [d["mode"] for d in plan["dry_run"]] == ["local"] and plan["dry_run"][0]["ok"] is True
    assert "validate_only" in plan["dry_run"][0]["detail"]
    assert api.writes == []  # planning sent nothing but reads
    assert [(m, u) for m, u, _ in api.calls] == [("GET", f"{BASE}/ad_account"), ("GET", f"{BASE}/campaigns/cmpn_1")]
    assert plan["ops"][0]["rollback"] == "auto"
    out = apply(plan)
    assert out["status"] == "applied", out
    assert [(m, u) for m, u, _ in api.writes] == [("POST", f"{BASE}/campaigns/cmpn_1/pause")]
    assert api.writes[0][2].get("json") is None
    assert api.entities[KEY_ACME][("campaigns", "cmpn_1")]["status"] == "paused"
    assert apply(plan)["error"] == "plan_already_applied" and len(api.writes) == 1
    kinds = [e["kind"] for e in write_core.journal.entries("acme", plan_id=plan["plan_id"])]
    assert kinds == ["plan", "apply_intent", "apply_result"]
    intent = write_core.journal.entries("acme", plan_id=plan["plan_id"], kinds=("apply_intent",))[0]
    assert intent["entity_key"] == "campaign/cmpn_1" and intent["before"]["account_id"] == "acct_acme"
    assert keys_sent(api) == {f"Bearer {KEY_ACME}"}
    assert KEY_ACME not in json.dumps(write_core.journal.entries("acme"))


@pytest.mark.parametrize("level, entity, path", [("ad_group", "adgrp_1", "ad_groups/adgrp_1"),
                                                 ("ad", "ad_1", "ads/ad_1")])
def test_ad_groups_and_ads_are_paused_the_same_way(api, mcp, level, entity, path):
    plan = openai_write.openai_ads_plan_pause(level, [entity], client_id="acme")
    assert "error" not in plan, plan
    assert apply(plan)["status"] == "applied"
    assert [u for _, u, _ in api.writes] == [f"{BASE}/{path}/pause"]


def test_already_paused_is_skipped_and_archived_is_refused(api, mcp):
    plan = openai_write.openai_ads_plan_pause("campaign", ["cmpn_1", "cmpn_2"], client_id="acme")
    assert plan["already_paused"] == ["cmpn_2"] and len(plan["ops"]) == 1
    none = openai_write.openai_ads_plan_pause("campaign", ["cmpn_2"], client_id="acme")
    assert none["error"] == "nothing_to_do"
    archived = openai_write.openai_ads_plan_pause("campaign", ["cmpn_3"], client_id="acme")
    assert archived["error"] == "entity_archived" and api.writes == []


@pytest.mark.parametrize("args, code", [
    (("campaign", []), "invalid_argument"),
    (("campaign", ["a"] * 2), "invalid_argument"),
    (("campaign", [f"c{i}" for i in range(26)]), "invalid_argument"),
    (("campaign", ["../x"]), "invalid_entity_id"),
    (("account", ["x"]), "invalid_level"),
])
def test_bad_plan_arguments_are_refused_without_network(api, mcp, args, code):
    assert openai_write.openai_ads_plan_pause(*args, client_id="acme")["error"] == code and api.calls == []


def test_write_switches_refuse_before_any_read(api, mcp, monkeypatch, env):
    monkeypatch.delenv("GEM_WRITE")
    assert openai_write.openai_ads_plan_pause("campaign", ["cmpn_1"], client_id="acme")["error"] == "write_disabled"
    monkeypatch.setenv("GEM_WRITE", "1")
    registry(env, acme_policy={"enabled": False})
    assert openai_write.openai_ads_plan_pause("campaign", ["cmpn_1"], client_id="acme")["error"] == \
        "write_not_enabled"
    assert api.calls == []


def test_gem_write_is_rechecked_at_apply(api, mcp, monkeypatch):
    plan = openai_write.openai_ads_plan_pause("campaign", ["cmpn_1"], client_id="acme")
    monkeypatch.delenv("GEM_WRITE")
    assert apply(plan)["error"] == "write_disabled" and api.writes == []


def test_ops_outside_the_field_allow_list_are_refused(api, mcp):
    c = ctx()
    extra = WriteOp(op_id="op1", platform="openai_ads", account_id="acct_acme", action="pause", method="POST",
                    url=f"{BASE}/campaigns/cmpn_1/pause", body={"name": "renamed"})
    assert write_core.create_plan(c, "openai_ads", [extra], title="t")["error"] == "field_not_allowed"
    unknown = WriteOp(op_id="op1", platform="openai_ads", account_id="acct_acme", action="archive",
                      method="POST", url=f"{BASE}/campaigns/cmpn_1/archive", risk="irreversible")
    refused = write_core.create_plan(c, "openai_ads", [unknown], title="t")
    codes = {v["error"] for v in refused["violations"]}
    assert {"delete_not_allowed", "irreversible_not_allowed", "no_field_allowlist"} <= codes
    assert api.writes == []


def test_daily_spend_limit_is_capped_and_other_spend_limits_stay_refused(api, mcp):
    """write_policy caps daily_spend_limit_micros like any daily budget; the other spend limits stay refused."""
    op = WriteOp(op_id="op1", platform="openai_ads", account_id="acct_acme", action="budget_change",
                 method="POST", url=f"{BASE}/campaigns/cmpn_1", body={"budget": {"daily_spend_limit_micros": 1}},
                 financial=False, risk="medium")
    out = write_core.create_plan(ctx(), "openai_ads", [op], title="t")
    assert "financial_flag_missing" in {v["error"] for v in out["violations"]}  # bounded by the budget locks
    for key in ("lifetime_spend_limit_micros", "daily_spend_limit"):
        other = WriteOp(**{**op.__dict__, "financial": True, "body": {"budget": {key: 1}}})
        refused = write_core.create_plan(ctx(), "openai_ads", [other], title="t")
        assert "budget_kind_unsupported" in {v["error"] for v in refused["violations"]}, key
    assert api.writes == []


# ------------------------------------------------------------------ foreign accounts, two clients
def test_entity_of_another_client_is_not_reachable(api, mcp):
    out = openai_write.openai_ads_plan_pause("campaign", ["cmpn_9"], client_id="acme")  # beta's campaign
    assert out["error"] == "not_available" and out["client_id"] == "acme" and api.writes == []
    assert keys_sent(api) == {f"Bearer {KEY_ACME}"}


def test_key_of_another_account_is_refused_by_the_snapshot_binding(api, mcp, monkeypatch):
    monkeypatch.setenv("GEM_SECRET_ACME_OPENAI", KEY_BETA)  # acme configured with the key of beta
    out = openai_write.openai_ads_plan_pause("campaign", ["cmpn_9"], client_id="acme")
    assert out["error"] == "account_not_bound" and api.writes == []


def test_account_not_owned_by_the_client_is_refused(api, mcp, env):
    registry(env, beta_policy={"account_allow": ["acct_acme"]})  # beta tries to allow acme's account
    beta = clients.resolve("beta")
    op = openai_write._pause_op("op1", "acct_acme", "campaigns", "cmpn_1")
    out = write_core.create_plan(beta, "openai_ads", [op], title="t")
    assert out["error"] == "account_not_owned" and api.calls == []
    own = openai_write.openai_ads_plan_pause("campaign", ["cmpn_9"], client_id="beta")
    assert own["error"] == "account_not_allowed" and api.calls == []  # its own account is not allowed either


def test_two_clients_are_isolated_end_to_end(api, mcp):
    pa = openai_write.openai_ads_plan_pause("campaign", ["cmpn_1"], client_id="acme")
    pb = openai_write.openai_ads_plan_pause("campaign", ["cmpn_9"], client_id="beta")
    assert pa["client_id"] == "acme" and pb["client_id"] == "beta"
    # a plan of one client can never be applied through the other
    assert apply(pa, client_id="beta")["error"] == "plan_not_found"
    swapped = write_core.apply_plan(ctx("beta"), "openai_ads", pb["plan_id"], pa["confirmation_token"], pb["confirm"])
    assert swapped["error"] not in (None,) and api.writes == []
    assert apply(pa)["status"] == "applied" and apply(pb, client_id="beta")["status"] == "applied"
    for method, url, kwargs in api.calls:
        key = kwargs["headers"]["Authorization"]
        if "cmpn_1" in url:
            assert key == f"Bearer {KEY_ACME}"
        if "cmpn_9" in url:
            assert key == f"Bearer {KEY_BETA}"
    assert write_core.journal.entries("acme", plan_id=pb["plan_id"]) == []
    assert write_core.journal.entries("beta", plan_id=pa["plan_id"]) == []


def test_client_id_is_required_with_two_clients(api, mcp):
    assert openai_write.openai_ads_plan_pause("campaign", ["cmpn_1"])["error"] == "client_required"
    assert api.calls == []


# ------------------------------------------------------------------ rollback
def test_rollback_reactivates_through_a_financial_plan_with_human_approval(api, mcp):
    plan = openai_write.openai_ads_plan_pause("campaign", ["cmpn_1"], client_id="acme")
    assert apply(plan)["status"] == "applied"
    back = write_core._rollback_tool(plan["plan_id"], "acme")
    assert "error" not in back, back
    assert back["rollback_of"] == plan["plan_id"] and back["human_approval_required"] is True
    assert back["ops"][0]["action"] == "set_status" and back["ops"][0]["financial"] is True
    assert back["dry_run"][0]["mode"] == "local"
    refused = apply(back)
    assert refused["error"] == "approval_required" and len(api.writes) == 1
    assert write_core._write_approval("acme", back["plan_id"], approver="test")["status"] == "approved"
    done = apply(back)
    assert done["status"] == "applied", done
    method, url, kwargs = api.writes[-1]
    assert (method, url, kwargs["json"]) == ("POST", f"{BASE}/campaigns/cmpn_1", {"status": "active"})
    assert api.entities[KEY_ACME][("campaigns", "cmpn_1")]["status"] == "active"
    # and the rollback of the rollback pauses again
    again = write_core._rollback_tool(back["plan_id"], "acme")
    assert again["ops"][0]["action"] == "pause" and again["human_approval_required"] is False


def test_rollback_activation_needs_allow_activate(api, mcp, env):
    registry(env, acme_policy={"allow_activate": False})
    plan = openai_write.openai_ads_plan_pause("campaign", ["cmpn_1"], client_id="acme")
    assert apply(plan)["status"] == "applied"
    back = write_core._rollback_tool(plan["plan_id"], "acme")
    assert back["error"] == "activation_not_allowed" and len(api.writes) == 1


def test_invert_never_reactivates_what_was_not_active():
    op = openai_write._pause_op("op1", "acct_acme", "campaigns", "cmpn_1")
    assert openai_write.invert(op, {"status": "paused"}) is None
    assert openai_write.invert(op, None) is None
    inverse = openai_write.invert(op, {"status": "active"})
    assert inverse.body == {"status": "active"} and inverse.financial is True
    assert inverse.url == f"{BASE}/campaigns/cmpn_1"  # never /activate: the policy must see the status


def test_entity_key_comes_from_the_snapshot_only():
    op = openai_write._pause_op("op1", "acct_acme", "campaigns", "cmpn_1")
    assert openai_write.entity_key(None, op) == ""
    snap = WriteOp(**{**op.__dict__, "before": {"level": "campaign", "id": "cmpn_1"}})
    assert openai_write.entity_key(None, snap) == "campaign/cmpn_1"


def test_dry_run_is_local_and_sends_nothing(api):
    op = openai_write._pause_op("op1", "acct_acme", "campaigns", "cmpn_1")
    assert openai_write.dry_run(None, op) == {"ok": True, "mode": "local", "detail": openai_write.LOCAL_DRY_RUN}
    bad = WriteOp(**{**op.__dict__, "body": {"status": "paused"}})
    assert openai_write.dry_run(None, bad)["ok"] is False
    archived = WriteOp(**{**op.__dict__, "before": {"status": "archived"}})
    assert openai_write.dry_run(None, archived)["ok"] is False
    assert api.calls == []
    source = inspect.getsource(openai_write.dry_run)
    assert "WRITE_CLIENT" not in source and "validate_only" not in json.dumps(op.body)


# ------------------------------------------------------------------ budget tools, once write_policy caps them
@pytest.fixture
def budget_policy(monkeypatch):
    """Simulates the write_policy change B3 needs (daily_spend_limit_micros as a daily budget key in micros)."""
    keys = {**write_policy.DAILY_BUDGET_KEYS, "daily_spend_limit_micros": 1}
    monkeypatch.setattr(write_policy, "DAILY_BUDGET_KEYS", keys)
    monkeypatch.setattr(write_policy, "_BUDGET_NK", {write_policy._nk(k): m for k, m in keys.items()})
    monkeypatch.setattr(write_policy, "_UNSUPPORTED_PREFIXES",
                        tuple(p for p in write_policy._UNSUPPORTED_PREFIXES if p != "dailyspendlimit"))
    assert openai_write.budget_supported() is True


@pytest.fixture
def budget_mcp(budget_policy):
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("test-openai-budget")
    server.names = openai_write.register(server)
    return server


def test_budget_tools_register_only_when_the_policy_caps_them(budget_mcp):
    assert budget_mcp.names == ["openai_ads_plan_pause", "openai_ads_plan_budget_change",
                                "openai_ads_plan_create_campaign"]
    tools = {t.name: t for t in asyncio.run(budget_mcp.list_tools())}
    for tool in tools.values():
        assert tool.annotations.readOnlyHint is True and "client_id" in tool.inputSchema["properties"]
    assert write_policy.field_money_ok("openai_ads", "budget_change") == frozenset({"budget.dailyspendlimitmicros"})


def test_budget_change_is_capped_bounded_and_approved(api, budget_mcp):
    over = openai_write.plan_budget_change("cmpn_1", 60_000_000, "acme")
    assert over["error"] in ("budget_over_cap", "increase_too_large") and api.writes == []
    jump = openai_write.plan_budget_change("cmpn_1", 13_000_000, "acme")
    assert jump["error"] == "increase_too_large"
    plan = openai_write.plan_budget_change("cmpn_1", 11_000_000, "acme")
    assert "error" not in plan, plan
    assert plan["risks"]["financial"] is True and plan["human_approval_required"] is True
    assert apply(plan)["error"] == "approval_required" and api.writes == []
    write_core._write_approval("acme", plan["plan_id"])
    assert apply(plan)["status"] == "applied"
    assert api.writes[-1][2]["json"] == {"budget": {"daily_spend_limit_micros": 11_000_000}}
    back = write_core._rollback_tool(plan["plan_id"], "acme")
    assert back["ops"][0]["action"] == "budget_change"
    assert "10000000" in json.dumps(write_core._plan_get(back["plan_id"], "acme")["requests"])


def test_budget_change_refuses_lifetime_budgets_and_other_currencies(api, budget_mcp, env):
    assert openai_write.plan_budget_change("cmpn_2", 1_000_000, "acme")["error"] == "budget_kind_unsupported"
    registry(env, acme_policy={"cap_currency": "USD"})
    assert openai_write.plan_budget_change("cmpn_1", 11_000_000, "acme")["error"] == "currency_mismatch"


def test_create_campaign_is_paused_with_an_explicit_start_time(api, budget_mcp):
    assert openai_write.plan_create_campaign("Spring", 1_000_000, None, client_id="acme")["error"] == \
        "invalid_start_time"
    plan = openai_write.plan_create_campaign("Spring sale", 20_000_000, 2_000_000_000, end_time=2_000_086_400,
                                             client_id="acme")
    assert "error" not in plan, plan
    assert plan["human_approval_required"] is True and plan["dry_run"][0]["mode"] == "local"
    write_core._write_approval("acme", plan["plan_id"])
    assert apply(plan)["status"] == "applied"
    method, url, kwargs = api.writes[-1]
    assert (method, url) == ("POST", f"{BASE}/campaigns")
    assert kwargs["json"] == {"name": "Spring sale", "start_time": 2_000_000_000, "end_time": 2_000_086_400,
                              "budget": {"daily_spend_limit_micros": 20_000_000}, "status": "paused"}
    over = openai_write.plan_create_campaign("Too much", 90_000_000, 2_000_000_000, client_id="acme")
    assert over["error"] == "budget_over_cap"


def test_an_active_creation_never_passes(api, budget_mcp):
    op = WriteOp(op_id="op1", platform="openai_ads", account_id="acct_acme", action="create_campaign",
                 method="POST", url=f"{BASE}/campaigns", financial=True, risk="medium",
                 body={"name": "x", "status": "active", "start_time": 2_000_000_000,
                       "budget": {"daily_spend_limit_micros": 1_000_000}})
    assert write_core.create_plan(ctx(), "openai_ads", [op], title="t")["error"] == "status_not_paused"
    assert api.writes == []


def test_no_grant_is_reused_and_every_write_had_one(api, mcp, monkeypatch):
    sent = []
    real = openai_write.WRITE_CLIENT.call

    def spy(method, url, **kwargs):
        if method == "POST":
            sent.append(kwargs.get("grant"))
        return real(method, url, **kwargs)

    monkeypatch.setattr(openai_write.WRITE_CLIENT, "call", spy)
    plan = openai_write.openai_ads_plan_pause("campaign", ["cmpn_1"], client_id="acme")
    assert apply(plan)["status"] == "applied"
    assert len(sent) == 1 and isinstance(sent[0], ads_common.WriteGrant) and sent[0].mac
    assert openai_write.WRITE_CLIENT.call("POST", sent[0].url, headers=openai_ads.headers_for(ctx()),
                                          grant=sent[0])["reason"] == "grant_replayed"

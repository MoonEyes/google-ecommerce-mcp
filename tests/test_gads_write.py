"""Offline tests of the Google Ads write module (gads_write): plan -> native dry-run (validateOnly) -> apply on a
fake Google Ads server, approval injected like test_write_approval, write allow-list refusals, foreign accounts,
two isolated clients, rollback round trips. Zero network (no_network, autouse), no OS keyring (fake module)."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import re
import subprocess
import sys

import pytest

from ads_fakes import FakeResponse, no_network  # noqa: F401  (autouse fixture)
from clients_fakes import ACME, BETA, setup_env, write_registry
from google_ecommerce_mcp import clients, gads_write, google_ads, write_core, write_policy
from google_ecommerce_mcp.write_core import WriteOp
from google_ecommerce_mcp.write_journal import Journal

ACME_CID, BETA_CID = "1111111111", "2222222222"
GADS_ENV = ("GADS_CUSTOMER_ID", "GADS_LOGIN_CUSTOMER_ID", "GADS_DEVELOPER_TOKEN", "GADS_TOKEN_FILE",
            "GADS_API_VERSION", "GEM_LIVE_TEST", "GADS_ALLOW_SENSITIVE_QUERIES")
_URL_RX = re.compile(r"https://googleads\.googleapis\.com/(v[0-9]+)/customers/([0-9]+)/"
                     r"(googleAds:searchStream|googleAds:mutate|campaigns:mutate|campaignBudgets:mutate)")


def policy(customer: str, **overrides) -> dict:
    base = {"enabled": True, "account_allow": [customer], "max_daily_budget_micros": 50_000_000,
            "cap_currency": "EUR", "max_increase_pct": 20, "allow_activate": False}
    base.update(overrides)
    return base


# ------------------------------------------------------------------ fake Google Ads server
class FakeGoogleAds:
    """searchStream on campaign / ad_group / ad_group_ad / campaign_budget / customer (by resource_name), and the
    three mutate services (update status / amountMicros, create budget + campaign, create negatives).
    validateOnly: checked, nothing changed, {} returned. Records (method, url, body, headers)."""

    def __init__(self) -> None:
        self.currency = {ACME_CID: "EUR", BETA_CID: "EUR"}
        self.entities: dict[str, dict] = {}
        self.calls: list[tuple[str, str, object, dict]] = []
        self.reject_validate = False
        self.fail_search = False
        self.created: list[str] = []
        self._next = 900

    # state helpers
    def campaign(self, customer, cid, status="ENABLED", budget="55"):
        self.entities[f"customers/{customer}/campaigns/{cid}"] = {
            "status": status, "campaignBudget": f"customers/{customer}/campaignBudgets/{budget}"}
        self.entities.setdefault(f"customers/{customer}/campaignBudgets/{budget}",
                                 {"status": "ENABLED", "amountMicros": 10_000_000})

    def budget(self, customer, bid, amount=10_000_000, shared=False, refs=1):
        self.entities[f"customers/{customer}/campaignBudgets/{bid}"] = {
            "status": "ENABLED", "amountMicros": amount, "explicitlyShared": shared, "referenceCount": refs}

    def status(self, name):
        return self.entities[name]["status"]

    def mutates(self):
        return [c for c in self.calls if c[1].endswith(":mutate")]

    # transport
    def request(self, method, url, **kwargs):
        body = kwargs.get("json")
        self.calls.append((method, url, copy.deepcopy(body), dict(kwargs.get("headers") or {})))
        match = _URL_RX.fullmatch(url)
        if method != "POST" or not match:
            return FakeResponse(404, {"error": {"code": 404, "message": "unexpected url " + url}})
        customer, service = match.group(2), match.group(3)
        if service == "googleAds:searchStream":
            return self._search(customer, body["query"])
        return self._mutate(customer, service.split(":", 1)[0], body)

    def _error(self, message, code="RESOURCE_NOT_FOUND"):
        return FakeResponse(400, {"error": {"code": 400, "message": "Request contains an invalid argument.",
                                            "status": "INVALID_ARGUMENT", "details": [
                                                {"errors": [{"errorCode": {"mutateError": code},
                                                             "message": message}]}]}})

    def _search(self, customer, query):
        if self.fail_search:
            return FakeResponse(401, {"error": {"code": 401, "status": "UNAUTHENTICATED", "message": "expired"}})
        resource = re.search(r"\bFROM (\w+)", query).group(1)
        money = {"customer": {"resourceName": f"customers/{customer}", "id": customer,
                              "currencyCode": self.currency[customer]}}
        if resource == "customer":
            return FakeResponse(200, [{"results": [money]}])
        name = re.search(r"resource_name = '([^']+)'", query).group(1)
        entity = self.entities.get(name)
        if entity is None or not name.startswith(f"customers/{customer}/"):
            return FakeResponse(200, [{"results": []}])
        key = {"campaign": "campaign", "ad_group": "adGroup", "ad_group_ad": "adGroupAd",
               "campaign_budget": "campaignBudget"}[resource]
        row = {"resourceName": name, "status": entity["status"]}
        if resource == "campaign_budget":
            row.update(amountMicros=str(entity["amountMicros"]), explicitlyShared=entity.get("explicitlyShared", False),
                       referenceCount=str(entity.get("referenceCount", 1)))
        if resource == "campaign":
            row.update(campaignBudget=entity["campaignBudget"], advertisingChannelType="SEARCH")
        return FakeResponse(200, [{"results": [{key: row, **money}]}])

    def _mutate(self, customer, service, body):
        validate = body.get("validateOnly") is True
        if validate and self.reject_validate:
            return self._error("The field is not valid.", "INVALID_FIELD")
        if service == "googleAds":
            operations = [(kind, op) for item in body["mutateOperations"] for kind, op in item.items()]
        else:
            kind = {"campaigns": "campaignOperation", "campaignBudgets": "campaignBudgetOperation"}[service]
            operations = [(kind, op) for op in body["operations"]]
        results = []
        for kind, op in operations:
            if "update" in op:
                name = op["update"]["resourceName"]
                if name not in self.entities or not name.startswith(f"customers/{customer}/"):
                    return self._error("Resource was not found.")
                if not validate:
                    for field in op["updateMask"].split(","):
                        camel = {"status": "status", "amount_micros": "amountMicros"}[field]
                        value = op["update"][camel]
                        self.entities[name][camel] = int(value) if camel == "amountMicros" else value
                results.append({kind.replace("Operation", "Result"): {"resourceName": name}})
            elif "create" in op and not validate:
                self._next += 1
                name = f"customers/{customer}/created/{self._next}"
                self.created.append(name)
                results.append({kind.replace("Operation", "Result"): {"resourceName": name}})
        if validate:
            return FakeResponse(200, {})
        if service == "googleAds":
            return FakeResponse(200, {"mutateOperationResponses": results})
        return FakeResponse(200, {"results": [next(iter(r.values())) for r in results]})


class FakeTokens:
    def __init__(self, token):
        self.token = token

    def headers(self):
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}


def token_of(client_id: str) -> str:
    return f"tok-{client_id}-" + "x" * 24


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def env(monkeypatch, tmp_path):
    _, path = setup_env(monkeypatch, tmp_path)
    for name in GADS_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GEM_WRITE", "1")
    monkeypatch.setenv("GEM_WRITE_HMAC_KEY", "ef" * 32)
    monkeypatch.setattr(write_core, "journal", Journal())
    monkeypatch.setattr(write_core, "_HANDLERS", {})
    monkeypatch.setattr(write_policy, "_FIELD_ALLOWLIST", {})
    monkeypatch.setattr(write_policy, "_FROZEN", False)
    monkeypatch.setattr(write_policy, "_TEST_PERMISSIVE", False)
    monkeypatch.setattr(gads_write, "_STATE", {})
    monkeypatch.setattr(google_ads, "client_tokens", lambda ctx: FakeTokens(token_of(ctx.client_id)))
    monkeypatch.setattr(google_ads, "_CURRENT_BEARER", {})
    google_ads.CLIENT.cache.clear()

    def configure(acme: dict | None = None, beta: dict | None = None) -> None:
        write_registry(path, {"acme": {**ACME, "write": {"google_ads": acme or policy(ACME_CID)}},
                              "beta": {**BETA, "write": {"google_ads": beta or policy(BETA_CID)}}})

    configure()
    server = FakeGoogleAds()
    server.configure = configure
    from mcp.server.fastmcp import FastMCP

    server.mcp = FastMCP("test-gads-write")
    server.names = gads_write.register(server.mcp)
    monkeypatch.setattr(google_ads.CLIENT, "session", server)
    monkeypatch.setattr(gads_write.write_client(), "session", server)
    yield server
    google_ads.CLIENT.cache.clear()


def ctx(client_id="acme"):
    return clients.resolve(client_id)


def apply(plan: dict, client_id="acme", **kw) -> dict:
    return write_core.apply_plan(ctx(client_id), "google_ads", plan["plan_id"],
                                 kw.get("token", plan["confirmation_token"]), kw.get("confirm", plan["confirm"]))


def ok(plan: dict) -> dict:
    assert "error" not in plan, plan
    assert plan["confirmation_token"] and all(d["ok"] and d["mode"] == "native" for d in plan["dry_run"])
    return plan


# ------------------------------------------------------------------ pause: plan -> native dry-run -> apply
def test_pause_campaign_plan_dry_run_then_apply(env):
    env.campaign(ACME_CID, "101")
    plan = ok(gads_write.gads_plan_pause_entities(level="campaign", ids=["101"], client_id="acme"))
    name = f"customers/{ACME_CID}/campaigns/101"
    assert plan["client_id"] == "acme" and plan["human_approval_required"] is False
    assert plan["ops"][0]["changes"] == ["status: ENABLED -> PAUSED"] and plan["ops"][0]["rollback"] == "auto"
    assert env.status(name) == "ENABLED"  # planning changed nothing
    [(_, url, body, _)] = env.mutates()
    assert url == f"https://googleads.googleapis.com/v26/customers/{ACME_CID}/campaigns:mutate"
    assert body == {"operations": [{"update": {"resourceName": name, "status": "PAUSED"}, "updateMask": "status"}],
                    "validateOnly": True}
    out = apply(plan)
    assert out["status"] == "applied", out
    assert env.status(name) == "PAUSED"
    sent = env.mutates()[-1]
    assert sent[2] == {"operations": [{"update": {"resourceName": name, "status": "PAUSED"},
                                       "updateMask": "status"}]}  # exactly the planned body, no validateOnly
    assert apply(plan)["error"] == "plan_already_applied"
    kinds = [e["kind"] for e in write_core.journal.entries("acme", plan_id=plan["plan_id"])]
    assert kinds == ["plan", "apply_intent", "apply_result"]


def test_every_request_is_a_read_a_dry_run_or_a_granted_write(env):
    env.campaign(ACME_CID, "101")
    apply(ok(gads_write.gads_plan_pause_entities(level="campaign", ids=["101"], client_id="acme")))
    reads = [c for c in env.calls if c[1].endswith(":searchStream")]
    assert reads and all(google_ads.ALLOW_LIST.is_allowed("POST", c[1], c[2]) for c in reads)
    dry, real = [c for c in env.mutates() if c[2].get("validateOnly") is True], \
        [c for c in env.mutates() if "validateOnly" not in c[2]]
    assert len(dry) == 1 and len(real) == 1
    assert gads_write.write_client().BLOCKED_ATTEMPTS == [] and google_ads.CLIENT.BLOCKED_ATTEMPTS == []
    for _, url, _, headers in env.calls:
        assert url.startswith(f"https://googleads.googleapis.com/v26/customers/{ACME_CID}/")
        assert headers["Authorization"] == f"Bearer {token_of('acme')}"
        assert headers["login-customer-id"] == "9999999999"  # the client's own manager, from clients.json


def test_pause_ad_group_and_ad_go_through_google_ads_mutate(env):
    env.entities[f"customers/{ACME_CID}/adGroups/7"] = {"status": "ENABLED"}
    env.entities[f"customers/{ACME_CID}/adGroupAds/7~8"] = {"status": "ENABLED"}
    for level, ident, key in (("ad_group", "7", "adGroupOperation"), ("ad", "7~8", "adGroupAdOperation")):
        plan = ok(gads_write.gads_plan_pause_entities(level=level, ids=[ident], client_id="acme"))
        url, body = env.mutates()[-1][1:3]
        assert url.endswith(f"/customers/{ACME_CID}/googleAds:mutate")
        assert list(body["mutateOperations"][0]) == [key]
        assert apply(plan)["status"] == "applied"
    assert env.status(f"customers/{ACME_CID}/adGroups/7") == "PAUSED"
    assert env.status(f"customers/{ACME_CID}/adGroupAds/7~8") == "PAUSED"


def test_pause_skips_paused_and_refuses_unknown_entities_without_planning(env):
    env.campaign(ACME_CID, "101", status="PAUSED")
    out = gads_write.gads_plan_pause_entities(level="campaign", ids=["101"], client_id="acme")
    assert out["status"] == "nothing_to_do" and env.mutates() == []
    out = gads_write.gads_plan_pause_entities(level="campaign", ids=["404"], client_id="acme")
    assert out["error"] == "not_found" and env.mutates() == []
    for bad in (["1; DROP"], ["../1"], [], ["1"] * 26):
        assert gads_write.gads_plan_pause_entities(level="campaign", ids=bad, client_id="acme")["error"] in (
            "invalid_id", "invalid_ids")
    assert gads_write.gads_plan_pause_entities(level="ad", ids=["12"], client_id="acme")["error"] == "invalid_id"


def test_failed_native_dry_run_gives_no_token(env):
    env.campaign(ACME_CID, "101")
    env.reject_validate = True
    out = gads_write.gads_plan_pause_entities(level="campaign", ids=["101"], client_id="acme")
    assert out["error"] == "dry_run_failed" and "confirmation_token" not in out
    assert "INVALID_FIELD" in json.dumps(out["dry_run"])
    assert env.status(f"customers/{ACME_CID}/campaigns/101") == "ENABLED"


def test_snapshot_read_failure_at_apply_refuses_before_any_write(env):
    env.campaign(ACME_CID, "101")
    plan = ok(gads_write.gads_plan_pause_entities(level="campaign", ids=["101"], client_id="acme"))
    env.fail_search = True
    before = len(env.mutates())
    assert apply(plan)["error"] == "snapshot_failed"
    assert len(env.mutates()) == before


# ------------------------------------------------------------------ budget: financial, approval, caps
def test_budget_change_needs_human_approval_then_applies(env):
    env.budget(ACME_CID, "55", 10_000_000)
    plan = ok(gads_write.gads_plan_budget_change(daily_budget_micros=11_500_000, budget_id="55", client_id="acme"))
    assert plan["human_approval_required"] is True and plan["risks"]["financial"] is True
    assert plan["ops"][0]["changes"] == ["amountMicros: 10000000 -> 11500000"]
    name = f"customers/{ACME_CID}/campaignBudgets/55"
    assert apply(plan)["error"] == "approval_required" and env.entities[name]["amountMicros"] == 10_000_000
    assert write_core._write_approval("acme", plan["plan_id"], approver="test")["status"] == "approved"
    out = apply(plan)
    assert out["status"] == "applied", out
    assert env.entities[name]["amountMicros"] == 11_500_000
    assert env.mutates()[-1][2] == {"operations": [{"update": {"resourceName": name, "amountMicros": "11500000"},
                                                    "updateMask": "amount_micros"}]}


def test_budget_change_by_campaign_reads_its_budget(env):
    env.campaign(ACME_CID, "101", budget="77")
    plan = ok(gads_write.gads_plan_budget_change(daily_budget_micros=9_000_000, campaign_id="101",
                                                 client_id="acme"))
    assert f"campaignBudgets/77" in json.dumps(env.mutates()[-1][2])
    assert plan["ops"][0]["risk"] == "medium"  # a decrease


@pytest.mark.parametrize("amount, code", [(13_000_000, "increase_too_large"), (60_000_000, "budget_over_cap")])
def test_budget_locks_refuse_before_any_dry_run(env, amount, code):
    env.budget(ACME_CID, "55", 10_000_000)
    out = gads_write.gads_plan_budget_change(daily_budget_micros=amount, budget_id="55", client_id="acme")
    assert out["error"] == code or code in {v["error"] for v in out.get("violations", [])}, out
    assert env.mutates() == [] and "confirmation_token" not in out


def test_budget_in_another_currency_than_the_cap_is_refused(env):
    env.currency[ACME_CID] = "USD"
    env.budget(ACME_CID, "55", 10_000_000)
    out = gads_write.gads_plan_budget_change(daily_budget_micros=10_500_000, budget_id="55", client_id="acme")
    assert out["error"] == "currency_mismatch" and env.mutates() == []


def test_budget_input_is_validated_without_network(env):
    for kwargs in ({"daily_budget_micros": 0, "budget_id": "55"}, {"daily_budget_micros": 5, "budget_id": "55",
                                                                    "campaign_id": "1"},
                   {"daily_budget_micros": 5}, {"daily_budget_micros": True, "budget_id": "55"}):
        assert "error" in gads_write.gads_plan_budget_change(client_id="acme", **kwargs)
    assert env.calls == []


# ------------------------------------------------------------------ switches and accounts
def test_write_switch_off_means_no_request_at_all(env, monkeypatch):
    env.campaign(ACME_CID, "101")
    monkeypatch.delenv("GEM_WRITE")
    assert gads_write.gads_plan_pause_entities(level="campaign", ids=["101"], client_id="acme")["error"] == \
        "write_disabled"
    monkeypatch.setenv("GEM_WRITE", "1")
    env.configure(acme=policy(ACME_CID, enabled=False))
    assert gads_write.gads_plan_pause_entities(level="campaign", ids=["101"], client_id="acme")["error"] == \
        "write_not_enabled"
    assert env.calls == []


def test_foreign_customer_is_refused_without_any_request(env):
    env.campaign(BETA_CID, "201")
    out = gads_write.gads_plan_pause_entities(level="campaign", ids=["201"], customer_id=BETA_CID,
                                              client_id="acme")
    assert out["error"] == "id_not_owned" and out["client_id"] == "acme"
    out = gads_write.gads_plan_budget_change(daily_budget_micros=1_000_000, budget_id="55",
                                             customer_id="999-999-9999", client_id="acme")  # acme's manager
    assert out["error"] == "id_not_owned"
    assert env.calls == []


def test_an_op_for_another_clients_account_is_refused_by_the_plan(env):
    env.campaign(BETA_CID, "201")
    op = gads_write.status_op(BETA_CID, "campaign", "201", "PAUSED", op_id="op1")
    out = write_core.create_plan(ctx("acme"), "google_ads", [op], title="sneaky")
    assert out["error"] in ("account_not_allowed", "account_not_owned") and env.calls == []
    # the handler refuses on its own too (defence in depth): no request
    assert gads_write.dry_run(ctx("acme"), op)["ok"] is False
    with pytest.raises(gads_write.SnapshotError):
        gads_write.snapshot(ctx("acme"), op)
    assert env.calls == []


def test_two_clients_stay_isolated(env):
    env.campaign(ACME_CID, "101")
    env.campaign(BETA_CID, "201", budget="66")
    plan_a = ok(gads_write.gads_plan_pause_entities(level="campaign", ids=["101"], client_id="acme"))
    plan_b = ok(gads_write.gads_plan_pause_entities(level="campaign", ids=["201"], client_id="beta"))
    # a plan, its token or its confirm string never cross clients
    assert apply(plan_b, client_id="acme")["error"] == "plan_not_found"
    assert "error" in apply({**plan_a, "confirmation_token": plan_b["confirmation_token"]})
    assert apply(plan_a)["status"] == "applied" and apply(plan_b, client_id="beta")["status"] == "applied"
    for _, url, body, headers in env.calls:
        customer = _URL_RX.fullmatch(url).group(2)
        owner = "acme" if customer == ACME_CID else "beta"
        assert headers["Authorization"] == f"Bearer {token_of(owner)}"
        other = BETA_CID if owner == "acme" else ACME_CID
        assert other not in json.dumps(body)
    assert {e["plan_id"] for e in write_core.journal.entries("acme")} == {plan_a["plan_id"]}
    assert {e["plan_id"] for e in write_core.journal.entries("beta")} == {plan_b["plan_id"]}


def test_legacy_mode_has_no_write(env, monkeypatch, tmp_path):
    monkeypatch.setenv("GEM_CLIENTS_FILE", str(tmp_path / "absent.json"))
    monkeypatch.setenv("GADS_CUSTOMER_ID", "333-333-3333")
    out = gads_write.gads_plan_pause_entities(level="campaign", ids=["101"])
    assert out["error"] == "write_not_enabled" and env.calls == []


# ------------------------------------------------------------------ write allow-list
def test_write_allow_list_rules_are_pinned_and_without_joker(env):
    rules = gads_write.write_client().write_allow_list.rules
    assert len(rules) == 6 and {k for _, _, k in rules} == {"validate", "write"}
    for method, pattern, _ in rules:
        assert method == "POST" and ".*" not in pattern and "|" not in pattern
        assert pattern.startswith(r"https://googleads\.googleapis\.com/v26/customers/[0-9]{10}/")
        assert pattern.endswith((r"/googleAds:mutate", r"/campaigns:mutate", r"/campaignBudgets:mutate"))
    # the read allow-list stays read-only
    assert not any("mutate" in p for _, p in google_ads.ALLOW_LIST.patterns)


@pytest.mark.parametrize("url, body, reason", [
    (f"https://googleads.googleapis.com/v26/customers/{ACME_CID}/adGroups:mutate",
     {"operations": [{"update": {"resourceName": "x", "status": "PAUSED"}}], "validateOnly": True},
     "not_on_write_allow_list"),
    (f"https://googleads.googleapis.com/v25/customers/{ACME_CID}/campaigns:mutate",
     {"operations": [{"update": {"resourceName": "x"}}], "validateOnly": True}, "not_on_write_allow_list"),
    (f"https://googleads.googleapis.com/v26/customers/{ACME_CID}/campaigns:mutate",
     {"operations": [{"remove": f"customers/{ACME_CID}/campaigns/1"}], "validateOnly": True},
     "not_on_write_allow_list"),
    (f"https://googleads.googleapis.com/v26/customers/{ACME_CID}/campaigns:mutate",
     {"operations": [{"update": {"resourceName": "x", "status": "PAUSED"}}], "validateOnly": 1},
     "not_on_write_allow_list"),
    (f"https://googleads.googleapis.com/v26/customers/{ACME_CID}/googleAds:mutate",
     {"mutateOperations": [{"customerOperation": {"update": {"resourceName": "x"}}}], "validateOnly": True},
     "not_on_write_allow_list"),
    (f"https://googleads.googleapis.com/v26/customers/{ACME_CID}/campaigns:mutate",
     {"operations": [{"update": {"resourceName": "x", "status": "PAUSED"}, "updateMask": "status"}]},
     "grant_missing"),
    (f"https://googleads.googleapis.com/v26/customers/{ACME_CID}/campaigns:mutate?validateOnly=true",
     {"operations": [{"update": {"resourceName": "x", "status": "PAUSED"}, "updateMask": "status"}]},
     "grant_missing"),
])
def test_requests_off_the_write_allow_list_are_blocked(env, url, body, reason):
    out = gads_write.write_client().call("POST", url, headers={"Authorization": "Bearer t"}, json=body)
    assert out["error"] == "blocked" and out["reason"] == reason, out
    assert env.calls == []


def test_dry_run_rule_needs_the_real_flag_and_validates_only(env):
    url = f"https://googleads.googleapis.com/v26/customers/{ACME_CID}/campaigns:mutate"
    env.campaign(ACME_CID, "101")
    body = {"operations": [{"update": {"resourceName": f"customers/{ACME_CID}/campaigns/101", "status": "PAUSED"},
                            "updateMask": "status"}], "validateOnly": True}
    assert gads_write.write_client().call("POST", url, json=body) == {}
    assert env.status(f"customers/{ACME_CID}/campaigns/101") == "ENABLED"


# ------------------------------------------------------------------ rollback round trips
def test_rollback_of_a_pause_needs_allow_activate(env):
    env.campaign(ACME_CID, "101")
    plan = ok(gads_write.gads_plan_pause_entities(level="campaign", ids=["101"], client_id="acme"))
    assert apply(plan)["status"] == "applied"
    out = write_core.rollback_plan(ctx(), "google_ads", plan["plan_id"])
    assert out["error"] == "activation_not_allowed"


def test_rollback_round_trip_pause_then_re_enable(env):
    env.configure(acme=policy(ACME_CID, allow_activate=True))
    env.campaign(ACME_CID, "101")
    name = f"customers/{ACME_CID}/campaigns/101"
    plan = ok(gads_write.gads_plan_pause_entities(level="campaign", ids=["101"], client_id="acme"))
    assert apply(plan)["status"] == "applied" and env.status(name) == "PAUSED"
    back = ok(write_core.rollback_plan(ctx(), "google_ads", plan["plan_id"]))
    assert back["rollback_of"] == plan["plan_id"] and back["ops"][0]["action"] == "set_status"
    assert back["ops"][0]["financial"] is True and back["human_approval_required"] is True
    assert env.mutates()[-1][2]["validateOnly"] is True and env.status(name) == "PAUSED"
    write_core._write_approval("acme", back["plan_id"])
    assert apply(back)["status"] == "applied" and env.status(name) == "ENABLED"
    again = ok(write_core.rollback_plan(ctx(), "google_ads", back["plan_id"]))  # and back again: a pause
    assert again["ops"][0]["action"] == "pause"
    assert apply(again)["status"] == "applied" and env.status(name) == "PAUSED"


def test_rollback_round_trip_budget(env):
    env.budget(ACME_CID, "55", 10_000_000)
    name = f"customers/{ACME_CID}/campaignBudgets/55"
    plan = ok(gads_write.gads_plan_budget_change(daily_budget_micros=11_000_000, budget_id="55", client_id="acme"))
    write_core._write_approval("acme", plan["plan_id"])
    assert apply(plan)["status"] == "applied" and env.entities[name]["amountMicros"] == 11_000_000
    back = ok(write_core.rollback_plan(ctx(), "google_ads", plan["plan_id"]))
    assert back["ops"][0]["changes"] == ["amountMicros: 11000000 -> 10000000"]
    write_core._write_approval("acme", back["plan_id"])
    assert apply(back)["status"] == "applied" and env.entities[name]["amountMicros"] == 10_000_000


# ------------------------------------------------------------------ negatives and campaign creation (P1)
def test_negatives_body_shape(env):
    op = gads_write.negatives_op(ACME_CID, ["free", "cheap shoes"], "PHRASE", op_id="op1", campaign_id="101")
    assert op.url == f"https://googleads.googleapis.com/v26/customers/{ACME_CID}/googleAds:mutate"
    assert op.body["mutateOperations"][1] == {"campaignCriterionOperation": {"create": {
        "campaign": f"customers/{ACME_CID}/campaigns/101", "negative": True,
        "keyword": {"text": "cheap shoes", "matchType": "PHRASE"}}}}
    shared = gads_write.negatives_op(ACME_CID, ["free"], "EXACT", op_id="op1", shared_set_id="42")
    assert shared.body["mutateOperations"][0]["sharedCriterionOperation"]["create"]["sharedSet"] == \
        f"customers/{ACME_CID}/sharedSets/42"
    assert not op.financial and op.rollback == "none" and not write_policy.is_remove(op)
    assert write_policy._field_violations(op) == [] and write_policy._field_violations(shared) == []


def test_negatives_input_is_validated_without_network(env):
    for kwargs in ({"keywords": [], "campaign_id": "1"}, {"keywords": ["a" * 81], "campaign_id": "1"},
                   {"keywords": ["a"]}, {"keywords": ["a"], "campaign_id": "1", "shared_set_id": "2"},
                   {"keywords": ["one two three four five six seven eight nine ten eleven"], "campaign_id": "1"},
                   {"keywords": ["bad\x1btext"], "campaign_id": "1"}):
        assert "error" in gads_write.gads_plan_add_negatives(client_id="acme", **kwargs), kwargs
    assert env.calls == []


def test_create_campaign_body_is_paused_search_without_bidding(env):
    op = gads_write.create_campaign_op(ACME_CID, "Brand FR", 5_000_000, op_id="op1",
                                       before={"account_id": ACME_CID, "currency": "EUR"})
    budget, campaign = (next(iter(i.values()))["create"] for i in op.body["mutateOperations"])
    assert campaign["status"] == "PAUSED" and campaign["advertisingChannelType"] == "SEARCH"
    assert campaign["campaignBudget"] == budget["resourceName"] == f"customers/{ACME_CID}/campaignBudgets/-1"
    assert budget["explicitlyShared"] is False and budget["amountMicros"] == "5000000"
    assert op.financial and write_policy._field_violations(op) == []
    # the one bidding setting: a fixed EMPTY manualCpc (Google requires a strategy); no bid amount, no eCPC
    assert campaign["manualCpc"] == {}
    assert write_policy._bidding(*write_policy._without_fixed_manual_cpc(op, write_policy._payload(op))) == []
    assert "bidding_not_supported" not in _bidding_codes(op)


def _campaign_create(op):
    return op.body["mutateOperations"][1]["campaignOperation"]["create"]


def _bidding_codes(op):
    from test_write_core import FakeCtx, open_policy
    return {v["error"] for v in write_policy.check_ops(FakeCtx(), "google_ads", [op], open_policy())}


@pytest.mark.parametrize("injected", [
    {"manualCpc": {"enhancedCpcEnabled": True}},
    {"manualCpc": {}, "maximizeConversions": {}},
    {"manualCpc": {}, "targetCpa": {"targetCpaMicros": "1000000"}},
    {"manualCpc": {"cpcBidMicros": "1000000"}},
])
def test_create_campaign_refuses_any_other_bidding_field(env, injected):
    op = gads_write.create_campaign_op(ACME_CID, "Brand FR", 5_000_000, op_id="op1",
                                       before={"account_id": ACME_CID, "currency": "EUR"})
    _campaign_create(op).update(injected)
    assert "bidding_not_supported" in _bidding_codes(op)


def test_empty_manual_cpc_is_refused_outside_create_campaign(env):
    op = gads_write.create_campaign_op(ACME_CID, "Brand FR", 5_000_000, op_id="op1",
                                       before={"account_id": ACME_CID, "currency": "EUR"})
    op = dataclasses.replace(op, action="budget_change")
    assert "bidding_not_supported" in _bidding_codes(op)
    twice = gads_write.create_campaign_op(ACME_CID, "Brand FR", 5_000_000, op_id="op1",
                                          before={"account_id": ACME_CID, "currency": "EUR"})
    twice.body["mutateOperations"][0]["campaignBudgetOperation"]["create"]["manualCpc"] = {}
    assert "bidding_not_supported" in _bidding_codes(twice)


def test_dry_run_error_names_the_missing_field_path():
    resp = {"error": "api_error", "detail": {"details": [{"errors": [{
        "errorCode": {"fieldError": "REQUIRED"}, "message": "The required field was not present.",
        "location": {"fieldPathElements": [{"fieldName": "mutate_operations", "index": 1},
                                           {"fieldName": "campaign_bidding_strategy"}]}}]}]}}
    assert "mutate_operations.campaign_bidding_strategy" in gads_write._messages(resp)[0]


# write_policy exempts Google Ads negative keywords and a budget created with a PAUSED campaign from the PAUSED
# rule (_gads_status_exempt); the bypass attempts are in tests/test_write_policy_security.py.
def test_add_negatives_full_flow(env):
    env.campaign(ACME_CID, "101")
    plan = ok(gads_write.gads_plan_add_negatives(keywords=["free"], campaign_id="101", client_id="acme"))
    assert apply(plan)["status"] == "applied" and env.created


def test_create_campaign_full_flow(env):
    plan = ok(gads_write.gads_plan_create_campaign(name="Brand FR", daily_budget_micros=5_000_000,
                                                   client_id="acme"))
    write_core._write_approval("acme", plan["plan_id"])
    assert apply(plan)["status"] == "applied" and len(env.created) == 2


def test_a_positive_criterion_or_a_lone_budget_is_still_refused_not_sent(env):
    env.campaign(ACME_CID, "101")
    positive = gads_write.negatives_op(ACME_CID, ["free"], "EXACT", op_id="op1", campaign_id="101")
    positive.body["mutateOperations"][0]["campaignCriterionOperation"]["create"]["negative"] = False
    lone = gads_write.create_campaign_op(ACME_CID, "Brand FR", 5_000_000, op_id="op1")
    lone.body["mutateOperations"].pop()  # the budget without its paused campaign
    c = clients.resolve("acme")
    for op in (positive, lone):
        out = write_core.create_plan(c, "google_ads", [op], title="t")
        assert "confirmation_token" not in out
        assert "status_missing" in {v["error"] for v in out.get("violations", [out])}, out
    assert env.mutates() == []


# ------------------------------------------------------------------ registration
def test_plan_tools_are_read_only_and_take_client_id(env):
    tools = {t.name: t for t in asyncio.run(env.mcp.list_tools())}
    assert set(tools) == {"gads_plan_pause_entities", "gads_plan_budget_change", "gads_plan_add_negatives",
                          "gads_plan_create_campaign"} == set(env.names)
    for tool in tools.values():
        ann = tool.annotations
        assert ann.readOnlyHint is True and ann.destructiveHint is False and tool.title and tool.description
        props = tool.inputSchema["properties"]
        assert "client_id" in props and "client_id" not in tool.inputSchema.get("required", [])
        assert all(p.get("description") for p in props.values())
    assert write_core.get_handler("google_ads") is gads_write.HANDLER


def test_field_allow_lists_registered_by_register(env):
    snap = write_policy.golden_allowlists()["google_ads"]
    assert set(snap) == {"pause", "set_status", "budget_change", "add_negatives", "create_campaign"}
    assert snap["budget_change"] == {"fields": ["operations.update.amountmicros", "operations.update.resourcename",
                                                "operations.updatemask"], "money_ok": []}
    assert all(not entry["money_ok"] for action, entry in snap.items() if action != "create_campaign")
    assert set(snap["create_campaign"]["money_ok"]) == {
        "mutateoperations.campaignbudgetoperation.create.amountmicros",
        "mutateoperations.campaignbudgetoperation.create.deliverymethod",
        "mutateoperations.campaignbudgetoperation.create.explicitlyshared",
        "mutateoperations.campaignbudgetoperation.create.name",
        "mutateoperations.campaignbudgetoperation.create.resourcename",
        "mutateoperations.campaignoperation.create.campaignbudget",
        "mutateoperations.campaignoperation.create.manualcpc"}
    gads_write.register(env.mcp.__class__("again"))  # identical lists again: a no-op
    write_policy.freeze_allowlists()
    try:
        gads_write._register_allowlists()  # still a no-op once frozen
    finally:
        write_policy._unfreeze_for_tests()


def test_importing_the_module_registers_nothing():
    code = ("import google_ecommerce_mcp.gads_write as g, google_ecommerce_mcp.write_policy as w, "
            "google_ecommerce_mcp.write_core as c; "
            "print(w.golden_allowlists(), c.get_handler('google_ads'), g._STATE)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "{} None {}"


def test_invert_restores_from_the_fresh_snapshot():
    op = gads_write.status_op(ACME_CID, "ad", "7~8", "PAUSED", op_id="op1")
    inverse = gads_write.invert(op, {"status": "ENABLED"})
    assert inverse.action == "set_status" and inverse.financial and "ENABLED" in json.dumps(inverse.body)
    assert gads_write.invert(op, {"status": "PAUSED"}) is None
    assert gads_write.invert(op, {"status": "REMOVED"}) is None
    budget = gads_write.budget_op(ACME_CID, "55", 2_000_000, op_id="b1")
    assert gads_write.invert(budget, {"amount_micros": 1_000_000}).body["operations"][0]["update"][
        "amountMicros"] == "1000000"
    created = gads_write.create_campaign_op(ACME_CID, "x", 1_000_000, op_id="c1")
    assert gads_write.invert(created, {"account_id": ACME_CID}) is None
    assert isinstance(op, WriteOp)

"""Offline tests of the Meta Ads write path (meta_write) and the Meta entity lists (meta_ads): plan -> native
dry-run (validate_only) -> apply on a fake Graph server, refusals off the write allow-list, foreign accounts, two
isolated clients, rollback, minor units (JPY 0 decimals), no network, no token in any URL."""

from __future__ import annotations

import asyncio
import importlib
import json
import re
from urllib.parse import urlsplit

import pytest

from ads_fakes import FakeResponse, no_network  # noqa: F401  (autouse fixture: zero real network)
from clients_fakes import ACME, BETA, setup_env, write_registry
from google_ecommerce_mcp import ads_common, clients, meta_ads, meta_write, write_core, write_policy
from google_ecommerce_mcp.write_core import WriteOp
from google_ecommerce_mcp.write_journal import Journal

V = "https://graph.facebook.com/v26.0"
# Fake tokens assembled at run time (no token-shaped literal in the source).
TOKEN_A = "EA" + "A" + "acme" + "a" * 40
TOKEN_B = "EA" + "A" + "beta" + "b" * 40
TOKEN_N = "EA" + "A" + "nipp" + "n" * 40
TOKENS = {"acme": TOKEN_A, "beta": TOKEN_B, "nippon": TOKEN_N}
CAP_EUR = 100_000_000  # 100 EUR per day per entity, in micros

NIPPON = {"label": "Nippon KK", "accounts": {"meta": {"ad_account_id": "act_3333"}}}


def meta_policy(account: str, **extra):
    policy = {"enabled": True, "account_allow": [account], "max_daily_budget_micros": CAP_EUR,
              "cap_currency": "EUR"}
    policy.update(extra)
    return {"meta": policy}


class FakeGraph:
    """A tiny Graph API: accounts, campaigns / ad sets / ads / creatives, per-token visibility. Records every
    request and fails the test on any token in a URL, any query string on a POST, or a write without the
    Bearer header."""

    FIELDS = {"campaign": {"id", "account_id", "status", "effective_status", "daily_budget", "lifetime_budget",
                           "objective", "name"},
              "adset": {"id", "account_id", "campaign_id", "status", "effective_status", "daily_budget",
                        "lifetime_budget", "name"},
              "ad": {"id", "account_id", "campaign_id", "adset_id", "status", "effective_status", "name"},
              "creative": {"id", "account_id", "name"}}

    def __init__(self):
        self.accounts = {"1111": "EUR", "2221": "EUR", "3333": "JPY"}
        # acme's token is an agency system user that can also SEE beta's account: only the policy stops it.
        self.visible = {TOKEN_A: {"1111", "2221"}, TOKEN_B: {"2221"}, TOKEN_N: {"3333"}}
        self.entities: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.next_id = 900_000
        self.fail_validate = False

    def add(self, eid, kind, account, **fields):
        self.entities[eid] = {"id": eid, "kind": kind, "account_id": account, **fields}
        return self.entities[eid]

    # -------------------------------------------------- transport
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        headers = kwargs.get("headers") or {}
        auth = headers.get("Authorization", "")
        for token in TOKENS.values():
            assert token not in url, "token in URL"
            assert token not in json.dumps(kwargs.get("params") or {}), "token in params"
            assert token not in json.dumps(kwargs.get("json") or {}), "token in body"
        assert "access_token" not in url and "access_token" not in json.dumps(kwargs.get("params") or {})
        assert auth.startswith("Bearer "), "no Bearer header"
        token = auth[len("Bearer "):]
        parts = urlsplit(url)
        assert parts.netloc == "graph.facebook.com"
        if method == "POST":
            assert not parts.query and not kwargs.get("params"), "POST with a query string or params"
        path = parts.path.split("/")[2:]  # drop "" and the version
        if method == "GET":
            return self._get(token, path, kwargs.get("params") or {})
        if method == "POST":
            return self._post(token, path, kwargs.get("json"))
        return self._error(400, "unsupported method")

    @staticmethod
    def _error(status, message, code=100, subcode=None):
        error = {"message": message, "code": code, "type": "OAuthException"}
        if subcode is not None:
            error["error_subcode"] = subcode
        return FakeResponse(status, {"error": error})

    def _get(self, token, path, params):
        fields = set(str(params.get("fields", "id")).split(","))
        if len(path) == 1 and path[0].startswith("act_"):
            account = path[0][4:]
            if account not in self.visible.get(token, set()):
                return self._error(400, "Unsupported get request")
            return FakeResponse(200, {"id": f"act_{account}", "account_id": account,
                                      "currency": self.accounts[account], "timezone_name": "Europe/Paris"})
        if len(path) == 2 and path[0].startswith("act_"):
            account, edge = path[0][4:], path[1]
            if account not in self.visible.get(token, set()):
                return self._error(400, "Unsupported get request")
            kind = {"campaigns": "campaign", "adsets": "adset", "ads": "ad"}[edge]
            rows = [{k: v for k, v in e.items() if k != "kind"} for e in self.entities.values()
                    if e["kind"] == kind and e["account_id"] == account]
            return FakeResponse(200, {"data": rows, "paging": {"cursors": {"after": "X"}}})
        if len(path) == 1:
            entity = self.entities.get(path[0])
            if entity is None or entity["account_id"] not in self.visible.get(token, set()):
                return self._error(400, f"Object with ID '{path[0]}' does not exist", subcode=33)
            unknown = fields - self.FIELDS[entity["kind"]]
            if unknown:
                return self._error(400, f"(#100) Tried accessing nonexisting field {sorted(unknown)}")
            return FakeResponse(200, {k: v for k, v in entity.items() if k in fields})
        return self._error(400, "unknown path")

    def _post(self, token, path, body):
        assert isinstance(body, dict)
        validate = body.get("execution_options") == ["validate_only"]
        if validate and self.fail_validate:
            return self._error(400, "(#100) Invalid parameter: daily_budget too low")
        if len(path) == 2 and path[0].startswith("act_"):
            account, edge = path[0][4:], path[1]
            if account not in self.visible.get(token, set()):
                return self._error(400, "no access")
            if validate:
                return FakeResponse(200, {"success": True})
            self.next_id += 1
            eid = str(self.next_id)
            kind = {"campaigns": "campaign", "adsets": "adset", "ads": "ad"}[edge]
            fields = {k: v for k, v in body.items()}
            self.add(eid, kind, account, **fields)
            return FakeResponse(200, {"id": eid})
        if len(path) == 1:
            entity = self.entities.get(path[0])
            if entity is None or entity["account_id"] not in self.visible.get(token, set()):
                return self._error(400, "no access")
            if validate:
                return FakeResponse(200, {"success": True})
            for key, value in body.items():
                entity[key] = str(value) if key == "daily_budget" else value
            return FakeResponse(200, {"success": True})
        return self._error(400, "unknown path")

    # -------------------------------------------------- views
    def writes(self):
        """POSTs that really change something (no validate_only)."""
        return [c for c in self.calls if c[0] == "POST" and (c[2].get("json") or {}).get("execution_options")
                is None]

    def validations(self):
        return [c for c in self.calls if c[0] == "POST"
                and (c[2].get("json") or {}).get("execution_options") == ["validate_only"]]


@pytest.fixture
def graph(monkeypatch, tmp_path):
    keyring, path = setup_env(monkeypatch, tmp_path)
    for name in ("META_ACCESS_TOKEN", "META_AD_ACCOUNT_ID", "META_API_VERSION", "GEM_LIVE_TEST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GEM_WRITE", "1")
    monkeypatch.setenv("GEM_WRITE_HMAC_KEY", "ab" * 32)
    for cid, token in TOKENS.items():
        monkeypatch.setenv(f"GEM_SECRET_{cid.upper()}_META", token)
    write_registry(path, {"acme": dict(ACME, write=meta_policy("act_1111")),
                          "beta": dict(BETA, write=meta_policy("2221")),
                          "nippon": dict(NIPPON, write=meta_policy("3333", cap_currency="JPY",
                                                                    max_daily_budget_micros=10_000 * 1_000_000))})
    monkeypatch.setattr(write_core, "journal", Journal())
    monkeypatch.setattr(write_core, "_HANDLERS", {})
    monkeypatch.setattr(write_policy, "_TEST_PERMISSIVE", False, raising=False)
    ads_common._forget_consumed_grants()
    register()
    fake = FakeGraph()
    fake.add("101", "campaign", "1111", status="ACTIVE", effective_status="ACTIVE", objective="OUTCOME_SALES",
             name="Acme sales")
    fake.add("201", "adset", "1111", campaign_id="101", status="ACTIVE", effective_status="ACTIVE",
             daily_budget="5000", lifetime_budget="0", name="Acme FR")
    fake.add("202", "adset", "1111", campaign_id="101", status="ACTIVE", lifetime_budget="90000", name="Lifetime")
    fake.add("203", "campaign", "1111", status="ACTIVE", objective="OUTCOME_SALES", name="CBO-less")
    fake.add("301", "ad", "1111", campaign_id="101", adset_id="201", status="ACTIVE", name="Ad 1")
    fake.add("401", "creative", "1111", name="Creative")
    fake.add("909", "campaign", "2221", status="ACTIVE", objective="OUTCOME_SALES", name="Beta campaign")
    fake.add("921", "adset", "2221", campaign_id="909", status="ACTIVE", daily_budget="5000", name="Beta FR")
    fake.add("931", "adset", "3333", campaign_id="930", status="ACTIVE", daily_budget="5000", name="Tokyo")
    fake.add("932", "adset", "3333", campaign_id="930", status="ACTIVE", daily_budget="9000", name="Osaka")
    monkeypatch.setattr(meta_write.WRITE_CLIENT, "session", fake)
    monkeypatch.setattr(meta_ads.LIST_CLIENT, "session", fake)
    meta_write.WRITE_CLIENT.BLOCKED_ATTEMPTS.clear()
    meta_ads.LIST_CLIENT.cache.clear()
    return fake


def register():
    from mcp.server.fastmcp import FastMCP

    return meta_write.register(FastMCP("test-meta-write"))


def ok(result):
    assert "error" not in result, result
    return result


def apply(plan, client_id=None, **kw):
    cid = client_id or plan["client_id"]
    return write_core._apply_tool("meta_apply", cid, plan["plan_id"], kw.get("token", plan["confirmation_token"]),
                                  kw.get("confirm", plan["confirm"]))


def approve(plan):
    assert write_core._write_approval(plan["client_id"], plan["plan_id"], approver="test")["status"] == "approved"


def assert_clean_requests(fake):
    for method, url, kwargs in fake.calls:
        assert "?" not in url, url
        for token in TOKENS.values():
            assert token not in url


# ------------------------------------------------------------------ P0: pause
def test_pause_plan_dry_runs_locally_then_applies(graph):
    plan = ok(meta_write.meta_plan_pause("campaign", ("101",), client_id="acme"))
    assert plan["platform"] == "meta" and plan["apply_tool"] == "meta_apply" and plan["client_id"] == "acme"
    assert plan["dry_run"][0]["mode"] == "local" and plan["dry_run"][0]["ok"] is True
    assert plan["human_approval_required"] is False and plan["ops"][0]["rollback"] == "auto"
    assert any("status: ACTIVE -> PAUSED" in c for c in plan["ops"][0]["changes"])
    # an update is never sent at plan time, not even with validate_only
    assert [c for c in graph.calls if c[0] == "POST"] == []
    assert graph.entities["101"]["status"] == "ACTIVE"

    out = apply(plan)
    assert out["status"] == "applied", out
    (write,) = graph.writes()
    assert write[1] == f"{V}/101" and write[2]["json"] == {"status": "PAUSED"}
    assert write[2]["headers"]["Authorization"] == f"Bearer {TOKEN_A}" and "params" not in write[2]
    assert graph.entities["101"]["status"] == "PAUSED"
    assert apply(plan)["error"] == "plan_already_applied"
    assert_clean_requests(graph)
    kinds = [e["kind"] for e in write_core.journal.entries("acme", plan_id=plan["plan_id"])]
    assert kinds == ["plan", "apply_intent", "apply_result"]


def test_pause_of_several_ads_and_already_paused_entity(graph):
    graph.add("302", "ad", "1111", campaign_id="101", adset_id="201", status="PAUSED", name="Ad 2")
    plan = ok(meta_write.meta_plan_pause("ad", ("301", "302", "301"), client_id="acme"))
    assert [o["op_id"] for o in plan["ops"]] == ["pause-ad-301", "pause-ad-302"]
    assert "already PAUSED" in plan["ops"][1]["summary"]
    assert apply(plan)["status"] == "applied"
    assert graph.entities["301"]["status"] == "PAUSED"


def test_pause_of_an_archived_entity_or_bad_input_is_refused_without_writing(graph):
    graph.add("305", "ad", "1111", campaign_id="101", adset_id="201", status="ARCHIVED", name="Old")
    assert meta_write.meta_plan_pause("ad", ("305",), client_id="acme")["error"] == "entity_not_pausable"
    assert meta_write.meta_plan_pause("ad", ("30a",), client_id="acme")["error"] == "invalid_argument"
    assert meta_write.meta_plan_pause("ad", (), client_id="acme")["error"] == "invalid_argument"
    assert meta_write.meta_plan_pause("campaign", ("777",), client_id="acme")["error"] == "not_available"
    # a campaign id given as an ad set: the level-specific fields make Meta refuse the read
    assert "error" in meta_write.meta_plan_pause("adset", ("101",), client_id="acme")
    assert graph.writes() == [] and graph.validations() == []


def test_write_switches_refuse_before_any_request(graph, monkeypatch):
    monkeypatch.setenv("GEM_WRITE", "0")
    assert meta_write.meta_plan_pause("campaign", ("101",), client_id="acme")["error"] == "write_disabled"
    assert meta_write.meta_plan_budget_change("adset", "201", 5500, client_id="acme")["error"] == "write_disabled"
    assert graph.calls == []


# ------------------------------------------------------------------ P0: daily budget (minor units, financial)
def test_budget_change_is_financial_needs_a_human_and_applies_in_minor_units(graph):
    plan = ok(meta_write.meta_plan_budget_change("adset", "201", 5500, client_id="acme"))
    assert plan["risks"]["financial"] is True and plan["human_approval_required"] is True
    assert "5000 minor units = 50 EUR -> 5500 minor units = 55 EUR" in plan["ops"][0]["summary"]
    assert plan["dry_run"][0]["mode"] == "local" and [c for c in graph.calls if c[0] == "POST"] == []
    refused = apply(plan)
    assert refused["error"] == "approval_required" and graph.writes() == []
    preview = write_core.approval_preview("acme", plan["plan_id"])
    assert preview["budget_totals_micros"] == {"EUR": 55_000_000}
    approve(plan)
    assert apply(plan)["status"] == "applied"
    (write,) = graph.writes()
    assert write[2]["json"] == {"daily_budget": 5500} and graph.entities["201"]["daily_budget"] == "5500"


def test_budget_over_the_increase_limit_or_the_cap_is_refused_before_the_dry_run(graph):
    over = meta_write.meta_plan_budget_change("adset", "201", 6500, client_id="acme")  # +30 % in 24 h
    assert over["error"] == "increase_too_large"
    graph.entities["201"]["daily_budget"] = "9500"
    capped = meta_write.meta_plan_budget_change("adset", "201", 10_500, client_id="acme")  # 105 EUR > 100 EUR
    assert capped["error"] == "budget_over_cap"
    assert graph.validations() == [] and graph.writes() == []


def test_lifetime_or_missing_daily_budget_is_explained(graph):
    assert meta_write.meta_plan_budget_change("adset", "202", 5000, client_id="acme")["error"] == \
        "lifetime_budget_not_supported"
    out = meta_write.meta_plan_budget_change("campaign", "203", 5000, client_id="acme")
    assert out["error"] == "no_daily_budget" and "ad sets" in out["fix"]
    for bad in (0, -5, 12.5, True, "5000"):
        assert meta_write.meta_plan_budget_change("adset", "201", bad, client_id="acme")["error"] == \
            "invalid_argument"
    assert graph.writes() == [] and graph.validations() == []


def test_jpy_has_no_decimals_so_minor_units_are_whole_yen(graph):
    assert meta_ads.minor_to_units(6000, "JPY") == 6000.0
    assert meta_ads.minor_to_units(6000, "EUR") == 60.0
    assert meta_ads.minor_to_units("1234", "KWD") == 1.234
    assert meta_ads.minor_to_units(6000, "XXX") is None
    plan = ok(meta_write.meta_plan_budget_change("adset", "931", 6000, client_id="nippon"))  # +20 % exactly
    assert "5000 minor units = 5000 JPY -> 6000 minor units = 6000 JPY" in plan["ops"][0]["summary"]
    preview = write_core.approval_preview("nippon", plan["plan_id"])
    assert preview["budget_totals_micros"] == {"JPY": 6_000_000_000}  # not 60 000 000 (2-decimal reading)
    # 10 500 JPY is over the 10 000 JPY cap (it would be 105 "EUR" if read with 2 decimals: still a refusal,
    # but for the right reason here)
    capped = meta_write.meta_plan_budget_change("adset", "932", 10_500, client_id="nippon")
    assert capped["error"] == "budget_over_cap" and "10500000000 micros" in capped["detail"]
    approve(plan)
    assert apply(plan)["status"] == "applied"
    assert graph.entities["931"]["daily_budget"] == "6000"
    assert graph.writes()[0][2]["headers"]["Authorization"] == f"Bearer {TOKEN_N}"


def test_cap_currency_must_match_the_account_currency(graph, tmp_path, monkeypatch):
    path = clients.registry_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    data["clients"]["nippon"]["write"]["meta"]["cap_currency"] = "EUR"
    path.write_text(json.dumps(data), encoding="utf-8")
    out = meta_write.meta_plan_budget_change("adset", "931", 5500, client_id="nippon")
    assert out["error"] == "currency_mismatch" and graph.writes() == []


# ------------------------------------------------------------------ write allow-list and policy refusals
def test_write_allow_list_has_no_joker_and_pins_the_host():
    assert meta_write.WRITE_ALLOW_LIST.hosts == {"graph.facebook.com"}
    for method, pattern, kind in meta_write.WRITE_ALLOW_LIST.rules:
        assert method == "POST" and kind in ("validate", "write")
        assert ".*" not in pattern and ".+" not in pattern and "\\S" not in pattern
        assert pattern.startswith(r"https://graph\.facebook\.com/v26\.0/")  # version pinned, as meta_eco
    assert meta_write.WRITE_ALLOW_LIST.rules == [
        ("POST", r"https://graph\.facebook\.com/v26\.0/act_[0-9]+/(campaigns|adsets|ads)", "validate"),
        ("POST", r"https://graph\.facebook\.com/v26\.0/act_[0-9]+/(campaigns|adsets|ads)", "write"),
        ("POST", r"https://graph\.facebook\.com/v26\.0/[0-9]+", "write"),
    ]  # no validate rule on an update (POST /<id>)
    for url in ("https://graph.facebook.com/v25.0/101", "https://graph.facebook.com/v26.1/act_1111/campaigns",
                f"{V}/101/copies", f"{V}/act_1111/campaigns/x"):
        assert meta_write.WRITE_ALLOW_LIST.match("POST", url, {"status": "PAUSED"}) is None, url
    assert meta_write.WRITE_ALLOW_LIST.match("POST", f"{V}/101", {"status": "PAUSED",
                                                                   "execution_options": ["validate_only"]}) is None
    assert meta_write._base() == V
    assert meta_ads.ALLOWED_ENDPOINTS == meta_ads.ALLOWED_ENDPOINTS[:3] and len(meta_ads.ALLOWED_ENDPOINTS) == 3
    assert all(m == "GET" for m, _ in meta_ads.LIST_ENDPOINTS + meta_write.SNAPSHOT_ENDPOINTS)


@pytest.mark.parametrize("method,url,body,reason", [
    ("POST", f"{V}/act_1111", {"spend_cap": 1}, "not_on_write_allow_list"),           # account / spend cap
    ("POST", f"{V}/act_1111/adcreatives", {"name": "x"}, "not_on_write_allow_list"),
    ("POST", f"{V}/act_1111/campaigns/copies", {"name": "x"}, "not_on_write_allow_list"),
    ("DELETE", f"{V}/101", None, "not_on_write_allow_list"),
    ("POST", "https://graph.facebook.com.evil.com/v26.0/101", {"status": "PAUSED"}, "not_on_write_allow_list"),
    ("POST", f"{V}/101", {"status": "PAUSED"}, "grant_missing"),                         # a write needs a grant
    ("POST", f"{V}/101", {"status": "PAUSED", "execution_options": "validate_only"}, "not_on_write_allow_list"),
    ("POST", f"{V}/101", {"status": "PAUSED", "access_token": "x"}, "not_on_write_allow_list"),
    ("POST", f"{V}/act_1111/campaigns", {"name": "x", "status": "PAUSED"}, "grant_missing"),
])
def test_requests_off_the_write_path_are_blocked_before_sending(graph, method, url, body, reason):
    out = meta_write.WRITE_CLIENT.call(method, url, headers={"Authorization": f"Bearer {TOKEN_A}"}, json=body)
    assert out["error"] == "blocked" and out.get("reason") == reason, out
    assert graph.calls == [] and TOKEN_A not in json.dumps(out)


def test_validate_only_goes_out_without_grant_only_on_a_creation(graph):
    out = meta_write.WRITE_CLIENT.call("POST", f"{V}/act_1111/campaigns",
                                       headers={"Authorization": f"Bearer {TOKEN_A}"},
                                       json={"name": "x", "status": "PAUSED", "execution_options": ["validate_only"]})
    assert out == {"success": True} and graph.writes() == []
    # on an update the flag opens nothing: without a grant the request is blocked before sending
    calls = len(graph.calls)
    out = meta_write.WRITE_CLIENT.call("POST", f"{V}/101", headers={"Authorization": f"Bearer {TOKEN_A}"},
                                       json={"status": "PAUSED", "execution_options": ["validate_only"]})
    assert out["error"] == "blocked" and out["reason"] == "not_on_write_allow_list"
    assert len(graph.calls) == calls and graph.entities["101"]["status"] == "ACTIVE"


class IgnoringGraph(FakeGraph):
    """A Graph API that IGNORES execution_options=["validate_only"]: every POST really writes."""

    def _post(self, token, path, body):
        body = {k: v for k, v in (body or {}).items() if k != "execution_options"}
        return super()._post(token, path, body)


def test_a_graph_ignoring_validate_only_still_gets_no_write_at_plan_time(graph, monkeypatch):
    fake = IgnoringGraph()
    fake.entities = graph.entities
    monkeypatch.setattr(meta_write.WRITE_CLIENT, "session", fake)
    pause = ok(meta_write.meta_plan_pause("campaign", ("101",), client_id="acme"))
    budget = ok(meta_write.meta_plan_budget_change("adset", "201", 5500, client_id="acme"))
    assert [c for c in fake.calls if c[0] == "POST"] == []  # 0 POST to /<id> while planning
    assert fake.entities["101"]["status"] == "ACTIVE" and fake.entities["201"]["daily_budget"] == "5000"
    assert pause["dry_run"][0]["mode"] == budget["dry_run"][0]["mode"] == "local"
    back = write_core.rollback_plan(clients.resolve("acme"), "meta", pause["plan_id"])
    assert "error" in back  # nothing applied: nothing to roll back
    assert [c for c in fake.calls if c[0] == "POST"] == []


def test_local_dry_run_of_an_update_sends_nothing_and_checks_the_snapshot(graph):
    before = {"id": "101", "level": "campaign", "account_id": "1111", "status": "ACTIVE"}
    good = _raw_op(before=before)
    assert meta_write.dry_run(None, good)["ok"] is True
    for op in (_raw_op(before={**before, "id": "102"}), _raw_op(before={**before, "status": "ARCHIVED"}),
               _raw_op(before=before, body={"status": "DELETED"}), _raw_op(before=before, params={"x": "1"}),
               _raw_op(before=before, url="https://graph.facebook.com/v25.0/101"),
               _raw_op(action="budget_campaign", before=before, body={"daily_budget": 10}, financial=True)):
        out = meta_write.dry_run(None, op)
        assert out["ok"] is False and out["mode"] == "local", op
    assert graph.calls == []


def test_meta_eco_has_no_native_dry_run():
    from google_ecommerce_mcp import meta_eco, meta_eco_write

    assert [k for _, _, k in meta_eco_write.WRITE_ALLOW_LIST.rules] == ["write"]
    assert all(m == "GET" for m, *_ in meta_eco.ALLOW_LIST.patterns)
    op = WriteOp(op_id="x", platform="meta_eco", account_id="1113", action="catalog_item_update", method="POST",
                 url=f"{V}/5001", body={"inventory": 1}, before={"inventory": 2, "currency": "EUR"})
    assert meta_eco_write.dry_run(None, op)["mode"] == "local"


def test_token_in_url_or_method_override_is_refused(graph):
    for url, params in ((f"{V}/101?access_token={TOKEN_A}", None), (f"{V}/101", {"access_token": TOKEN_A}),
                        (f"{V}/101", {"method": "delete"})):
        out = meta_write.WRITE_CLIENT.call("GET", url, params=params, headers={"Authorization": f"Bearer {TOKEN_A}"})
        assert out["error"] == "blocked" and TOKEN_A not in json.dumps(out)
    out = meta_write.WRITE_CLIENT.call("POST", f"{V}/act_1111/campaigns",
                                       headers={"Authorization": f"Bearer {TOKEN_A}",
                                                "X-HTTP-Method-Override": "DELETE"},
                                       json={"name": "x", "status": "PAUSED", "execution_options": ["validate_only"]})
    assert out["error"] == "blocked" and out["reason"] == "header_forbidden"
    assert graph.calls == []


def _raw_op(**kw):
    base = dict(op_id="x1", platform="meta", account_id="act_1111", action="pause_campaign", method="POST",
                url=f"{V}/101", body={"status": "PAUSED"}, params=None, risk="medium", summary="raw")
    base.update(kw)
    return WriteOp(**base)


@pytest.mark.parametrize("op, code", [
    (_raw_op(body={"status": "PAUSED", "name": "renamed"}), "field_not_allowed"),
    (_raw_op(body={"status": "PAUSED", "spend_cap": 100}), "budget_kind_unsupported"),
    (_raw_op(action="budget_adset", url=f"{V}/201", body={"daily_budget": 5500, "bid_amount": 10},
             financial=True), "bidding_not_supported"),
    (_raw_op(action="budget_adset", url=f"{V}/201", body={"daily_budget": 5500}), "financial_flag_missing"),
    (_raw_op(body={"status": "ACTIVE"}, financial=True), "activation_not_allowed"),
    (_raw_op(body={"status": "DELETED"}), "delete_not_allowed"),
    (_raw_op(action="create_campaign", url=f"{V}/act_1111/campaigns",
             body={"name": "x", "objective": "OUTCOME_SALES", "special_ad_categories": [], "status": "ACTIVE"}),
     "status_not_paused"),
    (_raw_op(action="create_campaign", url=f"{V}/act_1111/campaigns",
             body={"name": "x", "objective": "OUTCOME_SALES", "special_ad_categories": []}), "status_missing"),
    (_raw_op(action="update_name", body={"name": "x"}), "no_field_allowlist"),
    (_raw_op(url=f"{V}/101?fields=id"), "query_in_url"),
    (_raw_op(account_id="act_2221"), "account_not_allowed"),
])
def test_policy_refuses_what_the_meta_builders_never_build(graph, op, code):
    out = write_core.create_plan(clients.resolve("acme"), "meta", [op], title="raw")
    assert out["error"] == code or code in json.dumps(out.get("violations", [])), out
    assert graph.writes() == [] and graph.validations() == []


# ------------------------------------------------------------------ foreign account, two clients
def test_entity_of_another_account_is_refused_even_when_the_token_can_see_it(graph):
    for eid, out in (("909", meta_write.meta_plan_pause("campaign", ("909",), client_id="acme")),
                     ("921", meta_write.meta_plan_budget_change("adset", "921", 5500, client_id="acme"))):
        assert out["error"] == "entity_not_found_for_client", out
        text = json.dumps(out)
        # generic: neither the owner account nor the state of the other client's entity
        assert "2221" not in text and "ACTIVE" not in text and "5000" not in text, out
        reads = [c for c in graph.calls if c[1] == f"{V}/{eid}"]
        assert [c[2]["params"]["fields"] for c in reads] == ["id,account_id"]  # ownership only, nothing else
    assert graph.writes() == [] and graph.validations() == []


def test_creation_under_a_parent_of_another_account_is_refused(graph):
    out = meta_write.meta_plan_create_adset("FR", "909", "OFFSITE_CONVERSIONS", "IMPRESSIONS", ("FR",), 0,
                                            client_id="acme")
    assert out["error"] == "account_not_bound", out
    graph.add("499", "creative", "2221", name="Beta creative")
    out = meta_write.meta_plan_create_ad("Ad", "201", "499", client_id="acme")
    assert out["error"] == "account_not_bound", out
    assert graph.writes() == [] and graph.validations() == []


def test_two_clients_stay_isolated(graph):
    pa = ok(meta_write.meta_plan_pause("campaign", ("101",), client_id="acme"))
    pb = ok(meta_write.meta_plan_pause("campaign", ("909",), client_id="beta"))
    # a plan of one client cannot be applied as the other, nor with the other's token
    assert apply(pa, client_id="beta")["error"] == "plan_not_found"
    swapped = apply(pb, token=pa["confirmation_token"])
    assert "error" in swapped and graph.writes() == []
    assert apply(pa)["status"] == "applied" and apply(pb)["status"] == "applied"
    for method, url, kwargs in graph.calls:
        auth = kwargs["headers"]["Authorization"]
        if "/909" in url or "act_2221" in url:
            assert auth == f"Bearer {TOKEN_B}", url
        else:
            assert auth == f"Bearer {TOKEN_A}", url
    assert {e["plan_id"] for e in write_core.journal.entries("beta")} == {pb["plan_id"]}
    assert {e["plan_id"] for e in write_core.journal.entries("acme")} == {pa["plan_id"]}


def test_a_client_without_meta_token_cannot_plan(graph, monkeypatch):
    monkeypatch.delenv("GEM_SECRET_BETA_META")
    out = meta_write.meta_plan_pause("campaign", ("909",), client_id="beta")
    assert out["error"] == "not_configured" and "set-secret beta meta" in out["fix"]
    assert all(kw["headers"]["Authorization"] != f"Bearer {TOKEN_A}" for _, _, kw in graph.calls)


def test_client_is_required_with_several_clients(graph):
    out = meta_write.meta_plan_pause("campaign", ("101",))
    assert out["error"] == "client_required" and graph.calls == []


# ------------------------------------------------------------------ rollback
def test_budget_rollback_restores_the_previous_value(graph):
    plan = ok(meta_write.meta_plan_budget_change("adset", "201", 5500, client_id="acme"))
    approve(plan)
    assert apply(plan)["status"] == "applied"
    back = ok(write_core.rollback_plan(clients.resolve("acme"), "meta", plan["plan_id"]))
    assert back["rollback_of"] == plan["plan_id"] and back["ops"][0]["action"] == "budget_adset"
    assert any("daily_budget: 5500 -> 5000" in c for c in back["ops"][0]["changes"])
    approve(back)
    assert apply(back)["status"] == "applied"
    assert graph.entities["201"]["daily_budget"] == "5000"


def test_pause_rollback_is_an_activation_refused_unless_allowed(graph):
    plan = ok(meta_write.meta_plan_pause("campaign", ("101",), client_id="acme"))
    assert apply(plan)["status"] == "applied"
    refused = write_core.rollback_plan(clients.resolve("acme"), "meta", plan["plan_id"])
    assert refused["error"] == "activation_not_allowed"
    path = clients.registry_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    data["clients"]["acme"]["write"]["meta"]["allow_activate"] = True
    path.write_text(json.dumps(data), encoding="utf-8")
    back = ok(write_core.rollback_plan(clients.resolve("acme"), "meta", plan["plan_id"]))
    assert back["risks"]["financial"] is True and back["ops"][0]["action"] == "restore_status_campaign"
    approve(back)
    assert apply(back)["status"] == "applied" and graph.entities["101"]["status"] == "ACTIVE"


def test_creations_have_no_automatic_rollback(graph):
    plan = ok(meta_write.meta_plan_create_campaign("Black Friday", "OUTCOME_SALES", (), client_id="acme"))
    assert plan["ops"][0]["rollback"] == "none"
    assert apply(plan)["status"] == "applied"
    out = write_core.rollback_plan(clients.resolve("acme"), "meta", plan["plan_id"])
    assert out["error"] == "no_inverse"


# ------------------------------------------------------------------ P1: creations, always PAUSED
def test_create_campaign_is_paused_with_explicit_special_ad_categories(graph):
    plan = ok(meta_write.meta_plan_create_campaign("Black Friday", "OUTCOME_SALES", (), client_id="acme"))
    assert plan["human_approval_required"] is False
    (validation,) = graph.validations()
    assert validation[1] == f"{V}/act_1111/campaigns"
    assert validation[2]["json"] == {"name": "Black Friday", "objective": "OUTCOME_SALES",
                                     "special_ad_categories": [], "status": "PAUSED",
                                     "execution_options": ["validate_only"]}
    assert apply(plan)["status"] == "applied"
    (write,) = graph.writes()
    assert write[2]["json"]["status"] == "PAUSED" and write[2]["json"]["special_ad_categories"] == []
    created = graph.entities[str(graph.next_id)]
    assert created["status"] == "PAUSED" and created["account_id"] == "1111"


def test_create_campaign_with_budget_is_financial_and_capped(graph):
    plan = ok(meta_write.meta_plan_create_campaign("CBO", "OUTCOME_SALES", ("NONE",), daily_budget_minor=3000,
                                                   adset_budget_sharing=False, client_id="acme"))
    assert plan["risks"]["financial"] is True and plan["human_approval_required"] is True
    over = meta_write.meta_plan_create_campaign("CBO", "OUTCOME_SALES", (), daily_budget_minor=20_000,
                                                client_id="acme")
    assert over["error"] == "budget_over_cap"
    bad = meta_write.meta_plan_create_campaign("x", "OUTCOME_SALES", ("NONE", "HOUSING"), client_id="acme")
    assert bad["error"] == "invalid_argument"


def test_create_adset_and_ad_in_the_clients_own_account(graph):
    plan = ok(meta_write.meta_plan_create_adset("FR 25-54", "101", "OFFSITE_CONVERSIONS", "IMPRESSIONS",
                                                ("fr", "BE"), 0, age_min=25, age_max=54, daily_budget_minor=2000,
                                                pixel_id="1112", custom_event_type="PURCHASE", client_id="acme"))
    body = graph.validations()[0][2]["json"]
    assert body["status"] == "PAUSED" and body["campaign_id"] == "101" and body["daily_budget"] == 2000
    assert body["targeting"] == {"geo_locations": {"countries": ["FR", "BE"]}, "age_min": 25, "age_max": 54,
                                 "targeting_automation": {"advantage_audience": 0}}
    assert body["promoted_object"] == {"pixel_id": "1112", "custom_event_type": "PURCHASE"}
    approve(plan)
    assert apply(plan)["status"] == "applied"
    ad = ok(meta_write.meta_plan_create_ad("Ad FR", "201", "401", client_id="acme"))
    assert apply(ad)["status"] == "applied"
    assert graph.writes()[-1][2]["json"] == {"name": "Ad FR", "adset_id": "201", "creative": {"creative_id": "401"},
                                              "status": "PAUSED"}
    # a pixel of another client is refused before anything is read
    calls = len(graph.calls)
    out = meta_write.meta_plan_create_adset("x", "101", "OFFSITE_CONVERSIONS", "IMPRESSIONS", ("FR",), 1,
                                            pixel_id="2222", custom_event_type="PURCHASE", client_id="acme")
    assert out["error"] == "id_not_owned" and len(graph.calls) == calls


def test_dry_run_rejected_by_meta_gives_no_token_and_writes_nothing(graph):
    graph.fail_validate = True
    out = meta_write.meta_plan_create_campaign("x", "OUTCOME_SALES", (), client_id="acme")
    assert out["error"] == "dry_run_failed" and "confirmation_token" not in out
    assert graph.writes() == []


# ------------------------------------------------------------------ registration and annotations
def test_register_declares_plan_tools_read_only_with_client_id(graph):
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("t")
    names = meta_write.register(mcp)
    assert names == ["meta_plan_pause", "meta_plan_budget_change", "meta_plan_create_campaign",
                     "meta_plan_create_adset", "meta_plan_create_ad"]
    for tool in asyncio.run(mcp.list_tools()):
        assert tool.annotations.readOnlyHint is True and tool.annotations.destructiveHint is False, tool.name
        props = tool.inputSchema["properties"]
        assert "client_id" in props and "client_id" not in tool.inputSchema.get("required", []), tool.name
        assert all(p.get("description") for p in props.values()), tool.name
        assert tool.title and len(tool.description) > 60
    assert write_core.get_handler("meta") is meta_write.HANDLER
    assert not write_policy.allowlists_frozen()
    assert write_policy.field_allowlist("meta", "budget_adset") == frozenset({"dailybudget"})
    assert write_policy.field_money_ok("meta", "create_campaign") == frozenset({"isadsetbudgetsharingenabled"})


def test_allow_lists_are_registered_by_register_not_at_import(monkeypatch):
    monkeypatch.setattr(write_policy, "_FIELD_ALLOWLIST", {})
    monkeypatch.setattr(write_core, "_HANDLERS", {})
    module = importlib.reload(meta_write)
    try:
        assert write_policy.field_allowlist("meta", "pause_campaign") is None
        assert write_core.get_handler("meta") is None
        register()
        assert write_policy.field_allowlist("meta", "pause_campaign") == frozenset({"status"})
    finally:
        assert module is meta_write


def test_plan_tool_through_mcp_runs_offline(graph):
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("t")
    meta_write.register(mcp)
    result = asyncio.run(mcp.call_tool("meta_plan_pause", {"level": "campaign", "entity_ids": ["101"],
                                                           "client_id": "acme"}))
    assert "confirmation_token" in str(result) and graph.writes() == []


# ------------------------------------------------------------------ P0 reads: campaigns, ad sets, ads
def test_lists_carry_status_budget_and_currency(graph):
    out = meta_ads.meta_campaigns(client_id="acme")
    assert out["client_id"] == "acme" and out["account_id"] == "act_1111" and out["currency"] == "EUR"
    assert {c["id"] for c in out["campaigns"]} == {"101", "203"}
    adsets = meta_ads.meta_adsets(campaign_id="101", client_id="acme")
    row = next(r for r in adsets["adsets"] if r["id"] == "201")
    assert row["daily_budget_minor"] == 5000 and row["daily_budget"] == 50.0 and row["status"] == "ACTIVE"
    assert "minor unit" in adsets["budget_unit"] and adsets["untrusted_text"]["fields"] == ["adsets[].name"]
    get = next(c for c in graph.calls if c[1].endswith("/act_1111/adsets"))
    assert json.loads(get[2]["params"]["filtering"]) == [{"field": "campaign.id", "operator": "EQUAL",
                                                          "value": "101"}]
    ads = meta_ads.meta_ads_list(adset_id="201", statuses=("ACTIVE",), client_id="acme")
    assert ads["ads"][0]["id"] == "301" and "daily_budget" not in ads["ads"][0]
    tokyo = meta_ads.meta_adsets(client_id="nippon")
    assert {r["daily_budget"] for r in tokyo["adsets"]} == {5000.0, 9000.0} and tokyo["currency"] == "JPY"
    assert all(m == "GET" for m, _, _ in graph.calls)
    assert meta_ads.LIST_CLIENT.BLOCKED_ATTEMPTS == []
    assert_clean_requests(graph)


def test_lists_are_bounded(graph):
    for n in range(30):
        graph.add(str(5000 + n), "campaign", "1111", status="PAUSED", name=f"c{n}")
    out = meta_ads.meta_campaigns(limit=10, client_id="acme")
    assert out["count"] == 10 and out["complete"] is False and out["complete_note"]
    params = next(c for c in graph.calls if c[1].endswith("/campaigns"))[2]["params"]
    assert params["limit"] == 10


def test_lists_refuse_another_clients_account_and_bad_ids(graph):
    out = meta_ads.meta_campaigns(account_id="act_2221", client_id="acme")
    assert out["error"] == "id_not_owned" and out["client_id"] == "acme"
    assert meta_ads.meta_adsets(campaign_id="10 1", client_id="acme")["error"] == "invalid_argument"
    assert meta_ads.meta_ads_list(statuses=("NOPE",), client_id="acme")["error"] == "invalid_argument"
    assert meta_ads.meta_campaigns(client_id="nobody")["error"] == "unknown_client"
    assert graph.calls == []


def test_list_client_never_writes(graph):
    for method, url in (("POST", f"{V}/act_1111/campaigns"), ("GET", f"{V}/act_1111/insights"),
                        ("GET", f"{V}/101"), ("DELETE", f"{V}/act_1111/ads")):
        out = meta_ads.LIST_CLIENT.call(method, url, headers={"Authorization": f"Bearer {TOKEN_A}"}, json={})
        assert out["error"] == "blocked"
    assert graph.calls == []
    for method, pattern in meta_ads.LIST_ENDPOINTS:
        assert method == "GET" and not re.search(r"insights|copies|adcreatives", pattern)


def test_existing_read_tools_use_the_clients_token_and_stamp_client_id(graph, monkeypatch):
    accounts = {"data": [{"id": "act_1111", "account_id": "1111", "name": "Acme", "account_status": 1,
                          "currency": "EUR"},
                         {"id": "act_2221", "account_id": "2221", "name": "Beta", "account_status": 1,
                          "currency": "EUR"}]}

    class Session:
        def __init__(self):
            self.calls = []

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return FakeResponse(200, accounts if url.endswith("/me/adaccounts") else {"currency": "EUR"})

    session = Session()
    monkeypatch.setattr(meta_ads.CLIENT, "session", session)
    meta_ads.CLIENT.cache.clear()
    out = meta_ads.meta_ad_accounts(client_id="acme")
    assert out["client_id"] == "acme" and [a["account_id"] for a in out["accounts"]] == ["1111"]
    assert out["not_owned_hidden"] == 1 and out["configured_account_id"] == "1111"
    assert session.calls[0][2]["headers"]["Authorization"] == f"Bearer {TOKEN_A}"
    refused = meta_ads.meta_insights(account_id="2221", client_id="acme")
    assert refused["error"] == "id_not_owned" and len(session.calls) == 1

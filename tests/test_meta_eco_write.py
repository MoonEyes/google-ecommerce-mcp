"""meta_eco_write: plan -> (approval) -> meta_apply of a catalog product item update on a fake Graph server, its
rollback, and the locks (field allow-list, foreign catalog, other client, write allow-list, grant). Offline: no
network, fake keyring, GEM_WRITE_HMAC_KEY; approval injected as in test_write_approval.py."""

from __future__ import annotations

import asyncio
import copy

import pytest

from ads_fakes import META_TOKEN_PREFIX, FakeResponse, no_network  # noqa: F401  (autouse fixture)
from clients_fakes import ACME, BETA, setup_env, write_registry
from google_ecommerce_mcp import meta_eco_write as mw
from google_ecommerce_mcp import write_core, write_policy
from google_ecommerce_mcp.clients import resolve
from google_ecommerce_mcp.write_core import WriteOp
from google_ecommerce_mcp.write_journal import Journal

TOKENS = {"acme": META_TOKEN_PREFIX + "acmeTOKENabcdefghijkl", "beta": META_TOKEN_PREFIX + "betaTOKENabcdefghijkl"}
BASE = "https://graph.facebook.com/v26.0"
ITEM, FOREIGN = "5001", "6001"


class FakeGraph:
    """Product items in memory: GET /{id} returns them formatted as Meta does, POST /{id} updates them."""

    def __init__(self) -> None:
        self.items = {
            ITEM: {"catalog": "1113", "retailer_id": "SKU-1", "price": 1299, "sale_price": None, "currency": "EUR",
                   "availability": "in stock", "inventory": 40},
            FOREIGN: {"catalog": "2223", "retailer_id": "SKU-B", "price": 500, "sale_price": None,
                      "currency": "EUR", "availability": "in stock", "inventory": 1},
        }
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, copy.deepcopy(kwargs)))
        item_id = url.rsplit("/", 1)[-1]
        item = self.items.get(item_id)
        if item is None:
            return FakeResponse(400, {"error": {"code": 100, "message": "Unsupported get request"}})
        if method == "GET" and (kwargs.get("params") or {}).get("fields") == "id,product_catalog{id}":
            return FakeResponse(200, {"id": item_id, "product_catalog": {"id": item["catalog"]}})
        if method == "GET":
            out = {"id": item_id, "retailer_id": item["retailer_id"], "currency": item["currency"],
                   "availability": item["availability"], "inventory": item["inventory"],
                   "price": f"{item['price'] / 100:.2f}".replace(".", ",") + " €",
                   "product_catalog": {"id": item["catalog"]}}
            if item["sale_price"] is not None:
                out["sale_price"] = f"{item['sale_price'] / 100:.2f}".replace(".", ",") + " €"
            return FakeResponse(200, out)
        body = kwargs.get("json") or {}
        for key in ("price", "sale_price", "availability", "inventory"):
            if key in body:
                item[key] = body[key]
        return FakeResponse(200, {"success": True})

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


def policy(**extra):
    return {"enabled": True, "account_allow": ["1113"], **extra}


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    _, path = setup_env(monkeypatch, tmp_path)
    write_registry(path, {"acme": {**ACME, "write": {"meta_eco": policy()}}, "beta": BETA})
    monkeypatch.setenv("GEM_SECRET_ACME_META", TOKENS["acme"])
    monkeypatch.setenv("GEM_SECRET_BETA_META", TOKENS["beta"])
    monkeypatch.setenv("GEM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GEM_WRITE", "1")
    monkeypatch.setenv("GEM_WRITE_HMAC_KEY", "ab" * 32)
    monkeypatch.delenv("GEM_LIVE_TEST", raising=False)
    monkeypatch.setattr(write_core, "journal", Journal())
    monkeypatch.setattr(write_core, "_HANDLERS", {})
    monkeypatch.setattr(write_policy, "_FIELD_ALLOWLIST", dict(write_policy._FIELD_ALLOWLIST))
    monkeypatch.setattr(write_policy, "_FROZEN", False)
    monkeypatch.setattr(write_policy, "_TEST_PERMISSIVE", False)  # the real allow-list is checked
    mw.register_policy()
    return path


@pytest.fixture
def graph(monkeypatch):
    server = FakeGraph()
    monkeypatch.setattr(mw.CLIENT, "session", server)
    return server


def plan(**kw):
    kw.setdefault("client_id", "acme")
    kw.setdefault("product_item_id", ITEM)
    return mw.meta_eco_plan_catalog_item_update(**kw)


def apply(p, client_id="acme"):
    return write_core._apply_tool("meta_apply", client_id, p["plan_id"], p["confirmation_token"], p["confirm"])


def approve(p, client_id="acme"):
    assert write_core._write_approval(client_id, p["plan_id"], approver="test")["status"] == "approved"


def assert_bearer_only(graph):
    for _, url, kwargs in graph.calls:
        for token in TOKENS.values():
            assert token not in url and token not in str(kwargs.get("params")) and token not in str(kwargs.get("json"))
        assert "access_token" not in url and "?" not in url
        assert kwargs["headers"] == {"Authorization": f"Bearer {TOKENS['acme']}"}


# ------------------------------------------------------------------ allow-lists and registration
def test_write_allow_list_is_one_pinned_post_rule_without_joker():
    assert mw.WRITE_ALLOW_LIST.rules == [("POST", r"https://graph\.facebook\.com/v26\.0/[0-9]+", "write")]
    assert mw.WRITE_ALLOW_LIST.platform == "meta_eco"
    assert mw.WRITE_ALLOW_LIST.match("POST", f"{BASE}/{ITEM}") == "write"
    for url in (f"{BASE}/1113/items_batch", f"{BASE}/1113/product_feeds", f"{BASE}/1112/events",
                "https://graph.facebook.com/v25.0/5001", "https://graph.facebook.com.evil.io/v26.0/5001"):
        assert mw.WRITE_ALLOW_LIST.match("POST", url) is None, url
    assert mw.WRITE_ALLOW_LIST.match("DELETE", f"{BASE}/{ITEM}") is None


def test_field_allow_list_and_money_exemptions():
    snap = write_policy.golden_allowlists()["meta_eco"]["catalog_item_update"]
    assert snap == {"fields": ["availability", "currency", "inventory", "price", "saleprice"],
                    "money_ok": ["price", "saleprice"]}
    mw.register_policy()  # idempotent


def test_plan_tool_is_annotated_read_only():
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("t")
    assert mw.register(server) == ["meta_eco_plan_catalog_item_update"]
    tool = {t.name: t for t in asyncio.run(server.list_tools())}["meta_eco_plan_catalog_item_update"]
    assert tool.annotations.readOnlyHint is True and tool.annotations.destructiveHint is False
    assert "client_id" in tool.inputSchema["properties"] and tool.title and tool.description
    assert write_core.get_handler("meta_eco") is mw.HANDLER


def test_direct_writes_are_blocked_without_grant(graph):
    out = mw.CLIENT.call("POST", f"{BASE}/{ITEM}", headers={"Authorization": "Bearer x"}, json={"inventory": 1})
    assert out["error"] == "blocked" and out["reason"] == "grant_missing"
    out = mw.CLIENT.call("POST", f"{BASE}/1113/items_batch", json={"requests": []})
    assert out["error"] == "blocked" and out["reason"] == "not_on_write_allow_list"
    assert graph.calls == []


# ------------------------------------------------------------------ plan -> apply
def test_stock_plan_applies_without_human_approval(graph):
    p = plan(inventory=0, availability="out of stock")
    assert "error" not in p, p
    assert p["client_id"] == "acme" and p["apply_tool"] == "meta_apply"
    assert p["human_approval_required"] is False and p["ops"][0]["rollback"] == "auto"
    assert p["ops"][0]["account_id"] == "1113" and p["dry_run"][0]["mode"] == "local"
    assert graph.posts() == []  # planning writes nothing
    out = apply(p)
    assert out["status"] == "applied", out
    (_, url, kwargs), = graph.posts()
    assert url == f"{BASE}/{ITEM}" and kwargs["json"] == {"availability": "out of stock", "inventory": 0}
    assert graph.items[ITEM]["inventory"] == 0
    assert_bearer_only(graph)
    assert apply(p)["error"] == "plan_already_applied"


def test_price_plan_needs_approval_then_rolls_back(graph):
    p = plan(price=1499)
    assert "error" not in p, p
    op = write_core.load_plan("acme", p["plan_id"])["ops"][0]
    assert op["body"] == {"price": 1499, "currency": "EUR"} and op["financial"] is True and op["risk"] == "high"
    assert op["before"]["price"] == 1299 and op["before"]["account_id"] == "1113"
    assert op["inverse"]["body"] == {"price": 1299, "currency": "EUR"}
    assert any("price: 1299 -> 1499" in c for c in p["ops"][0]["changes"])
    assert p["human_approval_required"] is True
    out = apply(p)
    assert out["error"] == "approval_required" and graph.posts() == []
    approve(p)
    assert apply(p)["status"] == "applied"
    assert graph.items[ITEM]["price"] == 1499
    rb = write_core._rollback_tool(p["plan_id"], "acme")
    assert "error" not in rb, rb
    assert rb["rollback_of"] == p["plan_id"] and rb["human_approval_required"] is True
    approve(rb)
    assert apply(rb)["status"] == "applied"
    assert graph.items[ITEM]["price"] == 1299
    assert [c[2]["json"] for c in graph.posts()] == [{"price": 1499, "currency": "EUR"},
                                                     {"price": 1299, "currency": "EUR"}]
    assert_bearer_only(graph)


def test_unknown_previous_value_means_no_automatic_rollback(graph):
    p = plan(sale_price=999)  # the item has no sale price today
    assert "error" not in p, p
    assert p["ops"][0]["rollback"] == "none" and "no automatic rollback" in p["ops"][0]["summary"]


# ------------------------------------------------------------------ refusals
def test_item_of_another_catalog_is_refused(graph):
    out = plan(product_item_id=FOREIGN, inventory=3)
    assert out["error"] == "entity_not_found_for_client" and out["client_id"] == "acme"
    text = str(out)
    assert "2223" not in text and "SKU-B" not in text and "500" not in text, out  # nothing of the other catalog
    reads = [c for c in graph.calls if c[1].endswith(f"/{FOREIGN}")]
    assert [c[2]["params"]["fields"] for c in reads] == ["id,product_catalog{id}"]  # ownership only
    assert graph.posts() == []


def test_item_moved_to_another_catalog_before_apply_is_refused(graph):
    p = plan(inventory=5)
    graph.items[ITEM]["catalog"] = "2223"
    graph.calls.clear()
    out = apply(p)
    assert out["error"] == "account_not_bound" and graph.posts() == []
    assert "2223" not in str(out)
    assert [c[2]["params"]["fields"] for c in graph.calls] == ["id,product_catalog{id}"]


def test_price_ratio_is_checked_again_at_apply_on_the_fresh_snapshot(graph):
    p = plan(price=1499)
    assert "error" not in p, p
    approve(p)
    graph.items[ITEM]["price"] = 129_900  # someone set 1299.00 EUR meanwhile: 1499 is now 1/86 of it
    out = apply(p)
    assert out["status"] == "failed" and "10 times" in str(out["results"][0]), out
    assert graph.posts() == []


def test_execute_refuses_a_price_far_from_its_snapshot():
    op = WriteOp("op1", "meta_eco", "1113", mw.ACTION, "POST", f"{BASE}/{ITEM}",
                 body={"price": 99_000, "currency": "EUR"}, before={"price": 1299, "currency": "EUR"},
                 risk="high", financial=True)
    out = mw.execute(None, op, grant=None)
    assert out["error"] == "precondition_failed" and "10 times" in out["detail"]
    other = WriteOp("op1", "meta_eco", "1113", mw.ACTION, "POST", f"{BASE}/{ITEM}",
                    body={"price": 1499, "currency": "USD"}, before={"price": 1299, "currency": "EUR"},
                    risk="high", financial=True)
    assert mw.execute(None, other, grant=None)["error"] == "precondition_failed"


def test_values_are_checked_before_any_plan(graph):
    assert plan(price=129_900)["error"] == "invalid_argument"  # 100x: units instead of cents
    assert plan(price=0)["error"] == "invalid_argument"
    assert plan(inventory=-1)["error"] == "invalid_argument"
    assert plan(availability="sold")["error"] == "invalid_argument"
    assert plan()["error"] == "invalid_argument"
    assert plan(inventory=40)["error"] == "no_change"
    assert plan(product_item_id="50/01", inventory=1)["error"] == "invalid_argument"
    assert graph.posts() == []


def test_field_outside_the_allow_list_is_refused(graph):
    c = resolve("acme")
    before = mw.snapshot(c, WriteOp("x", "meta_eco", "1113", mw.ACTION, "POST", f"{BASE}/{ITEM}"))
    for body in ({"name": "Free gift"}, {"inventory": 1, "visibility": "hidden"}, {"price": 1, "method": "delete"}):
        op = WriteOp("op1", "meta_eco", "1113", mw.ACTION, "POST", f"{BASE}/{ITEM}", body=body, before=before,
                     risk="high", financial=True)
        out = write_core.create_plan(c, "meta_eco", [op], title="t")
        assert out["error"] in ("field_not_allowed", "method_override_in_payload"), out
    assert graph.posts() == []


def test_other_client_cannot_plan_or_apply(graph, env):
    p = plan(inventory=7)
    out = plan(client_id="beta", product_item_id=FOREIGN, inventory=3)
    assert out["error"] == "write_not_enabled"
    assert apply(p, client_id="beta")["error"] == "plan_not_found"
    write_registry(env, {"acme": {**ACME, "write": {"meta_eco": policy(enabled=False)}}, "beta": BETA})
    assert apply(p)["error"] in ("policy_changed", "write_not_enabled")
    assert graph.posts() == []


def test_catalog_not_in_account_allow_is_refused(graph, env):
    write_registry(env, {"acme": {**ACME, "write": {"meta_eco": {"enabled": True, "account_allow": ["1112"]}}},
                         "beta": BETA})
    assert plan(inventory=1)["error"] == "account_not_allowed"
    assert graph.posts() == []


def test_global_switch_off_refuses(graph, monkeypatch):
    p = plan(inventory=2)
    monkeypatch.setenv("GEM_WRITE", "0")
    assert apply(p)["error"] == "write_disabled" and graph.posts() == []


def test_tampered_token_is_refused(graph):
    p = plan(inventory=2)
    out = write_core._apply_tool("meta_apply", "acme", p["plan_id"], p["confirmation_token"][:-2] + "xx",
                                 p["confirm"])
    assert "error" in out and graph.posts() == []


# ------------------------------------------------------------------ price parsing
@pytest.mark.parametrize("text, currency, minor", [
    ("12,99 €", "EUR", 1299), ("$1,234.50", "USD", 123450), ("1.234,50 EUR", "EUR", 123450),
    ("¥1,200", "JPY", 1200), ("12 EUR", "EUR", 1200), ("1,234.567 KWD", "KWD", 1234567),
    ("12.3", "EUR", None), ("1,2,3", "EUR", None), ("12,99 €", None, None), ("12 or 13", "EUR", None),
    (None, "EUR", None),
])
def test_parse_price(text, currency, minor):
    assert mw.parse_price(text, currency) == minor

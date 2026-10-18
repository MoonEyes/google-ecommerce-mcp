"""Merchant product patch: plan -> google_apply on a fake Merchant API, explicit update mask (empty mask refused,
masked field without value refused, attribute outside the mask refused), local dry-run with a diff, price
(financial, human approval, cap, +20 %, currency), API data sources only, product changed since the plan,
rollback by inverse PATCH, foreign account without any request, write token only, two isolated clients,
allow-list without insert / delete / inventories. Offline."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from ads_fakes import no_network  # noqa: F401
from google_ecommerce_mcp import clients, merchant_write, write_core
from google_ecommerce_mcp.write_core import WriteOp
from test_google_write import apply, bearer
from test_merchant_tools import MAPI, mworld  # noqa: F401

SKU1 = "fr~FR~SKU1"
PATCH_URL = f"{MAPI}/products/v1/accounts/1111/productInputs/{SKU1}"


def plan(**kw):
    kw.setdefault("client_id", "acme")
    kw.setdefault("product_id", SKU1)
    return merchant_write.merchant_plan_product_patch(**kw)


def attrs(server, account="1111", pid=SKU1):
    return server.products[account][pid]["productAttributes"]


# ------------------------------------------------------------------ mask locks (no network)
def test_empty_mask_is_refused_without_any_request(mworld):
    out = plan(update_mask=[], product_attributes={"title": "x"})
    assert out["error"] == "update_mask_empty" and out["client_id"] == "acme"
    out = plan(update_mask=[" "], product_attributes={})
    assert out["error"] == "update_mask_empty"
    assert mworld.calls == []


@pytest.mark.parametrize("values", [{}, {"title": None}, {"title": ""}, {"title": "   "}, {"price": {}}])
def test_masked_field_without_value_is_refused(mworld, values):
    mask = list(values) or ["title"]
    out = plan(update_mask=mask, product_attributes=values)
    assert out["error"] == "mask_field_without_value" and "DELETE" in out["detail"]
    assert mworld.calls == []


def test_attribute_outside_the_mask_or_unsupported_is_refused(mworld):
    out = plan(update_mask=["title"], product_attributes={"title": "New", "description": "x"})
    assert out["error"] == "field_not_in_mask"
    out = plan(update_mask=["gtin"], product_attributes={"gtin": "123"})
    assert out["error"] == "field_not_supported"
    out = plan(update_mask=["availability"], product_attributes={"availability": "maybe"})
    assert out["error"] == "bad_request"
    out = plan(update_mask=["link"], product_attributes={"link": "javascript:alert(1)"})
    assert out["error"] == "bad_request"
    assert mworld.calls == []


def test_crafted_op_with_a_mask_field_without_value_is_refused(mworld):
    """Even an op built by hand (not by the tool) never reaches Google with a deleting mask."""
    ctx = clients.resolve("acme")
    op = WriteOp(op_id="p", platform="merchant", account_id="1111", action="product_patch", method="PATCH",
                 url=PATCH_URL, body={"productAttributes": {"title": "New"},
                                      "name": f"accounts/1111/productInputs/{SKU1}"},
                 params={"dataSource": "accounts/1111/dataSources/77",
                         "updateMask": "productAttributes.title,productAttributes.description"}, risk="low")
    out = write_core.create_plan(ctx, "merchant", [op], title="t")
    assert out["error"] == "dry_run_failed" and "would DELETE" in out["detail"]
    empty = dataclasses.replace(op, params={"dataSource": "accounts/1111/dataSources/77", "updateMask": ""})
    assert write_core.create_plan(ctx, "merchant", [empty], title="t")["error"] == "dry_run_failed"
    assert mworld.writes() == []


# ------------------------------------------------------------------ plan -> apply
def test_title_patch_plan_then_apply(mworld):
    out = plan(update_mask=["title"], product_attributes={"title": "Blue mug 33 cl"})
    assert "error" not in out, out
    assert out["platform"] == "merchant" and out["apply_tool"] == "google_apply"
    assert out["ops"][0]["risk"] == "low" and out["ops"][0]["financial"] is False
    assert out["human_approval_required"] is False and out["ops"][0]["rollback"] == "auto"
    assert out["dry_run"][0]["mode"] == "local"
    assert out["diff"] == [{"field": "title", "before": "Blue mug", "after": "Blue mug 33 cl"}]
    assert out["dry_run"][0]["detail"]["diff"] == out["diff"]
    assert any("title: Blue mug -> Blue mug 33 cl" in c for c in out["ops"][0]["changes"])
    assert "30 minutes" in out["note"]
    assert mworld.writes() == []
    result = apply("acme", out)
    assert result["status"] == "applied", result
    [patch] = mworld.writes()
    assert patch["method"] == "PATCH" and patch["url"] == PATCH_URL
    assert patch["params"] == {"dataSource": "accounts/1111/dataSources/77", "updateMask": "productAttributes.title"}
    assert patch["json"] == {"productAttributes": {"title": "Blue mug 33 cl"},
                             "name": f"accounts/1111/productInputs/{SKU1}"}
    assert patch["auth"] == "Bearer " + bearer("acme", "WRITE")
    assert {c["auth"] for c in mworld.calls} == {"Bearer " + bearer("acme", "WRITE")}  # read token never used
    assert attrs(mworld)["title"] == "Blue mug 33 cl" and attrs(mworld)["description"] == "A mug"
    assert result["results"][0]["result"]["changed"] == ["title"]


def test_availability_and_link_are_medium_risk(mworld):
    out = plan(update_mask=["availability", "link"],
               product_attributes={"availability": "OUT_OF_STOCK", "link": "https://acme.fr/p/sku1-v2"})
    assert "error" not in out, out
    assert out["ops"][0]["risk"] == "medium" and out["human_approval_required"] is False
    assert apply("acme", out)["status"] == "applied"
    assert mworld.writes()[0]["params"]["updateMask"] == "productAttributes.availability,productAttributes.link"
    assert attrs(mworld)["availability"] == "OUT_OF_STOCK"


def test_no_change_is_refused(mworld):
    out = plan(update_mask=["title"], product_attributes={"title": "Blue mug"})
    assert out["error"] == "no_change" and mworld.writes() == []


def test_offer_id_with_special_characters_uses_base64url(mworld):
    mworld.add_product("1111", "a/b 1", title="Slash")
    out = plan(product_id="fr~FR~a/b 1", update_mask=["title"], product_attributes={"title": "Slashed"})
    assert "error" not in out, out
    assert apply("acme", out)["status"] == "applied"
    url = mworld.writes()[0]["url"]
    segment = url.rsplit("/", 1)[1]
    assert "~" not in segment and "%" not in segment
    assert mworld.products["1111"]["fr~FR~a/b 1"]["productAttributes"]["title"] == "Slashed"


# ------------------------------------------------------------------ price
def test_price_is_financial_high_risk_and_needs_approval(mworld):
    out = plan(update_mask=["price"], product_attributes={"price": {"amount_micros": 13490000,
                                                                    "currency_code": "eur"}})
    assert "error" not in out, out
    op = out["ops"][0]
    assert op["risk"] == "high" and op["financial"] is True and out["human_approval_required"] is True
    refused = write_core._apply_tool("google_apply", "acme", out["plan_id"], out["confirmation_token"],
                                     out["confirm"])
    assert refused["error"] == "approval_required" and mworld.writes() == []
    assert apply("acme", out)["status"] == "applied"  # approval injected as in test_write_approval
    [patch] = mworld.writes()
    assert patch["json"]["productAttributes"]["price"] == {"amountMicros": "13490000", "currencyCode": "EUR"}
    assert attrs(mworld)["price"]["amountMicros"] == "13490000"


@pytest.mark.parametrize("price, code", [
    ({"amount_micros": 16000000, "currency_code": "EUR"}, "increase_too_large"),   # +23 % in one step
    ({"amount_micros": 600000000, "currency_code": "EUR"}, "budget_over_cap"),     # over 500 EUR cap
    ({"amount_micros": 12000000, "currency_code": "USD"}, "invalid_request"),      # currency change
    ({"amount_micros": "12.5", "currency_code": "EUR"}, "bad_request"),            # micros only
    ({"amount_micros": 0, "currency_code": "EUR"}, "bad_request"),
    ({"amount": 12, "currency_code": "EUR"}, "bad_request"),
])
def test_price_locks(mworld, price, code):
    out = plan(update_mask=["price"], product_attributes={"price": price})
    assert out["error"] == code, out
    assert mworld.writes() == []


@pytest.mark.parametrize("micros, ok", [(1_298_999, False), (1_299_000, True), (1, False)])
def test_price_has_a_lower_bound_of_a_tenth_of_the_current_one(mworld, micros, ok):
    out = plan(update_mask=["price"], product_attributes={"price": {"amount_micros": micros,
                                                                    "currency_code": "EUR"}})
    if ok:
        assert "error" not in out, out
    else:
        assert out["error"] == "invalid_request" and "1/10" in out["detail"], out
    assert mworld.writes() == []


def test_price_lower_bound_is_checked_again_at_apply(mworld):
    out = plan(update_mask=["price"], product_attributes={"price": {"amount_micros": 11_990_000,
                                                                    "currency_code": "EUR"}})
    assert "error" not in out, out
    attrs(mworld)["price"]["amountMicros"] = "129900000"  # someone set 129.90 EUR meanwhile
    result = apply("acme", out)
    assert result["status"] == "failed" and "1/10" in str(result["results"][0]) and mworld.writes() == []


def test_price_ratio_helper():
    eur = {"amountMicros": "12990000", "currencyCode": "EUR"}
    assert merchant_write.price_ratio_problem({"amountMicros": "1299000"}, eur) is None
    assert "1/10" in merchant_write.price_ratio_problem({"amountMicros": "1298999"}, eur)
    assert "greater than 0" in merchant_write.price_ratio_problem({"amountMicros": "0"}, eur)
    assert merchant_write.price_ratio_problem({"amountMicros": "x"}, eur)


def test_link_to_another_domain_is_high_risk_and_needs_approval(mworld):
    out = plan(update_mask=["link"], product_attributes={"link": "https://evil-shop.example/p/sku1"})
    assert "error" not in out, out
    assert out["ops"][0]["risk"] == "high" and out["human_approval_required"] is True
    refused = write_core._apply_tool("google_apply", "acme", out["plan_id"], out["confirmation_token"],
                                     out["confirm"])
    assert refused["error"] == "approval_required" and mworld.writes() == []
    # a subdomain of the same registrable domain stays medium
    same = plan(update_mask=["link"], product_attributes={"link": "https://shop.acme.fr/p/sku1"})
    assert "error" not in same and same["ops"][0]["risk"] == "medium" and same["human_approval_required"] is False


def test_crafted_medium_op_with_an_off_site_link_is_refused(mworld):
    ctx = clients.resolve("acme")
    op = WriteOp(op_id="p", platform="merchant", account_id="1111", action="product_patch", method="PATCH",
                 url=PATCH_URL, body={"productAttributes": {"link": "https://evil.example/x"},
                                      "name": f"accounts/1111/productInputs/{SKU1}"},
                 params={"dataSource": "accounts/1111/dataSources/77", "updateMask": "productAttributes.link"},
                 risk="medium")
    out = write_core.create_plan(ctx, "merchant", [op], title="t")
    assert out["error"] == "dry_run_failed" and "registrable domain" in out["detail"]
    assert mworld.writes() == []


@pytest.mark.parametrize("url, domain", [
    ("https://www.acme.fr/p", "acme.fr"), ("https://shop.acme.co.uk/x", "acme.co.uk"),
    ("http://ACME.com.au/", "acme.com.au"), ("https://localhost/x", "localhost"), ("https://1.2.3.4/", "1.2.3.4"),
    ("not a url", None), (None, None),
])
def test_registrable_domain(url, domain):
    assert merchant_write.registrable_domain(url) == domain


def test_price_needs_an_existing_price_and_the_cap_currency(mworld, monkeypatch):
    mworld.add_product("1111", "NOPRICE", title="No price")
    out = plan(product_id="fr~FR~NOPRICE", update_mask=["price"],
               product_attributes={"price": {"amount_micros": 1000000, "currency_code": "EUR"}})
    assert out["error"] == "invalid_request" and "no price yet" in out["detail"]
    usd = plan(product_id="en~US~B1", update_mask=["price"], client_id="beta",
               product_attributes={"price": {"amount_micros": 5500000, "currency_code": "USD"}})
    assert usd["error"] == "currency_mismatch"  # beta's cap_currency is EUR, its product is in USD
    assert mworld.writes() == []


# ------------------------------------------------------------------ data source, drift, rollback
def test_non_api_data_source_is_refused(mworld):
    out = plan(update_mask=["title"], product_attributes={"title": "x"}, data_source_id="88")
    assert out["error"] == "invalid_request" and "only an API" in out["detail"]
    mworld.products["1111"][SKU1]["dataSource"] = "accounts/1111/dataSources/88"
    out = plan(update_mask=["title"], product_attributes={"title": "x"})
    assert out["error"] == "invalid_request" and mworld.writes() == []
    out = plan(update_mask=["title"], product_attributes={"title": "x"}, data_source_id="99")  # beta's source
    # always looked up under the client's own account (1111), where beta's source 99 does not exist
    assert out["error"] == "not_available" and "accounts/1111/dataSources/99" in out["detail"], out
    assert "confirmation_token" not in out and not write_core.list_plans("acme")
    assert not any("/accounts/2222/" in c["url"] for c in mworld.calls)
    assert mworld.writes() == []


def test_product_changed_between_plan_and_apply_sends_nothing(mworld):
    out = plan(update_mask=["title"], product_attributes={"title": "New"})
    attrs(mworld)["title"] = "edited in Merchant Center"
    result = apply("acme", out)
    assert result["status"] == "failed" and "changed since the plan" in str(result["results"][0])
    assert mworld.writes() == []


def test_rollback_restores_the_previous_values(mworld):
    out = plan(update_mask=["title", "availability"],
               product_attributes={"title": "Blue mug XL", "availability": "BACKORDER"})
    assert apply("acme", out)["status"] == "applied"
    rollback = write_core._rollback_tool(out["plan_id"], client_id="acme")
    assert "error" not in rollback, rollback
    assert rollback["ops"][0]["action"] == "product_restore"
    assert apply("acme", rollback)["status"] == "applied"
    restore = mworld.writes()[-1]
    assert restore["method"] == "PATCH" and restore["url"] == PATCH_URL
    assert restore["params"]["updateMask"] == "productAttributes.title,productAttributes.availability"
    assert restore["json"]["productAttributes"] == {"title": "Blue mug", "availability": "IN_STOCK"}
    assert attrs(mworld)["title"] == "Blue mug" and attrs(mworld)["availability"] == "IN_STOCK"


def test_rollback_refused_when_edited_since_or_not_processed_yet(mworld):
    out = plan(update_mask=["title"], product_attributes={"title": "B"})
    mworld.process = False  # Merchant Center has not processed the patch yet
    assert apply("acme", out)["status"] == "applied"
    rollback = write_core._rollback_tool(out["plan_id"], client_id="acme")
    assert rollback["error"] == "dry_run_failed" and "30 minutes" in rollback["detail"]
    assert len(mworld.writes()) == 1


def test_attribute_absent_before_has_no_rollback(mworld):
    out = plan(product_id="fr~FR~SKU2", update_mask=["description"], product_attributes={"description": "New"})
    assert "error" not in out, out
    assert out["ops"][0]["rollback"] == "none" and "no rollback" in out["rollback_note"]
    assert apply("acme", out)["status"] == "applied"
    assert write_core._rollback_tool(out["plan_id"], client_id="acme")["error"] == "no_inverse"


def test_price_rollback_is_financial_too(mworld):
    out = plan(update_mask=["price"], product_attributes={"price": {"amount_micros": 11990000,
                                                                    "currency_code": "EUR"}})
    assert apply("acme", out)["status"] == "applied"
    rollback = write_core._rollback_tool(out["plan_id"], client_id="acme")
    assert "error" not in rollback, rollback
    assert rollback["ops"][0]["financial"] is True and rollback["human_approval_required"] is True
    assert apply("acme", rollback)["status"] == "applied"
    assert attrs(mworld)["price"]["amountMicros"] == "12990000"


# ------------------------------------------------------------------ locks: accounts, tokens, clients
def test_foreign_account_is_refused_without_any_request(mworld):
    out = plan(update_mask=["title"], product_attributes={"title": "x"}, account_id="2222")
    assert out["error"] == "id_not_owned" and mworld.calls == []
    ctx = clients.resolve("acme")
    url = f"{MAPI}/products/v1/accounts/2222/productInputs/en~US~B1"
    op = WriteOp(op_id="p", platform="merchant", account_id="1111", action="product_patch", method="PATCH", url=url,
                 body={"productAttributes": {"title": "x"}, "name": "accounts/2222/productInputs/en~US~B1"},
                 params={"dataSource": "accounts/2222/dataSources/99", "updateMask": "productAttributes.title"},
                 risk="low")
    assert write_core.create_plan(ctx, "merchant", [op], title="t")["error"] in ("account_not_bound",
                                                                                 "snapshot_failed")
    other = dataclasses.replace(op, account_id="2222")
    assert write_core.create_plan(ctx, "merchant", [other], title="t")["error"] in ("account_not_allowed",
                                                                                   "account_not_owned")
    # own account in the URL, another account's data source: refused before any request
    mixed = dataclasses.replace(op, url=PATCH_URL, body={"productAttributes": {"title": "x"},
                                                         "name": f"accounts/1111/productInputs/{SKU1}"})
    assert write_core.create_plan(ctx, "merchant", [mixed], title="t")["error"] in ("account_not_bound",
                                                                                   "snapshot_failed")
    assert mworld.calls == []


def test_switches_and_tokens(mworld, monkeypatch):
    monkeypatch.setenv("GEM_WRITE", "0")
    assert plan(update_mask=["title"], product_attributes={"title": "x"})["error"] == "write_disabled"
    monkeypatch.setenv("GEM_WRITE", "1")
    monkeypatch.delenv("GEM_SECRET_ACME_GOOGLE_W")
    out = plan(update_mask=["title"], product_attributes={"title": "x"})
    assert out["error"] == "write_token_missing" and "--client acme --write" in out["fix"]
    assert mworld.calls == []


def test_two_clients_are_isolated(mworld):
    acme = plan(update_mask=["title"], product_attributes={"title": "A"})
    beta = plan(product_id="en~US~B1", update_mask=["title"], product_attributes={"title": "B"}, client_id="beta")
    assert "error" not in acme and "error" not in beta, (acme, beta)
    assert write_core._apply_tool("google_apply", "beta", acme["plan_id"], acme["confirmation_token"],
                                  acme["confirm"])["error"] == "plan_not_found"
    assert apply("acme", acme)["status"] == "applied" and apply("beta", beta)["status"] == "applied"
    by_account: dict[str, set] = {}
    for call in mworld.calls:
        by_account.setdefault(call["url"].split("/accounts/")[1].split("/")[0], set()).add(call["auth"])
    assert by_account == {"1111": {"Bearer " + bearer("acme", "WRITE")},
                          "2222": {"Bearer " + bearer("beta", "WRITE")}}
    assert attrs(mworld, "2222", "en~US~B1")["title"] == "B" and attrs(mworld)["title"] == "A"
    assert write_core.journal.entries("beta", plan_id=acme["plan_id"]) == []


def test_unsigned_or_unlisted_writes_are_blocked(mworld):
    from google_ecommerce_mcp import google_write as gw

    ctx = clients.resolve("acme")
    for method, url in (("PATCH", PATCH_URL),
                        ("POST", f"{MAPI}/products/v1/accounts/1111/productInputs:insert"),
                        ("DELETE", PATCH_URL),
                        ("POST", f"{MAPI}/inventories/v1/accounts/1111/products/{SKU1}/localInventories:insert")):
        out = gw.write_call(merchant_write.WRITE, ctx, method, url, json={"productAttributes": {"title": "x"}})
        assert out["error"] == "blocked", (method, url)
    assert mworld.calls == []


def test_write_allow_list_is_patch_only():
    rules = merchant_write.WRITE.write_allow_list.rules
    assert [(m, k) for m, _, k in rules] == [("PATCH", "write")]
    assert not any(w in p for _, p, _ in rules for w in ("insert", "inventories", "delete"))
    assert {"productInputs:insert", "productInputs.delete", "localInventories"} <= set(
        merchant_write.VOLUNTARILY_ABSENT)
    assert merchant_write.HANDLER.dry_run is not None


def test_plan_tool_annotations_and_client_id(mworld):
    from mcp.server.fastmcp import FastMCP

    from google_ecommerce_mcp import merchant_tools

    mcp = FastMCP("annotations")
    names = merchant_tools.register(mcp) + merchant_write.register(mcp)
    assert set(names) == set(merchant_tools.TOOLS) | set(merchant_write.TOOLS)
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    for name in names:
        tool = tools[name]
        assert tool.annotations.readOnlyHint is True and tool.annotations.destructiveHint is False, name
        assert "client_id" in tool.inputSchema["properties"], name
        assert "client_id" not in tool.inputSchema.get("required", []), name
    assert not any(word in name for name in names for word in ("insert", "delete", "inventory"))

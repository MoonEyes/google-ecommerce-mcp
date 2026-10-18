"""GA4 Admin reads and plan -> apply on a fake Admin API: key event and custom dimension creations, duplicates,
quotas and immutability warning, foreign property, allow-list, no rollback, two isolated clients. Offline."""

from __future__ import annotations

import pytest

from ads_fakes import no_network  # noqa: F401
from google_ecommerce_mcp import clients, ga4_tools, write_core
from google_ecommerce_mcp.write_core import WriteOp
from test_google_write import ADMIN, apply, bearer, gworld  # noqa: F401

PROP = "111111111"


# ------------------------------------------------------------------ reads
def test_custom_definitions_with_quota_and_read_token(gworld):
    gworld.ga4[PROP]["customDimensions"] = [{"name": f"properties/{PROP}/customDimensions/1",
                                            "parameterName": "payment_type", "displayName": "Payment",
                                            "scope": "EVENT"}]
    gworld.ga4[PROP]["customMetrics"] = [{"name": "m", "parameterName": "tax", "displayName": "Tax",
                                         "scope": "EVENT", "measurementUnit": "CURRENCY"}]
    out = ga4_tools.ga4_custom_definitions(client_id="acme")
    assert out["client_id"] == "acme" and out["property_id"] == PROP
    assert out["custom_dimensions"][0]["parameter_name"] == "payment_type"
    assert out["quota"]["dimensions"]["EVENT"] == {"used": 1, "limit": 50}
    assert out["quota"]["metrics"] == {"used": 1, "limit": 50}
    assert {c["auth"] for c in gworld.calls} == {"Bearer " + bearer("acme", "READ")}
    assert all(c["params"]["pageSize"] == 200 for c in gworld.calls)


def test_key_events_read(gworld):
    gworld.ga4[PROP]["keyEvents"] = [{"name": f"properties/{PROP}/keyEvents/1", "eventName": "purchase",
                                     "countingMethod": "ONCE_PER_EVENT", "custom": False}]
    out = ga4_tools.ga4_key_events(client_id="acme")
    assert out["count"] == 1 and out["key_events"][0]["event_name"] == "purchase"


def test_property_of_another_client_is_refused_without_network(gworld):
    out = ga4_tools.ga4_key_events(property_id="222222222", client_id="acme")
    assert out["error"] == "id_not_owned" and gworld.calls == []
    out = ga4_tools.ga4_plan_key_event_create("generate_lead", property_id="222222222", client_id="acme")
    assert out["error"] == "id_not_owned" and gworld.calls == []


# ------------------------------------------------------------------ key events
def test_key_event_plan_then_apply(gworld):
    plan = ga4_tools.ga4_plan_key_event_create("generate_lead", client_id="acme")
    assert "error" not in plan, plan
    assert plan["platform"] == "ga4" and plan["apply_tool"] == "google_apply"
    assert plan["ops"][0]["risk"] == "medium" and plan["ops"][0]["financial"] is False
    assert plan["ops"][0]["rollback"] == "none" and plan["dry_run"][0]["mode"] == "local"
    assert gworld.writes() == []
    out = apply("acme", plan)
    assert out["status"] == "applied", out
    [write] = gworld.writes()
    assert write["url"] == f"{ADMIN}/properties/{PROP}/keyEvents"
    assert write["json"] == {"eventName": "generate_lead", "countingMethod": "ONCE_PER_EVENT"}
    assert write["auth"] == "Bearer " + bearer("acme", "WRITE")
    assert out["results"][0]["result"]["eventName"] == "generate_lead"
    # no automatic rollback: deleting a key event is not offered
    assert write_core._rollback_tool(plan["plan_id"], client_id="acme")["error"] == "no_inverse"


def test_duplicate_key_event_is_refused_at_plan_and_at_apply(gworld):
    gworld.ga4[PROP]["keyEvents"] = [{"eventName": "purchase"}]
    assert ga4_tools.ga4_plan_key_event_create("purchase", client_id="acme")["error"] == "invalid_request"
    plan = ga4_tools.ga4_plan_key_event_create("sign_up", client_id="acme")
    gworld.ga4[PROP]["keyEvents"].append({"eventName": "sign_up"})  # created meanwhile in the GA4 interface
    out = apply("acme", plan)
    assert out["status"] == "failed" and "already a key event" in str(out["results"][0])
    assert gworld.writes() == []


@pytest.mark.parametrize("name", ["", "1abc", "google_thing", "has space", "x" * 41])
def test_bad_event_names_are_refused(gworld, name):
    assert ga4_tools.ga4_plan_key_event_create(name, client_id="acme")["error"] == "invalid_request"


# ------------------------------------------------------------------ custom dimensions
def test_custom_dimension_plan_warns_it_is_immutable_then_applies(gworld):
    plan = ga4_tools.ga4_plan_custom_dimension_create("payment_type", "Payment type", client_id="acme")
    assert "error" not in plan, plan
    assert "can never be changed" in plan["ops"][0]["summary"]
    assert any("can never be changed" in w for w in plan["dry_run"][0]["detail"]["warnings"])
    out = apply("acme", plan)
    assert out["status"] == "applied", out
    [write] = gworld.writes()
    assert write["json"] == {"parameterName": "payment_type", "displayName": "Payment type", "scope": "EVENT"}


def test_custom_dimension_checks(gworld):
    gworld.ga4[PROP]["customDimensions"] = [{"parameterName": "payment_type", "scope": "EVENT"}]
    dup = ga4_tools.ga4_plan_custom_dimension_create("payment_type", "Again", client_id="acme")
    assert dup["error"] == "invalid_request" and "already exists" in dup["detail"]
    npa = ga4_tools.ga4_plan_custom_dimension_create("tier", "Tier", scope="EVENT",
                                                     disallow_ads_personalization=True, client_id="acme")
    assert npa["error"] == "invalid_request"
    user = ga4_tools.ga4_plan_custom_dimension_create("tier", "Tier", scope="USER",
                                                      disallow_ads_personalization=True, client_id="acme")
    assert "error" not in user, user
    gworld.ga4[PROP]["customDimensions"] = [{"parameterName": f"p{i}", "scope": "ITEM"} for i in range(10)]
    full = ga4_tools.ga4_plan_custom_dimension_create("p10", "P10", scope="ITEM", client_id="acme")
    assert "error" not in full and any("quota" in w for w in full["dry_run"][0]["detail"]["warnings"])
    assert gworld.writes() == []


# ------------------------------------------------------------------ locks
def test_writes_disabled_for_the_client(gworld, monkeypatch):
    monkeypatch.setenv("GEM_WRITE", "0")
    assert ga4_tools.ga4_plan_key_event_create("x_y", client_id="acme")["error"] == "write_disabled"
    assert gworld.calls == []


def test_fields_outside_the_allow_list_are_refused(gworld):
    ctx = clients.resolve("acme")
    op = WriteOp(op_id="k", platform="ga4", account_id=PROP, action="key_event_create", method="POST",
                 url=f"{ADMIN}/properties/{PROP}/keyEvents",
                 body={"eventName": "lead_x", "countingMethod": "ONCE_PER_EVENT",
                       "defaultValue": {"numericValue": 10, "currencyCode": "EUR"}}, risk="medium")
    out = write_core.create_plan(ctx, "ga4", [op], title="t")
    assert out["error"] == "field_not_allowed"
    assert gworld.writes() == []


def test_foreign_property_in_a_crafted_op_is_refused(gworld):
    ctx = clients.resolve("acme")
    op = WriteOp(op_id="k", platform="ga4", account_id=PROP, action="key_event_create", method="POST",
                 url=f"{ADMIN}/properties/222222222/keyEvents",
                 body={"eventName": "lead_x", "countingMethod": "ONCE_PER_EVENT"}, risk="medium")
    # the handler snapshot refuses before reading the other property (write_core reports snapshot_failed)
    assert write_core.create_plan(ctx, "ga4", [op], title="t")["error"] in ("account_not_bound", "snapshot_failed")
    op2 = WriteOp(op_id="k", platform="ga4", account_id="222222222", action="key_event_create", method="POST",
                  url=f"{ADMIN}/properties/222222222/keyEvents",
                  body={"eventName": "lead_x", "countingMethod": "ONCE_PER_EVENT"}, risk="medium")
    assert write_core.create_plan(ctx, "ga4", [op2], title="t")["error"] in ("account_not_allowed",
                                                                             "account_not_owned")
    assert gworld.calls == []


def test_two_clients_are_isolated(gworld):
    acme = ga4_tools.ga4_plan_key_event_create("lead_a", client_id="acme")
    beta = ga4_tools.ga4_plan_key_event_create("lead_b", client_id="beta")
    assert write_core._apply_tool("google_apply", "beta", acme["plan_id"], acme["confirmation_token"],
                                  acme["confirm"])["error"] == "plan_not_found"
    assert apply("acme", acme)["status"] == "applied" and apply("beta", beta)["status"] == "applied"
    auth = {c["url"].split("/properties/")[1].split("/")[0]: set() for c in gworld.calls}
    for call in gworld.calls:
        auth[call["url"].split("/properties/")[1].split("/")[0]].add(call["auth"])
    assert auth == {PROP: {"Bearer " + bearer("acme", "WRITE")}, "222222222": {"Bearer " + bearer("beta", "WRITE")}}
    assert [k["eventName"] for k in gworld.ga4["222222222"]["keyEvents"]] == ["lead_b"]


def test_wrong_apply_tool_is_refused(gworld):
    plan = ga4_tools.ga4_plan_key_event_create("lead_c", client_id="acme")
    out = write_core._apply_tool("gads_apply", "acme", plan["plan_id"], plan["confirmation_token"], plan["confirm"])
    assert out["error"] == "wrong_apply_tool" and gworld.writes() == []

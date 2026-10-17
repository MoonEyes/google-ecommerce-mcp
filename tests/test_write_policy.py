"""write_policy: every lock refuses on its own (one test per lock), and allows when its flag is set."""

from __future__ import annotations

import dataclasses

import pytest

from google_ecommerce_mcp import write_policy as wp
from google_ecommerce_mcp.write_core import WriteOp
from test_write_core import ACCOUNT, URL, FakeCtx, WritePolicyDouble, budget_op, open_policy, pause_op


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("GEM_WRITE", "1")
    # these tests check the other locks: ops without a registered field allow-list pass (dedicated tests below)
    monkeypatch.setattr(wp, "_TEST_PERMISSIVE", True)
    monkeypatch.setattr(wp, "_FIELD_ALLOWLIST", {})
    monkeypatch.setattr(wp, "_FROZEN", False)


def codes(ops, platform="google_ads", ctx=None, baseline=None, **policy):
    c = ctx or FakeCtx()
    out = wp.check_ops(c, platform, ops, open_policy(**policy), baseline=baseline)
    for v in out:
        assert v["detail"] and v["fix"]
    return [v["error"] for v in out]


def test_clean_ops_pass():
    assert codes([budget_op(), pause_op()]) == []


def test_switches(monkeypatch):
    assert codes([pause_op()], enabled=False) == ["write_not_enabled"]
    monkeypatch.setenv("GEM_WRITE", "true")
    assert codes([pause_op()]) == ["write_disabled"]
    monkeypatch.delenv("GEM_WRITE")
    assert codes([pause_op()]) == ["write_disabled"] and not wp.write_enabled()


def test_size_and_shape():
    assert codes([]) == ["empty_plan"]
    many = [pause_op(f"p{i}", url=f"{URL}#{i}") for i in range(4)]
    assert codes(many, max_ops_per_plan=3) == ["too_many_ops"]
    assert codes([pause_op(f"p{i}", url=f"{URL}#{i}") for i in range(101)], max_ops_per_plan=1000) == \
        ["too_many_ops"]
    assert codes([pause_op(), pause_op()]) == ["duplicate_op_id"]
    assert codes([pause_op(platform="meta")]) == ["platform_mismatch"]
    assert codes([pause_op(risk="huge")]) == ["bad_risk"]
    assert codes([pause_op(method="GET")]) == ["bad_method"]
    assert codes([pause_op(url="http://x.test/a")]) == ["bad_url"]
    assert codes([pause_op(rollback="maybe")]) == ["bad_rollback"]


def test_account_allow_and_ownership():
    assert codes([pause_op(account_id="5556667777")]) == ["account_not_allowed"]
    other = FakeCtx(owned=frozenset({"1"}))
    assert codes([pause_op()], ctx=other) == ["account_not_owned"]
    assert wp._norm_account("meta", "act_42") == wp._norm_account("meta", "42")
    assert wp._norm_account("google_ads", "111-222-3333") == wp._norm_account("google_ads", "1112223333")


def test_creation_must_be_paused():
    create = dict(op_id="c1", platform="google_ads", account_id=ACCOUNT, action="create_campaign", method="POST",
                  url=URL)
    active = {"operations": [{"create": {"name": "x", "status": "ENABLED"}}]}
    assert codes([WriteOp(**create, body=active)]) == ["status_not_paused"]
    assert codes([WriteOp(**create, body=active)], allow_activate=True) == ["status_not_paused"]
    assert codes([WriteOp(**create, body={"operations": [{"create": {"name": "x"}}]})]) == ["status_missing"]
    fixed = wp.force_paused(active, "google_ads")
    assert fixed["operations"][0]["create"]["status"] == "PAUSED" and active["operations"][0]["create"][
        "status"] == "ENABLED"
    assert codes([WriteOp(**create, body=fixed)]) == []
    assert wp.force_paused({"operations": [{"create": {"name": "x"}}]}, "google_ads")["operations"][0][
        "create"]["status"] == "PAUSED"
    assert wp.force_paused({"name": "x"}, "meta") == {"name": "x", "status": "PAUSED"}
    assert wp.force_paused({"name": "x", "configured_status": "ACTIVE"}, "meta")["configured_status"] == "PAUSED"
    assert wp.force_paused(None, "openai_ads") == {"status": "paused"}
    lower = WriteOp(**dict(create, platform="openai_ads"), body={"status": "active"})
    assert "status_not_paused" in codes([lower], platform="openai_ads")


def test_activation_needs_allow_activate_and_financial_flag():
    activate = pause_op(action="set_status", body={"status": "ENABLED"}, financial=True, risk="high")
    assert codes([activate]) == ["activation_not_allowed"]
    assert codes([activate], allow_activate=True) == []
    assert codes([dataclasses.replace(activate, financial=False)], allow_activate=True) == \
        ["financial_flag_missing"]
    assert codes([pause_op(body={"status": "active"}, financial=True)], allow_activate=False) == \
        ["activation_not_allowed"]


def test_delete_and_gem_managed_label():
    remove = pause_op(action="remove", body={"operations": [{"remove": f"customers/{ACCOUNT}/campaigns/2"}]},
                      before={"labels": ["other"]})
    assert codes([remove]) == ["delete_not_allowed"]
    assert codes([remove], allow_delete=True) == ["label_missing"]
    labelled = dataclasses.replace(remove, before={"labels": [{"name": wp.GEM_LABEL}]})
    assert codes([labelled], allow_delete=True) == []
    hidden = pause_op(action="update", body={"operations": [{"remove": f"customers/{ACCOUNT}/campaigns/2"}]})
    assert codes([hidden]) == ["delete_not_allowed"]
    # Meta: the URL names the campaign only, so the snapshot binds it to the account
    meta_delete = pause_op(platform="meta", method="DELETE", body=None,
                           url="https://graph.facebook.com/v23.0/123", before={"account_id": ACCOUNT})
    assert codes([meta_delete], platform="meta") == ["delete_not_allowed"]
    assert codes([meta_delete], platform="meta", allow_delete=True) == []
    assert wp.has_gem_label({"gem_managed": True}) and not wp.has_gem_label(None)
    assert not wp.has_gem_label({"labels": "gem-managed"})


def test_irreversible_and_pii():
    assert codes([pause_op(risk="irreversible")]) == ["irreversible_not_allowed"]
    assert codes([pause_op(risk="irreversible")], allow_irreversible=True) == []
    assert codes([pause_op(pii=True)]) == ["pii_not_allowed"]
    assert codes([pause_op(pii=True)], allow_pii=True) == []
    assert codes([pause_op(body={"status": "PAUSED", "hashedEmail": "ab12"})]) == ["pii_undeclared"]
    assert codes([pause_op(body={"status": "PAUSED", "hashedEmail": "ab12"}, pii=True)], allow_pii=True) == []


def test_creation_detected_from_the_payload_and_the_url():
    assert codes([pause_op(action="update", body={"operations": [{"create": {"name": "x"}}]})]) == \
        ["status_missing"]
    meta = pause_op(platform="meta", action="update", url=f"https://graph.facebook.com/v23.0/act_{ACCOUNT}/ads",
                    body={"name": "x"})
    assert wp.is_create(meta) and codes([meta], platform="meta") == ["status_missing"]


def test_currency_unit_unknown():
    meta = WriteOp(op_id="m1", platform="meta", account_id=ACCOUNT, action="budget_change", method="POST",
                   url="https://graph.facebook.com/v23.0/123", financial=True, body={"daily_budget": "10"},
                   before={"daily_budget": "10", "currency": "ZZZ", "account_id": ACCOUNT})
    assert codes([meta], platform="meta", cap_currency="ZZZ") == ["currency_unit_unknown"]


def test_budget_locks():
    assert codes([budget_op(financial=False)]) == ["financial_flag_missing"]
    assert codes([budget_op()], max_daily_budget_micros=None) == ["budget_cap_missing"]
    assert codes([budget_op()], cap_currency=None) == ["cap_currency_missing"]
    assert codes([budget_op(before={"amountMicros": 10_000_000})]) == ["currency_unknown"]
    assert codes([budget_op(before={"amountMicros": 10_000_000, "currency_code": "USD"})]) == \
        ["currency_mismatch"]
    assert codes([budget_op(new=60_000_000, old=55_000_000)]) == ["budget_over_cap"]
    assert codes([budget_op(before={"currency": "EUR"})]) == ["before_unknown"]
    assert codes([budget_op(new=12_000_001)]) == ["increase_too_large"]
    assert codes([budget_op(new=12_000_000)]) == []
    assert codes([budget_op(new=13_000_000)], max_increase_pct=30) == []
    assert codes([budget_op(new=9_000_000)]) == []  # decreases are always fine
    assert codes([budget_op(new=11_000_000)], baseline=lambda op: 5_000_000) == ["increase_too_large"]
    assert codes([budget_op(new=11_000_000)], baseline=lambda op: None) == []


def test_budget_on_creation_needs_currency_and_cap_but_no_before():
    create = WriteOp(op_id="c1", platform="google_ads", account_id=ACCOUNT, action="create_budget",
                     method="POST", url=URL, financial=True, before={"currency": "EUR"},
                     body={"operations": [{"create": {"amountMicros": "40000000", "status": "PAUSED"}}]})
    assert codes([create]) == []
    assert codes([dataclasses.replace(create, before=None)]) == ["currency_unknown"]
    assert codes([create], max_daily_budget_micros=30_000_000) == ["budget_over_cap"]


def test_meta_minor_units_and_unsupported_budget_kinds():
    meta = WriteOp(op_id="m1", platform="meta", account_id=ACCOUNT, action="budget_change", method="POST",
                   url="https://graph.facebook.com/v23.0/123", financial=True, body={"daily_budget": "1100"},
                   before={"daily_budget": "1000", "currency": "EUR", "account_id": ACCOUNT})
    assert wp.budget_values(meta.body) == [11_000_000]
    assert codes([meta], platform="meta") == []
    assert codes([dataclasses.replace(meta, body={"daily_budget": "6000"})], platform="meta") == \
        ["budget_over_cap", "increase_too_large"]
    lifetime = dataclasses.replace(meta, body={"lifetime_budget": "1000"})
    assert codes([lifetime], platform="meta") == ["budget_kind_unsupported"]
    assert wp.budget_values({"amountMicros": True, "x": {"amountMicros": "n/a"}}) == []


def test_policy_hash_changes_with_any_field():
    base = open_policy()
    assert wp.policy_hash(base) == wp.policy_hash(open_policy())
    for change in (dict(enabled=False), dict(account_allow=frozenset({"1"})), dict(max_increase_pct=21.0),
                   dict(allow_activate=True), dict(token_ttl_s=60)):
        assert wp.policy_hash(dataclasses.replace(base, **change)) != wp.policy_hash(base)
    assert isinstance(WritePolicyDouble(), WritePolicyDouble)


def test_live_test_flag(monkeypatch):
    monkeypatch.setenv("GEM_LIVE_TEST", "1")
    assert wp.live_test()
    monkeypatch.setenv("GEM_LIVE_TEST", "yes")
    assert not wp.live_test()

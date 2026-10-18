"""write_policy security gate: account binding (B1), strict parsing (B2), params and query (B3), currency from the
snapshot only (M3), plan-wide budget cap and bidding refusal (M4)."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass

import pytest

from google_ecommerce_mcp import write_policy as wp
from google_ecommerce_mcp.write_core import WriteOp
from test_write_core import ACCOUNT, URL, FakeCtx, WritePolicyDouble, budget_op, open_policy, pause_op

OTHER = "9998887777"  # account of another client
GADS = "https://googleads.googleapis.com/v26"
GRAPH = "https://graph.facebook.com/v23.0"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("GEM_WRITE", "1")
    monkeypatch.setattr(wp, "_TEST_PERMISSIVE", True)
    monkeypatch.setattr(wp, "_FIELD_ALLOWLIST", {})
    monkeypatch.setattr(wp, "_FROZEN", False)


def run(ops, platform="google_ads", ctx=None, policy=None, **overrides):
    c = ctx or FakeCtx()
    out = wp.check_ops(c, platform, ops, policy or open_policy(**overrides))
    for v in out:
        assert v["detail"] and v["fix"]
    return [v["error"] for v in out]


def meta_op(**kw):
    values = dict(op_id="m1", platform="meta", account_id=ACCOUNT, action="update", method="POST",
                  url=f"{GRAPH}/120001", body={"status": "PAUSED"}, before={"account_id": ACCOUNT})
    values.update(kw)
    return WriteOp(**values)


# ------------------------------------------------------------------ B1 account binding
def test_two_clients_crossed_url_is_refused():
    acme = FakeCtx(client_id="acme", owned=frozenset({ACCOUNT}))
    beta = FakeCtx(client_id="beta", owned=frozenset({OTHER}))
    crossed = pause_op(url=f"{GADS}/customers/{OTHER}/campaigns:mutate")
    assert run([crossed], ctx=acme) == ["account_not_bound"]
    # beta's own op on its own account passes for beta, and acme cannot borrow it
    own = pause_op(account_id=OTHER, url=f"{GADS}/customers/{OTHER}/campaigns:mutate")
    assert run([own], ctx=beta, account_allow=frozenset({OTHER})) == []
    assert run([own], ctx=acme) == ["account_not_allowed"]


def test_google_path_binding_variants():
    assert run([pause_op(url=f"{GADS}/customers/{ACCOUNT}/campaigns:mutate")]) == []
    assert run([pause_op(url=f"{GADS}/customers/{ACCOUNT}:mutate")]) == []
    dashed = f"{ACCOUNT[:3]}-{ACCOUNT[3:6]}-{ACCOUNT[6:]}"
    assert run([pause_op(url=f"{GADS}/customers/{dashed}/campaigns:mutate")]) == []
    two = f"{GADS}/customers/{ACCOUNT}/customers/{OTHER}/campaigns:mutate"
    assert run([pause_op(url=two)]) == ["account_not_bound"]
    encoded = f"{GADS}/customers/{ACCOUNT}/x/%63ustomers%2F{OTHER}/campaigns:mutate"
    assert run([pause_op(url=encoded)]) == ["account_not_bound"]


def test_dot_segments_are_refused():
    assert "bad_url" in run([pause_op(url=f"{GADS}/customers/{ACCOUNT}/x/../../{OTHER}/campaigns:mutate")])
    assert "bad_url" in run([pause_op(url=f"{GADS}/customers/{ACCOUNT}/%2e%2e/campaigns:mutate")])
    assert "bad_url" in run([pause_op(url=f"{GADS}/customers/{ACCOUNT}\\..\\x")])


def test_foreign_account_in_body_params_or_snapshot_is_refused():
    body = {"operations": [{"update": {"resourceName": f"customers/{OTHER}/campaigns/1", "status": "PAUSED"}}]}
    assert run([pause_op(body=body)]) == ["account_not_bound"]
    assert run([pause_op(params={"customer_id": OTHER})]) == ["account_not_bound"]
    assert run([pause_op(before={"resourceName": f"customers/{OTHER}/campaigns/1"})]) == ["account_not_bound"]
    same = {"operations": [{"update": {"resourceName": f"customers/{ACCOUNT}/campaigns/1", "status": "PAUSED"}}]}
    assert run([pause_op(body=same)]) == []


def test_meta_act_in_path():
    act = meta_op(url=f"{GRAPH}/act_{ACCOUNT}/campaigns", before=None)
    assert run([act], platform="meta") == []
    assert run([dataclasses.replace(act, url=f"{GRAPH}/ACT_{ACCOUNT}/campaigns")], platform="meta") == []
    assert run([dataclasses.replace(act, url=f"{GRAPH}/act_{OTHER}/campaigns")], platform="meta") == \
        ["account_not_bound"]
    assert run([dataclasses.replace(act, account_id=f"act_{ACCOUNT}")], platform="meta",
               ctx=FakeCtx(owned=frozenset({f"act_{ACCOUNT}"}))) == []
    assert run([dataclasses.replace(act, body={"status": "PAUSED", "parent": f"act_{OTHER}"})],
               platform="meta") == ["account_not_bound"]


def test_meta_url_without_account_needs_the_snapshot():
    assert run([meta_op()], platform="meta") == []
    assert run([meta_op(before={"account_id": f"act_{ACCOUNT}"})], platform="meta") == []
    assert run([meta_op(before={"ad_account": {"id": f"act_{ACCOUNT}"}})], platform="meta") == []
    assert run([meta_op(before={"account_id": OTHER})], platform="meta") == ["account_not_bound"]
    assert run([meta_op(before={"name": "x"})], platform="meta") == ["account_not_bound"]
    assert run([meta_op(before={"account_id": True})], platform="meta") == ["account_not_bound"]
    # no snapshot yet: deferred to the snapshot (before_unknown is re-checked by write_core after it)
    assert run([meta_op(before=None)], platform="meta") == ["before_unknown"]


def test_openai_and_unknown_platforms():
    slug = "acct_main"
    ctx = FakeCtx(owned=frozenset({slug}))
    op = WriteOp(op_id="o1", platform="openai_ads", account_id=slug, action="update", method="POST",
                 url=f"https://api.openai.test/v1/ad_accounts/{slug}/campaigns/1", body={"status": "paused"})
    assert run([op], platform="openai_ads", ctx=ctx, account_allow=frozenset({slug})) == []
    other = dataclasses.replace(op, url="https://api.openai.test/v1/ad_accounts/someone_else/campaigns/1")
    assert run([other], platform="openai_ads", ctx=ctx, account_allow=frozenset({slug})) == ["account_not_bound"]
    gtm = WriteOp(op_id="g1", platform="gtm", account_id=ACCOUNT, action="update", method="PUT",
                  url="https://tagmanager.googleapis.com/tagmanager/v2/accounts/1/containers/2/tags/3",
                  body={"name": "x"}, before={"name": "x"})
    assert run([gtm], platform="gtm") == ["account_not_bound"]
    assert run([dataclasses.replace(gtm, before={"name": "x", "account_id": ACCOUNT})], platform="gtm") == []


def test_binding_skipped_when_account_already_refused():
    assert run([pause_op(account_id=OTHER, url=f"{GADS}/customers/{ACCOUNT}/x")]) == ["account_not_allowed"]


# ------------------------------------------------------------------ B2 strict parsing
@pytest.mark.parametrize("status", [1, 0, {"v": "ENABLED"}, ["ENABLED"], None, True, "", "  "])
def test_non_string_status_is_unparseable(status):
    assert "status_unparseable" in run([pause_op(body={"status": status})])
    assert "status_unparseable" in run([pause_op(body={"x": {"configured_status": status}})])


def test_unknown_status_is_refused():
    assert run([pause_op(body={"status": "ENABLEDX"})]) == ["status_unknown"]
    assert run([pause_op(body={"Status": "Enabled"})]) == ["activation_not_allowed", "financial_flag_missing"]
    assert run([pause_op(body={"status": " paused "})]) == []


@pytest.mark.parametrize("value", [5e7, 50_000_000.0, "5e7", "5000000.5", True, False, None, -1, "-5", "",
                                   "1_000", "١٢", "0x10", [1], {"v": 1}])
def test_budget_values_must_be_strict_integers(value):
    out = run([budget_op(body={"amountMicros": value})])
    assert "budget_unparseable" in out
    assert wp.budget_values({"amountMicros": value}) == []


def test_budget_unparseable_next_to_a_valid_value_is_not_ignored():
    body = {"operations": [{"update": {"amountMicros": 11_000_000}}, {"update": {"amount_micros": 9e9}}]}
    assert run([budget_op(body=body)]) == ["budget_unparseable"]
    assert run([budget_op(body={"amountMicros": " 11000000 "})]) == []
    assert run([budget_op(before={"amountMicros": "n/a", "currency": "EUR"})]) == ["before_unknown"]


def test_budget_key_variants_are_recognised():
    for key in ("AmountMicros", "AMOUNT_MICROS", "daily-budget-micros", "dailyBudgetMicros"):
        assert run([budget_op(body={key: 60_000_000})]) == ["budget_over_cap", "increase_too_large"], key


@pytest.mark.parametrize("status", ["REMOVED", "deleted", "Archived"])
def test_removing_statuses_are_deletions(status):
    google = pause_op(action="set_status", body={"status": status}, before={"labels": []})
    assert run([google]) == ["delete_not_allowed"]
    assert run([google], allow_delete=True) == ["label_missing"]
    assert run([dataclasses.replace(google, before={"labels": [wp.GEM_LABEL]})], allow_delete=True) == []
    meta = meta_op(body={"status": status})
    assert run([meta], platform="meta") == ["delete_not_allowed"]
    assert run([meta], platform="meta", allow_delete=True) == []
    assert wp.is_remove(meta) and not wp.activates(meta)


def test_creation_with_removed_status_is_not_paused():
    create = WriteOp(op_id="c1", platform="google_ads", account_id=ACCOUNT, action="create_campaign",
                     method="POST", url=URL, body={"operations": [{"create": {"status": "REMOVED"}}]})
    assert "status_not_paused" in run([create])


def test_every_created_entity_needs_paused():
    body = {"mutateOperations": [{"campaignOperation": {"create": {"name": "a", "status": "PAUSED"}}},
                                 {"campaignOperation": {"create": {"name": "b"}}}]}
    create = WriteOp(op_id="c1", platform="google_ads", account_id=ACCOUNT, action="create_campaign",
                     method="POST", url=URL, body=body)
    assert run([create]) == ["status_missing"]
    fixed = wp.force_paused(body, "google_ads")
    assert fixed["mutateOperations"][1]["campaignOperation"]["create"]["status"] == "PAUSED"
    assert "status" not in body["mutateOperations"][1]["campaignOperation"]["create"]
    assert run([dataclasses.replace(create, body=fixed)]) == []


def test_nested_remove_in_mutate_operations():
    body = {"mutateOperations": [{"campaignOperation": {"remove": f"customers/{ACCOUNT}/campaigns/1"}}]}
    assert run([pause_op(action="update", body=body)]) == ["delete_not_allowed"]


# ------------------------------------------------------------------ B3 params and query string
def test_query_in_url_is_refused():
    assert run([pause_op(url=f"{URL}?status=ENABLED")]) == ["query_in_url"]
    assert run([pause_op(url=f"{URL}?")]) == ["query_in_url"]
    assert run([pause_op(url=f"{URL}#frag")]) == []


def test_params_are_inspected_like_the_body():
    assert run([pause_op(params={"status": "ACTIVE"})]) == ["activation_not_allowed", "financial_flag_missing"]
    assert run([pause_op(params={"status": 1})]) == ["status_unparseable"]
    assert run([pause_op(params={"remove": "x"})]) == ["delete_not_allowed"]
    assert run([pause_op(params={"status": "DELETED"})]) == ["delete_not_allowed"]
    over = budget_op(body=None, params={"daily_budget": "6000"})
    assert run([over]) == ["budget_over_cap", "increase_too_large"]
    assert run([budget_op(params={"amountMicros": "5e7"})]) == ["budget_unparseable"]
    assert run([pause_op(params={"lifetime_budget": "1"})]) == ["budget_kind_unsupported"]
    assert run([pause_op(params={"bid_amount": "1"})]) == ["bidding_not_supported"]


def test_json_encoded_strings_are_inspected():
    hidden = pause_op(params={"adset_spec": json.dumps({"status": "ACTIVE"})})
    assert run([hidden]) == ["activation_not_allowed", "financial_flag_missing"]
    assert "budget_over_cap" in run([budget_op(params={"spec": json.dumps([{"amountMicros": 90_000_000}])})])
    assert run([pause_op(params={"note": "{not json"})]) == []


def test_creation_paused_only_in_body_but_active_in_params():
    create = WriteOp(op_id="c1", platform="meta", account_id=ACCOUNT, action="create_campaign", method="POST",
                     url=f"{GRAPH}/act_{ACCOUNT}/campaigns", body={"status": "PAUSED"},
                     params={"status": "ACTIVE"})
    assert run([create], platform="meta") == ["status_not_paused"]


# ------------------------------------------------------------------ M3 currency from the snapshot only
def test_currency_only_from_before():
    body_only = budget_op(body={"amountMicros": 11_000_000, "currency": "EUR"},
                          before={"amountMicros": 10_000_000})
    assert run([body_only]) == ["currency_unknown"]
    lying = budget_op(body={"amountMicros": 11_000_000, "currency_code": "EUR"},
                      before={"amountMicros": 10_000_000, "currency": "USD"})
    assert run([lying]) == ["currency_mismatch"]
    assert wp.currency_of(body_only) is None


# ------------------------------------------------------------------ M4 plan-wide cap and bidding
@dataclass(frozen=True)
class PolicyPlus(WritePolicyDouble):
    max_plan_budget_micros: int | None = None


def _many_creates(n, amount):
    return WriteOp(op_id="c1", platform="google_ads", account_id=ACCOUNT, action="create_budget",
                   method="POST", url=URL, financial=True, before={"currency": "EUR"},
                   body={"operations": [{"create": {"amountMicros": amount, "status": "PAUSED"}}
                                        for _ in range(n)]})


def test_plan_total_is_capped():
    # each value is under the 50 M cap, but 30 of them in one op with max_ops_per_plan=1 is 1.5 B
    assert run([_many_creates(30, 50_000_000)], max_ops_per_plan=1) == ["plan_budget_over_cap"]
    assert run([_many_creates(1, 50_000_000)], max_ops_per_plan=1) == []
    assert run([_many_creates(25, 50_000_000)]) == []  # 25 x cap with max_ops_per_plan=25
    assert run([_many_creates(26, 50_000_000)]) == ["plan_budget_over_cap"]


def test_optional_max_plan_budget_micros():
    base = dict(enabled=True, account_allow=frozenset({ACCOUNT}), max_daily_budget_micros=50_000_000,
                cap_currency="EUR")
    tight = PolicyPlus(**base, max_plan_budget_micros=60_000_000)
    ops = [budget_op("a", url=URL + "#a"), budget_op("b", url=URL + "#b"), budget_op("c", url=URL + "#c"),
           budget_op("d", url=URL + "#d"), budget_op("e", url=URL + "#e"), budget_op("f", url=URL + "#f")]
    assert run(ops, policy=tight) == ["plan_budget_over_cap"]  # 6 x 11 M = 66 M
    assert run(ops[:5], policy=tight) == []
    assert run(ops, policy=PolicyPlus(**base, max_plan_budget_micros=None)) == []
    assert run(ops, policy=PolicyPlus(**base, max_plan_budget_micros=True)) == []  # junk ignored


@pytest.mark.parametrize("key", ["cpc_bid_micros", "cpcBidMicros", "targetCpaMicros", "target_cpa_micros",
                                 "bid_amount", "bid_strategy", "bidStrategyType", "biddingStrategy",
                                 "targetRoas", "maximizeConversionValue", "manualCpc", "bid_constraints",
                                 "cpc_bid_ceiling_micros", "target_impression_share"])
def test_bidding_fields_are_refused(key):
    assert run([pause_op(body={"status": "PAUSED", key: 1})]) == ["bidding_not_supported"]
    nested = {"operations": [{"update": {"resourceName": f"customers/{ACCOUNT}/adGroups/1", key: {"x": 1}}}]}
    assert run([pause_op(body=nested)]) == ["bidding_not_supported"]


def test_update_mask_naming_bid_fields_is_refused():
    body = {"operations": [{"update": {"resourceName": f"customers/{ACCOUNT}/adGroups/1"},
                            "updateMask": "status,cpc_bid_micros"}]}
    assert run([pause_op(body=body)]) == ["bidding_not_supported"]
    body["operations"][0]["updateMask"] = "campaign.target_cpa.target_cpa_micros"
    assert run([pause_op(body=body)]) == ["bidding_not_supported"]
    body["operations"][0]["updateMask"] = "spend_cap"
    assert run([pause_op(body=body)]) == ["budget_kind_unsupported"]


@pytest.mark.parametrize("key", ["spend_cap", "spendCapMicros", "lifetime_budget", "totalAmountMicros",
                                 "lifetime_spend_cap"])
def test_unsupported_budget_kinds(key):
    assert run([pause_op(body={"status": "PAUSED", key: "1"})]) == ["budget_kind_unsupported"]


def test_targeting_is_not_mistaken_for_bidding():
    assert run([meta_op(body={"targeting": {"geo_locations": {"countries": ["FR"]}}})], platform="meta") == []


# ------------------------------------------------------------------ N3 creation whatever the label
def test_create_key_under_update_label_is_a_creation():
    body = {"operations": [{"create": {"name": "x", "status": "ENABLED"}}]}
    sneaky = pause_op(action="update", body=body, financial=True)
    assert wp.is_create(sneaky)
    assert run([sneaky], allow_activate=True) == ["status_not_paused"]
    assert run([pause_op(action="update", body={"operations": [{"create": {"name": "x"}}]})]) == \
        ["status_missing"]
    nested = {"mutateOperations": [{"campaignOperation": {"Create": {"name": "x"}}}]}
    assert run([pause_op(action="set_status", body=nested)]) == ["status_missing"]
    assert run([pause_op(action="update", params={"create": json.dumps({"name": "x"})})]) == ["status_missing"]
    assert run([pause_op(action="update", body={"create": "x"})]) == ["status_missing"]
    assert run([pause_op(action="update", body={"create": [{"status": "PAUSED"}, {"name": "b"}]})]) == \
        ["status_missing"]
    paused = {"operations": [{"create": {"name": "x", "status": "PAUSED"}}]}
    assert run([pause_op(action="update", body=paused)]) == []
    assert not wp.is_create(pause_op(action="update", body={"create": None, "status": "PAUSED"}))


@pytest.mark.parametrize("path", [f"act_{ACCOUNT}/campaigns", f"act_{ACCOUNT}/adsets", f"act_{ACCOUNT}/ads",
                                  f"act_{ACCOUNT}/adcreatives", f"act_{ACCOUNT}/AdSets/", "120001/adsets",
                                  "120001/copies"])
def test_meta_post_to_a_collection_is_a_creation(path):
    op = meta_op(action="update", url=f"{GRAPH}/{path}", body={"name": "x"})
    assert wp.is_create(op)
    assert run([op], platform="meta") == ["status_missing"]
    active = dataclasses.replace(op, body={"name": "x", "status": "ACTIVE"}, financial=True)
    assert run([active], platform="meta", allow_activate=True) == ["status_not_paused"]
    paused = {"name": "x", "status": "PAUSED"}
    if path.endswith("copies"):  # a copy also needs status_option PAUSED (R3 fix a)
        assert run([dataclasses.replace(op, body=paused)], platform="meta") == ["status_missing"]
        paused = wp.force_paused(paused, "meta", copy=True)
    assert run([dataclasses.replace(op, body=paused)], platform="meta") == []


def test_meta_post_to_an_entity_is_not_a_creation():
    assert not wp.is_create(meta_op(url=f"{GRAPH}/120001"))
    assert not wp.is_create(meta_op(method="DELETE", url=f"{GRAPH}/act_{ACCOUNT}/adlabels"))


@pytest.mark.parametrize("url", ["https://api.openai.test/v1/campaigns", "https://api.openai.test/v1/ad_groups",
                                 "https://api.openai.test/v1/ads",
                                 "https://api.openai.test/v1/ad_accounts/acct_main/campaigns"])
def test_openai_post_to_a_collection_is_a_creation(url):
    slug = "acct_main"
    op = WriteOp(op_id="o1", platform="openai_ads", account_id=slug, action="update", method="POST", url=url,
                 body={"name": "x"}, before={"account_id": slug})
    assert wp.is_create(op)
    out = run([op], platform="openai_ads", ctx=FakeCtx(owned=frozenset({slug})), account_allow=frozenset({slug}))
    assert out == ["status_missing"]
    assert not wp.is_create(dataclasses.replace(op, url="https://api.openai.test/v1/campaigns/1"))


# ------------------------------------------------------------------ N5a currency minor units
def _meta_budget(currency, value, old=None, action="budget_change"):
    before = {"currency": currency, "account_id": ACCOUNT}
    if old is not None:
        before["daily_budget"] = old
    return meta_op(action=action, financial=True, body={"daily_budget": value}, before=before)


def test_minor_unit_multipliers():
    assert wp.minor_unit_multiplier("EUR") == 10_000
    assert wp.minor_unit_multiplier("jpy") == 1_000_000
    assert wp.minor_unit_multiplier("KWD") == 1_000
    assert wp.minor_unit_multiplier("XYZ") is None and wp.minor_unit_multiplier(None) is None
    for code in ("JPY", "KRW", "VND", "CLP", "ISK", "UGX", "XAF", "XOF", "PYG", "RWF"):
        assert wp.MINOR_UNIT_DECIMALS[code] == 0, code
    for code in ("BHD", "KWD", "OMR", "JOD", "TND"):
        assert wp.MINOR_UNIT_DECIMALS[code] == 3, code


def test_zero_decimal_currency_uses_its_exponent():
    # 60 JPY is 60 000 000 micros (over a 50 M cap); read as 2 decimals it would look like 600 000 micros
    assert run([_meta_budget("JPY", "60", old="55")], platform="meta", cap_currency="JPY") == ["budget_over_cap"]
    assert run([_meta_budget("JPY", "40", old="38")], platform="meta", cap_currency="JPY") == []
    assert wp.budget_values({"daily_budget": "60", "currency": "JPY"}) == [60_000_000]
    assert wp.budget_values({"daily_budget": "60"}, currency="KRW") == [60_000_000]


def test_three_decimal_currency_uses_its_exponent():
    # 40 000 fils = 40 KWD = 40 000 000 micros (under the cap); as 2 decimals it would be 400 M
    assert run([_meta_budget("KWD", "40000", old="38000")], platform="meta", cap_currency="KWD") == []
    assert run([_meta_budget("KWD", "60000", old="55000")], platform="meta", cap_currency="KWD") == \
        ["budget_over_cap"]


def test_increase_bound_uses_the_same_exponent():
    assert run([_meta_budget("JPY", "13", old="10")], platform="meta", cap_currency="JPY") == \
        ["increase_too_large"]


def test_unknown_currency_unit_is_refused():
    assert run([_meta_budget("XYZ", "10", old="10")], platform="meta", cap_currency="XYZ") == \
        ["currency_unit_unknown"]
    # micros fields do not depend on the currency decimals
    assert run([budget_op(before={"amountMicros": 10_000_000, "currency": "XYZ"})], cap_currency="XYZ") == []


def test_plan_total_uses_the_currency_exponent():
    ops = [dataclasses.replace(_meta_budget("JPY", "40", old="40"), op_id=f"m{i}", url=f"{GRAPH}/12000{i}")
           for i in range(2)]
    assert run(ops, platform="meta", cap_currency="JPY", max_ops_per_plan=1) == ["too_many_ops",
                                                                                   "plan_budget_over_cap"]


# ------------------------------------------------------------------ N5b undeclared personal data
@pytest.mark.parametrize("key", ["email", "emailAddress", "EMAIL_ADDRESS", "phone", "phone_number",
                                 "hashedEmail", "hashed-phone-number", "em", "ph", "userData", "user_identifiers",
                                 "first_name", "lastName", "address", "ip_address", "client_ip_address"])
def test_pii_keys_must_be_declared(key):
    nested = {"status": "PAUSED", "operations": [{"create": {"status": "PAUSED", "x": [{key: "v"}]}}]}
    assert run([pause_op(body=nested)]) == ["pii_undeclared"]
    assert run([pause_op(params={"data": json.dumps([{key: "v"}])})]) == ["pii_undeclared"]
    assert run([pause_op(body={key: "v"}, pii=True)]) == ["pii_not_allowed"]
    assert run([pause_op(body={key: "v"}, pii=True)], allow_pii=True) == []
    assert wp.pii_keys(pause_op(body={key: "v"})) == [key]


def test_pii_lookalikes_are_not_flagged():
    for key in ("phase", "item", "ip", "name", "geo_locations"):  # emails_sent: now flagged (substring, R3 e)
        assert run([pause_op(body={"status": "PAUSED", key: "v"})]) == [], key
    assert run([pause_op(body={"status": "PAUSED", "note": "jo@example.com"})]) == []


# ------------------------------------------------------------------ robustness
def test_huge_or_deep_payloads_fail_closed(monkeypatch):
    deep: dict = {}
    node = deep
    for _ in range(5000):
        node["x"] = {}
        node = node["x"]
    node["status"] = "ACTIVE"
    assert run([pause_op(body=deep)]) == ["activation_not_allowed", "financial_flag_missing"]
    monkeypatch.setattr(wp, "_MAX_NODES", 50)
    assert run([pause_op(body={"items": list(range(100))})]) == ["payload_too_large"]


# ------------------------------------------------------------------ R3 a: every status key, Meta copies
@pytest.mark.parametrize("key,value", [("status_option", "ACTIVE"), ("effective_status", "ACTIVE"),
                                       ("adset_status", "ACTIVE"), ("ad_status", "ACTIVE"),
                                       ("campaign_status", "ENABLED"), ("execution_status", "ACTIVE"),
                                       ("delivery_status", "ACTIVE"), ("state", "ENABLED"), ("enabled", True),
                                       ("is_active", "true"), ("active", True), ("paused", False),
                                       ("servingStatus", "ENABLED")])
def test_every_status_key_is_a_status(key, value):
    assert run([meta_op(body={key: value})], platform="meta") == ["activation_not_allowed",
                                                                   "financial_flag_missing"]
    assert wp.activates(meta_op(body={key: value}))


def test_boolean_status_keys_paused_and_force_paused():
    assert run([meta_op(body={"enabled": False})], platform="meta") == []
    assert run([meta_op(body={"paused": True})], platform="meta") == []
    assert "status_unparseable" in run([meta_op(body={"enabled": 1})], platform="meta")
    assert wp.force_paused({"enabled": True, "paused": False, "x": {"is_active": True}}, "gtm") == \
        {"enabled": False, "paused": True, "x": {"is_active": False}}


def test_meta_copy_needs_status_option_paused():
    copy = meta_op(action="copy", url=f"{GRAPH}/120001/copies", body={"status": "PAUSED"})
    assert run([copy], platform="meta") == ["status_missing"]  # copies inherit the source's status
    inherit = dataclasses.replace(copy, body={"status": "PAUSED", "status_option": "INHERITED_FROM_SOURCE"})
    assert run([inherit], platform="meta") == ["status_unknown", "status_missing"]
    active = dataclasses.replace(copy, body={"status_option": "ACTIVE"}, financial=True)
    assert run([active], platform="meta", allow_activate=True) == ["status_not_paused"]
    nested = dataclasses.replace(copy, body={"status": "PAUSED", "x": {"status_option": "PAUSED"}})
    assert run([nested], platform="meta") == ["status_missing"]
    assert run([dataclasses.replace(copy, params={"status_option": "PAUSED"})], platform="meta") == []
    assert wp.force_paused({"deep_copy": "true"}, "meta", copy=True)["status_option"] == "PAUSED"


# ------------------------------------------------------------------ R3 b: PAUSED at the first level only
def test_nested_paused_does_not_make_a_flat_creation_paused():
    create = meta_op(action="create_ad", url=f"{GRAPH}/act_{ACCOUNT}/ads", before=None,
                     body={"name": "x", "tracking_specs": [{"status": "PAUSED"}]})
    assert run([create], platform="meta") == ["status_missing"]
    assert run([dataclasses.replace(create, body={"name": "x", "status": "PAUSED",
                                                  "tracking_specs": [{"status": "PAUSED"}]})],
               platform="meta") == []
    assert run([dataclasses.replace(create, body={"name": "x"}, params={"status": "PAUSED"})],
               platform="meta") == []
    # Google Ads: a PAUSED directly in the create / update object counts
    google = WriteOp(op_id="c1", platform="google_ads", account_id=ACCOUNT, action="create_campaign",
                     method="POST", url=URL, body={"operations": [{"update": {"status": "PAUSED"}}]})
    assert run([google]) == []
    deep = dataclasses.replace(google, body={"operations": [{"update": {"x": {"status": "PAUSED"}}}]})
    assert run([deep]) == ["status_missing"]
    assert wp.force_paused({"name": "x", "tracking_specs": [{"status": "ACTIVE"}]}, "meta") == \
        {"name": "x", "tracking_specs": [{"status": "PAUSED"}], "status": "PAUSED"}


# ------------------------------------------------------------------ R3 c: method override, form strings
@pytest.mark.parametrize("key", ["method", "_method", "http_method", "httpMethod", "override",
                                 "X-HTTP-Method-Override", "relative_url", "batch"])
def test_method_override_in_payload_is_refused(key):
    assert run([pause_op(body={"status": "PAUSED", key: "DELETE"})]) == ["method_override_in_payload"]
    assert run([pause_op(params={"spec": json.dumps({key: "GET"})})]) == ["method_override_in_payload"]


def test_form_strings_are_inspected():
    assert run([pause_op(params={"spec": "name=x&status=ACTIVE"})]) == ["activation_not_allowed",
                                                                       "financial_flag_missing"]
    batch = json.dumps([{"method": "POST", "relative_url": "120001", "body": "name=x&status=ACTIVE"}])
    out = run([meta_op(params={"batch": batch})], platform="meta")
    assert "method_override_in_payload" in out and "activation_not_allowed" in out
    # a repeated key: every value is seen
    assert run([pause_op(params={"spec": "status=PAUSED&status=ACTIVE"})]) == ["activation_not_allowed",
                                                                              "financial_flag_missing"]
    assert run([budget_op(params={"spec": "amountMicros=90000000"})])[:1] == ["budget_over_cap"]
    # base64 padding, URLs and plain text are not form strings
    for text in ("YWJj==", "abc=", "https://example.com/?a=b", "a = b", "x"):
        assert run([pause_op(params={"note": text})]) == [], text


# ------------------------------------------------------------------ R3 d: budget references are financial
@pytest.mark.parametrize("body", [
    {"operations": [{"update": {"resourceName": f"customers/{ACCOUNT}/campaigns/1",
                                "campaignBudget": f"customers/{ACCOUNT}/campaignBudgets/9"}}]},
    {"operations": [{"update": {"resourceName": f"customers/{ACCOUNT}/campaigns/1"},
                     "updateMask": "campaign_budget"}]},
    {"budget_id": "9"}, {"shared_budget": "9"}, {"campaign_budget": {"id": "9"}}])
def test_budget_reference_without_budget_amount_is_financial(body):
    assert run([pause_op(body=body)]) == ["financial_undeclared"]
    assert run([pause_op(body=body, financial=True)]) == []


@pytest.mark.parametrize("key", ["adset_id", "campaign_id", "ad_group_id", "adGroup"])
def test_moving_an_entity_is_financial(key):
    assert run([meta_op(body={"status": "PAUSED", key: "120002"})], platform="meta") == ["financial_undeclared"]
    assert run([meta_op(body={"status": "PAUSED", key: "120002"}, financial=True)], platform="meta") == []
    # a creation names its parent: not a move
    create = meta_op(action="create_ad", url=f"{GRAPH}/act_{ACCOUNT}/ads", before=None,
                     body={"status": "PAUSED", key: "120002"})
    assert run([create], platform="meta") == []


# ------------------------------------------------------------------ R3 e: canonical PII list
@pytest.mark.parametrize("key", ["contact_email", "mobilePhone", "hashed_id", "user_data", "ipAddress",
                                 "firstName", "last_name", "zip_code", "postalCode", "external_id", "gclid",
                                 "fbc", "fbp", "fn", "ln", "zp", "client_user_agent"])
def test_pii_canonical_list(key):
    assert wp.is_pii_key(key)
    assert run([pause_op(body={"status": "PAUSED", key: "v"})]) == ["pii_undeclared"]
    assert run([pause_op(body={"status": "PAUSED", "updateMask": f"x.{key}"})]) == ["pii_undeclared"]


# ------------------------------------------------------------------ R3 g: huge integers
def test_huge_integer_budgets_are_unparseable():
    assert run([budget_op(body={"amountMicros": "9" * 5000})]) == ["budget_unparseable"]
    assert run([budget_op(body={"amountMicros": 10 ** 5000})]) == ["budget_unparseable"]
    assert wp.budget_values({"amountMicros": "1" * 5000}) == []


# ------------------------------------------------------------------ R3 structural: field allow-list
def test_no_field_allowlist_is_refused(monkeypatch):
    monkeypatch.setattr(wp, "_TEST_PERMISSIVE", False)
    assert run([pause_op()]) == ["no_field_allowlist"]
    monkeypatch.setattr(wp, "_TEST_PERMISSIVE", frozenset({"pause"}))
    assert run([pause_op()]) == []
    monkeypatch.setattr(wp, "_TEST_PERMISSIVE", frozenset({("google_ads", "pause")}))
    assert run([pause_op()]) == []
    assert run([pause_op(action="set_status")]) == ["no_field_allowlist"]


def test_field_allowlist_refuses_an_unknown_field(monkeypatch):
    monkeypatch.setattr(wp, "_TEST_PERMISSIVE", False)
    wp.register_field_allowlist("google_ads", "pause", ["status"])
    assert run([pause_op()]) == []
    out = wp.check_ops(FakeCtx(), "google_ads", [pause_op(body={"status": "PAUSED", "name": "x"})], open_policy())
    assert [v["error"] for v in out] == ["field_not_allowed"] and "'name'" in out[0]["detail"]
    assert run([pause_op(params={"note": "x"})]) == ["field_not_allowed"]
    # the allow-list of another action does not apply
    assert run([pause_op(action="set_status")]) == ["no_field_allowlist"]
    # the registry is per platform
    assert wp.field_allowlist("meta", "pause") is None and wp.field_allowlist("google_ads", "PAUSE") == {"status"}


def test_field_allowlist_dotted_paths_masks_and_subtrees(monkeypatch):
    monkeypatch.setattr(wp, "_TEST_PERMISSIVE", False)
    wp.register_field_allowlist("google_ads", "pause", ["operations.update.resourceName",
                                                        "operations.update.status", "operations.updateMask"])
    body = {"operations": [{"update": {"resourceName": f"customers/{ACCOUNT}/campaigns/1", "status": "PAUSED"},
                            "updateMask": "status"}]}
    assert run([pause_op(body=body)]) == []
    body["operations"][0]["update"]["name"] = "x"
    out = wp.check_ops(FakeCtx(), "google_ads", [pause_op(body=body)], open_policy())
    assert [v["error"] for v in out] == ["field_not_allowed"] and "operations.update.name" in out[0]["detail"]
    del body["operations"][0]["update"]["name"]
    body["operations"][0]["updateMask"] = "status,name"
    out = wp.check_ops(FakeCtx(), "google_ads", [pause_op(body=body)], open_policy())
    assert [v["error"] for v in out] == ["field_not_allowed"] and "updateMask:name" in out[0]["detail"]
    # an intermediate container must hold a container, not a scalar
    assert run([pause_op(body={"operations": "x"})]) == ["field_not_allowed"]
    # JSON / form strings are walked with the same paths
    wp.register_field_allowlist("meta", "update", ["status", "targeting.geo_locations.countries.*"])
    ok = meta_op(body={"status": "PAUSED", "targeting": json.dumps({"geo_locations": {"countries": ["FR"]}})})
    assert run([ok], platform="meta") == []
    assert run([meta_op(params={"status": "PAUSED", "spec": "status=PAUSED"})], platform="meta") == \
        ["field_not_allowed"]


def test_register_field_allowlist_validates_paths():
    for bad in ("", "a..b", "a.*.b", "*", "a.b.c.d.e"):
        with pytest.raises(ValueError):
            wp.register_field_allowlist("google_ads", "x", [bad])
    with pytest.raises(TypeError):
        wp.register_field_allowlist("google_ads", "x", "status")
    assert wp.register_field_allowlist("google_ads", "x", ["Update.Amount_Micros", "a.b.c.d.*"]) == \
        frozenset({"update.amountmicros", "a.b.c.d.*"})


def test_field_allowlist_deeper_than_four_needs_a_subtree(monkeypatch):
    monkeypatch.setattr(wp, "_TEST_PERMISSIVE", False)
    deep = {"status": "PAUSED", "a": {"b": {"c": {"d": {"e": 1}}}}}
    wp.register_field_allowlist("google_ads", "pause", ["status", "a.b.c.d"])
    assert run([pause_op(body=deep)]) == ["field_not_allowed"]
    monkeypatch.setattr(wp, "_TEST_PERMISSIVE", True)  # replace=True is accepted in tests only
    wp.register_field_allowlist("google_ads", "pause", ["status", "a.b.c.d.*"], replace=True)
    assert run([pause_op(body=deep)]) == []


# ------------------------------------------------------------------ R4 B2: non-string update masks
@pytest.mark.parametrize("key", ["updateMask", "update_mask", "fieldMask", "field_mask", "paths"])
@pytest.mark.parametrize("mask", [["campaign_budget"], {"paths": ["campaign_budget"]}, '["campaign_budget"]',
                                  None, 7, "status;campaign_budget"])
def test_non_string_update_mask_is_refused(key, mask):
    op = pause_op(body={"operations": [{"update": {"status": "PAUSED"}, key: mask}]})
    assert "update_mask_not_string" in run([op])
    assert "update_mask_not_string" in run([pause_op(params={key: mask})])


def test_string_masks_still_pass_and_are_checked(monkeypatch):
    assert run([pause_op(body={"status": "PAUSED", "update_mask": "status, name"})]) == []
    assert run([pause_op(body={"status": "PAUSED", "paths": "campaign_budget"})]) == ["financial_undeclared"]
    assert run([pause_op(body={"status": "PAUSED", "field_mask": "x.email"})]) == ["pii_undeclared"]
    monkeypatch.setattr(wp, "_TEST_PERMISSIVE", False)
    wp.register_field_allowlist("google_ads", "pause", ["status", "updateMask"])
    out = run([pause_op(body={"status": "PAUSED", "updateMask": ["campaign_budget"]})])
    assert "update_mask_not_string" in out


# ------------------------------------------------------------------ R4 B3: allow-list registration
@pytest.mark.parametrize("path", ["campaign_budget", "operations.update.campaignBudget", "budget.*",
                                  "a.b.daily_spend_limit", "x.price", "x.cost_cap", "x.Bid-Amount",
                                  "x.target_roas", "x.lifetime_budget", "x.conversion_value", "x.SPEND",
                                  "amount_micros.x", "a.b.Value", "a.b.limits.*", "a.b.campaignbudget",
                                  "a.b.maxcpc", "a.b.dailybudget.x"])
def test_register_refuses_money_fields(path):
    with pytest.raises(ValueError):
        wp.register_field_allowlist("google_ads", "x", [path])
    assert wp.field_allowlist("google_ads", "x") is None


def test_register_accepts_daily_budget_leaves_only():
    for key in wp.DAILY_BUDGET_KEYS:
        assert wp.register_field_allowlist("google_ads", f"b_{key}", [f"operations.update.{key}"])
    with pytest.raises(ValueError):  # a daily budget key is managed only as the leaf
        wp.register_field_allowlist("google_ads", "c", ["daily_budget.x"])


@pytest.mark.parametrize("path", ["operations.*", "operations.update.*", "targeting.*"])
def test_register_refuses_wide_subtrees(path):
    with pytest.raises(ValueError):
        wp.register_field_allowlist("meta", "update", [path])
    assert wp.register_field_allowlist("meta", "update", ["targeting.geo_locations.countries.*"])


def test_subtree_does_not_open_money_fields_at_check_time(monkeypatch):
    monkeypatch.setattr(wp, "_TEST_PERMISSIVE", False)
    wp.register_field_allowlist("google_ads", "pause", ["status", "a.b.c.*", "operations.updateMask"])
    assert run([pause_op(body={"status": "PAUSED", "a": {"b": {"c": {"name": "x"}}}})]) == []
    out = run([pause_op(body={"status": "PAUSED", "a": {"b": {"c": {"unit_price": 1}}}})])
    assert out == ["field_not_allowed"]
    out = run([pause_op(body={"status": "PAUSED", "a": {"b": {"c": {"nested": {"max_cost": 1}}}}})])
    assert out == ["field_not_allowed"]
    # a mask entering the subtree cannot name money either
    body = {"status": "PAUSED", "operations": [{"updateMask": "c.price"}]}
    assert run([pause_op(body=body)]) == ["field_not_allowed"]
    assert run([pause_op(body={"status": "PAUSED", "operations": [{"updateMask": "c.name"}]})]) == []


def test_register_refuses_a_silent_replacement():
    first = wp.register_field_allowlist("google_ads", "pause", ["status"])
    assert wp.register_field_allowlist("google_ads", "PAUSE", ["Status"]) == first  # same list: no-op
    with pytest.raises(ValueError):
        wp.register_field_allowlist("google_ads", "pause", ["status", "name"])
    assert wp.field_allowlist("google_ads", "pause") == {"status"}
    assert wp.register_field_allowlist("google_ads", "pause", ["status", "name"], replace=True) == \
        {"status", "name"}


def test_replace_is_refused_outside_tests(monkeypatch):
    wp.register_field_allowlist("google_ads", "pause", ["status"])
    monkeypatch.setattr(wp, "_TEST_PERMISSIVE", False)
    with pytest.raises(ValueError, match="tests only"):
        wp.register_field_allowlist("google_ads", "pause", ["status", "name"], replace=True)
    with pytest.raises(ValueError, match="tests only"):  # even for a new (platform, action)
        wp.register_field_allowlist("meta", "update", ["status"], replace=True)
    assert wp.field_allowlist("google_ads", "pause") == {"status"} and wp.field_allowlist("meta", "update") is None


def test_golden_allowlists_is_a_sorted_snapshot():
    assert wp.golden_allowlists() == {}
    wp.register_field_allowlist("meta", "update", ["status", "name"])
    wp.register_field_allowlist("google_ads", "pause", ["operations.update.status", "operations.updateMask"])
    wp.register_field_allowlist("google_ads", "budget", ["operations.update.amountMicros"])
    wp.register_field_allowlist("meta", "capi", ["data.custom_data.value", "data.event_name"],
                                money_ok=["data.custom_data.value"])
    snap = wp.golden_allowlists()
    assert snap == {"google_ads": {"budget": {"fields": ["operations.update.amountmicros"], "money_ok": []},
                                   "pause": {"fields": ["operations.update.status", "operations.updatemask"],
                                             "money_ok": []}},
                    "meta": {"capi": {"fields": ["data.customdata.value", "data.eventname"],
                                      "money_ok": ["data.customdata.value"]},
                             "update": {"fields": ["name", "status"], "money_ok": []}}}
    assert list(snap) == ["google_ads", "meta"] and list(snap["google_ads"]) == ["budget", "pause"]
    assert json.loads(json.dumps(snap)) == snap
    snap["meta"]["update"]["fields"].append("x")  # a copy: the registry is unchanged
    snap["meta"]["capi"]["money_ok"].append("x")
    assert wp.field_allowlist("meta", "update") == {"status", "name"}
    assert wp.field_money_ok("meta", "capi") == {"data.customdata.value"}


# ------------------------------------------------------------------ R5 N1/N2: money is matched by whole words
@pytest.mark.parametrize("path", ["caption", "link_data.caption", "landscape", "image.landscape_url",
                                  "forbidden", "limited", "is_limited", "display_name", "feed_id",
                                  "a.c.ap", "capture_mode"])
def test_ordinary_words_containing_money_letters_are_allowed(path):
    assert wp.register_field_allowlist("meta", "update", [path])


@pytest.mark.parametrize("path", ["maxCpc", "enhanced_cpc", "fixedCpm", "daily_spend_cap", "budget.amount",
                                  "salePrice", "custom_data.value", "price.amountMicros", "x.tcpa",
                                  "x.target_troas", "x.cpa", "x.cpv", "x.revenue", "x.fee", "x.charge",
                                  "x.pay", "x.payment", "x.margin", "x.markup", "x.fees", "x.spending",
                                  "x.bidding"])
def test_money_words_need_an_exact_money_ok(path):
    with pytest.raises(ValueError, match="names money"):
        wp.register_field_allowlist("meta", "update", [path])
    assert wp.field_allowlist("meta", "update") is None
    assert wp.register_field_allowlist("meta", "update", [path, "status"], money_ok=(path,))
    assert wp.field_money_ok("meta", "update") == {wp._norm_path(path)}


def test_money_ok_covers_the_exact_path_only():
    with pytest.raises(ValueError, match="not one of the listed fields"):  # a child is not covered
        wp.register_field_allowlist("meta", "capi", ["custom_data.value.x"], money_ok=("custom_data.value",))
    with pytest.raises(ValueError, match="names money"):  # another money path stays refused
        wp.register_field_allowlist("meta", "capi", ["custom_data.value", "custom_data.price"],
                                    money_ok=("custom_data.value",))
    with pytest.raises(ValueError, match="not a subtree"):
        wp.register_field_allowlist("meta", "capi", ["a.b.value.*"], money_ok=("a.b.value.*",))
    with pytest.raises(TypeError):
        wp.register_field_allowlist("meta", "capi", ["custom_data.value"], money_ok="custom_data.value")
    assert wp.field_allowlist("meta", "capi") is None
    # matching is on the normalized path
    assert wp.register_field_allowlist("meta", "capi", ["Custom_Data.Value"], money_ok=("customData.value",))


def test_money_ok_child_is_refused_at_check_time(monkeypatch):
    monkeypatch.setattr(wp, "_TEST_PERMISSIVE", False)
    wp.register_field_allowlist("meta", "update", ["status", "custom_data.value"], money_ok=("custom_data.value",))
    assert run([meta_op(body={"status": "PAUSED", "custom_data": {"value": "12.5"}})], platform="meta") == []
    out = run([meta_op(body={"status": "PAUSED", "custom_data": {"value": {"x": 1}}})], platform="meta")
    assert out == ["field_not_allowed"]


def test_daily_budget_leaves_need_no_money_ok():
    assert wp.register_field_allowlist("google_ads", "budget", ["operations.update.amountMicros",
                                                                "daily_budget"])
    assert wp.field_money_ok("google_ads", "budget") == frozenset()


def test_same_fields_with_another_money_ok_is_a_different_list():
    wp.register_field_allowlist("meta", "capi", ["custom_data.value"], money_ok=("custom_data.value",))
    with pytest.raises(ValueError, match="different"):
        wp.register_field_allowlist("meta", "capi", ["custom_data.value", "x.value"],
                                    money_ok=("custom_data.value", "x.value"))


def test_subtree_children_use_whole_words_at_check_time(monkeypatch):
    monkeypatch.setattr(wp, "_TEST_PERMISSIVE", False)
    wp.register_field_allowlist("meta", "update", ["status", "creative.object_story_spec.link_data.*"])
    spec = {"caption": "c", "picture": "https://x/landscape.jpg", "limited": "x"}
    body = {"status": "PAUSED", "creative": {"object_story_spec": {"link_data": spec}}}
    assert run([meta_op(body=body)], platform="meta") == []
    for money in ("maxCpc", "sale_price", "maxspend", "fee"):
        body["creative"]["object_story_spec"]["link_data"] = {money: 1}
        assert "field_not_allowed" in run([meta_op(body=body)], platform="meta"), money


@pytest.mark.parametrize("key", ["maxCpc", "enhanced_cpc", "fixedCpm", "max_bid", "x_tcpa", "target_troas",
                                 "cpa_goal", "cpv", "roas"])
def test_bid_words_are_refused_as_bidding(key):
    assert "bidding_not_supported" in run([pause_op(body={"status": "PAUSED", key: 1})])


@pytest.mark.parametrize("key", ["forbidden", "caption", "landscape", "limited"])
def test_words_containing_bid_letters_are_not_bidding(key):
    assert run([pause_op(body={"status": "PAUSED", key: "x"})]) == []


def test_field_words_split():
    assert wp.field_words("maxCpc") == ["max", "cpc"]
    assert wp.field_words("daily_spend_cap") == ["daily", "spend", "cap"]
    assert wp.field_words("CPCBid2x") == ["cpc", "bid", "2", "x"]
    assert wp.field_words("enhanced-cpc") == ["enhanced", "cpc"]
    assert wp.field_words("caption") == ["caption"]
    assert not wp.names_money(["caption"]) and wp.names_money(["salePrice"])
    assert not wp.names_money(["operations", "update", "amountMicros"])  # daily budget leaf


# ------------------------------------------------------------------ R5 N6: private, freezable registry
def test_registry_is_private():
    assert not hasattr(wp, "FIELD_ALLOWLIST") and "FIELD_ALLOWLIST" not in wp.__all__


def test_freeze_refuses_any_new_registration():
    wp.register_field_allowlist("meta", "update", ["status"])
    wp.freeze_allowlists()
    try:
        assert wp.allowlists_frozen()
        assert wp.register_field_allowlist("meta", "update", ["status"]) == {"status"}  # identical: no-op
        with pytest.raises(ValueError, match="registry_frozen"):
            wp.register_field_allowlist("meta", "pause", ["status"])
        with pytest.raises(ValueError, match="registry_frozen"):
            wp.register_field_allowlist("meta", "update", ["status", "name"], replace=True)
        assert wp.field_allowlist("meta", "pause") is None
    finally:
        wp._unfreeze_for_tests()
    assert not wp.allowlists_frozen()
    assert wp.register_field_allowlist("meta", "pause", ["status"])


# ------------------------------------------------------------------ R4 hardening: ASCII statuses
@pytest.mark.parametrize("value", ["pauſed", "PAUSED​", "РAUSED", "actıve", "ENABLED X"])
def test_non_ascii_status_is_unknown(value):
    assert "status_unknown" in run([pause_op(body={"status": value})])
    assert "status_unknown" in run([pause_op(body={"operations": [{"update": {"status": value}}]})])


# ------------------------------------------------------------------ M1: Google Ads negatives need no PAUSED
MUTATE = f"{GADS}/customers/{ACCOUNT}/googleAds:mutate"
KEYWORD = {"text": "free", "matchType": "EXACT"}


def gads_create(*items, action="add_negatives", financial=False, url=MUTATE, account=ACCOUNT):
    return WriteOp(op_id="g1", platform="google_ads", account_id=account, action=action, method="POST", url=url,
                   body={"mutateOperations": list(items)}, risk="low", financial=financial,
                   before={"account_id": account, "currency": "EUR"})


def criterion(**create):
    return {"campaignCriterionOperation": {"create": {"campaign": f"customers/{ACCOUNT}/campaigns/1", **create}}}


def shared(**create):
    return {"sharedCriterionOperation": {"create": {"sharedSet": f"customers/{ACCOUNT}/sharedSets/2", **create}}}


def test_negative_keywords_need_no_paused_status():
    assert run([gads_create(criterion(negative=True, keyword=KEYWORD))]) == []
    assert run([gads_create(shared(keyword=KEYWORD))]) == []
    assert run([gads_create(shared(keyword=KEYWORD, negative=True), criterion(negative=True, keyword=KEYWORD))]) == []


@pytest.mark.parametrize("create", [
    {"negative": False, "keyword": KEYWORD},           # a positive keyword spends
    {"keyword": KEYWORD},                              # no negative at all = positive
    {"negative": "true", "keyword": KEYWORD},          # a string is not the bool True
    {"negative": "True", "keyword": KEYWORD},
    {"negative": 1, "keyword": KEYWORD},
    {"negative": True},                                # not a keyword
    {"negative": True, "keyword": {}},
    {"negative": True, "keyword": "free"},
    {"negative": True, "placement": {"url": "x.example"}},
])
def test_positive_or_ambiguous_criteria_still_need_paused(create):
    assert run([gads_create(criterion(**create))]) == ["status_missing"]


def test_negative_exemption_is_google_ads_and_operation_kind_only():
    assert run([gads_create(shared(keyword=KEYWORD, negative=False))]) == ["status_missing"]
    assert run([gads_create(shared())]) == ["status_missing"]  # no keyword
    # the same create under another operation kind, or without one
    inner = criterion(negative=True, keyword=KEYWORD)["campaignCriterionOperation"]
    assert run([gads_create({"adGroupCriterionOperation": inner})]) == ["status_missing"]
    flat = dataclasses.replace(gads_create(), body={"operations": [{"create": {"negative": True,
                                                                               "keyword": KEYWORD}}]})
    assert run([flat]) == ["status_missing"]
    meta = meta_op(action="create", url=f"{GRAPH}/act_{ACCOUNT}/adsets",
                   body={"campaignCriterionOperation": {"create": {"negative": True, "keyword": KEYWORD}}})
    assert run([meta], platform="meta") == ["status_missing"]


def test_a_negative_does_not_exempt_a_positive_or_a_campaign_in_the_same_op():
    neg = criterion(negative=True, keyword=KEYWORD)
    assert run([gads_create(neg, criterion(negative=False, keyword=KEYWORD))]) == ["status_missing"]
    campaign = {"campaignOperation": {"create": {"name": "x"}}}
    assert run([gads_create(neg, campaign, action="create_campaign")]) == ["status_missing"]
    active = criterion(negative=True, keyword=KEYWORD, status="ENABLED")
    assert "status_not_paused" in run([gads_create(active)])


# ------------------------------------------------------------------ M1: a budget created with a PAUSED campaign
TEMP_BUDGET = f"customers/{ACCOUNT}/campaignBudgets/-1"


def budget_create(name=TEMP_BUDGET, amount="5000000"):
    return {"campaignBudgetOperation": {"create": {"resourceName": name, "name": "b", "amountMicros": amount,
                                                   "deliveryMethod": "STANDARD", "explicitlyShared": False}}}


def campaign_create(budget=TEMP_BUDGET, status="PAUSED", name=f"customers/{ACCOUNT}/campaigns/-2"):
    create = {"resourceName": name, "name": "c", "campaignBudget": budget}
    if status is not None:
        create["status"] = status
    return {"campaignOperation": {"create": create}}


def new_campaign(*items):
    return gads_create(*items, action="create_campaign", financial=True)


def test_budget_created_with_its_paused_campaign_needs_no_status():
    assert run([new_campaign(budget_create(), campaign_create())]) == []
    # two paused campaigns sharing the new budget, in the same op
    second = campaign_create(name=f"customers/{ACCOUNT}/campaigns/-3")
    assert run([new_campaign(budget_create(), campaign_create(), second)]) == []


@pytest.mark.parametrize("items", [
    [budget_create()],                                                     # alone
    [budget_create(), campaign_create(status=None)],                       # campaign without status
    [budget_create(), campaign_create(budget=f"customers/{ACCOUNT}/campaignBudgets/-9")],  # another budget
    [budget_create(name=f"customers/{ACCOUNT}/campaignBudgets/55"), campaign_create(
        budget=f"customers/{ACCOUNT}/campaignBudgets/55")],                # not a temporary id
    [budget_create(name=f"customers/{ACCOUNT}/campaignBudgets/-0"), campaign_create(
        budget=f"customers/{ACCOUNT}/campaignBudgets/-0")],                # -0 is not a temporary id
    [budget_create(), budget_create(), campaign_create()],                 # the same temporary name twice
])
def test_budget_without_its_paused_campaign_still_needs_paused(items):
    assert "status_missing" in run([new_campaign(*items)])


def test_budget_exemption_cannot_be_borrowed():
    # an enabled campaign using the budget: refused (status_not_paused), never exempted
    assert "status_not_paused" in run([new_campaign(budget_create(), campaign_create(status="ENABLED"))])
    # the budget referenced elsewhere in the op (an update moving an existing campaign onto it)
    move = {"campaignOperation": {"update": {"resourceName": f"customers/{ACCOUNT}/campaigns/7",
                                             "campaignBudget": TEMP_BUDGET}, "updateMask": "campaign_budget"}}
    assert "status_missing" in run([new_campaign(budget_create(), campaign_create(), move)])
    # the campaign in another op of the plan does not count
    a = new_campaign(budget_create())
    b = dataclasses.replace(new_campaign(campaign_create()), op_id="g2")
    assert run([a, b]) == ["status_missing"]
    # another account's temporary budget name
    foreign = f"customers/{OTHER}/campaignBudgets/-1"
    out = run([new_campaign(budget_create(name=foreign), campaign_create(budget=foreign))])
    assert "status_missing" in out and "account_not_bound" in out
    # a budget is still capped: over the cap is refused even with its paused campaign
    assert run([new_campaign(budget_create(amount="60000000"), campaign_create())]) == ["budget_over_cap"]
    # and on another platform the rule does not exist
    meta = meta_op(action="create", url=f"{GRAPH}/act_{ACCOUNT}/campaigns", financial=True,
                   body={"mutateOperations": [budget_create(), campaign_create()]})
    assert "status_missing" in run([meta], platform="meta")


# ------------------------------------------------------------------ M1: OpenAI Ads daily_spend_limit_micros
OPENAI = "https://api.ads.openai.com/v1"
OPENAI_BEFORE = {"account_id": ACCOUNT, "currency": "EUR", "budget": {"daily_spend_limit_micros": 10_000_000}}


def openai_budget(body, before=None, financial=True):
    return WriteOp(op_id="o1", platform="openai_ads", account_id=ACCOUNT, action="budget_change", method="POST",
                   url=f"{OPENAI}/campaigns/c1", body=body, risk="medium", financial=financial,
                   before=OPENAI_BEFORE if before is None else before)


def daily(micros):
    return {"budget": {"daily_spend_limit_micros": micros}}


def test_openai_daily_spend_limit_is_a_capped_daily_budget():
    assert wp.DAILY_BUDGET_KEYS["daily_spend_limit_micros"] == 1
    assert "dailyspendlimit" not in wp._UNSUPPORTED_PREFIXES
    assert run([openai_budget(daily(11_000_000))], platform="openai_ads") == []
    assert wp.budget_values(daily(11_000_000)) == [11_000_000]
    assert run([openai_budget(daily(60_000_000))], platform="openai_ads") == ["budget_over_cap",
                                                                              "increase_too_large"]
    assert run([openai_budget(daily(13_000_000))], platform="openai_ads") == ["increase_too_large"]
    assert "financial_flag_missing" in run([openai_budget(daily(1), financial=False)], platform="openai_ads")
    assert "budget_unparseable" in run([openai_budget(daily(1.5))], platform="openai_ads")
    # the currency comes from the snapshot only
    usd = {**OPENAI_BEFORE, "currency": "USD"}
    assert "currency_mismatch" in run([openai_budget(daily(11_000_000), usd)], platform="openai_ads")
    no_currency = {k: v for k, v in OPENAI_BEFORE.items() if k != "currency"}
    body = {"budget": {"daily_spend_limit_micros": 11_000_000}, "currency": "EUR"}
    assert "currency_unknown" in run([openai_budget(body, no_currency)], platform="openai_ads")


@pytest.mark.parametrize("key", ["daily_spend_limit", "daily_spend_limit_amount", "dailySpendLimitMinor",
                                 "lifetime_spend_limit_micros", "spend_cap"])
def test_other_spend_limits_stay_unsupported(key):
    assert "budget_kind_unsupported" in run([openai_budget({"budget": {key: 1}})], platform="openai_ads")


def test_openai_write_budget_supported_with_the_real_policy():
    from google_ecommerce_mcp import openai_write

    assert openai_write.budget_supported() is True
    assert not wp._is_unsupported("daily_spend_limit_micros") and wp._is_unsupported("daily_spend_limit")


# ------------------------------------------------------------------ M1: Meta write permission
class _Resp:
    def __init__(self, status):
        self.status_code, self.headers, self.ok = status, {}, False


@pytest.mark.parametrize("platform", ["meta", "meta_eco"])
def test_meta_permission_error_on_a_write_names_ads_management(platform):
    from google_ecommerce_mcp.ads_common import map_error

    data = {"error": {"message": "(#200) Requires ads_management permission", "code": 200}}
    for method in ("POST", "post", "DELETE", "PUT"):
        out = map_error(platform, _Resp(403), data, method=method)
        assert out["error"] == "missing_scope" and out["required_permission"] == "ads_management", method
        assert "ads_management" in out["fix"]
    for method in ("GET", "get", None):
        out = map_error(platform, _Resp(403), data, method=method)
        assert out["required_permission"] == "ads_read" and "ads_management" not in out["fix"], method
    assert map_error(platform, _Resp(403), data)["required_permission"] == "ads_read"
    google = map_error("google_ads", _Resp(403), {"error": {"message": "insufficient authentication scopes"}},
                       method="POST")
    assert "required_permission" not in google and google["error"] == "missing_scope"


def test_ads_client_reports_ads_management_on_a_refused_meta_write():
    from ads_fakes import FakeResponse

    from google_ecommerce_mcp.ads_common import AdsClient, AllowList

    class Session:
        trust_env = False

        def request(self, method, url, **kwargs):
            return FakeResponse(403, {"error": {"message": "(#200) Requires ads_management permission",
                                                "code": 200}})

    url = f"{GRAPH}/act_{ACCOUNT}"
    pattern = r"https://graph\.facebook\.com/v23\.0/act_[0-9]+"
    client = AdsClient("meta", AllowList([("GET", pattern), ("POST", pattern)]))
    client.session = Session()
    assert client.call("GET", url)["required_permission"] == "ads_read"
    assert client.call("POST", url, json={"status": "PAUSED"})["required_permission"] == "ads_management"


# ------------------------------------------------------------------ M1: %2F only in Search Console segments
GSC = "https://www.googleapis.com/webmasters/v3/sites"
SITE = "https%3A%2F%2Fwww.acme.fr%2F"
FEED_ENC = "https%3A%2F%2Fwww.acme.fr%2Fsitemap.xml"


def test_encoded_slash_is_accepted_in_the_search_console_segments_only():
    from google_ecommerce_mcp.ads_common import bare_url

    for url in (f"{GSC}/{SITE}/sitemaps/{FEED_ENC}", f"{GSC}/{SITE}/sitemaps", f"{GSC}/sc-domain%3Aacme.fr/sitemaps/"
                f"{FEED_ENC}", f"{GSC}/{SITE}/searchAnalytics/query", f"{GSC}/{SITE.lower()}/sitemaps"):
        assert bare_url(url) == url, url
    assert bare_url(f"{GSC}/{SITE}/sitemaps/{FEED_ENC}?x=1") == f"{GSC}/{SITE}/sitemaps/{FEED_ENC}"


@pytest.mark.parametrize("url", [
    f"{GADS}/customers/{ACCOUNT}%2Fx/campaigns:mutate",                         # Google Ads
    f"{GADS}/customers/{ACCOUNT}/campaigns%2F1:mutate",
    f"{GRAPH}/act_{ACCOUNT}%2Fcampaigns",                                         # Meta
    f"{GRAPH}/{ACCOUNT}%2f",
    "https://api.ads.openai.com/v1/campaigns/c1%2Fpause",                        # OpenAI Ads
    f"{GSC}/{SITE}/searchAnalytics%2Fquery",                                     # GSC, another segment
    f"{GSC}/{SITE}/sitemaps/{FEED_ENC}/x%2Fy",
    f"https://www.googleapis.com/webmasters/v3/x%2Fsites/{SITE}",
    f"https://www.googleapis.com/webmasters/v3%2Fsites/{SITE}",
    f"https://www.googleapis.com/indexing/v3/sites/{SITE}",                      # same host, other API
    f"https://www.googleapis.com:443/webmasters/v3/sites/{SITE}/sitemaps",       # host with a port
    f"https://searchconsole.googleapis.com/v1/sites/{SITE}",                     # other host
    f"https://evil.example/webmasters/v3/sites/{SITE}",
    f"{GSC}/{SITE.replace('%2F', '%252F')}/sitemaps",                            # double encoding
    f"{GSC}/{SITE}/sitemaps/{FEED_ENC.replace('%2F', '%252F')}",
    f"{GADS}/customers/{ACCOUNT}%252F/campaigns:mutate",
    f"{GSC}/{SITE}/sitemaps/https%3A%2F%2Fwww.acme.fr%2F%2e%2e%2Fx.xml",          # encoded dots
    f"{GSC}/{SITE}/sitemaps/https%3A%2F%2Fwww.acme.fr%5Cx.xml",                   # encoded backslash
    f"{GSC}/{SITE}/sitemaps/https%3A%2F%2Fwww.acme.fr%2F%00.xml",                 # encoded control character
    f"{GSC}/{SITE}/sitemaps/../{FEED_ENC}",                                       # dot segment
    f"{GSC}/{SITE}/sitemaps/{FEED_ENC};x",                                        # ;
    f"https://user@www.googleapis.com/webmasters/v3/sites/{SITE}/sitemaps",       # user info
    f"{GSC}/..%2F..%2F/sitemaps/x",                                               # dot components once decoded
    f"{GSC}/..%2Fsites/sitemaps/{FEED_ENC}",
    f"{GSC}/.%2F/sitemaps",
    f"{GSC}/{SITE}/sitemaps/https%3A%2F%2Fwww.acme.fr%2F..%2F..%2Fx.xml",
    f"{GSC}/{SITE}/sitemaps/..%2F",
    f"{GSC}/https%3A%2F%2Fwww.acme.fr%2F.%2F/sitemaps",
])
def test_encoded_slash_bypasses_are_refused(url):
    from google_ecommerce_mcp.ads_common import AllowList, WriteAllowList, bare_url

    assert bare_url(url) is None, url
    wide = r"https://www\.googleapis\.com/webmasters/v3/sites/[^/]+/sitemaps/[^/]+"
    assert WriteAllowList([("PUT", wide, None, "write")]).match("PUT", url) is None
    assert AllowList([("GET", wide)]).is_allowed("GET", url) is False


def test_grant_binds_the_encoded_url_as_sent():
    from google_ecommerce_mcp import ads_common

    url = f"{GSC}/{SITE}/sitemaps/{FEED_ENC}"
    digest = ads_common.request_digest("PUT", url)
    assert digest == ads_common.request_digest("PUT", url)
    decoded = f"{GSC}/https:%2F%2Fwww.acme.fr%2F/sitemaps/{FEED_ENC}"
    assert digest != ads_common.request_digest("PUT", decoded)
    assert ads_common.bare_url(url) != ads_common.bare_url(url.replace("%2F", "%2f"))  # never re-normalized


def test_ascii_status_with_spaces_still_parses():
    assert run([pause_op(body={"status": "  paused "})]) == []

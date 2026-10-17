"""Security gate M1: signed, single-use WriteGrant bound to the whole request; typed dry-run flags; strict write
patterns. Offline (FakeSession), fixed signing key."""

from __future__ import annotations

import dataclasses
import threading

import pytest

from ads_fakes import META_TOKEN_PREFIX, FakeResponse, FakeSession
from google_ecommerce_mcp import ads_common, ads_config, write_tokens
from google_ecommerce_mcp.ads_common import (CLIENT_HEADERS, MAX_WRITE_URL_LENGTH, AdsClient, AllowList,
                                             WriteAllowList, WriteGrant, body_digest, dry_run_validator,
                                             issue_grant, platform_dry_run_validator, request_digest)

TOKEN = META_TOKEN_PREFIX + "C" + "y" * 40
HOST = "https://ads.example-platform.com"
READS = AllowList([("GET", r"https://ads\.example-platform\.com/v1/accounts")])
WRITE_URL = f"{HOST}/v1/customers/123/campaigns:mutate"
VALIDATE_URL = f"{HOST}/v1/customers/123/campaignBudgets:mutate"
META_URL = f"{HOST}/v1/act_1/adsets"
WRITES = WriteAllowList([
    ("POST", r"https://ads\.example-platform\.com/v1/customers/\d+/campaignBudgets:mutate",
     platform_dry_run_validator("google_ads"), "validate"),
    ("POST", r"https://ads\.example-platform\.com/v1/customers/\d+/campaigns:mutate", None, "write"),
])
META_WRITES = WriteAllowList([
    ("POST", r"https://ads\.example-platform\.com/v1/act_\d+/adsets", platform_dry_run_validator("meta"),
     "validate"),
], platform="meta")
BODY = {"operations": [{"update": {"resourceName": "customers/123/campaigns/9", "status": "PAUSED"}}]}
AUTH = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


@pytest.fixture(autouse=True)
def _key(monkeypatch, tmp_path):
    monkeypatch.setenv(write_tokens.KEY_ENV, "ef" * 32)
    monkeypatch.setenv("GEM_STATE_DIR", str(tmp_path / "state"))
    ads_common._forget_consumed_grants()
    yield
    ads_common._forget_consumed_grants()


def writer(writes=WRITES, platform="google_ads"):
    c = AdsClient(platform, READS, secrets=lambda: [TOKEN], write_allow_list=writes)
    c.session = FakeSession([("", FakeResponse(200, {"results": [{}]}))])
    return c


def signed(op_id="op1", url=WRITE_URL, body=BODY, params=None, client_id="acme", plan_id="pl_1"):
    return issue_grant(client_id=client_id, plan_id=plan_id, op_id=op_id, method="post", url=url, body=body,
                       params=params)


def reason(result):
    assert result["error"] == "blocked", result
    return result["reason"]


# ------------------------------------------------------------------ signature
def test_issue_grant_signs_every_field():
    g = signed()
    assert g.method == "POST" and g.body_digest == body_digest(BODY)
    assert g.request_digest == request_digest("POST", WRITE_URL, None, BODY)
    assert len(g.mac) == 64 and write_tokens.verify_grant_mac(g)
    for name in ("client_id", "plan_id", "op_id", "method", "url", "body_digest", "request_digest"):
        assert not write_tokens.verify_grant_mac(dataclasses.replace(g, **{name: getattr(g, name) + "x"}))
    assert "mac" not in repr(g)


def test_signed_grant_is_sent_once():
    c = writer()
    assert c.call("POST", WRITE_URL, json=BODY, headers=AUTH, grant=signed()) == {"results": [{}]}
    assert len(c.session.calls) == 1


def test_grant_constructed_by_hand_is_refused():
    c = writer()
    hand = WriteGrant(client_id="acme", plan_id="pl_1", op_id="op1", method="POST", url=WRITE_URL,
                      body_digest=body_digest(BODY), request_digest=request_digest("POST", WRITE_URL, None, BODY))
    assert reason(c.call("POST", WRITE_URL, json=BODY, grant=hand)) == "grant_unsigned"
    forged = dataclasses.replace(hand, mac="a" * 64)
    assert reason(c.call("POST", WRITE_URL, json=BODY, grant=forged)) == "grant_mac_invalid"
    assert c.session.calls == []


def test_grant_signed_with_another_key_is_refused(monkeypatch):
    g = signed()
    monkeypatch.setenv(write_tokens.KEY_ENV, "01" * 32)
    assert reason(writer().call("POST", WRITE_URL, json=BODY, grant=g)) == "grant_mac_invalid"


def test_grant_moved_to_another_client_is_refused():
    g = signed(client_id="acme")
    moved = dataclasses.replace(g, client_id="other")
    assert reason(writer().call("POST", WRITE_URL, json=BODY, grant=moved)) == "grant_mac_invalid"


def test_mac_material_refuses_pipes_in_ids():
    with pytest.raises(ValueError):
        signed(op_id="op|1")
    g = WriteGrant("acme", "pl_1", "op|1", "POST", WRITE_URL, "d" * 64, "e" * 64, "f" * 64)
    assert write_tokens.verify_grant_mac(g) is False
    # url may hold '|' (it is followed only by '|'-free digests)
    assert write_tokens.sign_grant(dataclasses.replace(g, op_id="op1", url=WRITE_URL + "?a=b|c"))


def test_verify_grant_mac_is_false_without_a_key(monkeypatch):
    g = signed()

    def no_key(_client_id):
        raise write_tokens.TokenError("write_key_unavailable", "no key", "fix")

    monkeypatch.setattr(write_tokens, "client_key", no_key)
    assert write_tokens.verify_grant_mac(g) is False
    assert reason(writer().call("POST", WRITE_URL, json=BODY, grant=g)) == "grant_mac_invalid"


# ------------------------------------------------------------------ request digest
def test_params_and_query_are_bound():
    c = writer()
    g = signed(params={"b": "2", "a": 1})
    assert reason(c.call("POST", WRITE_URL, json=BODY, params={"a": 1, "b": "3"}, grant=g)) \
        == "request_digest_mismatch"
    assert reason(c.call("POST", WRITE_URL, json=BODY, grant=signed(op_id="op2", params={"a": 1}))) \
        == "request_digest_mismatch"
    assert reason(c.call("POST", WRITE_URL + "?partialFailure=true", json=BODY, grant=signed(op_id="op3"))) \
        == "request_digest_mismatch"
    assert c.session.calls == []
    # same params in another order: same digest, sent
    assert "error" not in c.call("POST", WRITE_URL, json=BODY, params={"b": "2", "a": "1"},
                                 grant=signed(op_id="op4", params={"a": 1, "b": "2"}))
    assert c.session.calls[0][2]["params"] == {"b": "2", "a": "1"}


def test_request_digest_is_canonical():
    assert request_digest("post", WRITE_URL, {"b": 1, "a": [1, 2]}, {"y": 1, "x": 2}) == \
        request_digest("POST", WRITE_URL, [("a", ["1", "2"]), ("b", "1")], {"x": 2, "y": 1})
    assert request_digest("POST", WRITE_URL, None, BODY) != request_digest("PATCH", WRITE_URL, None, BODY)
    assert request_digest("POST", WRITE_URL, None, BODY, {"X-Extra": "1"}) != \
        request_digest("POST", WRITE_URL, None, BODY)
    assert request_digest("POST", WRITE_URL, None, BODY, AUTH) == request_digest("POST", WRITE_URL, None, BODY)
    with pytest.raises((TypeError, ValueError)):
        request_digest("POST", WRITE_URL, None, {"x": float("nan")})


def test_body_is_deep_copied_before_the_check():
    c = writer()
    body = {"operations": [{"update": {"resourceName": "customers/123/campaigns/9", "status": "PAUSED"}}]}
    g = signed(body=body)
    assert "error" not in c.call("POST", WRITE_URL, json=body, grant=g)
    sent = c.session.calls[0][2]["json"]
    assert sent == body and sent is not body


# ------------------------------------------------------------------ headers
@pytest.mark.parametrize("name", ["X-HTTP-Method-Override", "x-http-method", "X-Method-Override"])
def test_method_override_header_is_refused_on_write_and_validate(name):
    c = writer()
    headers = dict(AUTH, **{name: "DELETE"})
    assert reason(c.call("POST", WRITE_URL, json=BODY, headers=headers, grant=signed())) == "header_forbidden"
    assert reason(c.call("POST", VALIDATE_URL, json=dict(BODY, validateOnly=True), headers=headers)) \
        == "header_forbidden"
    assert c.session.calls == []


def test_unexpected_header_is_refused():
    c = writer()
    headers = dict(AUTH, **{"X-Debug": "1"})
    assert reason(c.call("POST", WRITE_URL, json=BODY, headers=headers, grant=signed())) == "header_unexpected"
    assert reason(c.call("POST", VALIDATE_URL, json=dict(BODY, validateOnly=True), headers=headers)) \
        == "header_unexpected"
    assert c.session.calls == []


def test_connector_headers_are_accepted():
    c = writer()
    headers = {"Authorization": f"Bearer {TOKEN}", "developer-token": "devtok-123456", "login-customer-id": "1",
               "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "gem"}
    assert {k.lower() for k in headers} <= CLIENT_HEADERS
    assert "error" not in c.call("POST", WRITE_URL, json=BODY, headers=headers, grant=signed())
    assert "error" not in c.call("POST", WRITE_URL, json=BODY, headers=lambda: headers, grant=signed(op_id="o2"))


def test_read_path_headers_are_not_restricted():
    c = writer()
    c.session = FakeSession([("/accounts", FakeResponse(200, {"data": []}))])
    assert "error" not in c.call("GET", f"{HOST}/v1/accounts", headers={"X-Custom": "1"})


# ------------------------------------------------------------------ replay
def test_grant_serves_once_per_process():
    c = writer()
    g = signed()
    assert "error" not in c.call("POST", WRITE_URL, json=BODY, grant=g)
    assert reason(c.call("POST", WRITE_URL, json=BODY, grant=g)) == "grant_replayed"
    # another client object in the same process: still consumed
    assert reason(writer().call("POST", WRITE_URL, json=BODY, grant=g)) == "grant_replayed"
    # a fresh grant for the same (plan_id, op_id): still consumed
    assert reason(c.call("POST", WRITE_URL, json=BODY, grant=signed())) == "grant_replayed"
    assert len(c.session.calls) == 1


def test_refused_grant_is_not_consumed():
    c = writer()
    g = signed()
    assert reason(c.call("POST", WRITE_URL, json={"other": 1}, grant=g)) == "digest_mismatch"
    assert "error" not in c.call("POST", WRITE_URL, json=BODY, grant=g)


def test_concurrent_replay_sends_once():
    c = writer()
    g = signed()
    results = []
    threads = [threading.Thread(target=lambda: results.append(c.call("POST", WRITE_URL, json=BODY, grant=g)))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum("error" not in r for r in results) == 1
    assert sum(r.get("reason") == "grant_replayed" for r in results) == 7


# ------------------------------------------------------------------ url length
def test_write_url_longer_than_2048_is_refused():
    long_url = WRITE_URL + "?x=" + "a" * MAX_WRITE_URL_LENGTH
    assert WRITES.match("POST", long_url, BODY) is None
    assert reason(writer().call("POST", long_url, json=BODY, grant=signed(url=long_url))) == "url_too_long"
    with pytest.raises(ValueError, match="longer"):
        WriteAllowList([("POST", r"https://a\.example\.com/" + "x" * MAX_WRITE_URL_LENGTH, None, "write")])


# ------------------------------------------------------------------ typed dry-run flags
def test_meta_flag_must_be_a_list():
    c = writer(META_WRITES, "meta")
    assert "error" not in c.call("POST", META_URL, json={"name": "x", "execution_options": ["validate_only"]})
    for flag in ("validate_only", ["VALIDATE_ONLY"], [], {"validate_only": True}, None, ["include_recommendations"]):
        assert reason(c.call("POST", META_URL, json={"name": "x", "execution_options": flag})) \
            == "not_on_write_allow_list"
    assert len(c.session.calls) == 1


@pytest.mark.parametrize("flag", [True])
def test_google_flag_must_be_the_boolean_true(flag):
    v = platform_dry_run_validator("google_ads")
    assert v({"validateOnly": flag})
    for wrong in (1, 1.0, "true", "True", [True], False, None):
        assert not v({"validateOnly": wrong})
    openai = platform_dry_run_validator("openai_ads")
    assert openai({"validate_only": True}) and not openai({"validate_only": 1})


def test_dry_run_validator_marker_and_modes():
    v = dry_run_validator("execution_options", "validate_only", mode=ads_config.DRY_RUN_LIST)
    assert v.dry_run_flag == ("execution_options", "validate_only", "list_contains")
    assert v({"execution_options": ["validate_only"]}) and not v({"execution_options": "validate_only"})
    # default mode is strict equality: the bare string matches, a list does not
    eq = dry_run_validator("execution_options", "validate_only")
    assert eq({"execution_options": "validate_only"}) and not eq({"execution_options": ["validate_only"]})
    with pytest.raises(ValueError):
        dry_run_validator("k", True, mode="contains")
    with pytest.raises(KeyError):
        platform_dry_run_validator("tiktok")


def test_validate_rule_must_use_the_platform_flag():
    pattern = r"https://a\.example\.com/v1/act_\d+/adsets"
    # Meta with a string flag (the old shape) is refused for platform="meta"
    with pytest.raises(ValueError, match="dry-run flag of a meta"):
        WriteAllowList([("POST", pattern, dry_run_validator("execution_options", "validate_only"), "validate")],
                       platform="meta")
    with pytest.raises(ValueError, match="dry-run flag of a google_ads"):
        WriteAllowList([("POST", pattern, dry_run_validator("validate_only", True), "validate")],
                       platform="google_ads")
    WriteAllowList([("POST", pattern, platform_dry_run_validator("meta_eco"), "validate")], platform="meta")
    with pytest.raises(ValueError, match="unknown write platform"):
        WriteAllowList([], platform="tiktok")


def test_lenient_validator_with_a_marker_is_refused():
    pattern = r"https://a\.example\.com/v1/x"

    def loose(body):  # the pre-M1 behaviour: string or list, truthy ints...
        if not isinstance(body, dict) or "execution_options" not in body:
            return False
        value = body["execution_options"]
        return value == "validate_only" or (isinstance(value, list) and "validate_only" in value)

    loose.dry_run_flag = ("execution_options", "validate_only", ads_config.DRY_RUN_LIST)
    with pytest.raises(ValueError, match="wrong type"):
        WriteAllowList([("POST", pattern, loose, "validate")])

    def truthy(body):
        return isinstance(body, dict) and bool(body.get("validateOnly"))

    truthy.dry_run_flag = ("validateOnly", True, ads_config.DRY_RUN_EQUALS)
    with pytest.raises(ValueError, match="wrong type"):
        WriteAllowList([("POST", pattern, truthy, "validate")])


def test_client_refuses_a_write_list_of_another_platform():
    with pytest.raises(TypeError, match="write_allow_list is for meta"):
        AdsClient("google_ads", READS, write_allow_list=META_WRITES)
    AdsClient("meta_eco", READS, write_allow_list=META_WRITES)  # same Graph API flag


# ------------------------------------------------------------------ nested quantifiers
@pytest.mark.parametrize("pattern", [
    r"https://a\.example\.com/v1/(a+)+",
    r"https://a\.example\.com/v1/(\d+)*",
    r"https://a\.example\.com/v1/(\d*){2}",
    r"https://a\.example\.com/v1/((ab)+x)+",
    r"https://a\.example\.com/v1/((\d+))+",
    r"https://a\.example\.com/v1/([a-z]+/)+",
])
def test_nested_quantifiers_are_refused(pattern):
    with pytest.raises(ValueError, match="nest quantifiers"):
        WriteAllowList([("POST", pattern, None, "write")])


@pytest.mark.parametrize("pattern", [
    r"https://a\.example\.com/v1/act_\d+/(adsets|ads)/[^/?]+:mutate",
    r"https://a\.example\.com/v1/(ab)+/\d{1,20}",
    r"https://a\.example\.com/v1/(\d+)?/x",
    r"https://a\.example\.com/v1/customers/\d+/campaigns:mutate",
])
def test_simple_quantifiers_stay_accepted(pattern):
    WriteAllowList([("POST", pattern, None, "write")])

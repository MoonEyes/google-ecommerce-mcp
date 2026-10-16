"""Offline tests of the shared ads plumbing: allow-list, client, redaction, error mapping, metrics helpers."""

import asyncio
import json
import logging
from typing import Annotated, Literal

import pytest
import requests
from pydantic import Field

from ads_fakes import (GOOGLE_ACCESS_PREFIX, GOOGLE_REFRESH_PREFIX, META_TOKEN_PREFIX, OPENAI_KEY_PREFIX,
                       FakeAdapter, FakeResponse, FakeSession, no_network, real_session_with)  # noqa: F401
from google_ecommerce_mcp import ads_common
from google_ecommerce_mcp.ads_common import (AdsClient, AllowList, cap, date_range, guard, map_error, micros,
                                             redact, register_tools, roas, safe_ratio, total_roas, validate_dates)
from google_ecommerce_mcp.auth import NotAuthenticated
from google_ecommerce_mcp.config import NotConfigured

TOKEN = META_TOKEN_PREFIX + "B" + "x" * 40
HOST = "https://ads.example-platform.com"


def only_select(body):
    return isinstance(body, dict) and str(body.get("query", "")).strip().lower().startswith("select")


RULES = AllowList([
    ("GET", r"https://ads\.example-platform\.com/v1/accounts"),
    ("GET", r"https://ads\.example-platform\.com/v1/act_\d+/insights"),
    ("POST", r"https://ads\.example-platform\.com/v1/customers/\d+/search", only_select),
])


def client(rules=()):
    c = AdsClient("meta", RULES, secrets=lambda: [TOKEN])
    c.session = FakeSession(rules)
    return c


# ------------------------------------------------------------------ zero network
def test_real_network_is_blocked_in_tests():
    with pytest.raises(AssertionError, match="real network"):
        requests.Session().request("GET", "https://example.com/")


# ------------------------------------------------------------------ allow-list
@pytest.mark.parametrize("method,url,body,allowed", [
    ("GET", f"{HOST}/v1/accounts", None, True),
    ("get", f"{HOST}/v1/accounts?fields=id,name", None, True),
    ("GET", f"{HOST}/v1/act_12/insights", None, True),
    ("POST", f"{HOST}/v1/customers/1/search", {"query": "select x from y"}, True),
    ("POST", f"{HOST}/v1/customers/1/search", {"query": "UPDATE x"}, False),
    ("POST", f"{HOST}/v1/customers/1/search", None, False),
    ("POST", f"{HOST}/v1/accounts", None, False),
    ("DELETE", f"{HOST}/v1/act_12/insights", None, False),
    ("GET", f"{HOST}/v1/accounts/extra", None, False),
    ("GET", f"{HOST}/v1/act_12/insights/../campaigns", None, False),
    ("GET", f"{HOST}/v1/act_12%2Fcampaigns", None, False),
    ("GET", "http://ads.example-platform.com/v1/accounts", None, False),
    ("GET", "https://ads.example-platform.com.evil.com/v1/accounts", None, False),
    ("GET", "https://evil.com/ads.example-platform.com/v1/accounts", None, False),
    ("GET", "https://ads.example-platform.com@evil.com/v1/accounts", None, False),
    ("GET", "https://user@ads.example-platform.com/v1/accounts", None, False),
    ("GET", "https://ads.example-platform.com:8443/v1/accounts", None, False),
    ("GET", "https://ads.example-platform.com/v1/act_x/insights", None, False),
])
def test_allow_list(method, url, body, allowed):
    assert RULES.is_allowed(method, url, body) is allowed


def test_allow_list_pins_hosts_and_exposes_patterns():
    assert RULES.hosts == {"ads.example-platform.com"}
    assert [m for m, _ in RULES.patterns] == ["GET", "GET", "POST"]
    with pytest.raises(ValueError):
        AllowList([("GET", r"https://[a-z]+\.example\.com/v1")])
    with pytest.raises(ValueError):
        AllowList([("GET", r"http://example\.com/v1")])
    with pytest.raises(ValueError):
        AllowList([("GET", r".*")])


def test_crashing_validator_refuses():
    rules = AllowList([("POST", r"https://a\.example\.com/q", lambda body: body["missing"])])
    assert rules.is_allowed("POST", "https://a.example.com/q", {}) is False


# ------------------------------------------------------------------ client
@pytest.mark.parametrize("method,url", [
    ("POST", f"{HOST}/v1/act_1/campaigns"),
    ("DELETE", f"{HOST}/v1/act_1/insights"),
    ("PATCH", f"{HOST}/v1/accounts"),
    ("GET", "https://ads.example-platform.com.evil.com/v1/accounts"),
])
def test_blocked_requests_never_reach_the_network(method, url):
    c = client([("", FakeResponse(200, {}))])
    result = c.call(method, url, json={})
    assert result["error"] == "blocked" and result["platform"] == "meta"
    assert c.session.calls == []
    assert c.BLOCKED_ATTEMPTS == [(method, url)]


def test_blocked_attempts_are_per_client():
    a, b = client(), client()
    a.call("DELETE", f"{HOST}/v1/accounts")
    assert len(a.BLOCKED_ATTEMPTS) == 1 and b.BLOCKED_ATTEMPTS == []


@pytest.mark.parametrize("url,params", [
    (f"{HOST}/v1/accounts?access_token=abc", None),
    (f"{HOST}/v1/accounts", {"access_token": "abc"}),
    (f"{HOST}/v1/accounts", {"fields": TOKEN}),
    (f"{HOST}/v1/accounts?x={TOKEN}", None),
])
def test_secret_in_url_is_refused(url, params):
    c = client([("", FakeResponse(200, {}))])
    result = c.call("GET", url, params=params, headers={"Authorization": f"Bearer {TOKEN}"})
    assert result["error"] == "blocked" and c.session.calls == []
    assert TOKEN not in json.dumps(result) and TOKEN not in str(c.BLOCKED_ATTEMPTS)


def test_token_from_headers_is_not_in_url_but_in_header():
    c = client([("/accounts", FakeResponse(200, {"data": [{"id": "act_1"}]}))])
    result = c.call("GET", f"{HOST}/v1/accounts", params={"fields": "id"},
                    headers={"Authorization": f"Bearer {TOKEN}"})
    assert result == {"data": [{"id": "act_1"}]}
    method, url, kwargs = c.session.calls[0]
    assert kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}" and TOKEN not in url
    assert TOKEN not in json.dumps(kwargs["params"]) and kwargs["timeout"] == ads_common.TIMEOUT


def test_error_body_echoing_the_token_is_redacted(caplog):
    echo = {"error": {"message": f"Invalid OAuth access token {TOKEN} (Bearer {TOKEN})", "type": "OAuthException",
                      "code": 190, "fbtrace_id": "A1"}}
    c = client([("/accounts", FakeResponse(400, echo, text=json.dumps(echo)))])
    with caplog.at_level(logging.DEBUG):
        result = c.call("GET", f"{HOST}/v1/accounts", headers={"Authorization": f"Bearer {TOKEN}"})
        c.call("DELETE", f"{HOST}/v1/accounts", headers={"Authorization": f"Bearer {TOKEN}"})
    assert result["error"] == "auth_failed" and "META_ACCESS_TOKEN" in result["fix"]
    assert TOKEN not in json.dumps(result) and "[REDACTED]" in json.dumps(result)
    assert TOKEN not in caplog.text


def test_configured_secret_is_redacted_even_with_an_unknown_shape():
    plain = "plainsecretvalue123"
    c = AdsClient("openai_ads", RULES, secrets=[plain, None])
    c.session = FakeSession([("/accounts", FakeResponse(500, {"error": {"message": f"key {plain} broken"}}))])
    result = c.call("GET", f"{HOST}/v1/accounts")
    assert result["error"] == "api_error" and plain not in json.dumps(result)


def test_network_error_is_data_and_redacted():
    c = client([("/accounts", requests.ConnectionError(f"failed with Bearer {TOKEN}"))])
    result = c.call("GET", f"{HOST}/v1/accounts", headers={"Authorization": f"Bearer {TOKEN}"})
    assert result["error"] == "network" and TOKEN not in result["detail"]


def test_non_json_body_and_json_array():
    c = client([("/accounts", FakeResponse(502, ValueError("no json"), text="<html>Bad gateway</html>")),
                ("/search", FakeResponse(200, [{"results": [1]}, {"results": [2]}]))])
    bad = c.call("GET", f"{HOST}/v1/accounts")
    assert bad["error"] == "api_error" and bad["http_status"] == 502 and "Bad gateway" in json.dumps(bad)
    batches = c.call("POST", f"{HOST}/v1/customers/1/search", json={"query": "SELECT a FROM b"})
    assert batches == {"items": [{"results": [1]}, {"results": [2]}]}


def test_header_provider_failure_is_auth_failed_without_network():
    def provider():
        raise NotAuthenticated("No Google Ads token found. Run setup-ads.")

    c = AdsClient("google_ads", RULES)
    c.session = FakeSession([("", FakeResponse(200, {}))])
    result = c.call("GET", f"{HOST}/v1/accounts", headers=provider)
    assert result["error"] == "auth_failed" and "setup-ads" in result["fix"] and c.session.calls == []


def test_header_provider_not_configured_reaches_guard():
    def provider():
        raise NotConfigured("GADS_CUSTOMER_ID")

    c = client()

    @guard("current state")
    def tool():
        return c.call("GET", f"{HOST}/v1/accounts", headers=provider)

    assert tool()["error"] == "not_configured"


def test_discovery_cache_ttl(monkeypatch):
    now = [1000.0]
    c = client([("/accounts", FakeResponse(200, {"data": [1]}))])
    c.cache.clock = lambda: now[0]
    first = c.call("GET", f"{HOST}/v1/accounts", cache=True)
    second = c.call("GET", f"{HOST}/v1/accounts", cache=True)
    assert first["cached"] is False and second["cached"] is True and second["data"] == [1]
    assert len(c.session.calls) == 1
    second["data"].append(2)  # a caller mutating the result does not poison the cache
    assert c.call("GET", f"{HOST}/v1/accounts", cache=True)["data"] == [1]
    now[0] += 301
    assert c.call("GET", f"{HOST}/v1/accounts", cache=True)["cached"] is False and len(c.session.calls) == 2
    assert "cached" not in c.call("GET", f"{HOST}/v1/accounts")  # reports are never cached


def test_cache_key_depends_on_resolved_headers_and_holds_no_header_value():
    c = client([("/accounts", lambda kwargs: FakeResponse(200, {"auth": kwargs["headers"].get("Authorization")[-1:],
                                                                "login": kwargs["headers"].get("login-customer-id")}))])
    url = f"{HOST}/v1/accounts"
    token_a, token_b = "Bearer " + "a" * 30, "Bearer " + "b" * 30
    first = c.call("GET", url, headers={"Authorization": token_a}, cache=True)
    assert c.call("GET", url, headers=lambda: {"Authorization": token_a}, cache=True)["cached"] is True
    other_token = c.call("GET", url, headers={"Authorization": token_b}, cache=True)
    assert other_token["cached"] is False and other_token["auth"] == "b" and first["auth"] == "a"
    other_login = c.call("GET", url, headers={"Authorization": token_a, "login-customer-id": "111"}, cache=True)
    assert other_login["cached"] is False and other_login["login"] == "111"
    assert len(c.session.calls) == 3
    stored = repr(c.cache._items.keys())
    assert "a" * 30 not in stored and "b" * 30 not in stored and "111" not in stored
    assert all(len(k) == 64 for k in c.cache._items)  # sha256 hex digests


def test_missing_ads_token_fix_names_the_setup_ads_command():
    def provider():
        raise NotAuthenticated("No Google Ads token found.")

    c = AdsClient("google_ads", RULES)
    c.session = FakeSession([("", FakeResponse(200, {}))])
    result = c.call("GET", f"{HOST}/v1/accounts", headers=provider, cache=True)
    assert result["error"] == "auth_failed"
    assert "google-ecommerce-mcp setup-ads --client-secret <file>" in result["fix"] and c.session.calls == []


def test_errors_are_not_cached():
    c = client([("/accounts", FakeResponse(429, {"error": {"message": "slow down"}}, {"Retry-After": "7"}))])
    for _ in range(2):
        assert c.call("GET", f"{HOST}/v1/accounts", cache=True)["error"] == "rate_limited"
    assert len(c.session.calls) == 2


# ------------------------------------------------------------------ redact
@pytest.mark.parametrize("secret_text", [
    "Authorization: Bearer abc.DEF-123_xyz",
    "https://graph.facebook.com/me?access_token=abcdef123&x=1",
    '{"access_token": "abcdef123"}',
    "token " + META_TOKEN_PREFIX + "Gm0PX4ZCpsBAKZBZCZAZDZD",
    GOOGLE_ACCESS_PREFIX + "a0AfH6SMBxyz-123",
    "refresh " + GOOGLE_REFRESH_PREFIX + "0gAbCdEfGhIjKlMn",
    "key " + OPENAI_KEY_PREFIX + "proj-AbCdEfGhIjKlMnOpQrSt",
    "key " + OPENAI_KEY_PREFIX + "AbCdEfGhIjKlMnOpQrSt",
])
def test_redact_patterns(secret_text):
    out = redact(secret_text)
    assert "[REDACTED]" in out
    for fragment in ("abc.DEF-123_xyz", "abcdef123", META_TOKEN_PREFIX + "Gm0PX4", "a0AfH6SMBxyz", "0gAbCdEfGhIjKlMn",
                     "AbCdEfGhIjKlMnOpQrSt"):
        assert fragment not in out


def test_redact_exact_values_and_structures():
    data = {"error": {"message": "bad mysecretvalue", "list": ["mysecretvalue", 3]},
            "access_token": "short", "n": 1}
    out = redact(data, ["mysecretvalue"])
    assert "mysecretvalue" not in json.dumps(out) and out["n"] == 1 and out["error"]["list"][1] == 3
    assert out["access_token"] == "[REDACTED]"
    assert redact("https://googleads.googleapis.com/v26/customers") == "https://googleads.googleapis.com/v26/customers"
    assert redact("plain text", lambda: ["abc"]) == "plain text"  # too short to be a real secret: left alone


# ------------------------------------------------------------------ map_error
def resp(status, headers=None):
    return FakeResponse(status, {}, headers)


def test_map_error_auth_failed():
    assert map_error("openai_ads", resp(401), {"error": {"message": "bad key"}})["error"] == "auth_failed"
    meta = map_error("meta", resp(400), {"error": {"message": "Error validating access token", "code": 190}})
    assert meta["error"] == "auth_failed" and meta["http_status"] == 400 and meta["fix"]
    assert map_error("google_ads", resp(401), {"error": {"status": "UNAUTHENTICATED"}})["error"] == "auth_failed"


def test_map_error_missing_scope():
    google = map_error("google_ads", resp(403), {"error": {"message": "Request had insufficient authentication "
                                                                      "scopes.", "status": "PERMISSION_DENIED"}})
    assert google["error"] == "missing_scope" and google["missing_scopes"] == ["https://www.googleapis.com/auth/adwords"]
    assert "setup-ads" in google["fix"]
    meta = map_error("meta", resp(403), {"error": {"message": "(#200) Requires ads_read permission", "code": 200}})
    assert meta["error"] == "missing_scope" and meta["required_permission"] == "ads_read"


def test_map_error_rate_limited_with_meta_usage_headers():
    headers = {"x-business-use-case-usage": json.dumps({"123": [{"type": "ads_insights", "call_count": 98,
                                                                 "estimated_time_to_regain_access": 5}]}),
               "X-Ad-Account-Usage": json.dumps({"acc_id_util_pct": 99.5})}
    meta = map_error("meta", resp(400, headers), {"error": {"message": "User request limit reached", "code": 17}})
    assert meta["error"] == "rate_limited" and meta["retry_after_seconds"] == 300
    assert meta["usage"]["business_use_case"]["123"][0]["call_count"] == 98
    assert meta["usage"]["ad_account"]["acc_id_util_pct"] == 99.5
    throttled = map_error("meta", resp(400), {"error": {"code": 80000, "message": "too many calls"}})
    assert throttled["error"] == "rate_limited" and throttled["retry_after_seconds"] is None
    openai = map_error("openai_ads", resp(429, {"Retry-After": "12"}), {"error": {"message": "slow"}})
    assert openai["retry_after_seconds"] == 12
    both = {"X-Ad-Account-Usage": json.dumps({"acc_id_util_pct": 100, "reset_time_duration": 90}),
            "X-Business-Use-Case-Usage": json.dumps({"1": [{"estimated_time_to_regain_access": 2}]})}
    assert map_error("meta", resp(400, both), {"error": {"code": 17}})["retry_after_seconds"] == 120
    longer_reset = dict(both, **{"X-Ad-Account-Usage": json.dumps({"reset_time_duration": 600})})
    assert map_error("meta", resp(400, longer_reset), {"error": {"code": 17}})["retry_after_seconds"] == 600
    with_header = dict(both, **{"Retry-After": "15"})
    assert map_error("meta", resp(429, with_header), {"error": {"code": 17}})["retry_after_seconds"] == 15
    google = map_error("google_ads", resp(429), [{"error": {"status": "RESOURCE_EXHAUSTED"}}])
    assert google["error"] == "rate_limited" and google["detail"] == {"status": "RESOURCE_EXHAUSTED"}


def test_map_error_not_available_and_others():
    assert map_error("openai_ads", resp(404), {"error": {"message": "no"}})["error"] == "not_available"
    disabled = map_error("google_ads", resp(403), {"error": {"message": "Google Ads API has not been used in project "
                                                                       "1 before or it is disabled", "details": [
                                                                           {"reason": "SERVICE_DISABLED"}]}})
    assert disabled["error"] == "not_available" and "Google Ads API" in disabled["fix"]
    assert map_error("meta", resp(400), {"error": {"code": 100, "error_subcode": 33}})["error"] == "not_available"
    too_big = map_error("openai_ads", resp(413), {"error": {"message": "too many rows"}})
    assert too_big["error"] == "too_many_rows" and too_big["fix"] == "réduire la période ou ajouter des filtres"
    assert map_error("meta", resp(400), {"error": {"code": 100, "message": "Invalid parameter"}})["error"] == "api_error"
    assert map_error("google_ads", resp(403), {"error": {"message": "USER_PERMISSION_DENIED"}})["error"] == "auth_failed"


def test_map_error_redacts_token_shapes():
    out = map_error("meta", resp(400), {"error": {"message": "bad " + META_TOKEN_PREFIX + "Gm0PX4ZCpsBAKZBZCZAZDZD", "code": 190}})
    assert META_TOKEN_PREFIX + "Gm0PX4" not in json.dumps(out)


# ------------------------------------------------------------------ guard, register_tools
def test_guard_stamps_results_and_reports_not_configured():
    @guard("platform data; the last day can change")
    def ok(x: int = 1) -> dict:
        """Doc."""
        return {"x": x}

    @guard("current")
    def missing() -> dict:
        raise NotConfigured("META_AD_ACCOUNT_ID")

    result = ok(2)
    assert result["x"] == 2 and result["fetched_at"].endswith("+00:00") and result["freshness"].startswith("platform")
    assert ok.__name__ == "ok" and ok.__doc__ == "Doc." and ok.__wrapped__
    error = missing()
    assert error["error"] == "not_configured" and "META_AD_ACCOUNT_ID" in error["detail"] and error["fetched_at"]


def test_register_tools_declares_read_only_and_keeps_signatures():
    from mcp.server.fastmcp import FastMCP

    @guard("current state")
    def sample_tool(
        level: Annotated[Literal["account", "campaign"], Field(description="Aggregation level of the rows")] = "account",
        limit: Annotated[int, Field(description="Maximum rows returned (1 to 1000)", ge=1, le=1000)] = 50,
    ) -> dict:
        """A sample read-only ads tool used to check registration: title, annotations, documented parameters."""
        return {"level": level, "limit": limit}

    mcp = FastMCP("test-ads")
    assert register_tools(mcp, [("Sample tool", sample_tool)]) == ["sample_tool"]
    tools = asyncio.run(mcp.list_tools())
    assert len(tools) == 1
    t = tools[0]
    assert t.title == "Sample tool" and t.annotations.readOnlyHint is True and t.annotations.destructiveHint is False
    assert len(t.description) > 60
    assert set(t.inputSchema["properties"]) == {"level", "limit"}
    assert all(p.get("description") for p in t.inputSchema["properties"].values())
    result = asyncio.run(mcp.call_tool("sample_tool", {"level": "campaign"}))
    assert "campaign" in str(result)


# ------------------------------------------------------------------ numbers and dates
def test_micros_and_cap():
    assert micros("1234567") == 1.234567 and micros(2500000) == 2.5 and micros("9223372036854775807") == pytest.approx(9223372036854.775807)
    assert micros(None) is None and micros("abc") is None and micros("0") == 0.0 and micros("1.5e6") == 1.5
    assert cap(0) == 1 and cap(5000) == 1000 and cap(20, 10) == 10 and cap("7") == 7


def test_ratios_are_nullable():
    assert safe_ratio(1, 4) == 0.25 and safe_ratio(1, 0) is None and safe_ratio(None, 3) is None
    assert safe_ratio("2", "3", 2) == 0.67 and safe_ratio(1, None) is None
    assert roas(300, 100) == 3.0 and roas(300, 0) is None and roas(None, 100) is None and roas(0, 10) == 0.0


def test_total_roas_is_sum_of_values_over_sum_of_spends_not_mean():
    rows = [{"conversion_value": 1000, "spend": 100}, {"conversion_value": 10, "spend": 10}]
    mean_of_roas = (10.0 + 1.0) / 2
    assert total_roas(rows) == round(1010 / 110, 6) and total_roas(rows) != mean_of_roas
    assert total_roas([{"conversion_value": None, "spend": 5}]) is None
    assert total_roas([{"v": 5, "s": 0}], "v", "s") is None


@pytest.mark.parametrize("start,end", [
    ("2026-01-01'", "2026-01-31"), ("2026-02-30", "2026-03-01"), ("2026-03-01", "2026-02-01"),
    ("yesterday", "2026-01-01"), ("2026-01-01 OR 1=1", "2026-01-02"), ("", ""),
])
def test_validate_dates_refuses_anything_but_real_iso_dates(start, end):
    with pytest.raises(ValueError):
        validate_dates(start, end)


def test_date_range_reports_timezone_and_completeness():
    assert validate_dates("2026-01-01", "2026-01-31") == ("2026-01-01", "2026-01-31")
    info = date_range("2026-01-01", "2026-01-31", "Europe/Paris")
    assert info["timezone"] == "Europe/Paris" and info["data_complete"] is True
    unknown = date_range("2026-01-01", "2026-01-02", None)
    assert unknown["timezone"].startswith("UTC")


# ------------------------------------------------------------------ transport hardening (P3/P4)
def test_redirects_are_refused_and_never_followed():
    c = client([("/accounts", FakeResponse(307, {}, {"Location": "https://evil.example.com/steal?x=1"}))])
    result = c.call("GET", f"{HOST}/v1/accounts", headers={"Authorization": f"Bearer {TOKEN}",
                                                            "developer-token": "dev-token-123456"})
    assert result["error"] == "blocked" and "redirect refused" in result["detail"] and result["http_status"] == 307
    assert "evil.example.com" in result["detail"] and len(c.session.calls) == 1
    kwargs = c.session.calls[0][2]
    assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
    assert "dev-token-123456" not in json.dumps(result) and TOKEN not in json.dumps(result)


def test_session_ignores_environment_proxies_and_netrc():
    assert AdsClient("meta", RULES).session.trust_env is False


def test_huge_response_is_refused_without_reading_it_all():
    def ten_times_the_ceiling():  # finite: the suite still ends if the ceiling is ever removed
        for _ in range(30):
            yield b"x" * (1024 * 1024)

    huge = FakeResponse(200, content=ten_times_the_ceiling())
    c = client([("/accounts", huge)])
    result = c.call("GET", f"{HOST}/v1/accounts", max_bytes=3 * 1024 * 1024)
    assert result["error"] == "response_too_large" and result["fix"] == "réduire la période ou ajouter LIMIT"
    assert result["max_bytes"] == 3 * 1024 * 1024
    assert huge.bytes_read <= 3 * 1024 * 1024 + 64 * 1024 and huge.closed is True


def test_declared_content_length_over_the_ceiling_is_refused_before_reading():
    big = FakeResponse(200, {"data": []}, {"Content-Length": str(ads_common.MAX_RESPONSE_BYTES + 1)})
    c = client([("/accounts", big)])
    assert c.call("GET", f"{HOST}/v1/accounts")["error"] == "response_too_large" and big.bytes_read == 0
    assert ads_common.MAX_RESPONSE_BYTES == 20 * 1024 * 1024


def test_response_under_the_ceiling_is_decoded():
    c = client([("/accounts", FakeResponse(200, {"data": [{"id": "act_1"}]}))])
    assert c.call("GET", f"{HOST}/v1/accounts", max_bytes=1000) == {"data": [{"id": "act_1"}]}


@pytest.mark.parametrize("url", [
    f"{HOST}/v1/accounts?method=delete",
    f"{HOST}/v1/accounts?METHOD=post",
    f"{HOST}/v1/accounts?fields=id&access_token=abc",
])
def test_allow_list_refuses_method_and_access_token_in_the_query(url):
    assert RULES.is_allowed("GET", url) is False
    assert RULES.is_allowed("GET", f"{HOST}/v1/accounts?fields=id,name&limit=5") is True


def test_method_parameter_is_blocked_in_params_too():
    c = client([("", FakeResponse(200, {}))])
    result = c.call("GET", f"{HOST}/v1/accounts", params={"method": "delete"})
    assert result["error"] == "blocked" and c.session.calls == []


# ------------------------------------------------------------------ second hardening wave
@pytest.mark.parametrize("url", [
    f"{HOST}/v1/accounts\n",
    f" {HOST}/v1/accounts",
    f"{HOST}/v1/accounts\t",
    "HTTPS://ads.example-platform.com/v1/accounts",
    "Https://ads.example-platform.com/v1/accounts",
    "https:ads.example-platform.com/v1/accounts",
    "https:/ads.example-platform.com/v1/accounts",
    f"{HOST}/v1/accounts?a=b;method=delete",
    f"{HOST}/v1/accounts;x",
    f"{HOST}/v1/act_12/insights?f=caf\u00e9",
    f"{HOST}/v1/act_12/insights?f=a b",
    f"{HOST}/v1/act_12\\insights",
    f"{HOST}/v1/act_12%2finsights",
    f"{HOST}/v1/act_12/%2e%2e/insights",
    f"{HOST}/v1/act_12/%2E%2E/insights",
])
def test_allow_list_refuses_malformed_urls(url):
    assert RULES.is_allowed("GET", url) is False


def test_allow_list_still_accepts_clean_urls_with_queries():
    assert RULES.is_allowed("GET", f"{HOST}/v1/act_12/insights?fields=spend,clicks&time_increment=1")


def test_default_transport_ignores_the_environment(monkeypatch):
    monkeypatch.delenv("ADS_TRUST_ENV", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
    c = client([("/accounts", FakeResponse(200, {}))])
    c.call("GET", f"{HOST}/v1/accounts")
    kwargs = c.session.calls[0][2]
    assert "proxies" not in kwargs and "verify" not in kwargs
    assert AdsClient("meta", RULES).session.trust_env is False and AdsClient("meta", RULES).trust_env is False


@pytest.mark.parametrize("opt_in", ["env", "argument"])
def test_trust_env_opt_in_uses_proxy_and_ca_bundle_only(monkeypatch, opt_in, tmp_path):
    bundle = tmp_path / "corp-ca.pem"
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(bundle))
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    if opt_in == "env":
        monkeypatch.setenv("ADS_TRUST_ENV", "1")
        c = AdsClient("meta", RULES, secrets=[TOKEN])
    else:
        monkeypatch.delenv("ADS_TRUST_ENV", raising=False)
        c = AdsClient("meta", RULES, secrets=[TOKEN], trust_env=True)
    assert c.session.trust_env is False  # netrc never consulted
    c.session = FakeSession([("/accounts", FakeResponse(200, {}))])
    c.call("GET", f"{HOST}/v1/accounts")
    kwargs = c.session.calls[0][2]
    assert kwargs["proxies"].get("https") == "http://proxy.corp:3128" and kwargs["verify"] == str(bundle)
    assert kwargs["allow_redirects"] is False  # redirects stay refused in every mode


def test_no_redirect_session_forces_allow_redirects_off(monkeypatch):
    seen = {}

    def record(self, method, url, *args, **kwargs):
        seen.update(kwargs)
        return FakeResponse(200, {})

    monkeypatch.setattr(requests.Session, "request", record)
    session = ads_common.NoRedirectSession()
    session.request("GET", "https://example.com/", allow_redirects=True)
    assert seen["allow_redirects"] is False and session.max_redirects == 0 and session.trust_env is False


def test_network_error_fix_mentions_the_proxy_opt_in():
    c = client([("/accounts", requests.ConnectionError("proxy refused"))])
    result = c.call("GET", f"{HOST}/v1/accounts")
    assert result["error"] == "network" and "ADS_TRUST_ENV=1" in result["fix"]


def test_exact_value_masking_needs_six_characters_but_patterns_always_apply():
    """Choice: values under 6 characters are not masked by exact match (they would erase ordinary words and
    numbers); real tokens are far longer, and the token shapes are masked whatever their length."""
    assert redact("code 12345 failed", ["12345"]) == "code 12345 failed"
    assert redact("code 123456 failed", ["123456"]) == "code [REDACTED] failed"
    assert "abc" not in redact("Authorization: Bearer abc", ["abc"])
    assert "x1" not in redact("?access_token=x1", ["x1"])


# ------------------------------------------------------------------ final wave
PLATFORM_URLS = {
    "google_ads": ("POST", "https://googleads.googleapis.com/v26/customers/1/googleAds:searchStream",
                   r"https://googleads\.googleapis\.com/v26/customers/\d+/googleAds:searchStream"),
    "meta": ("GET", "https://graph.facebook.com/v26.0/me/adaccounts",
             r"https://graph\.facebook\.com/v26\.0/me/adaccounts"),
    "openai_ads": ("GET", "https://api.ads.openai.com/v1/ad_account", r"https://api\.ads\.openai\.com/v1/ad_account"),
}


@pytest.mark.parametrize("platform", sorted(PLATFORM_URLS))
def test_real_no_redirect_session_302_is_blocked_not_network(monkeypatch, platform):
    method, url, pattern = PLATFORM_URLS[platform]
    c = AdsClient(platform, AllowList([(method, pattern)]), secrets=[TOKEN])
    adapter = FakeAdapter(302, {"Location": "https://evil.example.com/collect"})
    real_session_with(monkeypatch, c.session, adapter)
    result = c.call(method, url, headers={"Authorization": f"Bearer {TOKEN}", "developer-token": "dev-123456"},
                    json={"query": "SELECT campaign.id FROM campaign"} if method == "POST" else None)
    assert result["error"] == "blocked" and "redirect refused" in result["detail"]
    assert result["http_status"] == 302 and "evil.example.com" in result["detail"]
    assert len(adapter.sent) == 1 and adapter.sent[0].url == url  # the redirect target was never requested
    assert c.BLOCKED_ATTEMPTS == [(method, "redirect 302 to https://evil.example.com")]
    assert TOKEN not in json.dumps(result) and "dev-123456" not in json.dumps(result)


def test_too_many_redirects_without_response_is_still_blocked():
    c = client([("/accounts", requests.TooManyRedirects("Exceeded 0 redirects."))])
    result = c.call("GET", f"{HOST}/v1/accounts")
    assert result["error"] == "blocked" and result["http_status"] is None and len(c.BLOCKED_ATTEMPTS) == 1


def test_missing_ca_bundle_is_a_safe_network_error(monkeypatch, tmp_path):
    missing = tmp_path / "nowhere" / "corp-ca.pem"
    monkeypatch.setenv("ADS_TRUST_ENV", "1")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(missing))
    error = OSError(f"Could not find a suitable TLS CA certificate bundle, invalid path: {missing}")
    c = client([("/accounts", error)])
    result = c.call("GET", f"{HOST}/v1/accounts")
    assert result["error"] == "network" and "REQUESTS_CA_BUNDLE" in result["fix"] and "ADS_TRUST_ENV" in result["fix"]
    assert str(missing) not in result["detail"] and result["detail"].startswith("OSError")
    assert c.session.calls[0][2]["verify"] == str(missing)


def test_os_error_while_reading_the_body_is_data():
    def broken_chunks():
        yield b'{"data": '
        raise OSError("disk full")

    c = client([("/accounts", FakeResponse(200, content=broken_chunks()))])
    assert c.call("GET", f"{HOST}/v1/accounts")["error"] == "network"


@pytest.mark.parametrize("value,enabled", [("1", True), (" 1 ", True), ("0", False), ("false", False),
                                           ("true", False), ("yes", False), ("", False)])
def test_trust_env_opt_in_is_strictly_1(monkeypatch, value, enabled):
    monkeypatch.setenv("ADS_TRUST_ENV", value)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
    assert ads_common.trust_env_enabled() is enabled
    c = client([("/accounts", FakeResponse(200, {}))])
    c.call("GET", f"{HOST}/v1/accounts")
    kwargs = c.session.calls[0][2]
    assert ("proxies" in kwargs and "verify" in kwargs) is enabled


def test_hardened_session_passes_the_ca_bundle_only_on_opt_in(monkeypatch, tmp_path):
    bundle = tmp_path / "corp-ca.pem"
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(bundle))
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
    monkeypatch.delenv("ADS_TRUST_ENV", raising=False)
    assert ads_common.hardened_session("https://oauth2.googleapis.com/token").verify is True
    monkeypatch.setenv("ADS_TRUST_ENV", "1")
    session = ads_common.hardened_session("https://oauth2.googleapis.com/token")
    assert session.verify == str(bundle) and session.trust_env is False
    assert ads_common.hardened_session("https://oauth2.googleapis.com/token", trust_env=False).verify is True

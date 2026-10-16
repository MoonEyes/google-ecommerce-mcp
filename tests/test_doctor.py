"""Offline tests of `google-ecommerce-mcp doctor`: offline checks, --live through fake sessions, --json, --only,
masking, warnings, token separation. Zero network (no_network), in-memory keyring (no_keyring), Claude Desktop
config in tmp_path only."""

import ast
import json
import os
from pathlib import Path

import pytest
import requests

from ads_fakes import (GOOGLE_ACCESS_PREFIX, GOOGLE_REFRESH_PREFIX, META_TOKEN_PREFIX, OPENAI_KEY_PREFIX,  # noqa: F401
                       FakeResponse, FakeSession, no_keyring, no_network)
from google_ecommerce_mcp import doctor, google_ads, meta_ads, openai_ads
from google_ecommerce_mcp.__main__ import main
from google_ecommerce_mcp.ads_config import ADS_SCOPE
from google_ecommerce_mcp.config import KEYRING_SERVICE, MERCHANT_SCOPE, READ_ONLY_SCOPES

META_TOKEN = META_TOKEN_PREFIX + "Tdoctor" + "q" * 40
OPENAI_KEY = OPENAI_KEY_PREFIX + "doctor" + "K" * 30
CUSTOMER = "1234567890"
META_ACCOUNT = "9876543210"
ADS_BASE = "https://googleads.googleapis.com/v26"


def token_json(scopes, access="g", refresh="r"):
    return json.dumps({"token": GOOGLE_ACCESS_PREFIX + access * 30, "refresh_token": GOOGLE_REFRESH_PREFIX + refresh * 30,
                       "token_uri": "https://oauth2.googleapis.com/token", "client_id": "client-id",
                       "client_secret": "client-" + "s" * 20, "scopes": scopes, "expiry": "2099-01-01T00:00:00Z"})


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path, no_keyring):
    for name in doctor.KNOWN_ENV:
        monkeypatch.delenv(name, raising=False)
    for client in (google_ads.CLIENT, meta_ads.CLIENT, openai_ads.CLIENT):
        client.cache.clear()
        monkeypatch.setattr(client, "session", FakeSession())
        monkeypatch.setattr(client, "BLOCKED_ATTEMPTS", [])
    google_ads._CURRENT_BEARER.clear()
    monkeypatch.setattr(google_ads, "TOKENS", google_ads.AdsTokenProvider())
    yield no_keyring
    for client in (google_ads.CLIENT, meta_ads.CLIENT, openai_ads.CLIENT):
        client.cache.clear()


def write_config(tmp_path, env, name="google-ecommerce"):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {name: {"command": "uvx", "args": ["google-ecommerce-mcp"],
                                                       "env": env}}}))
    return cfg


def google_token_file(tmp_path, scopes=None, name="google-token.json"):
    path = tmp_path / name
    path.write_text(token_json(READ_ONLY_SCOPES if scopes is None else scopes))
    return str(path)


def ads_token_file(tmp_path, scopes=(ADS_SCOPE,), name="ads-token.json"):
    path = tmp_path / name
    path.write_text(token_json(list(scopes), access="a", refresh="b"))
    return str(path)


def run(capsys, cfg, *extra):
    code = main(["doctor", "--config", str(cfg), *extra])
    out = capsys.readouterr().out
    return code, out


def run_json(capsys, cfg, *extra):
    code, out = run(capsys, cfg, "--json", *extra)
    return code, json.loads(out), out


def row(result, service):
    return next(s for s in result["services"] if s["service"] == service)


# ------------------------------------------------------------------ helpers
@pytest.mark.parametrize("value, masked", [("1234567890", "123***890"), ("123-456-7890", "123***890"),
                                            ("GTM-ABC1234", "GTM***234"), ("123456", "1***6"), ("12345", "***"),
                                            ("", "***"), (None, "***")])
def test_mask_never_shows_the_full_value(value, masked):
    assert doctor.mask(value) == masked
    if value and len(value) > 5:
        assert value not in doctor.mask(value)


def test_parse_only_canonical_order_and_unknown():
    assert doctor.parse_only("meta, gads") == ("gads", "meta")
    assert doctor.parse_only(None) == doctor.guide.SERVICE_IDS
    for bad in ("gads,tiktok", ",", "", " , "):
        with pytest.raises(ValueError):
            doctor.parse_only(bad)


# ------------------------------------------------------------------ offline
def test_nothing_configured_exits_2_with_a_first_command(capsys, tmp_path):
    code, out = run(capsys, tmp_path / "missing.json")
    assert code == 2
    assert "Nothing is set up yet" in out and "google-ecommerce-mcp install --yes --client-secret" in out
    assert "Claude Desktop config not found" in out


def test_entry_env_of_claude_desktop_is_read(capsys, tmp_path):
    cfg = write_config(tmp_path, {"GA4_PROPERTY_ID": "123456789",
                                  "GOOGLE_TOKEN_FILE": google_token_file(tmp_path)})
    code, result, out = run_json(capsys, cfg, "--only", "ga4")
    assert code == 0 and result["exit_code"] == 0
    ga4 = row(result, "ga4")
    assert ga4["configured"] and ga4["secret_present"] and ga4["token_check"] == "ok" and ga4["status"] == "ok"
    assert ga4["account"] == "property 123***789" and "123456789" not in out
    assert result["claude_desktop"]["registered"] is True


def test_process_env_is_overlaid_by_the_config_entry(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("GADS_CUSTOMER_ID", "1111111111")
    cfg = write_config(tmp_path, {"GADS_CUSTOMER_ID": CUSTOMER})
    _, result, _ = run_json(capsys, cfg, "--only", "gads")
    assert row(result, "gads")["account"] == "customer 123***890"


def test_ads_partial_customer_without_ads_token(capsys, tmp_path):
    cfg = write_config(tmp_path, {"GADS_CUSTOMER_ID": "123-456-7890"})
    code, out = run(capsys, cfg, "--only", "gads,meta,openai")
    assert code == 1
    assert "google-ecommerce-mcp setup-ads --client-secret <client_secret.json>" in out
    assert "then: google-ecommerce-mcp doctor --only gads --live" in out
    assert "123***890" in out and CUSTOMER not in out and "123-456-7890" not in out
    assert "Meta Ads" in out and "GA4" not in out


def test_ads_token_present_but_no_customer_id(capsys, tmp_path):
    cfg = write_config(tmp_path, {"GADS_TOKEN_FILE": ads_token_file(tmp_path)})
    code, result, _ = run_json(capsys, cfg, "--only", "gads")
    gads = row(result, "gads")
    assert code == 1 and gads["status"] == "fix" and gads["token_check"] == "ok"
    assert gads["next_step"] == "google-ecommerce-mcp install --yes --skip-auth --gads-customer-id <customer id>"


def test_ads_token_ok_offline(capsys, tmp_path, clean):
    clean[(KEYRING_SERVICE, "oauth-token-ads")] = token_json([ADS_SCOPE], access="a")
    cfg = write_config(tmp_path, {"GADS_CUSTOMER_ID": CUSTOMER})
    code, result, _ = run_json(capsys, cfg, "--only", "gads")
    assert code == 0 and row(result, "gads")["status"] == "ok"


def test_ads_token_with_wrong_scope(capsys, tmp_path):
    cfg = write_config(tmp_path, {"GADS_CUSTOMER_ID": CUSTOMER,
                                  "GADS_TOKEN_FILE": ads_token_file(tmp_path, scopes=[READ_ONLY_SCOPES[0]])})
    _, result, _ = run_json(capsys, cfg, "--only", "gads")
    assert row(result, "gads")["token_check"] == "missing scope"
    assert "setup-ads" in row(result, "gads")["next_step"] and "--token-file" in row(result, "gads")["next_step"]


@pytest.mark.parametrize("case", ["same_file", "same_token", "mixed_scopes"])
def test_ads_token_must_be_separate(capsys, tmp_path, clean, case):
    google = google_token_file(tmp_path)
    env = {"GADS_CUSTOMER_ID": CUSTOMER, "GA4_PROPERTY_ID": "123456789"}
    if case == "same_file":
        env.update(GOOGLE_TOKEN_FILE=google, GADS_TOKEN_FILE=google)
    elif case == "same_token":
        raw = token_json([ADS_SCOPE])  # only the adwords scope: caught by the content check alone
        clean[(KEYRING_SERVICE, "oauth-token")] = raw
        clean[(KEYRING_SERVICE, "oauth-token-ads")] = raw
    else:
        env["GADS_TOKEN_FILE"] = ads_token_file(tmp_path, scopes=[ADS_SCOPE, READ_ONLY_SCOPES[0]])
    code, result, _ = run_json(capsys, write_config(tmp_path, env), "--only", "gads")
    gads = row(result, "gads")
    assert code == 1 and gads["token_check"] == "not separate" and gads["status"] == "fix"
    assert "setup-ads" in gads["next_step"]
    if case == "same_file":
        assert "GADS_TOKEN_FILE is the same file as GOOGLE_TOKEN_FILE" in gads["problem"]


def test_google_token_holding_adwords_is_warned(capsys, tmp_path, clean):
    clean[(KEYRING_SERVICE, "oauth-token")] = token_json([*READ_ONLY_SCOPES, ADS_SCOPE])
    _, result, _ = run_json(capsys, write_config(tmp_path, {"GA4_PROPERTY_ID": "123456789"}), "--only", "ga4")
    assert any("also holds the Google Ads permission" in w for w in result["warnings"])


def test_meta_token_without_account(capsys, tmp_path, clean):
    clean[(KEYRING_SERVICE, "meta-access-token")] = META_TOKEN
    code, result, out = run_json(capsys, write_config(tmp_path, {}), "--only", "meta")
    meta = row(result, "meta")
    assert code == 1 and meta["configured"] and meta["secret_present"] and meta["status"] == "fix"
    assert meta["next_step"] == "google-ecommerce-mcp install --yes --skip-auth --meta-account-id <ad account id>"
    assert META_TOKEN not in out


def test_meta_account_without_token(capsys, tmp_path):
    code, out = run(capsys, write_config(tmp_path, {"META_AD_ACCOUNT_ID": f"act_{META_ACCOUNT}"}), "--only", "meta")
    assert code == 1 and "google-ecommerce-mcp set-secret meta-access-token" in out
    assert "987***210" in out and META_ACCOUNT not in out


def test_openai_key_in_keyring_is_ok(capsys, tmp_path, clean):
    clean[(KEYRING_SERVICE, "openai-ads-key")] = OPENAI_KEY
    code, result, out = run_json(capsys, write_config(tmp_path, {}), "--only", "openai")
    assert code == 0 and row(result, "openai")["status"] == "ok" and OPENAI_KEY not in out


def test_google_ids_without_token_and_merchant_scope(capsys, tmp_path):
    token = google_token_file(tmp_path)  # read-only scopes, no content scope
    cfg = write_config(tmp_path, {"MERCHANT_ACCOUNT_ID": "555666777", "GSC_SITE_URL": "sc-domain:example.com",
                                  "GOOGLE_TOKEN_FILE": token})
    code, result, out = run_json(capsys, cfg, "--only", "gsc,merchant")
    assert code == 1
    assert row(result, "gsc")["status"] == "ok"
    merchant = row(result, "merchant")
    assert merchant["token_check"] == "missing scope"
    assert merchant["next_step"] == f'google-ecommerce-mcp setup --client-secret <client_secret.json> --token-file "{token}" --with-merchant'
    assert "example.com" not in json.dumps(row(result, "gsc")["account"])


def test_google_token_missing(capsys, tmp_path):
    cfg = write_config(tmp_path, {"GTM_CONTAINER_ID": "GTM-ABC1234"})
    code, result, _ = run_json(capsys, cfg, "--only", "gtm")
    assert code == 1 and row(result, "gtm")["problem"] == "No Google token is stored yet"


def test_only_filters_rows(capsys, tmp_path):
    _, result, _ = run_json(capsys, write_config(tmp_path, {}), "--only", "openai,gads")
    assert [s["service"] for s in result["services"]] == ["gads", "openai"]


def test_unknown_only_value_exits_2(capsys, tmp_path):
    code, out = run(capsys, write_config(tmp_path, {}), "--only", "tiktok")
    assert code == 2 and "unknown service tiktok" in out


def test_warnings_trust_env_sensitive_queries_and_plain_secrets(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("GADS_ALLOW_SENSITIVE_QUERIES", "1")
    cfg = write_config(tmp_path, {"ADS_TRUST_ENV": "1", "META_ACCESS_TOKEN": META_TOKEN,
                                  "META_AD_ACCOUNT_ID": META_ACCOUNT})
    code, result, out = run_json(capsys, cfg)
    text = " ".join(result["warnings"])
    assert "ADS_TRUST_ENV=1" in text and "GADS_ALLOW_SENSITIVE_QUERIES" in text
    assert "META_ACCESS_TOKEN is written in plain text" in text
    assert META_TOKEN not in out and META_ACCOUNT not in out
    code_text, out_text = run(capsys, cfg)
    assert "Warnings:" in out_text and META_TOKEN not in out_text


def test_not_registered_is_warned(capsys, tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    _, result, _ = run_json(capsys, cfg)
    assert any("not registered in Claude Desktop" in w for w in result["warnings"])


def test_broken_config_is_reported_not_raised(capsys, tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text("{broken")
    code, result, _ = run_json(capsys, cfg)
    assert code == 2 and any("could not be read" in w for w in result["warnings"])
    assert cfg.read_text() == "{broken"


def test_json_is_valid_and_has_no_secret(capsys, tmp_path, clean):
    clean[(KEYRING_SERVICE, "meta-access-token")] = META_TOKEN
    clean[(KEYRING_SERVICE, "openai-ads-key")] = OPENAI_KEY
    raw = token_json(READ_ONLY_SCOPES)
    clean[(KEYRING_SERVICE, "oauth-token")] = raw
    cfg = write_config(tmp_path, {"GA4_PROPERTY_ID": "123456789", "META_AD_ACCOUNT_ID": META_ACCOUNT,
                                  "GADS_CUSTOMER_ID": CUSTOMER, "PAGESPEED_API_KEY": "AIza" + "p" * 35})
    code, result, out = run_json(capsys, cfg)
    for secret in (META_TOKEN, OPENAI_KEY, GOOGLE_ACCESS_PREFIX, GOOGLE_REFRESH_PREFIX, "client-sss",
                   "AIza" + "p" * 35, CUSTOMER, META_ACCOUNT, "123456789"):
        assert secret not in out
    assert {"mode", "services", "warnings", "exit_code", "claude_desktop"} <= set(result)
    assert all(isinstance(s["configured"], bool) and isinstance(s["secret_present"], bool) for s in result["services"])


def test_keyring_without_backend_is_reported(capsys, tmp_path, monkeypatch):
    import keyring

    def broken(service, user):
        raise RuntimeError("no backend")

    monkeypatch.setattr(keyring, "get_password", broken)
    code, result, _ = run_json(capsys, write_config(tmp_path, {"META_AD_ACCOUNT_ID": META_ACCOUNT}), "--only",
                               "meta,ga4")
    assert code == 1 and "keyring: RuntimeError" in row(result, "meta")["problem"]


def test_text_table_columns(capsys, tmp_path):
    code, out = run(capsys, write_config(tmp_path, {"META_AD_ACCOUNT_ID": META_ACCOUNT}))
    header = next(line for line in out.splitlines() if line.startswith("Service"))
    assert all(col in header for col in ("Set up", "Secret", "Token", "Status", "Account"))
    assert "To fix (one at a time" in out and "Next command: google-ecommerce-mcp set-secret meta-access-token" in out


# ------------------------------------------------------------------ live
def use(client, rules):
    session = FakeSession(rules)
    client.session = session
    return session


def gads_ready(tmp_path, extra=None):
    env = {"GADS_CUSTOMER_ID": CUSTOMER, "GADS_TOKEN_FILE": ads_token_file(tmp_path), **(extra or {})}
    return write_config(tmp_path, env)


def test_live_gads_ok_with_short_timeout_and_env_restored(capsys, tmp_path):
    session = use(google_ads.CLIENT, [("listAccessibleCustomers", FakeResponse(200, {
        "resourceNames": [f"customers/{CUSTOMER}", "customers/5555555555"]}))])
    before = dict(os.environ)
    timeout_before = google_ads.CLIENT.timeout
    code, result, out = run_json(capsys, gads_ready(tmp_path), "--live", "--only", "gads")
    assert code == 0 and row(result, "gads")["live"] == "ok" and result["mode"] == "live"
    assert session.calls and session.calls[0][2]["timeout"] == doctor.LIVE_TIMEOUT
    assert dict(os.environ) == before and google_ads.CLIENT.timeout == timeout_before
    assert "5555555555" not in out and GOOGLE_ACCESS_PREFIX not in out


def test_live_gads_customer_not_directly_visible(capsys, tmp_path):
    use(google_ads.CLIENT, [("listAccessibleCustomers", FakeResponse(200, {"resourceNames": ["customers/1"]}))])
    code, result, _ = run_json(capsys, gads_ready(tmp_path), "--live", "--only", "gads")
    gads = row(result, "gads")
    assert code == 1 and gads["live"] == "not_visible" and "--gads-login-customer-id" in gads["next_step"]


@pytest.mark.parametrize("response, code, needle", [
    (FakeResponse(401, {"error": {"status": "UNAUTHENTICATED"}}), "auth_failed", "setup-ads"),
    (FakeResponse(403, {"error": {"status": "PERMISSION_DENIED", "message": "SCOPE_INSUFFICIENT"}}), "missing_scope",
     "setup-ads"),
    (FakeResponse(429, {"error": {"status": "RESOURCE_EXHAUSTED"}}, headers={"Retry-After": "30"}), "rate_limited",
     "doctor --only gads --live"),
    (FakeResponse(404, {"error": {"status": "NOT_FOUND"}}), "not_available", "doctor --only gads --live"),
    (requests.ConnectionError("boom"), "network", "--corporate-proxy"),
    (FakeResponse(200, {}, headers={"Content-Length": str(10 ** 9)}), "response_too_large",
     "doctor --only gads --live"),
])
def test_live_gads_structured_errors(capsys, tmp_path, response, code, needle):
    use(google_ads.CLIENT, [("listAccessibleCustomers", response)])
    exit_code, result, out = run_json(capsys, gads_ready(tmp_path), "--live", "--only", "gads")
    gads = row(result, "gads")
    assert exit_code == 1 and gads["live"] == code and gads["status"] == "fix"
    assert needle in gads["next_step"]
    if code == "rate_limited":
        assert "wait 30 seconds" in gads["problem"]
    assert "UNAUTHENTICATED" not in out and "SCOPE_INSUFFICIENT" not in out  # platform detail never echoed


def test_live_skipped_when_offline_check_fails(capsys, tmp_path):
    session = use(google_ads.CLIENT, [])
    code, result, _ = run_json(capsys, write_config(tmp_path, {"GADS_CUSTOMER_ID": CUSTOMER}), "--live", "--only",
                               "gads")
    assert code == 1 and session.calls == [] and row(result, "gads")["live"] == ""


def meta_ready(clean, tmp_path):
    clean[(KEYRING_SERVICE, "meta-access-token")] = META_TOKEN
    return write_config(tmp_path, {"META_AD_ACCOUNT_ID": f"act_{META_ACCOUNT}"})


def test_live_meta_ok(capsys, tmp_path, clean):
    session = use(meta_ads.CLIENT, [("/me/adaccounts", FakeResponse(200, {"data": [
        {"id": f"act_{META_ACCOUNT}", "account_id": META_ACCOUNT, "name": "Shop", "account_status": 1}]}))])
    code, result, out = run_json(capsys, meta_ready(clean, tmp_path), "--live", "--only", "meta")
    assert code == 0 and row(result, "meta")["live"] == "ok"
    assert META_TOKEN not in out and META_ACCOUNT not in out and "Shop" not in out
    assert session.calls[0][2]["headers"]["Authorization"] == f"Bearer {META_TOKEN}"


def test_live_meta_account_not_visible(capsys, tmp_path, clean):
    use(meta_ads.CLIENT, [("/me/adaccounts", FakeResponse(200, {"data": [{"id": "act_111", "account_id": "111"}]}))])
    code, result, _ = run_json(capsys, meta_ready(clean, tmp_path), "--live", "--only", "meta")
    assert code == 1 and row(result, "meta")["live"] == "not_visible"
    assert "Business Settings" in row(result, "meta")["problem"]


@pytest.mark.parametrize("payload, status, code, needle", [
    ({"error": {"code": 190, "message": "Invalid OAuth access token"}}, 400, "auth_failed",
     "set-secret meta-access-token"),
    ({"error": {"code": 200, "message": "Requires ads_read"}}, 403, "missing_scope", "set-secret meta-access-token"),
    ({"error": {"code": 17, "message": "User request limit reached"}}, 400, "rate_limited", "doctor --only meta"),
])
def test_live_meta_errors(capsys, tmp_path, clean, payload, status, code, needle):
    use(meta_ads.CLIENT, [("/me/adaccounts", FakeResponse(status, payload))])
    exit_code, result, out = run_json(capsys, meta_ready(clean, tmp_path), "--live", "--only", "meta")
    meta = row(result, "meta")
    assert exit_code == 1 and meta["live"] == code and needle in meta["next_step"]
    assert "Invalid OAuth" not in out


def test_live_openai_ok_and_auth_failed(capsys, tmp_path, clean):
    clean[(KEYRING_SERVICE, "openai-ads-key")] = OPENAI_KEY
    cfg = write_config(tmp_path, {})
    use(openai_ads.CLIENT, [("/v1/ad_account", FakeResponse(200, {"id": "acct_1", "name": "Shop"}))])
    code, result, out = run_json(capsys, cfg, "--live", "--only", "openai")
    assert code == 0 and row(result, "openai")["live"] == "ok" and OPENAI_KEY not in out
    openai_ads.CLIENT.cache.clear()
    use(openai_ads.CLIENT, [("/v1/ad_account", FakeResponse(401, {"error": {"message": "bad key"}}))])
    code, result, out = run_json(capsys, cfg, "--live", "--only", "openai")
    assert code == 1 and row(result, "openai")["next_step"] == "google-ecommerce-mcp set-secret openai-ads-key"


def test_live_openai_not_available(capsys, tmp_path, clean):
    clean[(KEYRING_SERVICE, "openai-ads-key")] = OPENAI_KEY
    use(openai_ads.CLIENT, [("/v1/ad_account", FakeResponse(404, {"error": {"message": "nope"}}))])
    code, result, _ = run_json(capsys, write_config(tmp_path, {}), "--live", "--only", "openai")
    assert code == 1 and row(result, "openai")["live"] == "not_available"


@pytest.fixture
def google_http(monkeypatch):
    from google_ecommerce_mcp import server

    def install(rules):
        session = FakeSession(rules)
        monkeypatch.setattr(server, "HTTP", session)
        return session

    return install


def test_live_google_services(capsys, tmp_path, google_http):
    from google_ecommerce_mcp import server

    saved = server.SETTINGS
    session = google_http([
        ("accountSummaries", FakeResponse(200, {"accountSummaries": [{"account": "accounts/1", "propertySummaries": [
            {"property": "properties/123456789", "displayName": "Shop"}]}]})),
        ("/sitemaps", FakeResponse(403, {"error": {"message": "Request had insufficient authentication scopes."}})),
        ("dataSources", FakeResponse(403, {"error": {"message": "SERVICE_DISABLED Merchant API has not been used "
                                                                "in project 1"}})),
        ("tagmanager", requests.ConnectionError("down")),
    ])
    token = google_token_file(tmp_path, scopes=[*READ_ONLY_SCOPES, MERCHANT_SCOPE])
    cfg = write_config(tmp_path, {"GA4_PROPERTY_ID": "123456789", "GSC_SITE_URL": "sc-domain:example.com",
                                  "MERCHANT_ACCOUNT_ID": "555666777", "GTM_CONTAINER_ID": "GTM-ABC1234",
                                  "GOOGLE_TOKEN_FILE": token})
    code, result, out = run_json(capsys, cfg, "--live", "--only", "ga4,gsc,merchant,gtm")
    assert code == 1
    assert row(result, "ga4")["live"] == "ok" and row(result, "ga4")["status"] == "ok"
    assert row(result, "gsc")["live"] == "missing_scope" and "setup --client-secret" in row(result, "gsc")["next_step"]
    assert row(result, "merchant")["live"] == "api_disabled" and "Merchant API" in row(result, "merchant")["problem"]
    assert row(result, "gtm")["live"] == "network"
    assert all(call[2]["timeout"] == doctor.LIVE_TIMEOUT for call in session.calls)
    assert server.SETTINGS is saved
    assert "example.com" not in out and "555666777" not in out


def test_live_ga4_property_not_visible(capsys, tmp_path, google_http):
    google_http([("accountSummaries", FakeResponse(200, {"accountSummaries": []}))])
    cfg = write_config(tmp_path, {"GA4_PROPERTY_ID": "123456789", "GOOGLE_TOKEN_FILE": google_token_file(tmp_path)})
    code, result, _ = run_json(capsys, cfg, "--live", "--only", "ga4")
    assert code == 1 and row(result, "ga4")["live"] == "not_visible"


def test_live_pagespeed_is_never_called(capsys, tmp_path, google_http):
    session = google_http([])
    code, result, _ = run_json(capsys, write_config(tmp_path, {"PAGESPEED_API_KEY": "AIza" + "x" * 35}), "--live",
                               "--only", "pagespeed")
    assert code == 0 and row(result, "pagespeed")["live"] == "skipped" and session.calls == []


def test_live_text_output_has_live_column(capsys, tmp_path):
    use(google_ads.CLIENT, [("listAccessibleCustomers", FakeResponse(200, {"resourceNames": [f"customers/{CUSTOMER}"]}))])
    code, out = run(capsys, gads_ready(tmp_path), "--live", "--only", "gads")
    assert code == 0 and "Live" in out and "live test" in out


def test_advice_covers_every_structured_error():
    env = {}
    for service in ("ga4", "gads", "meta", "openai"):
        for code in ("not_configured", "auth_failed", "missing_scope", "rate_limited", "not_available", "network",
                     "response_too_large", "blocked", "internal_error"):
            problem, command = doctor.advice(service, {"error": code}, env)
            assert problem and command.startswith("google-ecommerce-mcp ")


# ------------------------------------------------------------------ compat
@pytest.mark.parametrize("module", ["doctor.py", "setup_guide.py", "installer.py", "__main__.py"])
def test_python_310_syntax(module):
    path = Path(doctor.__file__).with_name(module)
    ast.parse(path.read_text(encoding="utf-8"), filename=module, feature_version=(3, 10))


def test_not_set_up_google_service_with_scope_needs_only_the_id(capsys, tmp_path):
    cfg = write_config(tmp_path, {"GA4_PROPERTY_ID": "123456789", "GOOGLE_TOKEN_FILE": google_token_file(tmp_path)})
    _, result, _ = run_json(capsys, cfg, "--only", "gsc,merchant")
    assert row(result, "gsc")["next_step"] == "google-ecommerce-mcp install --yes --skip-auth --gsc <property>"
    assert row(result, "merchant")["next_step"].startswith("google-ecommerce-mcp install --yes --client-secret")
    assert row(result, "gsc")["token_check"] == row(result, "merchant")["token_check"] == "-"


# ------------------------------------------------------------------ review fixes (P3)
PLAIN_SECRET = "plain-secret-value-0042"  # no known token shape: only the exact-value masking (_secrets) hides it


def test_run_redacts_shaped_and_configured_secrets(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", PLAIN_SECRET)

    def leaky(*args, **kwargs):
        return {"mode": "offline", "claude_desktop": {"registered": True}, "services": [],
                "warnings": [f"value {PLAIN_SECRET}", f"shaped {META_TOKEN}"], "exit_code": 2}

    monkeypatch.setattr(doctor, "diagnose", leaky)
    for extra in ((), ("--json",)):
        _, out = run(capsys, write_config(tmp_path, {}), *extra)
        assert PLAIN_SECRET not in out and META_TOKEN not in out and "[REDACTED]" in out


def test_secrets_list_reads_environment_and_keyring(monkeypatch, clean):
    clean[(KEYRING_SERVICE, "openai-ads-key")] = OPENAI_KEY
    assert set(doctor._secrets({"PAGESPEED_API_KEY": PLAIN_SECRET})) == {PLAIN_SECRET, OPENAI_KEY}


def test_read_errors_keep_only_the_class_name(capsys, tmp_path, monkeypatch):
    import keyring

    def broken(service, user):
        raise RuntimeError("leak-me-" + PLAIN_SECRET)

    monkeypatch.setattr(keyring, "get_password", broken)
    assert doctor._read(lambda source: broken("", ""), None) == (None, "RuntimeError")
    code, out = run(capsys, write_config(tmp_path, {"GA4_PROPERTY_ID": "123456789", "META_AD_ACCOUNT_ID": "123456"}))
    assert code == 1 and "RuntimeError" in out and "leak-me" not in out


@pytest.mark.parametrize("env, service, problem", [
    ({"GA4_PROPERTY_ID": "G-ABC1234567"}, "ga4", "digits only"),
    ({"MERCHANT_ACCOUNT_ID": "acc-1234567"}, "merchant", "digits only"),
    ({"GTM_CONTAINER_ID": "ABC1234567"}, "gtm", "GTM-XXXXXXX"),
    ({"META_AD_ACCOUNT_ID": "act_abc123456"}, "meta", "digits"),
    ({"GADS_CUSTOMER_ID": "123456789"}, "gads", "10 digits"),
])
def test_wrong_id_format_is_fixed_and_fully_hidden(capsys, tmp_path, clean, env, service, problem):
    clean[(KEYRING_SERVICE, "oauth-token")] = token_json([*READ_ONLY_SCOPES, MERCHANT_SCOPE])
    clean[(KEYRING_SERVICE, "meta-access-token")] = META_TOKEN
    clean[(KEYRING_SERVICE, "oauth-token-ads")] = token_json([ADS_SCOPE], access="a")
    code, result, out = run_json(capsys, write_config(tmp_path, env), "--only", service)
    item = row(result, service)
    assert code == 1 and item["status"] == "fix" and problem in item["problem"]
    assert item["account"].endswith(" ***") and "--force" in item["next_step"]
    assert list(env.values())[0] not in out


@pytest.mark.parametrize("value, kind", [(META_TOKEN_PREFIX + "123456789", "meta"), ("ya" + "29." + "abcdefstu", "gads"),
                                         ("s" + "k-" + "abc123456", "ga4"), ("EAA-not-gtm", "gtm"), ("12345678901", "gads")])
def test_mask_hides_wrong_formats_completely(value, kind):
    assert doctor.mask(value, kind) == "***"


def test_mask_of_valid_formats():
    assert doctor.mask("123-456-7890", "gads") == "123***890"
    assert doctor.mask("act_9876543210", "meta") == "987***210"
    assert doctor.mask("GTM-ABC1234", "gtm") == "GTM***234"


def test_meta_token_from_this_terminal_counts(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", META_TOKEN)
    code, result, out = run_json(capsys, write_config(tmp_path, {"META_AD_ACCOUNT_ID": "123456"}), "--only", "meta")
    assert code == 0 and row(result, "meta")["secret_present"] and META_TOKEN not in out


def test_config_with_bom_is_read(capsys, tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_bytes(b"\xef\xbb\xbf" + json.dumps({"mcpServers": {"google-ecommerce": {
        "env": {"META_AD_ACCOUNT_ID": "123456"}}}}).encode())
    _, result, _ = run_json(capsys, cfg, "--only", "meta")
    assert result["claude_desktop"]["registered"] and row(result, "meta")["configured"]


@pytest.mark.parametrize("raw", [json.dumps({"mcpServers": {}}).encode("utf-16"), b"[]", b"null",
                                 b'{"mcpServers": ["x"]}', b'{"mcpServers": null}'])
def test_unreadable_or_wrong_shape_config_is_a_warning(capsys, tmp_path, raw):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_bytes(raw)
    code, result, out = run_json(capsys, cfg)
    assert code == 2 and any("could not be read" in w for w in result["warnings"]) and "Traceback" not in out


def test_oversized_config_is_a_warning(capsys, tmp_path, monkeypatch):
    from google_ecommerce_mcp import installer

    monkeypatch.setattr(installer, "MAX_CONFIG_BYTES", 5)
    _, result, _ = run_json(capsys, write_config(tmp_path, {"META_AD_ACCOUNT_ID": "123456"}))
    assert any("could not be read" in w for w in result["warnings"])


def test_effective_env_strips_and_skips_blanks():
    env = doctor.effective_env({"GA4_PROPERTY_ID": " 123 ", "GSC_SITE_URL": "  ", "OTHER": "x"},
                               base={"META_AD_ACCOUNT_ID": " 456 ", "GTM_CONTAINER_ID": " "})
    assert env == {"GA4_PROPERTY_ID": "123", "META_AD_ACCOUNT_ID": "456"}


def test_exit_code_ignores_unconfigured_reports():
    unconfigured_fix = doctor.ServiceReport("gads", "Google Ads", configured=False, status=doctor.FIX)
    ok = doctor.ServiceReport("meta", "Meta Ads", configured=True, status=doctor.OK)
    assert doctor.exit_code([unconfigured_fix]) == 2
    assert doctor.exit_code([unconfigured_fix, ok]) == 0
    assert doctor.exit_code([ok, doctor.ServiceReport("openai", "x", configured=True, status=doctor.FIX)]) == 1


def test_indexing_scope_is_kept_in_the_reauthorization_command(capsys, tmp_path):
    from google_ecommerce_mcp.config import INDEXING_SCOPE

    assert doctor.guide.google_setup_command(indexing=True).endswith(" --with-indexing")
    token = google_token_file(tmp_path, scopes=[READ_ONLY_SCOPES[0], INDEXING_SCOPE])
    _, result, _ = run_json(capsys, write_config(tmp_path, {"GTM_CONTAINER_ID": "GTM-ABC1234",
                                                            "GOOGLE_TOKEN_FILE": token}), "--only", "gtm")
    assert row(result, "gtm")["next_step"].endswith(" --with-indexing")


@pytest.mark.parametrize("raw, wait", [("12.5", 12), ("30", 30), ("Wed, 21 Oct 2026 07:28:00 GMT", 60), (None, 60),
                                       ("-5", 1)])
def test_retry_after_is_always_an_integer(raw, wait):
    problem, _ = doctor.advice("meta", {"error": "rate_limited", "retry_after_seconds": raw}, {})
    assert f"wait {wait} seconds" in problem and "GMT" not in problem


def test_only_comma_is_rejected(capsys, tmp_path):
    code, out = run(capsys, write_config(tmp_path, {}), "--only", ",")
    assert code == 2 and "at least one service" in out


def test_live_twice_reuses_nothing(capsys, tmp_path):
    saved_tokens = google_ads.TOKENS
    session = use(google_ads.CLIENT, [("listAccessibleCustomers", FakeResponse(200, {
        "resourceNames": [f"customers/{CUSTOMER}"]}))])
    cfg = gads_ready(tmp_path)
    for _ in range(2):
        assert run_json(capsys, cfg, "--live", "--only", "gads")[0] == 0
    assert len(session.calls) == 2  # the 5-minute discovery cache was not reused
    assert google_ads.TOKENS is saved_tokens and google_ads._CURRENT_BEARER == {}
    assert google_ads.CLIENT.cache.get("anything") is None


def _fstring_310_violations(source: str) -> list[str]:
    """Copy of tests/test_report_render.py: f-string syntax accepted only from Python 3.12 (PEP 701), a backslash
    or the outer quote reused inside a replacement field, which ast's feature_version does not check."""
    import io
    import sys
    import tokenize

    if sys.version_info < (3, 12):  # older tokenizers reject such code outright: ast.parse already failed
        return []
    out: list[str] = []
    stack: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        name = tokenize.tok_name[tok.type]
        if name == "FSTRING_START":
            quote = tok.string.lstrip("rRfFbBuU")
            if stack and (quote[0] in "".join(stack) or "\\" in tok.string):
                out.append(f"line {tok.start[0]}: nested f-string reuses a quote")
            stack.append(quote)
        elif name == "FSTRING_END":
            stack.pop()
        elif stack and name == "STRING":
            body = tok.string.lstrip("rRbBuU")
            if body[0] in "".join(stack) or "\\" in tok.string:
                out.append(f"line {tok.start[0]}: string in an f-string expression reuses a quote or has a backslash")
        elif len(stack) > 1 and name == "FSTRING_MIDDLE" and "\\" in tok.string:
            out.append(f"line {tok.start[0]}: backslash inside a nested f-string")
    return out


SETUP_FILES = ["doctor.py", "setup_guide.py", "installer.py", "__main__.py"]
SETUP_TESTS = ["test_doctor.py", "test_setup_install.py", "test_setup_guide.py"]


def test_no_python_312_only_fstrings():
    files = [Path(doctor.__file__).with_name(m) for m in SETUP_FILES] + [Path(__file__).with_name(t)
                                                                          for t in SETUP_TESTS]
    problems = {p.name: v for p in files if (v := _fstring_310_violations(p.read_text(encoding="utf-8")))}
    assert problems == {}
    for path in files[len(SETUP_FILES):]:
        ast.parse(path.read_text(encoding="utf-8"), filename=path.name, feature_version=(3, 10))


def test_fstring_checker_catches_312_syntax():
    assert _fstring_310_violations("x = f'{\"a\" if 1 else \"\"}'\n") == []
    if __import__("sys").version_info >= (3, 12):
        assert _fstring_310_violations("x = f'{'a'}'\n")
        assert _fstring_310_violations("x = f'{\"" + chr(92) + "n\".join(y)}'\n")

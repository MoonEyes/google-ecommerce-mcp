"""Tests for ads_overview: side-by-side blocks, partial results, currency rule, pooled ROAS, mandatory warning.

summary() is monkeypatched, or the three real summaries run on FakeSession (no network); no_network
and no_keyring are autouse."""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import random

import pytest

from ads_fakes import FakeResponse, FakeSession, no_keyring, no_network  # noqa: F401  (fixtures)
from google_ecommerce_mcp import ads_overview as ov
from google_ecommerce_mcp import google_ads, meta_ads, openai_ads
from google_ecommerce_mcp.config import NotConfigured

WARNING = ("Les conversions de chaque plateforme sont attribuées indépendamment et se recoupent. "
           "La somme n'est pas dédupliquée et surestime les commandes réelles.")
ENV = ["GADS_CUSTOMER_ID", "GADS_LOGIN_CUSTOMER_ID", "GADS_DEVELOPER_TOKEN", "GADS_TOKEN_FILE", "GADS_API_VERSION",
       "META_ACCESS_TOKEN", "META_AD_ACCOUNT_ID", "META_API_VERSION", "OPENAI_ADS_API_KEY"]
START, END = "2026-09-01", "2026-09-30"
MODULES = {"google_ads": google_ads, "meta": meta_ads, "openai_ads": openai_ads}


@pytest.fixture(autouse=True)
def env(monkeypatch, no_keyring):  # noqa: F811
    """Clean ads environment and in-memory keyring: a real token on this machine never reaches the tests."""
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


def ok(platform, spend, value, currency="EUR", conversions=10.0, **extra):
    attribution = {"google_ads": "réglage par action de conversion",
                   "meta": {"windows": ["7d_click", "1d_view"], "default_used": True},
                   "openai_ads": {"click_window_days": 30, "view_window_days": 1}}[platform]
    out = {"platform": platform, "spend": spend, "impressions": 1000, "clicks": 50, "conversions": conversions,
           "conversion_value": value, "roas": (round(value / spend, 6) if value is not None and spend else None),
           "currency": currency, "attribution": attribution,
           "date_range": {"start_date": START, "end_date": END}, "totals_complete": True}
    out.update(extra)
    return out


def not_configured(platform):
    return {"platform": platform, "error": "not_configured", "detail": f"{platform} is not configured"}


def patch(monkeypatch, **results):
    """Replace each platform summary with a fake returning results[name] (or raising it if an exception)."""
    calls = []
    for name, module in MODULES.items():
        result = results.get(name, not_configured(name))

        def fake(start_date, end_date, _name=name, _result=result):
            calls.append((_name, start_date, end_date))
            if isinstance(_result, BaseException):
                raise _result
            return _result

        monkeypatch.setattr(module, "summary", fake)
    return calls


def run():
    return ov.ads_overview(start_date=START, end_date=END)


# ------------------------------------------------------------------ all three platforms
def test_three_platforms_side_by_side_and_combined(monkeypatch):
    calls = patch(monkeypatch, google_ads=ok("google_ads", 100.0, 400.0), meta=ok("meta", 50.0, 100.0),
                  openai_ads=ok("openai_ads", 50.0, 0.0))
    out = run()
    assert sorted(calls) == sorted((p, START, END) for p in MODULES)
    assert set(out["platforms"]) == {"google_ads", "meta", "openai_ads"}
    for name, block in out["platforms"].items():
        assert block["status"] == "ok"
        for key in ("spend", "impressions", "clicks", "conversions", "conversion_value",
                    "roas_reported_by_platform", "currency", "attribution", "date_range"):
            assert key in block, (name, key)
    assert out["platforms"]["google_ads"]["roas_reported_by_platform"] == 4.0
    c = out["combined"]
    assert c["currency"] == "EUR" and c["spend"] == 200.0 and c["impressions"] == 3000 and c["clicks"] == 150
    assert c["conversions_sum_not_deduplicated"] == 30.0
    assert c["conversion_value_sum_not_deduplicated"] == 500.0
    assert out["partial"] is False and out["not_configured"] == [] and out["errors"] == []
    assert "mer" not in out and "mer" not in c


def test_combined_roas_is_pooled_never_averaged(monkeypatch):
    # ROAS 4.0, 2.0 and 0.0: mean = 2.0, pooled = 500 / 200 = 2.5
    patch(monkeypatch, google_ads=ok("google_ads", 100.0, 400.0), meta=ok("meta", 50.0, 100.0),
          openai_ads=ok("openai_ads", 50.0, 0.0))
    out = run()
    platform_roas = [b["roas_reported_by_platform"] for b in out["platforms"].values()]
    assert sum(platform_roas) / len(platform_roas) == 2.0
    assert out["combined"]["roas"] == 2.5


def test_attribution_windows_side_by_side(monkeypatch):
    patch(monkeypatch, google_ads=ok("google_ads", 10.0, 20.0), meta=ok("meta", 10.0, 20.0),
          openai_ads=ok("openai_ads", 10.0, 20.0))
    side = run()["attribution_side_by_side"]
    assert side["google_ads"] == {"basis": "réglage par action de conversion"}  # string wrapped in a dict
    assert all(isinstance(v, dict) for v in side.values())
    assert side["meta"]["windows"] == ["7d_click", "1d_view"]
    assert side["openai_ads"]["click_window_days"] == 30 and side["openai_ads"]["view_window_days"] == 1


def test_documented_attribution_defaults_shown_when_platform_absent(monkeypatch):
    patch(monkeypatch)
    side = run()["attribution_side_by_side"]
    assert all(isinstance(v, dict) for v in side.values())
    assert "action" in side["google_ads"]["basis"]
    assert side["meta"]["windows"] == ["7d_click", "1d_view"]
    assert (side["openai_ads"]["click_window_days"], side["openai_ads"]["view_window_days"]) == (30, 1)


# ------------------------------------------------------------------ one platform absent (one test each)
@pytest.mark.parametrize("absent", ["google_ads", "meta", "openai_ads"])
def test_one_platform_not_configured_does_not_block_others(monkeypatch, absent):
    results = {p: ok(p, 100.0, 300.0) for p in MODULES if p != absent}
    patch(monkeypatch, **results)
    out = run()
    assert out["platforms"][absent]["status"] == "not_configured"
    assert out["platforms"][absent]["error"] == "not_configured"
    assert out["not_configured"] == [absent] and out["partial"] is True
    others = [p for p in MODULES if p != absent]
    assert all(out["platforms"][p]["status"] == "ok" for p in others)
    assert out["combined"]["platforms_included"] == others
    assert out["combined"]["spend"] == 200.0 and out["combined"]["roas"] == 3.0
    assert out["warning"] == WARNING


def test_not_configured_raised_by_summary_becomes_a_block(monkeypatch):
    patch(monkeypatch, google_ads=ok("google_ads", 10.0, 20.0), meta=ok("meta", 10.0, 20.0),
          openai_ads=NotConfigured("OPENAI_ADS_API_KEY is not set"))
    out = run()
    assert out["platforms"]["openai_ads"]["status"] == "not_configured"
    assert out["combined"]["spend"] == 20.0


def test_nothing_configured(monkeypatch):
    patch(monkeypatch)
    out = run()
    assert out["combined"] is None and out["combined_reason"] == "no platform returned data"
    assert sorted(out["not_configured"]) == sorted(MODULES)
    assert out["warning"] == WARNING


def test_real_summaries_without_configuration_are_not_configured_blocks():
    """No monkeypatch: the real connectors, with an empty environment, answer without any network call."""
    out = run()
    assert out["warning"] == WARNING
    assert out["combined"] is None
    for name in ("google_ads", "meta"):
        assert out["platforms"][name]["status"] == "not_configured", out["platforms"][name]
    assert out["platforms"]["openai_ads"]["status"] in ("not_configured", "error")


# ------------------------------------------------------------------ currencies
def test_mixed_currencies_give_no_combined(monkeypatch):
    patch(monkeypatch, google_ads=ok("google_ads", 100.0, 300.0, currency="EUR"),
          meta=ok("meta", 100.0, 300.0, currency="USD"), openai_ads=ok("openai_ads", 100.0, 300.0, currency="EUR"))
    out = run()
    assert out["combined"] is None
    assert "different currencies" in out["combined_reason"] and "meta=USD" in out["combined_reason"]
    assert out["platforms"]["meta"]["currency"] == "USD"  # blocks still shown side by side
    assert out["warning"] == WARNING


def test_unknown_currency_gives_no_combined(monkeypatch):
    patch(monkeypatch, google_ads=ok("google_ads", 100.0, 300.0), meta=ok("meta", 100.0, 300.0),
          openai_ads=ok("openai_ads", 100.0, 300.0, currency=None))
    out = run()
    assert out["combined"] is None and "currency unknown for openai_ads" in out["combined_reason"]


def test_missing_conversion_value_keeps_spend_but_no_roas(monkeypatch):
    patch(monkeypatch, google_ads=ok("google_ads", 100.0, 300.0), meta=ok("meta", 100.0, None))
    c = run()["combined"]
    assert c["spend"] == 200.0 and c["roas"] is None and "roas_reason" in c


# ------------------------------------------------------------------ errors
def test_one_platform_error_gives_partial_result(monkeypatch):
    error = {"platform": "meta", "error": "auth_failed", "http_status": 401, "detail": {"code": 190},
             "fix": "create a new access token"}
    patch(monkeypatch, google_ads=ok("google_ads", 100.0, 250.0), meta=error,
          openai_ads=ok("openai_ads", 100.0, 150.0))
    out = run()
    block = out["platforms"]["meta"]
    assert block["status"] == "error" and block["error"] == "auth_failed" and block["fix"]
    assert out["errors"] == ["meta"] and out["partial"] is True
    assert out["combined"]["platforms_included"] == ["google_ads", "openai_ads"]
    assert out["combined"]["spend"] == 200.0 and out["combined"]["roas"] == 2.0
    assert out["warning"] == WARNING


def test_crashing_summary_is_contained_and_redacted(monkeypatch):
    secret = "sk-" + "a" * 30
    patch(monkeypatch, google_ads=RuntimeError(f"boom with {secret}"), meta=ok("meta", 10.0, 20.0))
    out = run()
    block = out["platforms"]["google_ads"]
    assert block["status"] == "error" and block["error"] == "internal_error"
    assert secret not in json.dumps(out, ensure_ascii=False)
    assert out["platforms"]["meta"]["status"] == "ok"


def test_incomplete_totals_flagged(monkeypatch):
    patch(monkeypatch, google_ads=ok("google_ads", 10.0, 20.0, totals_complete=False), meta=ok("meta", 10.0, 20.0))
    assert run()["combined"]["totals_complete"] is False


# ------------------------------------------------------------------ warning and inputs
@pytest.mark.parametrize("scenario", ["all_ok", "none", "mixed", "error", "bad_dates"])
def test_warning_present_and_literal_in_every_output(monkeypatch, scenario):
    if scenario == "all_ok":
        patch(monkeypatch, google_ads=ok("google_ads", 1.0, 2.0), meta=ok("meta", 1.0, 2.0),
              openai_ads=ok("openai_ads", 1.0, 2.0))
    elif scenario == "mixed":
        patch(monkeypatch, google_ads=ok("google_ads", 1.0, 2.0), meta=ok("meta", 1.0, 2.0, currency="USD"))
    elif scenario == "error":
        patch(monkeypatch, meta={"platform": "meta", "error": "rate_limited", "retry_after_seconds": 60})
    else:
        patch(monkeypatch)
    out = ov.ads_overview(start_date="2026-13-01", end_date=END) if scenario == "bad_dates" else run()
    assert out["warning"] == WARNING
    assert WARNING in json.dumps(out, ensure_ascii=False)


def test_invalid_dates_call_no_platform(monkeypatch):
    calls = patch(monkeypatch)
    out = ov.ads_overview(start_date=END, end_date=START)
    assert out["error"] == "invalid_dates" and calls == []


def test_output_is_json_and_has_guard_fields(monkeypatch):
    patch(monkeypatch, google_ads=ok("google_ads", 1.0, 2.0))
    out = run()
    json.dumps(out)
    assert out["fetched_at"] and out["freshness"]


# ------------------------------------------------------------------ structure
def test_no_direct_http_in_module():
    tree = ast.parse(inspect.getsource(ov))
    names = {alias.name for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
             for alias in node.names}
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert "requests" not in names and "requests" not in modules
    assert "AdsClient" not in names and "CLIENT" not in inspect.getsource(ov)


def test_register_declares_read_only_documented_tool():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test-overview")
    assert ov.register(mcp) == ["ads_overview"]
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert set(tools) == {"ads_overview"}
    tool = tools["ads_overview"]
    assert tool.title and len(tool.description) > 60
    assert tool.annotations.readOnlyHint is True and tool.annotations.destructiveHint is False
    props = tool.inputSchema["properties"]
    assert set(props) == {"start_date", "end_date", "client_id"}
    assert set(tool.inputSchema.get("required", [])) == {"start_date", "end_date"}
    assert all(p.get("description") for p in props.values())


# ------------------------------------------------------------------ platforms without activity
def idle(platform, currency=None, **extra):
    out = {"platform": platform, "spend": 0.0, "impressions": 0, "clicks": 0, "conversions": 0.0,
           "conversion_value": 0.0, "roas": None, "currency": currency, "attribution": None,
           "date_range": {"start_date": START, "end_date": END}, "totals_complete": True, "no_activity": True}
    out.update(extra)
    return out


def test_platform_without_activity_needs_no_currency(monkeypatch):
    patch(monkeypatch, google_ads=ok("google_ads", 100.0, 300.0), meta=idle("meta"),
          openai_ads=ok("openai_ads", 100.0, 100.0))
    out = run()
    c = out["combined"]
    assert c is not None and c["currency"] == "EUR"
    assert c["platforms_included"] == ["google_ads", "meta", "openai_ads"]
    assert c["platforms_without_activity"] == ["meta"] and out["platforms_without_activity"] == ["meta"]
    assert c["spend"] == 200.0 and c["roas"] == 2.0 and c["impressions"] == 2000
    assert out["platforms"]["meta"]["no_activity"] is True


def test_idle_platform_with_other_currency_does_not_block(monkeypatch):
    patch(monkeypatch, google_ads=ok("google_ads", 100.0, 300.0), meta=idle("meta", currency="USD"))
    c = run()["combined"]
    assert c["currency"] == "EUR" and c["spend"] == 100.0


def test_all_platforms_without_activity(monkeypatch):
    patch(monkeypatch, google_ads=idle("google_ads", no_activity=False), meta=idle("meta"),
          openai_ads=idle("openai_ads"))
    out = run()
    assert out["combined"] is None and out["combined_reason"] == "no activity on any platform"
    assert out["platforms_without_activity"] == ["google_ads", "meta", "openai_ads"]
    assert out["warning"] == WARNING


def test_idle_platforms_with_a_known_currency_give_zero_combined(monkeypatch):
    patch(monkeypatch, google_ads=idle("google_ads", currency="EUR"), meta=idle("meta"))
    c = run()["combined"]
    assert c["currency"] == "EUR" and c["spend"] == 0.0 and c["roas"] is None


def test_zero_spend_with_conversion_value_still_needs_a_currency(monkeypatch):
    patch(monkeypatch, google_ads=ok("google_ads", 100.0, 300.0),
          meta=idle("meta", conversion_value=50.0, conversions=2.0))
    out = run()
    assert out["combined"] is None and "currency unknown for meta" in out["combined_reason"]
    assert out["platforms_without_activity"] == []


def test_extra_fields_kept_in_blocks(monkeypatch):
    info_error = {"error": "network", "detail": "timeout"}
    patch(monkeypatch, google_ads=ok("google_ads", 10.0, 20.0, account_info_error=info_error),
          meta=idle("meta"), openai_ads=idle("openai_ads", currency_note="currency not returned; never guessed"))
    blocks = run()["platforms"]
    assert blocks["google_ads"]["account_info_error"] == info_error
    assert blocks["meta"]["no_activity"] is True and blocks["openai_ads"]["no_activity"] is True
    assert blocks["openai_ads"]["currency_note"] == "currency not returned; never guessed"
    assert "currency_error" not in ov._EXTRA_FIELDS


def test_attribution_mix_of_string_and_dict_is_normalised(monkeypatch):
    patch(monkeypatch, google_ads=ok("google_ads", 10.0, 20.0),           # string
          meta=ok("meta", 10.0, 20.0),                                    # dict
          openai_ads=idle("openai_ads"))                                  # None -> documented default
    out = run()
    side = out["attribution_side_by_side"]
    assert side["google_ads"] == {"basis": "réglage par action de conversion"}
    assert side["meta"]["windows"] == ["7d_click", "1d_view"]
    assert side["openai_ads"]["click_window_days"] == 30
    assert out["platforms"]["openai_ads"]["attribution_source"] == "documented default"
    assert all(isinstance(b["attribution"], dict) for b in out["platforms"].values())


def test_google_error_block_has_dict_attribution(monkeypatch):
    patch(monkeypatch, google_ads={"platform": "google_ads", "error": "auth_failed"})
    side = run()["attribution_side_by_side"]
    assert isinstance(side["google_ads"], dict) and side["google_ads"]["basis"]


# ------------------------------------------------------------------ the three REAL summaries, fake sessions
class _FakeTokens:
    def headers(self):
        return {"Authorization": "Bearer ya29." + "a" * 40, "Content-Type": "application/json"}


GOOGLE_ACTIVE = [{"results": [{
    "customer": {"resourceName": "customers/1234567890", "currencyCode": "EUR", "timeZone": "Europe/Paris"},
    "metrics": {"costMicros": "12500000", "impressions": "1000", "clicks": "50", "conversions": 5.0,
                "conversionsValue": 250.0}}], "fieldMask": "metrics.costMicros", "requestId": "r1"}]


@pytest.fixture
def real_accounts(monkeypatch):
    """Configure the three real connectors on fake sessions; returns a function installing the Google answer."""
    monkeypatch.setenv("GADS_CUSTOMER_ID", "1234567890")
    monkeypatch.setenv("META_ACCESS_TOKEN", "EAAT" + "z" * 44)
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "act_1234567890")
    monkeypatch.setenv("OPENAI_ADS_API_KEY", "sk-" + "test" + "A" * 30)
    monkeypatch.setattr(google_ads, "TOKENS", _FakeTokens())
    sessions = {
        "meta": FakeSession([("/insights", FakeResponse(200, {"data": []})),
                             ("act_1234567890", FakeResponse(200, {"id": "act_1234567890"}))]),
        "openai_ads": FakeSession([("/insights", FakeResponse(200, {"data": []})),
                                   ("/ad_account", FakeResponse(200, {}))]),
    }
    for name in ("meta", "openai_ads"):
        monkeypatch.setattr(MODULES[name].CLIENT, "session", sessions[name])
    for module in MODULES.values():
        monkeypatch.setattr(module.CLIENT, "BLOCKED_ATTEMPTS", [])
        module.CLIENT.cache.clear()

    def google(stream):
        sessions["google_ads"] = FakeSession([("googleAds:searchStream", FakeResponse(200, stream))])
        monkeypatch.setattr(google_ads.CLIENT, "session", sessions["google_ads"])
        return sessions

    yield google
    for module in MODULES.values():
        module.CLIENT.cache.clear()


def test_real_summaries_on_empty_accounts(real_accounts):
    sessions = real_accounts([])
    out = run()
    for name, block in out["platforms"].items():
        assert block["status"] == "ok", (name, block)
        assert block["spend"] == 0 and isinstance(block["attribution"], dict)
        assert MODULES[name].CLIENT.BLOCKED_ATTEMPTS == []
        assert sessions[name].calls, name  # answered by the fake session, not the network
    assert out["platforms_without_activity"] == ["google_ads", "meta", "openai_ads"]
    assert out["combined"] is None and out["combined_reason"] == "no activity on any platform"
    assert out["platforms"]["meta"]["no_activity"] is True
    assert out["platforms"]["openai_ads"]["no_activity"] is True
    assert "currency_note" in out["platforms"]["openai_ads"]
    assert out["warning"] == WARNING


def test_real_summaries_one_active_two_empty(real_accounts):
    real_accounts(GOOGLE_ACTIVE)
    out = run()
    c = out["combined"]
    assert c is not None, out.get("combined_reason")
    assert c["currency"] == "EUR" and c["spend"] == 12.5 and c["roas"] == 20.0
    assert c["platforms_included"] == ["google_ads", "meta", "openai_ads"]
    assert c["platforms_without_activity"] == ["meta", "openai_ads"]
    assert out["partial"] is False and out["warning"] == WARNING


# ------------------------------------------------------------------ robustness (gate) and surviving mutants

_JUNK = [None, 0, 0.0, -1, 1.5, 10**20, float("inf"), True, False, "", " ", "EUR", "USD", "abc", "0",
         [], ["EUR"], [1, 2], {}, {"currency": "EUR"}, {"a": [None]}]
_KEYS = ["platform", "spend", "impressions", "clicks", "conversions", "conversion_value", "roas", "currency",
         "attribution", "date_range", "totals_complete", "no_activity", "currency_note", "account_info_error",
         "error", "units"]


def _junk_summary(rng):
    """A deterministic badly typed summary answer: a non-dict, or a dict of random keys and values."""
    if rng.random() < 0.1:
        return rng.choice([None, [], ["x"], "text", 42, 1.5, True])
    return {k: rng.choice(_JUNK) for k in _KEYS if rng.random() < 0.8}


def test_200_badly_typed_answers_never_crash_and_keep_the_warning(monkeypatch):
    rng = random.Random(20261016)
    for _ in range(200):
        patch(monkeypatch, **{name: _junk_summary(rng) for name in MODULES})
        out = run()
        assert isinstance(out, dict) and out["warning"] == WARNING
        assert "platforms" in out, out  # handled block by block, not by the last-resort internal_error
        json.dumps(out, default=str)
        for block in out["platforms"].values():
            currency = block.get("currency")
            assert currency is None or (isinstance(currency, str) and currency.strip())
            assert isinstance(block["attribution"], dict)
        if out.get("combined"):
            assert isinstance(out["combined"]["currency"], str)


@pytest.mark.parametrize("currency", [123, 1.5, ["EUR"], {"code": "EUR"}, "", "  ", True])
def test_non_string_currency_is_ignored_with_a_note(monkeypatch, currency):
    patch(monkeypatch, google_ads=ok("google_ads", 10.0, 20.0, currency=currency), meta=ok("meta", 10.0, 20.0))
    out = run()
    block = out["platforms"]["google_ads"]
    assert block["currency"] is None and "currency ignored" in block["currency_note"]
    assert out["combined"] is None and "currency unknown for google_ads" in out["combined_reason"]


def test_unexpected_crash_returns_internal_error_with_warning(monkeypatch):
    secret = "sk-" + "b" * 30
    patch(monkeypatch, google_ads=ok("google_ads", 10.0, 20.0))

    def boom(blocks):
        raise TypeError(f"bad {secret}")

    monkeypatch.setattr(ov, "_combined", boom)
    out = run()
    assert out["error"] == "internal_error" and out["warning"] == WARNING
    assert secret not in json.dumps(out)


@pytest.mark.parametrize("answer", [None, [], ["x"], "text"])
def test_non_dict_summary_becomes_an_internal_error_block(monkeypatch, answer):
    patch(monkeypatch, google_ads=answer, meta=ok("meta", 10.0, 20.0))
    out = run()
    block = out["platforms"]["google_ads"]
    assert block["status"] == "error" and block["error"] == "internal_error"
    assert block["detail"].startswith("summary returned")
    assert out["combined"]["spend"] == 10.0


def test_zero_spend_with_impressions_stays_active_and_needs_a_currency(monkeypatch):
    zero_spend = ok("google_ads", 0.0, 0.0, currency=None, conversions=0.0)  # impressions 1000, no flag
    patch(monkeypatch, google_ads=zero_spend, meta=ok("meta", 10.0, 20.0))
    out = run()
    assert out["platforms_without_activity"] == []
    assert out["combined"] is None and "currency unknown for google_ads" in out["combined_reason"]


def test_no_activity_flag_alone_with_missing_metrics_is_idle(monkeypatch):
    flagged = idle("openai_ads", impressions=None, clicks=None)
    patch(monkeypatch, google_ads=ok("google_ads", 10.0, 20.0), openai_ads=flagged)
    out = run()
    assert out["platforms_without_activity"] == ["openai_ads"]
    c = out["combined"]
    assert c is not None and c["spend"] == 10.0 and c["impressions"] == 1000 and c["clicks"] == 50


def test_idle_none_counts_as_zero_but_active_none_blocks_the_sum(monkeypatch):
    patch(monkeypatch, google_ads=ok("google_ads", 10.0, 20.0), meta=idle("meta", conversion_value=None))
    c = run()["combined"]
    assert c["conversion_value_sum_not_deduplicated"] == 20.0 and c["roas"] == 2.0
    patch(monkeypatch, google_ads=ok("google_ads", 10.0, 20.0, impressions=None), meta=idle("meta"))
    c = run()["combined"]
    assert c["impressions"] is None and c["spend"] == 10.0


def test_default_attribution_is_never_aliased(monkeypatch):
    patch(monkeypatch)
    out = run()
    out["platforms"]["meta"]["attribution"]["windows"].append("28d_click")
    out["attribution_side_by_side"]["google_ads"]["basis"] = "changed"
    assert ov.DEFAULT_ATTRIBUTION["meta"]["windows"] == ["7d_click", "1d_view"]
    assert ov.DEFAULT_ATTRIBUTION["google_ads"]["basis"] == "réglage par action de conversion"

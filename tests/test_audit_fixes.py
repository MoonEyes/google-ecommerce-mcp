"""Regression tests of the defects found by the end-to-end audit (tests/test_e2e_*.py):

- a network failure while REFRESHING a token (google.auth TransportError, requests SSLError) escaped the Google
  tools as a raw exception, and became auth_failed on Google Ads: it is now the structured "network" error, with a
  readable fix and neither the raw message nor a secret; invalid_grant keeps its authentication code;
- an absolute change that rounds to zero was shown as "−0,0 pt" / "+0": it is now "stable", without a sign;
- Lighthouse lab values ("3.1 s", written in English by the API) are formatted in the report's language.
"""

from __future__ import annotations

import importlib
import json

import pytest
import requests
from google.auth.exceptions import RefreshError, TransportError

from ads_fakes import GOOGLE_REFRESH_PREFIX, no_keyring, no_network  # noqa: F401  (fixtures)
from google_ecommerce_mcp import ads_auth, auth, doctor, google_ads
from google_ecommerce_mcp.report.contract import sanitize
from google_ecommerce_mcp.report.i18n import NNBSP
from google_ecommerce_mcp.report.render import build_context
from google_ecommerce_mcp.report.sections.cwv import lab_text

SECRET = GOOGLE_REFRESH_PREFIX + "audit-refresh-token-must-not-leak"
FAILURES = {
    "transport": lambda: TransportError(f"HTTPSConnectionPool: Max retries exceeded (refresh_token={SECRET})"),
    "ssl": lambda: requests.exceptions.SSLError(f"[SSL: CERTIFICATE_VERIFY_FAILED] while sending {SECRET}"),
}


class FailingCreds:
    """Expired credentials whose refresh raises `exc`."""

    token_uri = "https://oauth2.googleapis.com/token"
    valid = False
    token = None

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    def refresh(self, request) -> None:
        self.calls += 1
        raise self.exc


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("GA4_PROPERTY_ID", "424242")
    monkeypatch.setenv("GOOGLE_TOKEN_FILE", str(tmp_path / "token.json"))
    import google_ecommerce_mcp.server as module

    module = importlib.reload(module)
    sent = []
    monkeypatch.setattr(module, "HTTP", type("NoHTTP", (), {"request": lambda self, *a, **k: sent.append(a)})())
    yield module, sent
    monkeypatch.undo()
    importlib.reload(module)


def _assert_network(result: dict) -> None:
    assert result["error"] == "network", result
    assert result["fix"] and "connection" in result["fix"]
    text = json.dumps(result)
    assert SECRET not in text and "Max retries" not in text and "CERTIFICATE_VERIFY_FAILED" not in text


# ---------------------------------------------------------------------------------------- token refresh
@pytest.mark.parametrize("failure", sorted(FAILURES))
def test_google_tools_turn_a_refresh_network_failure_into_a_network_error(server, failure):
    module, sent = server
    creds = FailingCreds(FAILURES[failure]())
    module.TOKENS._creds = creds
    result = module.ga4_report()
    _assert_network(result)
    assert "fetched_at" in result and sent == []  # stopped before any request
    status = module.server_status()
    assert status["token"].startswith("network error: The Google token could not be refreshed")
    assert SECRET not in json.dumps(status)
    assert module.TOKENS._creds is creds and creds.calls == 2  # kept: the next call retries the refresh


def test_invalid_grant_stays_an_authentication_error(server):
    module, _ = server
    module.TOKENS._creds = FailingCreds(RefreshError("invalid_grant: Token has been expired or revoked."))
    result = module.ga4_report()
    assert result["error"] == "not_authenticated" and "expired or was revoked" in result["detail"]


def test_token_network_error_is_still_a_not_authenticated_for_older_callers():
    error = auth.TokenNetworkError("x", kind="TransportError")
    assert isinstance(error, auth.NotAuthenticated) and error.local is False


@pytest.mark.parametrize("failure", sorted(FAILURES))
def test_google_ads_refresh_network_failure_is_a_network_error(monkeypatch, failure):
    monkeypatch.delenv("GADS_TOKEN_FILE", raising=False)
    monkeypatch.delenv("ADS_TRUST_ENV", raising=False)
    provider = ads_auth.AdsTokenProvider()
    provider._creds, provider._loaded_from = FailingCreds(FAILURES[failure]()), None
    monkeypatch.setattr(google_ads, "TOKENS", provider)
    google_ads.CLIENT.cache.clear()
    result = google_ads.gads_accounts()
    _assert_network(result)
    assert result["platform"] == "google_ads"


def test_google_ads_local_tls_failure_keeps_its_local_fix(monkeypatch):
    monkeypatch.delenv("GADS_TOKEN_FILE", raising=False)
    provider = ads_auth.AdsTokenProvider()
    provider._creds, provider._loaded_from = FailingCreds(FileNotFoundError("missing CA bundle")), None
    monkeypatch.setattr(google_ads, "TOKENS", provider)
    google_ads.CLIENT.cache.clear()
    result = google_ads.gads_accounts()
    assert result["error"] == "network" and "REQUESTS_CA_BUNDLE" in result["fix"]


def test_google_ads_invalid_grant_stays_auth_failed(monkeypatch):
    monkeypatch.delenv("GADS_TOKEN_FILE", raising=False)
    provider = ads_auth.AdsTokenProvider()
    provider._creds, provider._loaded_from = FailingCreds(RefreshError("invalid_grant")), None
    monkeypatch.setattr(google_ads, "TOKENS", provider)
    google_ads.CLIENT.cache.clear()
    assert google_ads.gads_accounts()["error"] == "auth_failed"


@pytest.mark.parametrize("service,expected", [("ga4", "Google could not be reached"),
                                              ("gads", "could not be reached")])
def test_doctor_translates_the_network_code(server, service, expected):
    module, _ = server
    module.TOKENS._creds = FailingCreds(FAILURES["transport"]())
    if service == "ga4":
        result = doctor._live_call("ga4")  # the real tool doctor --live calls, with the failing refresh
        _assert_network(result)
    else:
        result = {"error": "network"}
    problem, command = doctor.advice(service, result, {})
    assert expected in problem and command


# ---------------------------------------------------------------------------------------- report display
@pytest.fixture
def ctx_fr():
    return build_context(sanitize({"meta": {"lang": "fr", "currency": "EUR"}}))


@pytest.mark.parametrize("diff,metric,expected", [
    (-0.00028, "conversion_rate", "stable"),      # was "−0,0 pt"
    (0.0004, "conversion_rate", "stable"),        # was "+0,0 pt"
    (-0.4, "orders", "stable"),                   # was "−0"
    (-0.004, "revenue", "stable"),                # was "−0 €" style
    (0.0012, "conversion_rate", "+0,1 pt"),
    (-3, "orders", "−3"),
])
def test_absolute_change_is_rounded_before_its_sign(ctx_fr, diff, metric, expected):
    assert ctx_fr._abs_text(diff, metric, "EUR") == expected


def test_kpi_pastille_never_shows_minus_zero(ctx_fr):
    html = ctx_fr.delta(0.02067, 0.02095, "conversion_rate", n=(9435, 7548), small=True)
    assert "−0,0" not in html and "stable" in html


@pytest.mark.parametrize("raw,lang,expected", [
    ("3.1 s", "fr", "3,1 s"),
    ("3.1 s", "fr", "3,1 s"),
    ("1,230 ms", "fr", f"1{NNBSP}230 ms"),
    ("0.08", "fr", "0,08"),
    ("240 ms", "fr", "240 ms"),
    ("3.1 s", "en", "3.1 s"),
    ("1,230 ms", "en", "1,230 ms"),
    ("n/a", "fr", "n/a"),
])
def test_lighthouse_lab_values_follow_the_report_language(raw, lang, expected):
    assert lab_text(raw, lang) == expected

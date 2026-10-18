"""Offline tests of the ads configuration and the separate Google Ads token. The OS keyring is replaced by an
in-memory one and the browser consent by a fake flow: nothing leaves the process."""

import json
import sys
import types

import pytest

from ads_fakes import (GOOGLE_ACCESS_PREFIX, GOOGLE_REFRESH_PREFIX, META_TOKEN_PREFIX, OPENAI_KEY_PREFIX,
                       no_keyring, no_network)  # noqa: F401  (fixtures)
from google_ecommerce_mcp import ads_auth, ads_config
from google_ecommerce_mcp.ads_config import ADS_SCOPE, KEYRING_USER_ADS, AdsSettings
from google_ecommerce_mcp.auth import NotAuthenticated
from google_ecommerce_mcp.config import KEYRING_SERVICE, KEYRING_USER, scopes_for

ENV = ["GADS_CUSTOMER_ID", "GADS_LOGIN_CUSTOMER_ID", "GADS_DEVELOPER_TOKEN", "GADS_TOKEN_FILE", "GADS_API_VERSION",
       "META_ACCESS_TOKEN", "META_AD_ACCOUNT_ID", "META_API_VERSION", "OPENAI_ADS_API_KEY", "GOOGLE_TOKEN_FILE"]
META_TOKEN = META_TOKEN_PREFIX + "B" + "m" * 40
OPENAI_KEY = OPENAI_KEY_PREFIX + "proj-" + "k" * 30
DEV_TOKEN = "devtoken-" + "d" * 20


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, no_keyring):
    for name in ENV + ["ADS_TRUST_ENV"]:
        monkeypatch.delenv(name, raising=False)
    # Never run the real icacls from tests: the Windows ACL step is checked with a recorder below.
    monkeypatch.setattr(ads_auth.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0))
    return no_keyring


# ------------------------------------------------------------------ scopes and storage are kept apart
@pytest.mark.parametrize("merchant", [False, True])
@pytest.mark.parametrize("indexing", [False, True])
def test_default_google_scopes_never_include_adwords(merchant, indexing):
    assert not any("adwords" in s for s in scopes_for(merchant=merchant, indexing=indexing))


def test_ads_scope_and_keyring_entry_are_separate():
    assert ADS_SCOPE == "https://www.googleapis.com/auth/adwords"
    assert ads_config.ADS_WRITE_CAPABLE == {ADS_SCOPE}
    assert KEYRING_USER_ADS == "oauth-token-ads" and KEYRING_USER_ADS != KEYRING_USER


class FakeCreds:
    def __init__(self, token=GOOGLE_ACCESS_PREFIX + "fake-access", valid=True, scopes=(ADS_SCOPE,)):
        self.token = token
        self.valid = valid
        self.scopes = list(scopes)
        self.granted_scopes = list(scopes)

    def to_json(self):
        return json.dumps({"token": self.token, "refresh_token": GOOGLE_REFRESH_PREFIX + "fake", "client_id": "id",
                           "client_secret": "s", "scopes": self.scopes})


GOOD_CLIENT_FILE = {"installed": {"client_id": "123-abc.apps.googleusercontent.com", "project_id": "p",
                                  "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                                  "token_uri": "https://oauth2.googleapis.com/token",
                                  "client_secret": "GOCSPX-" + "z" * 20, "redirect_uris": ["http://localhost"]}}


@pytest.fixture
def fake_flow(monkeypatch, tmp_path):
    """Replace google_auth_oauthlib.flow.InstalledAppFlow: records the scopes asked, never opens a browser.
    A genuine Desktop app client file "client_secret.json" is written in the (temporary) current folder."""
    asked = {}
    (tmp_path / "client_secret.json").write_text(json.dumps(GOOD_CLIENT_FILE), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    class Flow:
        @classmethod
        def from_client_config(cls, config, scopes):
            asked["config"], asked["scopes"] = config, list(scopes)
            return cls()

        def run_local_server(self, **kwargs):
            asked["kwargs"] = kwargs
            return FakeCreds()

    module = types.ModuleType("google_auth_oauthlib.flow")
    module.InstalledAppFlow = Flow
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib.flow", module)
    return asked


def test_setup_ads_asks_only_adwords_and_keeps_the_google_token(fake_flow, clean_env):
    clean_env[(KEYRING_SERVICE, KEYRING_USER)] = "existing google token"
    where = ads_auth.run_setup_ads("client_secret.json", None)
    assert fake_flow["scopes"] == [ADS_SCOPE]
    assert clean_env[(KEYRING_SERVICE, KEYRING_USER)] == "existing google token"
    assert json.loads(clean_env[(KEYRING_SERVICE, KEYRING_USER_ADS)])["scopes"] == [ADS_SCOPE]
    assert set(clean_env) == {(KEYRING_SERVICE, KEYRING_USER), (KEYRING_SERVICE, KEYRING_USER_ADS)}
    assert "keyring" in where


def test_setup_ads_to_file_and_refuses_the_google_token_file(fake_flow, clean_env, tmp_path, monkeypatch):
    google_file = tmp_path / "google.json"
    google_file.write_text("google token", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_TOKEN_FILE", str(google_file))
    with pytest.raises(ValueError):
        ads_auth.run_setup_ads("client_secret.json", str(google_file))
    assert google_file.read_text(encoding="utf-8") == "google token" and "scopes" not in fake_flow
    ads_file = tmp_path / "ads" / "ads.json"
    assert "file" in ads_auth.run_setup_ads("client_secret.json", str(ads_file))
    assert json.loads(ads_file.read_text(encoding="utf-8"))["scopes"] == [ADS_SCOPE]
    assert google_file.read_text(encoding="utf-8") == "google token" and clean_env == {}


# ------------------------------------------------------------------ token provider
def stored_token(scopes=(ADS_SCOPE,)):
    # Far-future expiry: the token is valid, so no refresh (no network) happens.
    return json.dumps({"token": GOOGLE_ACCESS_PREFIX + "stored-ads", "refresh_token": GOOGLE_REFRESH_PREFIX + "refresh-ads", "client_id": "cid",
                       "client_secret": "csecret", "scopes": list(scopes), "expiry": "2999-01-01T00:00:00Z"})


def test_provider_reads_the_ads_entry_not_the_google_one(clean_env):
    clean_env[(KEYRING_SERVICE, KEYRING_USER)] = stored_token(["https://www.googleapis.com/auth/analytics.readonly"])
    provider = ads_auth.AdsTokenProvider()
    with pytest.raises(NotAuthenticated, match="setup-ads"):
        provider.headers()
    assert provider.granted_scopes() == []
    clean_env[(KEYRING_SERVICE, KEYRING_USER_ADS)] = stored_token()
    assert provider.headers()["Authorization"] == "Bearer " + GOOGLE_ACCESS_PREFIX + "stored-ads"
    assert provider.granted_scopes() == [ADS_SCOPE]


def test_provider_follows_gads_token_file(tmp_path, monkeypatch, clean_env):
    token_file = tmp_path / "ads.json"
    token_file.write_text(stored_token(), encoding="utf-8")
    provider = ads_auth.AdsTokenProvider()
    with pytest.raises(NotAuthenticated):
        provider.headers()
    monkeypatch.setenv("GADS_TOKEN_FILE", str(token_file))
    assert provider.headers()["Authorization"] == "Bearer " + GOOGLE_ACCESS_PREFIX + "stored-ads"
    assert ads_auth.AdsTokenProvider(str(token_file)).headers()["Content-Type"] == "application/json"


def test_expired_ads_token_is_not_authenticated(clean_env):
    from google.auth.exceptions import RefreshError

    class Expired:
        valid = False
        token_uri = "https://oauth2.googleapis.com/token"

        def refresh(self, request):
            raise RefreshError("invalid_grant: Token has been expired or revoked.")

    provider = ads_auth.AdsTokenProvider()
    provider._creds, provider._loaded_from = Expired(), None
    with pytest.raises(NotAuthenticated, match="setup-ads") as info:
        provider.headers()
    assert "7 days" in str(info.value) and provider._creds is None


# ------------------------------------------------------------------ settings
def test_settings_are_read_at_each_use(monkeypatch):
    assert AdsSettings.from_env().gads_customer_id is None
    monkeypatch.setenv("GADS_CUSTOMER_ID", "123-456-7890")
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "act_42")
    settings = AdsSettings.from_env()
    assert settings.gads_customer_id == "1234567890" and settings.meta_ad_account_id == "42"
    assert settings.gads_api_version == "v26" and settings.meta_api_version == "v26.0"
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "43")
    monkeypatch.setenv("GADS_API_VERSION", "v27")
    assert AdsSettings.from_env().meta_ad_account_id == "43" and AdsSettings.from_env().gads_api_version == "v27"


def test_repr_and_status_never_show_secrets(monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", META_TOKEN)
    monkeypatch.setenv("OPENAI_ADS_API_KEY", OPENAI_KEY)
    monkeypatch.setenv("GADS_DEVELOPER_TOKEN", DEV_TOKEN)
    monkeypatch.setenv("GADS_CUSTOMER_ID", "1234567890")
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "act_42")
    settings = AdsSettings.from_env()
    for secret in (META_TOKEN, OPENAI_KEY, DEV_TOKEN):
        assert secret not in repr(settings) and secret not in str(settings)
    status = settings.status()
    assert all(isinstance(v, bool) for v in status.values())
    assert status["meta"] and status["openai_ads"] and status["google_ads"] and status["google_ads_developer_token"]
    assert sorted(settings.secrets()) == sorted([META_TOKEN, OPENAI_KEY, DEV_TOKEN])
    assert status["google_ads_login_customer_id"] is False


def test_developer_token_is_optional():
    settings = AdsSettings.from_env()
    assert settings.gads_developer_token is None and settings.status()["google_ads_developer_token"] is False


# ------------------------------------------------------------------ other secrets
def test_get_secret_env_first_then_keyring(monkeypatch, clean_env):
    assert ads_auth.get_secret("meta-access-token") is None
    clean_env[(KEYRING_SERVICE, "meta-access-token")] = "from-keyring-token"
    assert ads_auth.get_secret("meta-access-token") == "from-keyring-token"
    assert AdsSettings.from_env().meta_access_token == "from-keyring-token"
    assert AdsSettings.from_env(keyring_fallback=False).meta_access_token is None
    monkeypatch.setenv("META_ACCESS_TOKEN", META_TOKEN)
    assert ads_auth.get_secret("meta-access-token") == META_TOKEN
    with pytest.raises(ValueError):
        ads_auth.get_secret("oauth-token")


def test_get_secret_survives_a_broken_keyring(monkeypatch):
    import keyring

    def broken(service, user):
        raise RuntimeError("no backend")

    monkeypatch.setattr(keyring, "get_password", broken)
    assert ads_auth.get_secret("openai-ads-key") is None


def test_set_secret_stores_only_known_names(clean_env):
    where = ads_auth.set_secret("openai-ads-key", f"  {OPENAI_KEY}  ")
    assert clean_env[(KEYRING_SERVICE, "openai-ads-key")] == OPENAI_KEY and OPENAI_KEY not in where
    for name in ("oauth-token", "oauth-token-ads", "anything"):
        with pytest.raises(ValueError):
            ads_auth.set_secret(name, "x" * 20)
    with pytest.raises(ValueError):
        ads_auth.set_secret("meta-access-token", "   ")


# ------------------------------------------------------------------ hardening (P3/P4)
def test_refresh_is_refused_for_an_unexpected_token_endpoint(clean_env):
    class Elsewhere:
        valid = False
        token_uri = "https://evil.example.com/token"

        def refresh(self, request):  # pragma: no cover - must never be reached
            raise AssertionError("refresh token sent to another endpoint")

    provider = ads_auth.AdsTokenProvider()
    provider._creds, provider._loaded_from = Elsewhere(), None
    with pytest.raises(NotAuthenticated, match="unexpected token endpoint"):
        provider.headers()
    assert provider._creds is None


def test_unreadable_token_is_not_authenticated_without_echoing_it(tmp_path, clean_env, monkeypatch):
    broken = tmp_path / "ads.json"
    broken.write_text('{"refresh_token": "' + GOOGLE_REFRESH_PREFIX + 'leakme-0123456789", ', encoding="utf-8")
    provider = ads_auth.AdsTokenProvider(str(broken))
    with pytest.raises(NotAuthenticated) as info:
        provider.headers()
    assert "leakme" not in str(info.value) and "setup-ads" in str(info.value)
    assert provider.granted_scopes() == []

    import keyring

    def no_backend(service, user):
        raise RuntimeError("keyring backend secret-detail")

    monkeypatch.setattr(keyring, "get_password", no_backend)
    with pytest.raises(NotAuthenticated) as info:
        ads_auth.AdsTokenProvider().headers()
    assert "secret-detail" not in str(info.value) and "RuntimeError" in str(info.value)


def test_save_refuses_to_overwrite_a_non_ads_token_file(tmp_path):
    readonly = tmp_path / "google.json"
    original = json.dumps({"refresh_token": GOOGLE_REFRESH_PREFIX + "x", "scopes": ["https://www.googleapis.com/auth/analytics.readonly"]})
    readonly.write_text(original, encoding="utf-8")
    with pytest.raises(ValueError, match="not a Google Ads token"):
        ads_auth.save_ads_token(FakeCreds().to_json(), str(readonly))
    assert readonly.read_text(encoding="utf-8") == original
    junk = tmp_path / "notes.txt"
    junk.write_text("my notes", encoding="utf-8")
    with pytest.raises(ValueError):
        ads_auth.save_ads_token(FakeCreds().to_json(), str(junk))
    assert junk.read_text(encoding="utf-8") == "my notes"


def test_save_overwrites_an_ads_token_file_privately(tmp_path):
    import os
    import stat

    target = tmp_path / "ads.json"
    ads_auth.save_ads_token(FakeCreds(token=GOOGLE_ACCESS_PREFIX + "first").to_json(), str(target))
    ads_auth.save_ads_token(FakeCreds(token=GOOGLE_ACCESS_PREFIX + "second").to_json(), str(target))
    assert json.loads(target.read_text(encoding="utf-8"))["token"] == GOOGLE_ACCESS_PREFIX + "second"
    if os.name != "nt":  # POSIX permissions; Windows ignores the mode bits beyond read-only
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o600


def test_set_secret_with_empty_value_exits_1(monkeypatch, clean_env):
    import getpass
    import runpy

    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "   ")
    monkeypatch.setattr(sys, "argv", ["google-ecommerce-mcp", "set-secret", "meta-access-token"])
    monkeypatch.delitem(sys.modules, "google_ecommerce_mcp.__main__", raising=False)
    with pytest.raises(SystemExit) as info:
        runpy.run_module("google_ecommerce_mcp", run_name="__main__")
    assert info.value.code == 1 and clean_env == {}


# ------------------------------------------------------------------ second hardening wave
class Refreshable:
    valid = False
    token_uri = "https://oauth2.googleapis.com/token"
    token = None

    def __init__(self):
        self.requests = []

    def refresh(self, request):
        self.requests.append(request)
        self.token, self.valid = GOOGLE_ACCESS_PREFIX + "refreshed", True


def test_refresh_uses_a_session_without_environment_nor_redirects(clean_env, monkeypatch):
    from google_ecommerce_mcp.ads_common import NoRedirectSession

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
    creds = Refreshable()
    provider = ads_auth.AdsTokenProvider()
    provider._creds, provider._loaded_from = creds, None
    assert provider.headers()["Authorization"] == "Bearer " + GOOGLE_ACCESS_PREFIX + "refreshed"
    session = creds.requests[0].session
    assert isinstance(session, NoRedirectSession) and session.trust_env is False and session.max_redirects == 0
    assert not session.proxies


def test_refresh_honours_the_proxy_opt_in(clean_env, monkeypatch):
    monkeypatch.setenv("ADS_TRUST_ENV", "1")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    creds = Refreshable()
    provider = ads_auth.AdsTokenProvider()
    provider._creds, provider._loaded_from = creds, None
    provider.headers()
    session = creds.requests[0].session
    assert session.trust_env is False and session.proxies.get("https") == "http://proxy.corp:3128"


def test_refresh_network_failure_is_not_authenticated(clean_env):
    from google.auth.exceptions import TransportError

    class Offline(Refreshable):
        def refresh(self, request):
            raise TransportError("connection refused to " + GOOGLE_REFRESH_PREFIX + "secret-refresh")

    provider = ads_auth.AdsTokenProvider()
    provider._creds, provider._loaded_from = Offline(), None
    with pytest.raises(NotAuthenticated, match="ADS_TRUST_ENV=1") as info:
        provider.headers()
    assert "secret-refresh" not in str(info.value)


def test_save_ads_token_refuses_google_token_file_directly(tmp_path, monkeypatch):
    google_file = tmp_path / "google.json"
    monkeypatch.setenv("GOOGLE_TOKEN_FILE", str(google_file))
    with pytest.raises(ValueError, match="GOOGLE_TOKEN_FILE"):
        ads_auth.save_ads_token(FakeCreds().to_json(), str(google_file))
    assert not google_file.exists()


def test_windows_acl_is_restricted_with_icacls(tmp_path, monkeypatch, clean_env):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(ads_auth, "_IS_WINDOWS", True)
    monkeypatch.setattr(ads_auth.subprocess, "run", run)
    monkeypatch.setenv("USERNAME", "shopowner")
    monkeypatch.setenv("SystemRoot", r"D:\WinRoot")
    target = tmp_path / "ads.json"
    where = ads_auth.save_ads_token(FakeCreds().to_json(), str(target))
    args, kwargs = calls[0]
    assert args == [r"D:\WinRoot\System32\icacls.exe", str(target), "/inheritance:r", "/grant:r",
                    "shopowner:F"]
    assert kwargs["check"] is False and kwargs["timeout"] and "shell" not in kwargs
    assert "warning" not in where


@pytest.mark.parametrize("outcome", ["failed", "raises"])
def test_windows_acl_failure_never_raises_and_warns(tmp_path, monkeypatch, clean_env, fake_flow, outcome):
    def run(args, **kwargs):
        if outcome == "raises":
            raise FileNotFoundError("icacls")
        return types.SimpleNamespace(returncode=5)

    monkeypatch.setattr(ads_auth, "_IS_WINDOWS", True)
    monkeypatch.setattr(ads_auth.subprocess, "run", run)
    target = tmp_path / "ads.json"
    where = ads_auth.run_setup_ads("client_secret.json", str(target))
    assert target.exists() and ads_auth.WINDOWS_ACL_WARNING in where


def test_no_icacls_outside_windows(tmp_path, monkeypatch, clean_env):
    def run(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("icacls called on POSIX")

    monkeypatch.setattr(ads_auth, "_IS_WINDOWS", False)
    monkeypatch.setattr(ads_auth.subprocess, "run", run)
    assert "warning" not in ads_auth.save_ads_token(FakeCreds().to_json(), str(tmp_path / "ads.json"))


# ------------------------------------------------------------------ final wave
def test_icacls_path_is_absolute_under_system_root(monkeypatch):
    monkeypatch.setenv("SystemRoot", r"E:\Win")
    assert ads_auth._icacls_path() == r"E:\Win\System32\icacls.exe"
    monkeypatch.delenv("SystemRoot")
    assert ads_auth._icacls_path() == r"C:\Windows\System32\icacls.exe"


def test_refresh_os_error_is_not_authenticated_without_content(clean_env, tmp_path):
    missing = str(tmp_path / "nowhere" / "corp-ca.pem")

    class LocalFailure(Refreshable):
        def refresh(self, request):
            raise OSError(f"Could not find a suitable TLS CA certificate bundle, invalid path: {missing}")

    provider = ads_auth.AdsTokenProvider()
    provider._creds, provider._loaded_from = LocalFailure(), None
    with pytest.raises(NotAuthenticated, match="REQUESTS_CA_BUNDLE") as info:
        provider.headers()
    assert missing not in str(info.value)

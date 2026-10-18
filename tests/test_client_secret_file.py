"""The OAuth client file is validated BEFORE any consent flow (auth.consent / validate_client_secret).

A hostile file (token_uri on another host, auth_uri outside accounts.google.com, a "web" client, a redirect that is
not a local loopback) is refused with location / cause / fix, without starting InstalledAppFlow nor opening a
browser, and nothing is stored. Offline: the flow module and the browser are replaced by tripwires."""

from __future__ import annotations

import json
import sys
import types
import webbrowser

import pytest

from ads_fakes import no_network  # noqa: F401  (autouse: any real HTTP request fails the test)
from clients_fakes import ACME, setup_env, write_registry
from google_ecommerce_mcp import auth, client_cli, installer
from google_ecommerce_mcp.__main__ import main

GOOD = {"installed": {"client_id": "123-abc.apps.googleusercontent.com", "project_id": "p",
                      "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                      "token_uri": "https://oauth2.googleapis.com/token",
                      "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                      "client_secret": "GOCSPX-" + "z" * 20, "redirect_uris": ["http://localhost"]}}


def variant(**changes):
    data = json.loads(json.dumps(GOOD))
    for key, value in changes.items():
        if value is None:
            data["installed"].pop(key, None)
        else:
            data["installed"][key] = value
    return data


HOSTILE = {
    "token_uri_http_evil": variant(token_uri="http://evil.example/token"),
    "token_uri_https_evil": variant(token_uri="https://evil.example/token"),
    "token_uri_lookalike": variant(token_uri="https://oauth2.googleapis.com.evil.example/token"),
    "auth_uri_external": variant(auth_uri="https://evil.example/o/oauth2/auth"),
    "auth_uri_lookalike": variant(auth_uri="https://accounts.google.com.evil.example/o/oauth2/auth"),
    "auth_uri_userinfo": variant(auth_uri="https://accounts.google.com@evil.example/o/oauth2/auth"),
    "auth_uri_http": variant(auth_uri="http://accounts.google.com/o/oauth2/auth"),
    "redirect_external": variant(redirect_uris=["https://evil.example/cb"]),
    "redirect_mixed": variant(redirect_uris=["http://localhost", "http://evil.example/"]),
    "redirect_missing": variant(redirect_uris=None),
    "token_uri_missing": variant(token_uri=None),
    "no_client_secret": variant(client_secret=None),
    "web_client": {"web": GOOD["installed"]},
    "web_and_installed": {"installed": GOOD["installed"], "web": GOOD["installed"]},
    "not_an_object": ["installed"],
}


@pytest.fixture
def tripwires(monkeypatch):
    """Any use of the OAuth flow or of the browser fails the test."""
    used = []

    class Flow:
        @classmethod
        def from_client_secrets_file(cls, *a, **k):
            used.append("from_client_secrets_file")
            raise AssertionError("flow started")

        @classmethod
        def from_client_config(cls, *a, **k):
            used.append("from_client_config")
            raise AssertionError("flow started")

    module = types.ModuleType("google_auth_oauthlib.flow")
    module.InstalledAppFlow = Flow
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib.flow", module)

    def no_browser(*a, **k):
        used.append("browser")
        raise AssertionError("browser opened")

    monkeypatch.setattr(webbrowser, "open", no_browser)
    return used


def write(tmp_path, data, name="client_secret_x.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_hostile_client_file_is_refused_before_any_flow(tmp_path, tripwires, name):
    path = write(tmp_path, HOSTILE[name])
    with pytest.raises(auth.ClientSecretRefused) as exc:
        auth.consent(str(path), ["https://www.googleapis.com/auth/analytics.readonly"])
    assert tripwires == []
    text = str(exc.value)
    assert str(path) in text and "Nothing was opened nor stored" in text and "Fix:" in text
    assert "GOCSPX" not in text  # the client secret value is never echoed


@pytest.mark.parametrize("raw", ["not json", "", "{\"installed\": 1}"])
def test_unreadable_client_file_is_refused(tmp_path, tripwires, raw):
    path = tmp_path / "client_secret_bad.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(auth.ClientSecretRefused):
        auth.consent(str(path), [])
    with pytest.raises(auth.ClientSecretRefused):
        auth.consent(str(tmp_path / "missing.json"), [])
    assert tripwires == []


@pytest.mark.parametrize("redirects", [["http://localhost"], ["http://127.0.0.1:8080/"], ["http://[::1]"],
                                       ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"]])
def test_genuine_desktop_client_file_reaches_the_flow_with_its_validated_content(tmp_path, monkeypatch, redirects):
    path = write(tmp_path, variant(redirect_uris=redirects))
    seen = {}

    class Creds:
        granted_scopes = ["https://www.googleapis.com/auth/analytics.readonly"]

        def to_json(self):
            return json.dumps({"token": "t", "refresh_token": "r", "scopes": ["a", "b"]})

    class Flow:
        @classmethod
        def from_client_config(cls, config, scopes):
            seen["config"], seen["scopes"] = config, scopes
            return cls()

        def run_local_server(self, **kwargs):
            seen["kwargs"] = kwargs
            return Creds()

    module = types.ModuleType("google_auth_oauthlib.flow")
    module.InstalledAppFlow = Flow
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib.flow", module)
    raw = auth.consent(str(path), ["https://www.googleapis.com/auth/analytics.readonly"])
    assert list(seen["config"]) == ["installed"] and seen["config"]["installed"]["redirect_uris"] == redirects
    # the stored scopes are the ones Google GRANTED, so check_granted sees the truth
    assert json.loads(raw)["scopes"] == ["https://www.googleapis.com/auth/analytics.readonly"]


def test_setup_client_with_hostile_file_stores_nothing_and_says_where_why_fix(monkeypatch, tmp_path, tripwires,
                                                                              capsys):
    fake, path = setup_env(monkeypatch, tmp_path)
    write_registry(path, {"acme": ACME})
    hostile = write(tmp_path, HOSTILE["token_uri_http_evil"])
    assert main(["setup", "--client", "acme", "--client-secret", str(hostile)]) == 2
    monkeypatch.setattr(client_cli, "_is_tty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert main(["setup", "--client", "acme", "--write", "--client-secret", str(hostile)]) == 2
    assert main(["setup-ads", "--client", "acme", "--client-secret", str(hostile)]) == 2
    err = capsys.readouterr().err
    assert fake.store == {} and tripwires == []
    assert "token_uri must be exactly https://oauth2.googleapis.com/token" in err and str(hostile) in err


@pytest.mark.parametrize("name", ["token_uri_https_evil", "auth_uri_external", "web_client", "redirect_external"])
def test_legacy_setup_and_setup_ads_refuse_a_hostile_file_before_any_flow(monkeypatch, tmp_path, tripwires, name):
    """No registry, no --client: run_setup / run_setup_ads validate the file exactly like consent()."""
    from google_ecommerce_mcp import ads_auth

    fake, _ = setup_env(monkeypatch, tmp_path)
    hostile = write(tmp_path, HOSTILE[name])
    with pytest.raises(auth.ClientSecretRefused) as exc:
        auth.run_setup(str(hostile), None, ["https://www.googleapis.com/auth/analytics.readonly"])
    assert str(hostile) in str(exc.value) and "GOCSPX" not in str(exc.value)
    with pytest.raises(auth.ClientSecretRefused):
        ads_auth.run_setup_ads(str(hostile), None)
    with pytest.raises(auth.ClientSecretRefused):
        auth.run_setup(str(tmp_path / "missing.json"), None)
    assert fake.store == {} and tripwires == []


def test_legacy_setup_and_setup_ads_keep_working_with_a_genuine_file(monkeypatch, tmp_path):
    from google_ecommerce_mcp import ads_auth

    fake, _ = setup_env(monkeypatch, tmp_path)
    good = write(tmp_path, GOOD)
    seen = []

    class Creds:
        def to_json(self):
            return json.dumps({"token": "t", "refresh_token": "r", "scopes": ["s"]})

    class Flow:
        @classmethod
        def from_client_config(cls, config, scopes):
            seen.append((config, list(scopes)))
            return cls()

        def run_local_server(self, **kwargs):
            return Creds()

    module = types.ModuleType("google_auth_oauthlib.flow")
    module.InstalledAppFlow = Flow
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib.flow", module)
    scopes = ["https://www.googleapis.com/auth/analytics.readonly"]
    assert auth.run_setup(str(good), None, scopes) == "operating system keyring"
    assert "file" in ads_auth.run_setup_ads(str(good), str(tmp_path / "ads.json"))
    assert seen == [(GOOD, scopes), (GOOD, ["https://www.googleapis.com/auth/adwords"])]
    assert set(fake.users()) == {"oauth-token"} and (tmp_path / "ads.json").exists()


def test_install_client_with_web_client_file_stores_nothing(monkeypatch, tmp_path, tripwires, capsys):
    fake, path = setup_env(monkeypatch, tmp_path)
    monkeypatch.delenv("GEM_STATE_DIR", raising=False)
    monkeypatch.setattr(installer.shutil, "which", lambda name: "/bin/uvx")
    hostile = write(tmp_path, HOSTILE["web_client"])
    cfg = tmp_path / "claude_desktop_config.json"
    code = main(["install", "--yes", "--client", "acme", "--ga4", "123456789", "--gads-customer-id", "1234567890",
                 "--client-secret", str(hostile), "--config", str(cfg)])
    out = capsys.readouterr().out
    assert code == 0, out  # the install goes on; both authorizations are listed as still to do
    assert fake.store == {} and tripwires == []
    assert "root key must be \"installed\"" in out and "setup --client acme --client-secret" in out

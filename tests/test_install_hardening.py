"""Private files written by the installer and the setup commands, and the legacy keyring pairs of "default".

Offline: icacls is replaced by a recorder (no subprocess), the keyring is the in-memory FakeKeyring."""

from __future__ import annotations

import argparse
import json
import os
import stat

import pytest

from clients_fakes import setup_env, write_registry
from google_ecommerce_mcp import ads_auth, auth, client_cli, installer, secrets_store

SERVICE = "google-ecommerce-mcp"


@pytest.fixture
def acl(monkeypatch):
    """Record the Windows ACL restrictions instead of running icacls."""
    seen = []
    monkeypatch.setattr(ads_auth, "_restrict_windows_acl", lambda path: seen.append(str(path)) or True)
    return seen


def private(path, acl_calls) -> bool:
    if os.name == "nt":
        return str(path) in acl_calls
    return stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


# ------------------------------------------------------------------ Claude config backups and temporary file
def test_backup_is_private_and_the_temporary_file_is_random(tmp_path, monkeypatch, acl):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"x": {"command": "x"}}}), encoding="utf-8")
    replaced = []
    real_replace = installer.os.replace

    def spy(src, dst):
        replaced.append(os.fspath(src))
        return real_replace(src, dst)

    monkeypatch.setattr(installer.os, "replace", spy)
    first = installer.register(cfg, "a", {"command": "uvx", "args": ["google-ecommerce-mcp"], "env": {}})
    second = installer.register(cfg, "b", {"command": "uvx", "args": ["google-ecommerce-mcp"], "env": {}})
    for backup in (first, second):
        assert backup and private(tmp_path / os.path.basename(backup), acl)
    names = [os.path.basename(p) for p in replaced]
    assert len(set(names)) == 2 and all(n.startswith(".claude_desktop_config.json.") and n.endswith(".tmp")
                                        for n in names)
    assert not list(tmp_path.glob("*.tmp")) and not list(tmp_path.glob(".*.tmp"))
    assert json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["x"] == {"command": "x"}


def test_failed_write_leaves_no_backup_nor_temporary_file(tmp_path, monkeypatch, acl):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text("{}", encoding="utf-8")

    def broken(src, dst):
        raise PermissionError("locked")

    monkeypatch.setattr(installer.os, "replace", broken)
    with pytest.raises(installer.ConfigError):
        installer.register(cfg, "a", {"command": "uvx", "args": ["google-ecommerce-mcp"], "env": {}})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["claude_desktop_config.json"]


def test_pin_is_only_accepted_as_an_addition_when_allowed(tmp_path, acl):
    cfg = tmp_path / "c.json"
    plain = {"command": "uvx", "args": ["google-ecommerce-mcp"], "env": {"A": "1"}}
    installer.register(cfg, "s", plain)
    pinned = {"command": "uvx", "args": ["google-ecommerce-mcp==9.9.9"], "env": {"A": "1", "GEM_WRITE": "1"}}
    with pytest.raises(FileExistsError):
        installer.register(cfg, "s", pinned, allow_additions=True)
    assert installer.register(cfg, "s", pinned, allow_additions=True, allow_pin=True)
    other = {"command": "uvx", "args": ["something-else"], "env": {"A": "1"}}
    with pytest.raises(FileExistsError):
        installer.register(cfg, "s", other, allow_additions=True, allow_pin=True)
    assert installer.server_entry({}, uvx="uvx", args=[installer.pinned_package()])["args"][0].startswith(
        "google-ecommerce-mcp==")


# ------------------------------------------------------------------ legacy token file
def test_legacy_token_file_is_written_privately(tmp_path, monkeypatch, acl):
    seen = []
    real = ads_auth._write_private
    monkeypatch.setattr(ads_auth, "_write_private", lambda path, text: seen.append(path) or real(path, text))
    target = tmp_path / "tok" / "google.json"
    where = auth.save_token('{"token": "x"}', str(target))
    assert seen == [target] and where == f"file {target}"
    assert target.read_text(encoding="utf-8") == '{"token": "x"}' and private(target, acl)


def test_legacy_token_file_reports_a_failed_windows_acl(tmp_path, monkeypatch):
    monkeypatch.setattr(ads_auth, "_write_private", lambda path, text: False)
    where = auth.save_token("{}", str(tmp_path / "t.json"))
    assert ads_auth.WINDOWS_ACL_WARNING in where


# ------------------------------------------------------------------ "default" -> legacy entries: coherent pairs only
@pytest.mark.parametrize("kind, name, ok", [
    ("google", "oauth-token", True), ("gads", "oauth-token-ads", True), ("meta", "meta-access-token", True),
    ("openai", "openai-ads-key", True),
    ("google-w", "oauth-token", False), ("google-w", "oauth-token-ads", False), ("google", "oauth-token-ads", False),
    ("gads", "oauth-token", False), ("meta", "openai-ads-key", False), ("openai", "meta-access-token", False),
    ("meta-capi", "meta-access-token", False), ("pagespeed", "openai-ads-key", False),
])
def test_default_maps_only_coherent_legacy_pairs(kind, name, ok):
    assert secrets_store.valid_secret_name("default", name, kind) is ok
    assert secrets_store.legacy_pair_ok("default", kind, name) is ok
    assert secrets_store.valid_secret_name("acme", name, kind) is False


def test_incoherent_legacy_pair_is_never_read_nor_written(monkeypatch, tmp_path):
    fake, _ = setup_env(monkeypatch, tmp_path)
    fake.store[(SERVICE, "oauth-token")] = "READ-TOKEN"
    assert secrets_store.get_secret("default", "google", name="oauth-token") == "READ-TOKEN"
    assert secrets_store.get_secret("default", "google-w", name="oauth-token") is None
    with pytest.raises(secrets_store.SecretError):
        secrets_store.set_secret("default", "google-w", '{"token": "w"}', name="oauth-token")
    with pytest.raises(secrets_store.SecretError):
        secrets_store.set_secret("default", "meta", "EAAvalue", name="openai-ads-key")
    assert fake.store == {(SERVICE, "oauth-token"): "READ-TOKEN"}


# ------------------------------------------------------------------ set-secret fallback message
def test_keyring_failure_recommends_set_secret_and_calls_env_a_clear_text_last_resort(monkeypatch, tmp_path,
                                                                                         capsys):
    fake, path = setup_env(monkeypatch, tmp_path)
    write_registry(path, {"acme": {"accounts": {"meta": {"ad_account_id": "123456"}}}})

    def no_backend(*_a):
        raise RuntimeError("no backend")

    monkeypatch.setattr(fake, "set_password", no_backend)
    monkeypatch.setattr(client_cli, "_is_tty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "EAAsecretvalue123456")
    assert client_cli._cmd_set_secret(argparse.Namespace(name="acme", kind="meta")) == 1
    err = capsys.readouterr().err
    assert "LAST RESORT" in err and "CLEAR TEXT" in err and "client set-secret acme meta" in err
    assert "GEM_SECRET_ACME_META" in err and "EAAsecret" not in err

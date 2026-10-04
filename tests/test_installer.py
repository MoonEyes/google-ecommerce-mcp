"""Installer tests: config merge, backup, conflicts. No Google call, no real Claude config touched."""

import json

import pytest

from google_ecommerce_mcp import installer


def entry(ga4="1"):
    return installer.server_entry({"GA4_PROPERTY_ID": ga4, "GTM_CONTAINER_ID": ""}, uvx="/bin/uvx")


def test_entry_drops_empty_values_and_pins_repo():
    e = entry()
    assert e["env"] == {"GA4_PROPERTY_ID": "1"}
    assert e["command"] == "/bin/uvx" and e["args"] == ["google-ecommerce-mcp"]


def test_register_creates_missing_config(tmp_path):
    cfg = tmp_path / "Claude" / "claude_desktop_config.json"
    assert installer.register(cfg, "google-ecommerce", entry()) == ""
    assert json.loads(cfg.read_text())["mcpServers"]["google-ecommerce"]["env"]["GA4_PROPERTY_ID"] == "1"


def test_register_keeps_other_settings_and_backs_up(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    original = {"mcpServers": {"other": {"command": "x"}}, "preferences": {"theme": "dark"}}
    cfg.write_text(json.dumps(original))
    backup = installer.register(cfg, "google-ecommerce", entry())
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["other"] == {"command": "x"} and data["preferences"] == {"theme": "dark"}
    assert json.loads(open(backup).read()) == original


def test_register_refuses_to_overwrite_without_force(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    installer.register(cfg, "google-ecommerce", entry("1"))
    with pytest.raises(FileExistsError):
        installer.register(cfg, "google-ecommerce", entry("2"))
    installer.register(cfg, "google-ecommerce", entry("2"), force=True)
    assert json.loads(cfg.read_text())["mcpServers"]["google-ecommerce"]["env"]["GA4_PROPERTY_ID"] == "2"


def test_same_entry_twice_is_idempotent(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    installer.register(cfg, "google-ecommerce", entry())
    installer.register(cfg, "google-ecommerce", entry())


def test_claude_code_command():
    cmd = installer.claude_code_command("shop", {"GA4_PROPERTY_ID": "1", "GSC_SITE_URL": ""})
    assert cmd == "claude mcp add shop --scope user -e GA4_PROPERTY_ID=1 -- uvx google-ecommerce-mcp"


def test_install_without_any_id_stops(monkeypatch, capsys):
    from google_ecommerce_mcp.__main__ import main

    assert main(["install", "--yes", "--skip-auth", "--no-desktop"]) == 2


def test_install_end_to_end_without_google(tmp_path, monkeypatch):
    from google_ecommerce_mcp.__main__ import main

    cfg = tmp_path / "claude_desktop_config.json"
    code = main(["install", "--yes", "--skip-auth", "--ga4", "123", "--gsc", "sc-domain:example.com",
                 "--token-file", str(tmp_path / "none.json"), "--config", str(cfg)])
    assert code == 0
    env = json.loads(cfg.read_text())["mcpServers"]["google-ecommerce"]["env"]
    assert env == {"GA4_PROPERTY_ID": "123", "GSC_SITE_URL": "sc-domain:example.com",
                   "GOOGLE_TOKEN_FILE": str(tmp_path / "none.json")}


def test_register_accepts_utf8_bom(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_bytes(b"\xef\xbb\xbf" + json.dumps({"mcpServers": {"other": {"command": "x"}}}).encode())
    installer.register(cfg, "google-ecommerce", entry())
    assert set(json.loads(cfg.read_text())["mcpServers"]) == {"other", "google-ecommerce"}


def test_register_rejects_invalid_json_without_touching_it(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text("{broken")
    with pytest.raises(ValueError):
        installer.register(cfg, "google-ecommerce", entry())
    assert cfg.read_text() == "{broken"


def test_no_backup_when_nothing_changes_or_refused(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text("{}")
    assert installer.register(cfg, "google-ecommerce", entry("1"))  # first write: backup of "{}"
    assert installer.register(cfg, "google-ecommerce", entry("1")) == ""  # identical: untouched
    with pytest.raises(FileExistsError):
        installer.register(cfg, "google-ecommerce", entry("2"))
    assert len(list(tmp_path.glob("*.bak-*"))) == 1

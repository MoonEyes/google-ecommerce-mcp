"""`install` paid media step, `setup-ads` / `set-secret` reminders. Claude Desktop config in tmp_path only, OS keyring
replaced by an in-memory dict, OAuth flows replaced by fakes, zero network."""

import builtins
import json

import pytest

from ads_fakes import GOOGLE_ACCESS_PREFIX, META_TOKEN_PREFIX, OPENAI_KEY_PREFIX, no_keyring, no_network  # noqa: F401
from google_ecommerce_mcp import ads_auth, doctor, installer
from google_ecommerce_mcp.__main__ import main
from google_ecommerce_mcp.ads_config import ADS_SCOPE
from google_ecommerce_mcp.config import KEYRING_SERVICE

META_TOKEN = META_TOKEN_PREFIX + "Tinstall" + "m" * 40
OPENAI_KEY = OPENAI_KEY_PREFIX + "install" + "O" * 30
CLIENT_SECRET_BODY = json.dumps({"installed": {"client_id": "cid", "client_secret": "csecret-" + "z" * 20}})


@pytest.fixture(autouse=True)
def clean(monkeypatch, no_keyring):
    for name in doctor.KNOWN_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(installer.shutil, "which", lambda name: "/bin/uvx")
    calls = []

    def fake_setup_ads(client_secret, token_file=None):
        calls.append((client_secret, token_file))
        ads_auth.save_ads_token(json.dumps({"token": GOOGLE_ACCESS_PREFIX + "x" * 30, "scopes": [ADS_SCOPE]}),
                                token_file)
        return "operating system keyring (entry oauth-token-ads)"

    monkeypatch.setattr(ads_auth, "run_setup_ads", fake_setup_ads)
    no_keyring.setup_ads_calls = calls
    return no_keyring


def install(capsys, cfg, *extra):
    code = main(["install", "--yes", "--config", str(cfg), *extra])
    return code, capsys.readouterr().out


def entry_env(cfg, name="google-ecommerce"):
    return json.loads(cfg.read_text())["mcpServers"][name]["env"]


def client_secret(tmp_path):
    path = tmp_path / "client_secret.json"
    path.write_text(CLIENT_SECRET_BODY)
    return path


# ------------------------------------------------------------------ --yes
def test_yes_ads_ids_only_merges_with_backup_and_lists_what_is_left(capsys, tmp_path, clean):
    cfg = tmp_path / "claude_desktop_config.json"
    original = {"mcpServers": {"other": {"command": "x"}}, "preferences": {"theme": "dark"}}
    cfg.write_text(json.dumps(original))
    code, out = install(capsys, cfg, "--skip-auth", "--gads-customer-id", "123-456-7890",
                        "--meta-account-id", "act_9876543210")
    assert code == 0
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["other"] == {"command": "x"} and data["preferences"] == {"theme": "dark"}
    assert entry_env(cfg) == {"GADS_CUSTOMER_ID": "1234567890", "META_AD_ACCOUNT_ID": "9876543210"}
    backups = list(tmp_path.glob("*.bak-*"))
    assert len(backups) == 1 and json.loads(backups[0].read_text()) == original
    assert "Still to do:" in out
    assert "google-ecommerce-mcp setup-ads --client-secret <client_secret.json>" in out
    assert "google-ecommerce-mcp set-secret meta-access-token" in out
    assert "Next command: google-ecommerce-mcp setup-ads" in out
    assert out.count("Summary") == 1
    assert clean.setup_ads_calls == [] and not any(k[1] == "meta-access-token" for k in clean)


def test_yes_never_prompts(capsys, tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("prompted under --yes")

    monkeypatch.setattr(builtins, "input", refuse)
    monkeypatch.setattr(installer.getpass, "getpass", refuse)
    code, _ = install(capsys, tmp_path / "c.json", "--skip-auth", "--meta-account-id", "123456")
    assert code == 0


def test_yes_gads_runs_the_separate_oauth_with_the_same_client_file(capsys, tmp_path, clean):
    cfg = tmp_path / "claude_desktop_config.json"
    secret = client_secret(tmp_path)
    code, out = install(capsys, cfg, "--client-secret", str(secret), "--gads-customer-id", "1234567890")
    assert code == 0
    assert clean.setup_ads_calls == [(str(secret), None)]
    assert (KEYRING_SERVICE, "oauth-token-ads") in clean and (KEYRING_SERVICE, "oauth-token") not in clean
    assert "Google Ads token: stored (separate from the Google token)" in out
    assert "Configured: Google Ads (customer 123***890)" in out
    # the client file is never copied nor shown
    assert CLIENT_SECRET_BODY not in out and "csecret" not in out and "csecret" not in cfg.read_text()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["claude_desktop_config.json", "client_secret.json"]


def test_existing_ads_token_is_kept_under_yes(capsys, tmp_path, clean):
    clean[(KEYRING_SERVICE, "oauth-token-ads")] = json.dumps({"scopes": [ADS_SCOPE]})
    code, out = install(capsys, tmp_path / "c.json", "--client-secret", str(client_secret(tmp_path)),
                        "--gads-customer-id", "1234567890")
    assert code == 0 and clean.setup_ads_calls == [] and "already stored" in out


def test_ads_token_file_equal_to_google_token_file_is_refused(capsys, tmp_path, monkeypatch, clean):
    token = tmp_path / "google-token.json"
    monkeypatch.setenv("GADS_TOKEN_FILE", str(token))
    cfg = tmp_path / "claude_desktop_config.json"
    code, out = install(capsys, cfg, "--client-secret", str(client_secret(tmp_path)), "--token-file", str(token),
                        "--gads-customer-id", "1234567890", "--skip-auth")
    assert code == 2 and "must be kept apart" in out
    assert clean.setup_ads_calls == [] and not cfg.exists()


def test_gads_token_file_is_written_as_a_path_only(capsys, tmp_path, monkeypatch):
    ads_file = tmp_path / "ads-token.json"
    monkeypatch.setenv("GADS_TOKEN_FILE", str(ads_file))
    cfg = tmp_path / "c.json"
    code, _ = install(capsys, cfg, "--client-secret", str(client_secret(tmp_path)), "--gads-customer-id", "1234567890")
    assert code == 0 and entry_env(cfg)["GADS_TOKEN_FILE"] == str(ads_file) and ads_file.exists()
    assert GOOGLE_ACCESS_PREFIX not in cfg.read_text()


def test_corporate_proxy_and_manager_id(capsys, tmp_path):
    cfg = tmp_path / "c.json"
    code, _ = install(capsys, cfg, "--skip-auth", "--gads-customer-id", "1234567890",
                      "--gads-login-customer-id", "111-222-3333", "--corporate-proxy")
    assert code == 0
    assert entry_env(cfg) == {"GADS_CUSTOMER_ID": "1234567890", "GADS_LOGIN_CUSTOMER_ID": "1112223333",
                              "ADS_TRUST_ENV": "1"}


@pytest.mark.parametrize("flag, value", [("--gads-customer-id", "12AB"), ("--meta-account-id", "act_x1"),
                                         ("--gads-login-customer-id", "abc")])
def test_invalid_ids_stop_without_writing(capsys, tmp_path, flag, value):
    cfg = tmp_path / "c.json"
    code, out = install(capsys, cfg, "--skip-auth", flag, value)
    assert code == 2 and "Nothing was changed" in out and not cfg.exists()


def test_skip_ads_ignores_paid_media(capsys, tmp_path):
    code, out = install(capsys, tmp_path / "c.json", "--skip-auth", "--skip-ads", "--gads-customer-id", "1234567890")
    assert code == 2 and "No service configured" in out


# ------------------------------------------------------------------ idempotence, merge, --force
def test_rerun_is_idempotent(capsys, tmp_path):
    cfg = tmp_path / "c.json"
    args = ("--skip-auth", "--gads-customer-id", "1234567890", "--meta-account-id", "123456789")
    assert install(capsys, cfg, *args)[0] == 0
    first = cfg.read_text()
    assert install(capsys, cfg, *args)[0] == 0
    assert cfg.read_text() == first and list(tmp_path.glob("*.bak-*")) == []
    assert len(json.loads(first)["mcpServers"]) == 1


def test_one_service_at_a_time_adds_without_force(capsys, tmp_path):
    cfg = tmp_path / "c.json"
    assert main(["install", "--yes", "--skip-auth", "--ga4", "123456789", "--token-file", str(tmp_path / "t.json"),
                 "--config", str(cfg)]) == 0
    code, out = install(capsys, cfg, "--skip-auth", "--meta-account-id", "123456789")
    assert code == 0
    assert entry_env(cfg) == {"GA4_PROPERTY_ID": "123456789", "GOOGLE_TOKEN_FILE": str(tmp_path / "t.json"),
                              "META_AD_ACCOUNT_ID": "123456789"}
    assert len(list(tmp_path.glob("*.bak-*"))) == 1


def test_changed_value_needs_force_and_force_keeps_the_rest(capsys, tmp_path):
    cfg = tmp_path / "c.json"
    install(capsys, cfg, "--skip-auth", "--gads-customer-id", "1234567890", "--meta-account-id", "123456789")
    before = cfg.read_text()
    code, out = install(capsys, cfg, "--skip-auth", "--gads-customer-id", "9999999999")
    assert code == 3 and "GADS_CUSTOMER_ID" in out and "--force" in out and "9999999999" not in out
    assert cfg.read_text() == before
    code, _ = install(capsys, cfg, "--skip-auth", "--force", "--gads-customer-id", "9999999999")
    assert code == 0
    assert entry_env(cfg) == {"GADS_CUSTOMER_ID": "9999999999", "META_AD_ACCOUNT_ID": "123456789"}


def test_entry_written_by_hand_with_other_command_needs_force(capsys, tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"mcpServers": {"google-ecommerce": {"command": "python", "args": ["-m", "x"]}}}))
    code, out = install(capsys, cfg, "--skip-auth", "--meta-account-id", "123456789")
    assert code == 3 and "--force" in out


def test_merchant_scope_is_kept_when_another_google_service_is_added(capsys, tmp_path, monkeypatch):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"mcpServers": {"google-ecommerce": {"command": "/bin/uvx",
                                                                   "args": ["google-ecommerce-mcp"],
                                                                   "env": {"MERCHANT_ACCOUNT_ID": "555666777"}}}}))
    seen = {}

    def fake_run_setup(client_secret, token_file, scopes):
        seen["scopes"] = scopes
        return "operating system keyring"

    from google_ecommerce_mcp import auth

    monkeypatch.setattr(auth, "run_setup", fake_run_setup)
    code, _ = install(capsys, cfg, "--client-secret", str(client_secret(tmp_path)), "--ga4", "123456789",
                      "--skip-ads")
    assert code == 0 and "https://www.googleapis.com/auth/content" in seen["scopes"]


# ------------------------------------------------------------------ interactive
class TTY:
    def isatty(self):
        return True


def interactive(monkeypatch, answers, hidden):
    answers, hidden = iter(answers), iter(hidden)
    prompts = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        return next(answers)

    def fake_getpass(prompt=""):
        prompts.append(prompt)
        return next(hidden)

    monkeypatch.setattr(installer.sys, "stdin", TTY())
    monkeypatch.setattr(builtins, "input", fake_input)
    monkeypatch.setattr(installer.getpass, "getpass", fake_getpass)
    return prompts


def test_interactive_paid_media_stores_secrets_in_keyring_only(capsys, tmp_path, monkeypatch, clean):
    prompts = interactive(monkeypatch,
                          ["", "", "", "",          # Google questions skipped
                           "y",                         # set up paid media
                           "123-456-7890", "",          # Google Ads id, no manager
                           "act_123456789",             # Meta account
                           "y"],                        # ChatGPT Ads
                          ["", META_TOKEN, OPENAI_KEY])
    cfg = tmp_path / "c.json"
    code = main(["install", "--skip-auth", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert code == 0
    assert clean[(KEYRING_SERVICE, "meta-access-token")] == META_TOKEN
    assert clean[(KEYRING_SERVICE, "openai-ads-key")] == OPENAI_KEY
    text = cfg.read_text()
    assert META_TOKEN not in text and OPENAI_KEY not in text and META_TOKEN not in out and OPENAI_KEY not in out
    assert entry_env(cfg) == {"GADS_CUSTOMER_ID": "1234567890", "META_AD_ACCOUNT_ID": "123456789"}
    assert any("hidden" in p for p in prompts)
    assert "Meta Ads (ad account 123***789)" in out and "ChatGPT Ads" in out
    assert "setup-ads" in out  # --skip-auth: Google Ads still to authorize


def test_interactive_retries_a_bad_id_then_accepts(capsys, tmp_path, monkeypatch):
    interactive(monkeypatch, ["", "", "", "", "y", "12AB", "1234567890", "", "", "n"], [""])
    cfg = tmp_path / "c.json"
    assert main(["install", "--skip-auth", "--config", str(cfg)]) == 0
    assert entry_env(cfg) == {"GADS_CUSTOMER_ID": "1234567890"}


def test_interactive_existing_secret_is_not_replaced_by_default(capsys, tmp_path, monkeypatch, clean):
    clean[(KEYRING_SERVICE, "meta-access-token")] = META_TOKEN
    interactive(monkeypatch, ["", "", "", "", "y", "", "", "123456789", "", "n"], [""])
    assert main(["install", "--skip-auth", "--config", str(tmp_path / "c.json")]) == 0
    assert clean[(KEYRING_SERVICE, "meta-access-token")] == META_TOKEN
    assert "Meta access token: already stored" in capsys.readouterr().out


def test_interactive_empty_secret_is_left_to_do(capsys, tmp_path, monkeypatch, clean):
    interactive(monkeypatch, ["", "", "", "", "y", "", "", "123456789", "n"], ["", ""])
    assert main(["install", "--skip-auth", "--config", str(tmp_path / "c.json")]) == 0
    out = capsys.readouterr().out
    assert "Next command: google-ecommerce-mcp set-secret meta-access-token" in out
    assert (KEYRING_SERVICE, "meta-access-token") not in clean


def test_interactive_no_paid_media(capsys, tmp_path, monkeypatch):
    interactive(monkeypatch, ["", "", "", "", "n"], [""])
    assert main(["install", "--skip-auth", "--config", str(tmp_path / "c.json")]) == 2


# ------------------------------------------------------------------ setup-ads / set-secret reminders
def test_setup_ads_prints_next_step(capsys, tmp_path, clean):
    assert main(["setup-ads", "--client-secret", str(client_secret(tmp_path))]) is None
    out = capsys.readouterr().out
    assert "google-ecommerce-mcp doctor --only gads --live" in out and GOOGLE_ACCESS_PREFIX not in out


@pytest.mark.parametrize("name, service, value", [("meta-access-token", "meta", META_TOKEN),
                                                  ("openai-ads-key", "openai", OPENAI_KEY)])
def test_set_secret_prints_next_step(capsys, monkeypatch, clean, name, service, value):
    import getpass

    monkeypatch.setattr(getpass, "getpass", lambda prompt="": value)
    assert main(["set-secret", name]) is None
    out = capsys.readouterr().out
    assert f"google-ecommerce-mcp doctor --only {service} --live" in out and value not in out
    assert clean[(KEYRING_SERVICE, name)] == value


def test_interactive_empty_openai_key_is_left_to_do(capsys, tmp_path, monkeypatch, clean):
    interactive(monkeypatch, ["", "", "", "", "y", "", "", "", "y"], ["", ""])
    assert main(["install", "--skip-auth", "--config", str(tmp_path / "c.json")]) == 0
    out = capsys.readouterr().out
    assert "Next command: google-ecommerce-mcp set-secret openai-ads-key" in out
    assert (KEYRING_SERVICE, "openai-ads-key") not in clean


# ------------------------------------------------------------------ review fixes (P3)
def existing(tmp_path, env, command="/bin/uvx", args=("google-ecommerce-mcp",)):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"google-ecommerce": {"command": command, "args": list(args),
                                                                   "env": env}}}))
    return cfg


def guide_words(command, cfg):
    import re

    return re.sub(r"<[^>]+>", "123", command).split()[1:] + ["--config", str(cfg)]


def test_corporate_proxy_alone_updates_an_existing_entry_end_to_end(capsys, tmp_path, clean):
    from google_ecommerce_mcp import setup_guide as guide

    cfg = existing(tmp_path, {"GADS_CUSTOMER_ID": "1234567890"})
    code = main(guide_words(guide.CORPORATE_PROXY, cfg))  # the exact command doctor prints on a network error
    out = capsys.readouterr().out
    assert code == 0, out
    assert entry_env(cfg) == {"GADS_CUSTOMER_ID": "1234567890", "ADS_TRUST_ENV": "1"}
    assert len(list(tmp_path.glob("*.bak-*"))) == 1 and clean.setup_ads_calls == []


def test_corporate_proxy_alone_without_entry_registers(capsys, tmp_path):
    cfg = tmp_path / "c.json"
    assert install(capsys, cfg, "--skip-auth", "--corporate-proxy")[0] == 0
    assert entry_env(cfg) == {"ADS_TRUST_ENV": "1"}


def test_pagespeed_key_by_flag_counts_as_a_service(capsys, tmp_path):
    key = "AIza" + "f" * 35
    cfg = tmp_path / "c.json"
    code, out = install(capsys, cfg, "--skip-auth", "--pagespeed-key", key)
    assert code == 0 and entry_env(cfg) == {"PAGESPEED_API_KEY": key}
    assert key not in out and "Configured: PageSpeed" in out


def test_pagespeed_key_from_environment_is_never_printed(capsys, tmp_path, monkeypatch):
    key = "AIza" + "e" * 35
    monkeypatch.setenv("PAGESPEED_API_KEY", key)
    cfg = tmp_path / "c.json"
    code, out = install(capsys, cfg, "--skip-auth")
    assert code == 0 and entry_env(cfg) == {"PAGESPEED_API_KEY": key}
    assert key not in out and "read from PAGESPEED_API_KEY" in out
    assert "1 variable(s) not shown" in out


def test_pagespeed_guide_step_runs_end_to_end_with_a_hidden_prompt(capsys, tmp_path, monkeypatch):
    from google_ecommerce_mcp import setup_guide as guide

    key = "AIza" + "h" * 35
    prompts = interactive(monkeypatch, ["", "", "", ""], [key])
    cfg = tmp_path / "c.json"
    command = guide.service("pagespeed").step("pagespeed-install").command
    assert main(guide_words(command, cfg)) == 0
    out = capsys.readouterr().out
    assert entry_env(cfg) == {"PAGESPEED_API_KEY": key} and key not in out
    assert any("PageSpeed" in p and "hidden" in p for p in prompts)


def test_conflict_exits_3_before_any_authorization(capsys, tmp_path, monkeypatch, clean):
    from google_ecommerce_mcp import auth

    def refuse(*args, **kwargs):
        raise AssertionError("authorized before the conflict check")

    monkeypatch.setattr(auth, "run_setup", refuse)
    cfg = existing(tmp_path, {"GADS_CUSTOMER_ID": "1111111111", "GA4_PROPERTY_ID": "1"})
    before = cfg.read_text()
    code, out = install(capsys, cfg, "--client-secret", str(client_secret(tmp_path)), "--ga4", "123456789",
                        "--gads-customer-id", "2222222222", "--meta-account-id", "123456")
    assert code == 3 and "Nothing was changed" in out and "GA4_PROPERTY_ID, GADS_CUSTOMER_ID" in out
    assert clean.setup_ads_calls == [] and dict(clean) == {} and cfg.read_text() == before


def test_other_command_exits_3_before_any_authorization(capsys, tmp_path, clean):
    cfg = existing(tmp_path, {}, command="python", args=("-m", "x"))
    code, out = install(capsys, cfg, "--client-secret", str(client_secret(tmp_path)), "--gads-customer-id",
                        "1234567890")
    assert code == 3 and "Nothing was changed" in out and clean.setup_ads_calls == []


def test_write_failure_after_authorization_says_what_was_done(capsys, tmp_path, monkeypatch, clean):
    def fail(*args, **kwargs):
        raise installer.ConfigError("cannot write.")

    monkeypatch.setattr(installer, "register", fail)
    code, out = install(capsys, tmp_path / "c.json", "--client-secret", str(client_secret(tmp_path)),
                        "--gads-customer-id", "1234567890")
    assert code == 3 and clean.setup_ads_calls
    assert "The Claude Desktop config was not changed. Already done and kept: Google Ads token stored." in out
    assert "Nothing was changed" not in out


BAD_SHAPES = ["[]", "null", '"x"', "3", '{"mcpServers": null}', '{"mcpServers": []}', '{"mcpServers": "x"}']


@pytest.mark.parametrize("text", BAD_SHAPES + ["{broken"])
def test_wrong_shape_config_exits_3_before_anything(capsys, tmp_path, clean, text):
    cfg = tmp_path / "c.json"
    cfg.write_text(text)
    code, out = install(capsys, cfg, "--client-secret", str(client_secret(tmp_path)), "--gads-customer-id",
                        "1234567890")
    assert code == 3 and "Nothing was changed" in out and "Traceback" not in out
    assert cfg.read_text() == text and clean.setup_ads_calls == [] and list(tmp_path.glob("*.bak-*")) == []


@pytest.mark.parametrize("text", BAD_SHAPES)
def test_register_and_existing_entry_on_wrong_shapes(tmp_path, text):
    cfg = tmp_path / "c.json"
    cfg.write_text(text)
    with pytest.raises(installer.ConfigError):
        installer.register(cfg, "google-ecommerce", installer.server_entry({"GA4_PROPERTY_ID": "1"}, uvx="u"))
    assert installer.existing_entry(cfg, "google-ecommerce") is None and cfg.read_text() == text


def test_entry_that_is_not_an_object_needs_force(capsys, tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"mcpServers": {"google-ecommerce": "x"}}))
    assert install(capsys, cfg, "--skip-auth", "--meta-account-id", "123456")[0] == 3
    assert install(capsys, cfg, "--skip-auth", "--force", "--meta-account-id", "123456")[0] == 0
    assert entry_env(cfg) == {"META_AD_ACCOUNT_ID": "123456"}


def test_utf16_config_is_refused_cleanly(capsys, tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_bytes(json.dumps({"mcpServers": {}}).encode("utf-16"))
    code, out = install(capsys, cfg, "--skip-auth", "--meta-account-id", "123456")
    assert code == 3 and "not UTF-8" in out and "Traceback" not in out


def test_oversized_config_is_refused(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "MAX_CONFIG_BYTES", 10)
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    code, out = install(capsys, cfg, "--skip-auth", "--meta-account-id", "123456")
    assert code == 3 and "larger than" in out


def test_read_only_config_leaves_no_tmp_and_no_backup(capsys, tmp_path, monkeypatch):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    before = cfg.read_text()

    def deny(src, dst):
        raise PermissionError("read-only")

    monkeypatch.setattr(installer.os, "replace", deny)
    code, out = install(capsys, cfg, "--skip-auth", "--meta-account-id", "123456")
    assert code == 3 and "could not be written (PermissionError)" in out and "Traceback" not in out
    assert cfg.read_text() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["c.json"]


def test_backups_never_collide(tmp_path, monkeypatch):
    import datetime as real

    class Frozen(real.datetime):
        @classmethod
        def now(cls, tz=None):
            return real.datetime(2026, 10, 16, 12, 0, 0, 123456)

    monkeypatch.setattr(installer.datetime, "datetime", Frozen)
    cfg = tmp_path / "c.json"
    cfg.write_text("{}")
    for value in ("1", "2", "3"):
        installer.register(cfg, "s", installer.server_entry({"GA4_PROPERTY_ID": value}, uvx="u"), force=True)
    backups = sorted(p.name for p in tmp_path.glob("*.bak-*"))
    assert len(backups) == 3 and len(set(backups)) == 3


def test_empty_appdata_gives_an_absolute_path(monkeypatch):
    monkeypatch.setattr(installer.platform, "system", lambda: "Windows")
    monkeypatch.setenv("APPDATA", "")
    assert installer.claude_desktop_config_path().is_absolute()
    monkeypatch.setattr(installer.platform, "system", lambda: "Linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    assert installer.claude_desktop_config_path().is_absolute()


def test_claude_code_command_is_a_whitelist(capsys, tmp_path):
    leaks = {"GOOGLE_REFRESH_TOKEN": GOOGLE_ACCESS_PREFIX + "r" * 30, "MY_API_SECRET": "very-secret-value-42",
             "PAGESPEED_API_KEY": "AIza" + "w" * 35, "META_ACCESS_TOKEN": META_TOKEN}
    cfg = existing(tmp_path, dict(leaks))
    code, out = install(capsys, cfg, "--skip-auth", "--meta-account-id", "123456")
    assert code == 0
    for value in leaks.values():
        assert value not in out
    assert "-e META_AD_ACCOUNT_ID=123456" in out and "4 variable(s) not shown" in out
    assert entry_env(cfg)["MY_API_SECRET"] == "very-secret-value-42"  # kept where the user put it


@pytest.mark.parametrize("entry", [{"GOOGLE_TOKEN_FILE": "same.json", "GADS_TOKEN_FILE": "same.json"},
                                   {"GADS_TOKEN_FILE": "same.json"}])
def test_token_separation_uses_the_claude_desktop_entry(capsys, tmp_path, clean, entry):
    entry = {k: str(tmp_path / v) for k, v in entry.items()}
    cfg = existing(tmp_path, entry)
    extra = [] if "GOOGLE_TOKEN_FILE" in entry else ["--token-file", str(tmp_path / "same.json")]
    code, out = install(capsys, cfg, "--client-secret", str(client_secret(tmp_path)), "--gads-customer-id",
                        "1234567890", *extra)
    assert code == 2 and "must be kept apart" in out and clean.setup_ads_calls == []
    assert entry_env(cfg) == entry


@pytest.mark.parametrize("value", ["123456789", "12345678901", "123-456-789"])
def test_google_ads_id_is_exactly_ten_digits(capsys, tmp_path, value):
    code, out = install(capsys, tmp_path / "c.json", "--skip-auth", "--gads-customer-id", value)
    assert code == 2 and "10 digits" in out
    assert installer.normalize_gads_id("123 456 7890") == "1234567890"


@pytest.mark.parametrize("error", [KeyboardInterrupt, EOFError])
def test_interrupt_exits_130_without_traceback(capsys, tmp_path, monkeypatch, error):
    def stop(prompt=""):
        raise error()

    monkeypatch.setattr(installer.sys, "stdin", TTY())
    monkeypatch.setattr(builtins, "input", stop)
    cfg = tmp_path / "c.json"
    assert main(["install", "--config", str(cfg)]) == 130
    out = capsys.readouterr().out
    assert "Install interrupted. Nothing was changed." in out and not cfg.exists()


def test_interrupt_after_a_stored_secret_says_so(capsys, tmp_path, monkeypatch, clean):
    answers = iter(["", "", "", "", "y", "", "", "123456", "y"])
    hidden = iter(["", META_TOKEN])

    def fake_getpass(prompt=""):
        try:
            return next(hidden)
        except StopIteration:
            raise KeyboardInterrupt() from None

    monkeypatch.setattr(installer.sys, "stdin", TTY())
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(answers))
    monkeypatch.setattr(installer.getpass, "getpass", fake_getpass)
    assert main(["install", "--skip-auth", "--config", str(tmp_path / "c.json")]) == 130
    out = capsys.readouterr().out
    assert "Already done before this stop: Meta access token stored in the keyring." in out
    assert META_TOKEN not in out


def test_uvx_path_of_the_existing_entry_is_kept(capsys, tmp_path):
    cfg = existing(tmp_path, {"GA4_PROPERTY_ID": "1"}, command="/custom/bin/uvx")
    assert install(capsys, cfg, "--skip-auth", "--meta-account-id", "123456")[0] == 0
    entry = json.loads(cfg.read_text())["mcpServers"]["google-ecommerce"]
    assert entry["command"] == "/custom/bin/uvx" and entry["env"]["GA4_PROPERTY_ID"] == "1"


def test_yes_with_a_terminal_still_never_prompts(capsys, tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("prompted under --yes")

    monkeypatch.setattr(installer.sys, "stdin", TTY())
    monkeypatch.setattr(builtins, "input", refuse)
    monkeypatch.setattr(installer.getpass, "getpass", refuse)
    code, out = install(capsys, tmp_path / "c.json", "--skip-auth", "--meta-account-id", "123456")
    assert code == 0 and "1. store the Meta access token (hidden prompt)" in out


def test_set_secret_empty_returns_1_and_stores_nothing(capsys, monkeypatch, clean):
    import getpass as gp

    monkeypatch.setattr(gp, "getpass", lambda prompt="": "   ")
    assert main(["set-secret", "meta-access-token"]) == 1
    assert dict(clean) == {}


def test_set_secret_refuses_other_names():
    with pytest.raises(SystemExit) as exc:
        main(["set-secret", "google-token"])
    assert exc.value.code == 2

"""`install --client X`: client add (few questions) -> setup --client -> optional writing (default no) -> Claude
Desktop entry with a minimal env. Offline: answers simulated, consent faked, keyring in memory, config in tmp_path."""

from __future__ import annotations

import json

import pytest

from ads_fakes import no_network  # noqa: F401  (autouse: any real HTTP request fails the test)
from clients_fakes import ACME, setup_env, write_registry
from google_ecommerce_mcp import __version__, auth, client_cli, clients, installer
from google_ecommerce_mcp.__main__ import main
from google_ecommerce_mcp.ads_config import ADS_SCOPE

SERVICE = "google-ecommerce-mcp"
A = "https://www.googleapis.com/auth/"


def token(scopes, mark):
    return json.dumps({"token": "ya29." + mark * 20, "refresh_token": "1//0" + mark * 20,
                       "token_uri": "https://oauth2.googleapis.com/token", "client_id": "cid",
                       "client_secret": "csecret", "scopes": list(scopes)})


@pytest.fixture
def env(monkeypatch, tmp_path):
    fake, path = setup_env(monkeypatch, tmp_path)
    monkeypatch.delenv("GEM_STATE_DIR", raising=False)
    monkeypatch.setattr(installer.shutil, "which", lambda name: "/bin/uvx")
    secret = tmp_path / "client_secret.json"
    secret.write_text("{}", encoding="utf-8")
    calls = []

    def fake_consent(client_secret, scopes):
        calls.append(list(scopes))
        return token(scopes, mark=str(len(calls)))

    monkeypatch.setattr(auth, "consent", fake_consent)
    cfg = tmp_path / "claude_desktop_config.json"
    return fake, path, secret, calls, cfg


def terminal(monkeypatch, *answers):
    """A real terminal (stdin and stdout) answering input() with answers, then EOF."""
    monkeypatch.setattr(installer, "_stdin_tty", lambda: True)
    monkeypatch.setattr(client_cli, "_is_tty", lambda: True)
    queue, asked = list(answers), []

    def fake_input(prompt=""):
        asked.append(prompt)
        if not queue:
            raise EOFError
        return queue.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)

    def no_getpass(prompt=""):
        asked.append(prompt)
        return ""

    monkeypatch.setattr("getpass.getpass", no_getpass)
    return asked


def entry(cfg, name="google-ecommerce"):
    return json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"][name]


def reg(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_interactive_chain_read_only_by_default(env, monkeypatch, capsys):
    fake, path, secret, calls, cfg = env
    original = {"mcpServers": {"other": {"command": "x"}}, "preferences": {"theme": "dark"}}
    cfg.write_text(json.dumps(original), encoding="utf-8")
    # minimal questions: GA4, Search Console, Merchant, GTM, Google Ads, Meta; then "enable writing?" -> Enter (no)
    asked = terminal(monkeypatch, "123456789", "sc-domain:acme.fr", "", "", "", "", "")
    code = main(["install", "--client", "acme", "--client-secret", str(secret), "--config", str(cfg)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert len([q for q in asked if "?" not in q]) == 6  # six id questions, nothing else asked about ids
    client = reg(path)["clients"]["acme"]
    assert client["accounts"] == {"ga4": {"property_id": "123456789"}, "gsc": {"site_url": "sc-domain:acme.fr"}}
    assert client["write"] == {}
    assert {u for _, u in fake.store} == {"acme/google"}  # read token only, no write token
    assert calls == [[A + "analytics.readonly", A + "webmasters.readonly", A + "tagmanager.readonly"]]
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["mcpServers"]["other"] == {"command": "x"} and data["preferences"] == {"theme": "dark"}
    assert entry(cfg)["env"] == {"GEM_CLIENTS_FILE": str(path)}  # minimal: no id, no secret
    backups = list(cfg.parent.glob("*.bak-*"))
    assert len(backups) == 1 and json.loads(backups[0].read_text()) == original
    for n in (1, 2, 3, 4):
        assert f"Step {n}/4" in out
    assert "Writing left off" in out and "client enable-write acme" in out
    assert "Next steps" not in out  # client add's own reminder is not repeated: install runs those steps
    assert "ya29." not in out and "1//0" not in out


def test_writing_yes_enables_policy_then_separate_write_token(env, monkeypatch, capsys):
    fake, path, secret, calls, cfg = env
    terminal(monkeypatch, "123456789", "", "", "", "", "",  # ids: GA4 only
             "y",      # enable writing?
             "acme")   # type the client name
    code = main(["install", "--client", "acme", "--client-secret", str(secret), "--config", str(cfg), "--pin"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert calls[0] == [A + "analytics.readonly", A + "webmasters.readonly", A + "tagmanager.readonly"]
    assert calls[1] == [A + "analytics.edit"]  # only the client's Google platform, never publish
    read_first = token(calls[0], "1")
    assert fake.store[(SERVICE, "acme/google")] == read_first  # the write consent did not replace it
    assert json.loads(fake.store[(SERVICE, "acme/google-w")])["scopes"] == [A + "analytics.edit"]
    policy = reg(path)["clients"]["acme"]["write"]["ga4"]
    assert policy["enabled"] is True and policy["account_allow"] == ["123456789"]
    assert entry(cfg)["env"] == {"GEM_CLIENT": "acme", "GEM_CLIENTS_FILE": str(path), "GEM_WRITE": "1"}
    assert entry(cfg)["args"] == [f"google-ecommerce-mcp=={__version__}"]
    assert "approve <plan_id>" in out and "acme/google-w" in out


def test_writing_yes_but_wrong_name_changes_nothing(env, monkeypatch, capsys):
    fake, path, secret, calls, cfg = env
    terminal(monkeypatch, "123456789", "", "", "", "", "", "y", "beta")
    assert main(["install", "--client", "acme", "--client-secret", str(secret), "--config", str(cfg)]) == 0
    assert reg(path)["clients"]["acme"]["write"] == {}
    assert len(calls) == 1 and "GEM_WRITE" not in entry(cfg)["env"]


def test_yes_mode_never_prompts_and_never_enables_writing(env, monkeypatch, capsys):
    fake, path, secret, calls, cfg = env

    def refuse(*_a, **_k):
        raise AssertionError("no prompt under --yes")

    monkeypatch.setattr("builtins.input", refuse)
    monkeypatch.setattr("getpass.getpass", refuse)
    code = main(["install", "--yes", "--client", "shop", "--ga4", "123456789", "--gads-customer-id", "123-456-7890",
                 "--meta-account-id", "act_4242", "--write", "--client-secret", str(secret), "--config", str(cfg),
                 "--corporate-proxy"])
    out = capsys.readouterr().out
    assert code == 0, out
    client = reg(path)["clients"]["shop"]
    assert client["accounts"]["google_ads"] == {"customer_id": "1234567890"} and client["write"] == {}
    assert {u for _, u in fake.store} == {"shop/google", "shop/gads"}
    assert calls[1] == [ADS_SCOPE]
    assert entry(cfg)["env"] == {"ADS_TRUST_ENV": "1", "GEM_CLIENTS_FILE": str(path)}
    assert "google-ecommerce-mcp set-secret --client shop meta" in out  # Meta token: hidden prompt, later
    assert "interactive terminal is required" in out


def test_existing_client_is_kept_and_flags_are_not_applied(env, monkeypatch, capsys):
    fake, path, secret, calls, cfg = env
    write_registry(path, {"acme": ACME})
    code = main(["install", "--yes", "--client", "acme", "--ga4", "999999999", "--skip-auth", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert reg(path)["clients"]["acme"]["accounts"]["ga4"] == {"property_id": "111111111"}
    assert "already registered" in out and "client add acme --replace" in out
    assert calls == [] and fake.store == {}
    assert "setup --client acme --client-secret" in out  # still to do: the read token


def test_conflict_in_claude_entry_stops_before_any_effect(env, monkeypatch, capsys):
    fake, path, secret, calls, cfg = env
    cfg.write_text(json.dumps({"mcpServers": {"google-ecommerce": {
        "command": "/bin/uvx", "args": ["google-ecommerce-mcp"], "env": {"GEM_CLIENT": "beta"}}}}), encoding="utf-8")
    before = cfg.read_text(encoding="utf-8")
    code = main(["install", "--yes", "--client", "acme", "--pin", "--ga4", "123456789", "--client-secret", str(secret),
                 "--config", str(cfg)])
    assert code == 3
    assert not path.exists() and fake.store == {} and calls == []
    assert cfg.read_text(encoding="utf-8") == before and not list(cfg.parent.glob("*.bak-*"))


def test_existing_legacy_entry_keeps_its_options(env, monkeypatch, capsys):
    fake, path, secret, calls, cfg = env
    cfg.write_text(json.dumps({"mcpServers": {"google-ecommerce": {
        "command": "/bin/uvx", "args": ["google-ecommerce-mcp"],
        "env": {"GA4_PROPERTY_ID": "123456789", "ADS_TRUST_ENV": "1"}}}}), encoding="utf-8")
    code = main(["install", "--yes", "--client", "acme", "--ga4", "123456789", "--client-secret", str(secret),
                 "--config", str(cfg)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert entry(cfg)["env"] == {"GA4_PROPERTY_ID": "123456789", "ADS_TRUST_ENV": "1", "GEM_CLIENTS_FILE": str(path)}
    assert "GA4_PROPERTY_ID in the Claude entry are now ignored" in out


def test_invalid_client_name_and_no_desktop(env, monkeypatch, capsys):
    fake, path, secret, calls, cfg = env
    assert main(["install", "--yes", "--client", "Bad Name", "--config", str(cfg)]) == 2
    assert not path.exists()
    assert main(["install", "--yes", "--client", "acme", "--ga4", "123456789", "--no-desktop",
                 "--client-secret", str(secret)]) == 0
    assert not cfg.exists() and {u for _, u in fake.store} == {"acme/google"}


# ------------------------------------------------------------------ GEM_WRITE only with a consent typed in this run
INSTALL_WRITE = "google-ecommerce-mcp install --client acme --skip-auth --write"


def test_yes_with_an_enabled_policy_never_adds_gem_write(env, monkeypatch, capsys):
    fake, path, secret, calls, cfg = env
    write_registry(path, {"acme": ACME})
    clients.set_write("acme", "ga4", True)
    code = main(["install", "--yes", "--client", "acme", "--skip-auth", "--skip-ads", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "GEM_WRITE" not in entry(cfg)["env"]
    assert entry(cfg)["args"] == ["google-ecommerce-mcp"]  # not pinned: writing is not on for this server
    assert "GEM_WRITE=1 is NOT added" in out and INSTALL_WRITE in out


def test_declined_consent_with_an_enabled_policy_never_adds_gem_write(env, monkeypatch, capsys):
    fake, path, secret, calls, cfg = env
    write_registry(path, {"acme": ACME})
    clients.set_write("acme", "ga4", True)
    terminal(monkeypatch, "")  # Google properties: Enter = no
    code = main(["install", "--client", "acme", "--skip-auth", "--skip-ads", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "GEM_WRITE" not in entry(cfg)["env"] and INSTALL_WRITE in out


def test_consent_typed_in_this_run_adds_gem_write_and_pins_the_version(env, monkeypatch, capsys):
    fake, path, secret, calls, cfg = env
    write_registry(path, {"solo": {"accounts": {"ga4": {"property_id": "123456789"}}}})
    cfg.write_text(json.dumps({"mcpServers": {"google-ecommerce": {
        "command": "/bin/uvx", "args": ["google-ecommerce-mcp"], "env": {"GEM_CLIENTS_FILE": str(path)}}}}),
        encoding="utf-8")
    terminal(monkeypatch, "y", "solo")
    code = main(["install", "--client", "solo", "--skip-auth", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert entry(cfg)["env"] == {"GEM_CLIENTS_FILE": str(path), "GEM_WRITE": "1"}
    assert entry(cfg)["args"] == [f"google-ecommerce-mcp=={__version__}"]  # existing entry pinned, no --force
    assert f"-- uvx google-ecommerce-mcp=={__version__}" in out
    # a later read-only run keeps the pin and GEM_WRITE (never removed silently)
    assert main(["install", "--yes", "--client", "solo", "--skip-auth", "--config", str(cfg)]) == 0
    assert entry(cfg)["args"] == [f"google-ecommerce-mcp=={__version__}"] and entry(cfg)["env"]["GEM_WRITE"] == "1"


def test_writing_is_asked_family_by_family_and_spending_only_with_write(env, monkeypatch, capsys):
    fake, path, secret, calls, cfg = env
    write_registry(path, {"acme": ACME})
    # without --write: one question only (the Google properties), the spending groups are not offered
    asked = terminal(monkeypatch, "")
    assert main(["install", "--client", "acme", "--skip-auth", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    questions = [q for q in asked if "Enable writing" in q]
    assert len(questions) == 1 and "Google properties" in questions[0]
    assert "not offered without --write" in out and "Google Ads, Meta" in out
    # with --write: one question and one typed name PER group
    asked = terminal(monkeypatch, "y", "acme",   # Google properties: yes, name typed
                     "n",                        # Google Ads: no
                     "y", "beta",                # Meta: yes, but the wrong name
                     "y", "acme")                # ChatGPT Ads: yes, name typed
    assert main(["install", "--client", "acme", "--skip-auth", "--write", "--config", str(cfg)]) == 0
    questions = [q for q in asked if "Enable writing" in q]
    assert len(questions) == 4
    assert [q for q in asked if "Type the client name" in q] and len([q for q in asked if "Type the client" in q]) == 3
    write = reg(path)["clients"]["acme"]["write"]
    on = sorted(f for f, p in write.items() if p["enabled"])
    assert on == ["ga4", "gsc", "gtm", "merchant", "openai_ads"]
    assert entry(cfg)["env"]["GEM_WRITE"] == "1"
    assert calls == []  # --skip-auth: the write token is left to do, no consent


def test_write_token_in_install_needs_an_explicit_client_file(env, monkeypatch, tmp_path, capsys):
    """A client_secret*.json found automatically (and confirmed) may serve the read token, never the write one."""
    fake, path, _, calls, cfg = env
    write_registry(path, {"solo": {"accounts": {"ga4": {"property_id": "123456789"}}}})
    work = tmp_path / "work"
    work.mkdir()
    (work / "client_secret_1.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(work)
    asked = terminal(monkeypatch, "y",       # use the found file for the read token
                     "y", "solo",            # Google properties: write on
                     "")                     # path for the WRITE token: Enter = none
    assert main(["install", "--client", "solo", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert len(calls) == 1 and A + "analytics.readonly" in calls[0]  # read token only
    assert {u for _, u in fake.store} == {"solo/google"}
    assert any("WRITE token" in q for q in asked)
    assert "setup --client solo --write --client-secret" in out


def test_shell_paths_must_be_absolute_to_reach_the_claude_entry(env, monkeypatch, tmp_path, capsys):
    fake, path, secret, calls, cfg = env
    monkeypatch.setenv("GEM_STATE_DIR", "relative/state")
    code = main(["install", "--yes", "--client", "acme", "--ga4", "123456789", "--skip-auth", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "GEM_STATE_DIR" not in entry(cfg)["env"] and "not an absolute path" in out
    state = tmp_path / "state" / "deep"
    monkeypatch.setenv("GEM_STATE_DIR", str(state))
    assert main(["install", "--yes", "--client", "acme", "--skip-auth", "--config", str(cfg)]) == 0
    assert entry(cfg)["env"]["GEM_STATE_DIR"] == str(state)


def test_shell_path_problem_rules(tmp_path):
    assert installer._shell_path_problem("clients.json", False) == "not an absolute path"
    assert installer._shell_path_problem(str(tmp_path / "a" / "b" / "clients.json"), False) == ""
    assert installer._shell_path_problem(str(tmp_path), False) == "exists but is not a file"
    file = tmp_path / "f.txt"
    file.write_text("x", encoding="utf-8")
    assert installer._shell_path_problem(str(file), True) == "exists but is not a folder"
    assert installer._shell_path_problem(str(file / "sub"), True) != ""  # parent is a file
    assert installer._shell_path_problem(str(tmp_path / "new-state"), True) == ""


def test_legacy_install_without_client_is_unchanged(env, monkeypatch, capsys):
    fake, path, secret, calls, cfg = env
    seen = []
    monkeypatch.setattr(auth, "run_setup", lambda f, t, s: seen.append(s) or "operating system keyring")
    from google_ecommerce_mcp import server

    monkeypatch.setattr(server, "check", lambda: {"token": "ok"})
    code = main(["install", "--yes", "--ga4", "123456789", "--client-secret", str(secret), "--config", str(cfg)])
    assert code == 0
    assert entry(cfg)["env"] == {"GA4_PROPERTY_ID": "123456789"}
    assert not path.exists() and calls == [] and len(seen) == 1

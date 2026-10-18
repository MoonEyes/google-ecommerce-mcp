"""`setup --client X [--write]`, `setup-ads --client X`, `set-secret --client X`, `quickstart`: offline.

The browser consent (auth.consent) is replaced by a fake that records the scopes and returns a token JSON; the
keyring is the in-memory FakeKeyring of clients_fakes; no network (ads_fakes.no_network is autouse here)."""

from __future__ import annotations

import argparse
import json
import re

import pytest

from ads_fakes import no_network  # noqa: F401  (autouse: any real HTTP request fails the test)
from clients_fakes import ACME, BETA, setup_env, write_registry
from google_ecommerce_mcp import auth, client_cli
from google_ecommerce_mcp import setup_guide as guide
from google_ecommerce_mcp.__main__ import main
from google_ecommerce_mcp.ads_config import ADS_SCOPE

SERVICE = "google-ecommerce-mcp"
A = "https://www.googleapis.com/auth/"
WRITE_SCOPES = [A + "analytics.edit", A + "tagmanager.edit.containers", A + "webmasters", A + "content"]
FORBIDDEN = (A + "tagmanager.publish", A + "tagmanager.edit.containerversions")


def token(scopes, mark="t"):
    return json.dumps({"token": "ya29." + mark * 20, "refresh_token": "1//0" + mark * 20,
                       "token_uri": "https://oauth2.googleapis.com/token", "client_id": "cid",
                       "client_secret": "csecret", "scopes": list(scopes)})


@pytest.fixture
def env(monkeypatch, tmp_path):
    fake, path = setup_env(monkeypatch, tmp_path)
    secret = tmp_path / "client_secret.json"
    secret.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "csecret"}}), encoding="utf-8")
    calls = []

    def fake_consent(client_secret, scopes):
        calls.append((client_secret, list(scopes)))
        return token(scopes, mark=str(len(calls)))

    monkeypatch.setattr(auth, "consent", fake_consent)
    return fake, path, secret, calls


def users(fake):
    return {user for (_, user) in fake.store}


# ------------------------------------------------------------------ setup --client (read token)
def test_setup_client_stores_only_in_that_clients_read_entry(env, capsys):
    fake, path, secret, calls = env
    write_registry(path, {"acme": ACME, "beta": BETA})
    assert main(["setup", "--client", "acme", "--client-secret", str(secret)]) == 0
    assert users(fake) == {"acme/google"}
    stored = json.loads(fake.store[(SERVICE, "acme/google")])
    assert sorted(stored["scopes"]) == sorted(calls[0][1])
    # read-only scopes, plus content because acme has a Merchant Center id; never a write scope
    assert set(calls[0][1]) == {A + "analytics.readonly", A + "webmasters.readonly", A + "tagmanager.readonly",
                                A + "content"}
    out = capsys.readouterr().out
    assert "acme/google" in out and "doctor --client acme" in out
    assert "ya29." not in out and "1//0" not in out


def test_setup_client_without_merchant_asks_no_content_scope(env):
    fake, path, secret, calls = env
    write_registry(path, {"solo": {"accounts": {"ga4": {"property_id": "123456789"}}}})
    assert main(["setup", "--client", "solo", "--client-secret", str(secret)]) == 0
    assert A + "content" not in calls[0][1]
    assert users(fake) == {"solo/google"}


def test_setup_client_uses_the_registry_entry_name_of_default(env):
    """The client "default" created from legacy mode keeps the legacy keyring entries: setup writes there."""
    fake, path, secret, calls = env
    write_registry(path, {"default": {"accounts": {"ga4": {"property_id": "123456789"}},
                                      "secrets": {"google_read": "oauth-token"}}})
    assert main(["setup", "--client", "default", "--client-secret", str(secret)]) == 0
    assert users(fake) == {"oauth-token"}


def test_setup_without_client_uses_the_only_registered_client(env):
    fake, path, secret, _ = env
    write_registry(path, {"acme": ACME})
    assert main(["setup", "--client-secret", str(secret)]) == 0
    assert users(fake) == {"acme/google"}


def test_setup_without_client_and_two_clients_is_refused(env, capsys):
    fake, path, secret, calls = env
    write_registry(path, {"acme": ACME, "beta": BETA})
    assert main(["setup", "--client-secret", str(secret)]) == 1
    assert calls == [] and fake.store == {}
    assert "client_required" in capsys.readouterr().err


def test_setup_unknown_client_stores_nothing(env, capsys):
    fake, path, secret, calls = env
    write_registry(path, {"acme": ACME})
    assert main(["setup", "--client", "zeta", "--client-secret", str(secret)]) == 1
    assert calls == [] and fake.store == {}
    assert "unknown_client" in capsys.readouterr().err


def test_setup_client_refuses_token_file(env, tmp_path):
    fake, path, secret, calls = env
    write_registry(path, {"acme": ACME})
    assert main(["setup", "--client", "acme", "--client-secret", str(secret),
                 "--token-file", str(tmp_path / "t.json")]) == 2
    assert calls == [] and fake.store == {}


def tty(monkeypatch, *answers):
    """stdin and stdout are a terminal; input() answers in order, then EOF; returns the prompts."""
    monkeypatch.setattr(client_cli, "_is_tty", lambda: True)
    queue, asked = list(answers), []

    def fake_input(prompt=""):
        asked.append(prompt)
        if not queue:
            raise EOFError
        return queue.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)
    return asked


def test_setup_client_uses_a_found_client_secret_file_only_after_confirmation(env, monkeypatch, tmp_path, capsys):
    fake, path, _, calls = env
    write_registry(path, {"acme": ACME})
    work = tmp_path / "work"
    work.mkdir()
    (work / "client_secret_123.apps.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(work)
    asked = tty(monkeypatch, "y")
    assert main(["setup", "--client", "acme"]) == 0
    assert calls[0][0].endswith("client_secret_123.apps.json")
    assert any("client_secret_123.apps.json" in q and "[y/N]" in q for q in asked)
    assert "client_secret_123.apps.json" in capsys.readouterr().out


def test_found_client_secret_file_declined_or_not_interactive_is_refused(env, monkeypatch, tmp_path, capsys):
    fake, path, _, calls = env
    write_registry(path, {"acme": ACME})
    work = tmp_path / "work"
    work.mkdir()
    (work / "client_secret_123.apps.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(work)
    tty(monkeypatch, "")  # Enter = no
    assert main(["setup", "--client", "acme"]) == 2
    assert calls == [] and fake.store == {}
    monkeypatch.setattr(client_cli, "_is_tty", lambda: False)
    assert main(["setup", "--client", "acme"]) == 2
    err = capsys.readouterr().err
    assert calls == [] and fake.store == {}
    assert "--client-secret" in err and "client_secret_123.apps.json" in err and "interactive terminal" in err


def test_write_never_discovers_the_client_secret_file(env, monkeypatch, tmp_path, capsys):
    fake, path, _, calls = env
    write_registry(path, {"acme": ACME})
    work = tmp_path / "work"
    work.mkdir()
    (work / "client_secret_123.apps.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(work)
    asked = tty(monkeypatch, "y", "y")
    assert main(["setup", "--client", "acme", "--write"]) == 2
    assert calls == [] and fake.store == {} and asked == []
    assert "explicit --client-secret" in capsys.readouterr().err
    path_found, why = auth.find_client_secret(None, [work], write=True, confirm=lambda p: True)
    assert path_found is None and "explicit" in why


def test_setup_client_without_any_client_secret_file(env, monkeypatch, tmp_path, capsys):
    fake, path, _, calls = env
    write_registry(path, {"acme": ACME})
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    monkeypatch.setattr(auth.Path, "home", lambda: empty)
    assert main(["setup", "--client", "acme"]) == 2
    assert calls == [] and fake.store == {}
    assert "--client-secret" in capsys.readouterr().err


def test_find_client_secret_refuses_two_candidates(tmp_path):
    (tmp_path / "client_secret_a.json").write_text("{}")
    (tmp_path / "client_secret_b.json").write_text("{}")
    path, why = auth.find_client_secret(None, [tmp_path])
    assert path is None and "2 client_secret" in why


def test_setup_warns_when_google_grants_fewer_scopes(env, monkeypatch, capsys):
    fake, path, secret, _ = env
    write_registry(path, {"acme": ACME})
    monkeypatch.setattr(auth, "consent", lambda f, scopes: token([A + "analytics.readonly"]))
    assert main(["setup", "--client", "acme", "--client-secret", str(secret)]) == 0
    out = capsys.readouterr().out
    assert "did not grant" in out and "webmasters.readonly" in out


def test_consent_failure_stores_nothing(env, monkeypatch, capsys):
    fake, path, secret, _ = env
    write_registry(path, {"acme": ACME})

    def refused(*_a):
        raise RuntimeError("access_denied with secret ya29.zzzzzzzzzzzzzzzz")

    monkeypatch.setattr(auth, "consent", refused)
    assert main(["setup", "--client", "acme", "--client-secret", str(secret)]) == 1
    err = capsys.readouterr().err
    assert fake.store == {} and "RuntimeError" in err and "ya29." not in err


def test_read_token_with_merchant_is_announced_as_not_read_only(env, capsys):
    fake, path, secret, _ = env
    write_registry(path, {"acme": ACME})  # acme has a Merchant Center id: the read token gets content
    assert main(["setup", "--client", "acme", "--client-secret", str(secret)]) == 0
    out = capsys.readouterr().out
    assert "WARNING: the READ token" in out and "content (Merchant Center)" in out and "CAN change" in out
    assert "NOT read-only" in out and "(read-only)" not in out


def test_read_token_with_indexing_is_announced_and_plain_read_is_not(env, capsys):
    fake, path, secret, calls = env
    write_registry(path, {"solo": {"accounts": {"ga4": {"property_id": "123456789"}}}})
    assert main(["setup", "--client", "solo", "--client-secret", str(secret), "--with-indexing"]) == 0
    out = capsys.readouterr().out
    assert "indexing (Indexing API)" in out and "NOT read-only" in out
    assert main(["setup", "--client", "solo", "--client-secret", str(secret)]) == 0
    out = capsys.readouterr().out
    assert "WARNING" not in out and "read-only" in out


@pytest.mark.parametrize("granted", [
    [A + "analytics.readonly", A + "webmasters.readonly", A + "tagmanager.readonly", A + "content",
     A + "tagmanager.publish"],                                    # forbidden scope
    [A + "analytics.readonly", A + "analytics.edit"],              # a scope that was not requested
])
def test_token_with_forbidden_or_extra_scope_is_never_stored(env, monkeypatch, capsys, granted):
    fake, path, secret, _ = env
    write_registry(path, {"acme": ACME})
    monkeypatch.setattr(auth, "consent", lambda f, scopes: token(granted))
    assert main(["setup", "--client", "acme", "--client-secret", str(secret)]) == 2
    assert fake.store == {}
    err = capsys.readouterr().err
    assert "not stored" in err and "ya29." not in err


def test_gads_token_with_extra_scope_is_never_stored(env, monkeypatch, capsys):
    fake, path, secret, _ = env
    write_registry(path, {"acme": ACME})
    monkeypatch.setattr(auth, "consent", lambda f, scopes: token([ADS_SCOPE, A + "content"]))
    assert main(["setup-ads", "--client", "acme", "--client-secret", str(secret)]) == 2
    assert fake.store == {} and "not requested" in capsys.readouterr().err


# ------------------------------------------------------------------ setup --client --write
def test_write_token_is_separate_and_never_replaces_the_read_token(env, monkeypatch, capsys):
    fake, path, secret, calls = env
    write_registry(path, {"acme": ACME, "beta": BETA})
    fake.store[(SERVICE, "acme/google")] = "READ-TOKEN-UNCHANGED"
    tty(monkeypatch, "y")
    assert main(["setup", "--client", "acme", "--write", "--client-secret", str(secret)]) == 0
    assert fake.store[(SERVICE, "acme/google")] == "READ-TOKEN-UNCHANGED"
    assert users(fake) == {"acme/google", "acme/google-w"}
    assert calls[0][1] == WRITE_SCOPES
    assert not set(calls[0][1]) & set(FORBIDDEN)
    out = capsys.readouterr().out
    assert "WARNING" in out and "acme/google-w" in out and "never tagmanager.publish" in out


def test_write_scopes_follow_the_clients_google_platforms(env, monkeypatch):
    fake, path, secret, calls = env
    write_registry(path, {"solo": {"accounts": {"ga4": {"property_id": "123456789"},
                                                "gsc": {"site_url": "sc-domain:solo.fr"}}}})
    tty(monkeypatch, "y")
    assert main(["setup", "--client", "solo", "--write", "--client-secret", str(secret)]) == 0
    assert calls[0][1] == [A + "analytics.edit", A + "webmasters"]
    assert users(fake) == {"solo/google-w"}


def test_write_asks_confirmation_before_widening_rights(env, monkeypatch, capsys):
    fake, path, secret, calls = env
    write_registry(path, {"acme": ACME})
    tty(monkeypatch, "n")
    assert main(["setup", "--client", "acme", "--write", "--client-secret", str(secret)]) == 1
    assert calls == [] and fake.store == {}
    assert "Cancelled" in capsys.readouterr().out
    tty(monkeypatch, "y")
    assert main(["setup", "--client", "acme", "--write", "--client-secret", str(secret)]) == 0
    assert users(fake) == {"acme/google-w"}


def test_write_yes_never_skips_the_confirmation(env, monkeypatch, capsys):
    """--yes does not suppress the --write confirmation: Enter (no) still cancels."""
    fake, path, secret, calls = env
    write_registry(path, {"acme": ACME})
    asked = tty(monkeypatch, "")
    assert main(["setup", "--client", "acme", "--write", "--yes", "--client-secret", str(secret)]) == 1
    assert calls == [] and fake.store == {} and len(asked) == 1
    assert "Cancelled" in capsys.readouterr().out


@pytest.mark.parametrize("stdin_tty, stdout_tty", [(False, True), (True, False), (False, False)])
def test_write_needs_stdin_and_stdout_terminals(env, monkeypatch, capsys, stdin_tty, stdout_tty):
    """Same helper as enable-write (client_cli._is_tty): a pipe on either side refuses, even with --yes."""
    fake, path, secret, calls = env
    write_registry(path, {"acme": ACME})

    class Stream:
        def __init__(self, real, flag):
            self.real, self.flag = real, flag

        def isatty(self):
            return self.flag

        def __getattr__(self, name):
            return getattr(self.real, name)

    import sys

    monkeypatch.setattr(sys, "stdin", Stream(sys.stdin, stdin_tty))
    monkeypatch.setattr(sys, "stdout", Stream(sys.stdout, stdout_tty))
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert main(["setup", "--client", "acme", "--write", "--yes", "--client-secret", str(secret)]) == 1
    assert calls == [] and fake.store == {}
    assert "interactive terminal is required" in capsys.readouterr().err


def test_write_warning_in_french(env, monkeypatch, capsys):
    fake, path, secret, _ = env
    write_registry(path, {"acme": ACME})
    monkeypatch.setenv("GEM_LANG", "fr")
    tty(monkeypatch, "o")
    assert main(["setup", "--client", "acme", "--write", "--client-secret", str(secret)]) == 0
    out = capsys.readouterr().out
    assert "ATTENTION" in out and "SECOND jeton" in out and "acme/google-w" in out


def test_write_refused_in_legacy_mode_and_without_google_id(env, capsys):
    fake, path, secret, calls = env
    assert main(["setup", "--write", "--client-secret", str(secret)]) == 2  # no registry
    assert main(["setup", "--client", "default", "--write", "--yes", "--client-secret", str(secret)]) == 2
    write_registry(path, {"ads": {"accounts": {"meta": {"ad_account_id": "123456"}}}})
    assert main(["setup", "--client", "ads", "--write", "--yes", "--client-secret", str(secret)]) == 2
    assert calls == [] and fake.store == {}


def test_forbidden_scopes_are_refused_by_run_client_setup(env):
    fake, path, secret, calls = env
    write_registry(path, {"acme": ACME})
    ctx = client_cli._context("acme")
    with pytest.raises(auth.SetupRefused):
        auth.run_client_setup(ctx, str(secret), write=True, scopes=[A + "tagmanager.publish"])
    assert calls == [] and fake.store == {}
    assert auth.write_scopes_for(["ga4", "gtm", "gsc", "merchant"]) == WRITE_SCOPES
    assert not set(auth.GOOGLE_WRITE_SCOPES) & set(FORBIDDEN)


def test_legacy_setup_without_registry_is_unchanged(env, monkeypatch, tmp_path):
    """No registry, no --client: today's run_setup(client_secret, GOOGLE_TOKEN_FILE, scopes)."""
    fake, path, secret, calls = env
    seen = []
    monkeypatch.setattr(auth, "run_setup", lambda f, t, s: seen.append((f, t, s)) or "operating system keyring")
    monkeypatch.setenv("GOOGLE_TOKEN_FILE", str(tmp_path / "tok.json"))
    assert main(["setup", "--client-secret", str(secret), "--with-merchant"]) is None
    assert seen == [(str(secret), str(tmp_path / "tok.json"), auth.scopes_for(merchant=True))]
    with pytest.raises(SystemExit) as exc:
        main(["setup"])
    assert exc.value.code == 2


# ------------------------------------------------------------------ setup-ads --client / set-secret --client
def test_setup_ads_client_stores_only_the_adwords_token_of_that_client(env, capsys):
    fake, path, secret, calls = env
    write_registry(path, {"acme": ACME, "beta": BETA})
    fake.store[(SERVICE, "acme/google")] = "READ"
    assert main(["setup-ads", "--client", "beta", "--client-secret", str(secret)]) == 0
    assert calls[0][1] == [ADS_SCOPE]
    assert users(fake) == {"acme/google", "beta/gads"} and fake.store[(SERVICE, "acme/google")] == "READ"
    assert "doctor --client beta --only gads" in capsys.readouterr().out


def test_set_secret_client_is_an_alias_of_client_set_secret(env, monkeypatch, capsys):
    fake, path, _, _ = env
    write_registry(path, {"acme": ACME, "beta": BETA})
    monkeypatch.setattr(client_cli, "_is_tty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "EAAsecretvalue123456")
    assert main(["set-secret", "--client", "beta", "meta"]) == 0
    assert users(fake) == {"beta/meta"}
    assert main(["set-secret", "--client", "acme", "openai"]) == 0
    assert users(fake) == {"beta/meta", "acme/openai"}
    out = capsys.readouterr().out
    assert "EAAsecret" not in out and "doctor --client acme --only openai" in out


def test_set_secret_client_needs_a_terminal(env, monkeypatch):
    fake, path, _, _ = env
    write_registry(path, {"acme": ACME})
    monkeypatch.setattr(client_cli, "_is_tty", lambda: False)
    assert main(["set-secret", "--client", "acme", "meta"]) == 1
    assert fake.store == {}


# ------------------------------------------------------------------ quickstart and the shared steps
def test_quickstart_prints_eight_lines_and_writes_nothing(env, capsys):
    fake, path, _, calls = env
    assert main(["quickstart"]) == 0
    lines = capsys.readouterr().out.rstrip("\n").split("\n")
    assert len(lines) == 8
    text = "\n".join(lines)
    for command in ("client add acme", "setup --client acme --client-secret", "doctor --client acme",
                    "install --client acme", "install --client acme --skip-auth --write",
                    "setup --client acme --write"):
        assert command in text
    assert not path.exists() and fake.store == {} and fake.reads == [] and calls == []


def test_quickstart_uses_the_given_or_the_only_client(env, capsys):
    fake, path, _, _ = env
    assert main(["quickstart", "--client", "shop-fr"]) == 0
    assert "client add shop-fr" in capsys.readouterr().out
    write_registry(path, {"beta": BETA})
    assert main(["quickstart"]) == 0
    assert "setup --client beta" in capsys.readouterr().out
    assert main(["quickstart", "--client", "Bad Name"]) == 2


def _parses(command, monkeypatch):
    words = re.sub(r"<[^>]+>", "123", command).split()[1:]
    real = argparse.ArgumentParser.parse_args

    def parse(self, args=None, namespace=None):
        real(self, args, namespace)
        raise SystemExit(0)

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", parse)
    try:
        main(words)
    except SystemExit as exc:
        return exc.code == 0
    finally:
        monkeypatch.setattr(argparse.ArgumentParser, "parse_args", real)
    return False


def test_every_multi_client_command_parses(monkeypatch):
    steps = guide.client_steps() + guide.client_write_steps()
    commands = [s.command for s in steps if s.command]
    commands += [line.split(". ", 1)[1].split("   (")[0] for line in guide.quickstart_lines("acme")
                 if re.match(r"\s+\d\. ", line)]
    commands += [guide.client_secret_command("acme", "openai"), guide.client_doctor_command("acme", live=True)]
    assert len(commands) >= 15
    for command in commands:
        assert command.startswith("google-ecommerce-mcp "), command
        assert _parses(command, monkeypatch), command


def test_multi_client_steps_are_ordered_and_secret_free():
    steps = guide.client_steps("acme")
    assert [s.id for s in steps] == ["client-oauth-client", "client-add", "client-setup", "client-ads",
                                     "client-check", "client-claude"]
    write = guide.client_write_steps("acme")
    assert [s.id for s in write] == ["write-enable", "write-token", "write-server", "write-approve", "write-check"]
    for step in steps + write:
        assert step.title and step.prerequisite and step.verify
        assert "EAA" not in step.command and "sk-" not in step.command
        assert not re.search(r"--(token|key|secret)\b", step.command.replace("--client-secret", ""))

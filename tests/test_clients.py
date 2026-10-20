"""Client registry, legacy mode, pinning, file permissions and the `client` CLI. Offline: fake keyring, tmp files."""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import sys
from pathlib import Path

import pytest

from clients_fakes import ACME, BETA, setup_env, write_registry
from google_ecommerce_mcp import client_cli, clients, paths, secrets_store
from google_ecommerce_mcp.__main__ import main
from google_ecommerce_mcp.clients import ClientError, WritePolicy, resolve

PKG = Path(clients.__file__).parent


@pytest.fixture
def env(monkeypatch, tmp_path):
    fake, path = setup_env(monkeypatch, tmp_path)
    return fake, path


def answers(monkeypatch, *values):
    """Feed input() with values; any extra question gets EOF (= default)."""
    queue = list(values)
    asked = []

    def fake_input(prompt=""):
        asked.append(prompt)
        if not queue:
            raise EOFError
        return queue.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)
    return asked


def no_getpass(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("no secret may be asked")

    monkeypatch.setattr("getpass.getpass", refuse)


def tty(monkeypatch, value=True):
    """The terminal check is a single injection point; production keeps the real isatty test."""
    monkeypatch.setattr(client_cli, "_is_tty", lambda: value)


def reg(path):
    return json.loads(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ contracts
def test_client_error_shape():
    exc = ClientError("client_required", "where and cause.", "do this")
    assert (exc.code, exc.detail, exc.fix) == ("client_required", "where and cause.", "do this")
    assert exc.to_dict() == {"error": "client_required", "detail": "where and cause.", "fix": "do this"}
    assert "Fix: do this" in str(exc)


def test_write_policy_defaults_are_closed():
    p = WritePolicy()
    assert (p.enabled, p.account_allow, p.max_daily_budget_micros, p.cap_currency) == (False, frozenset(), None, None)
    assert (p.max_increase_pct, p.max_ops_per_plan, p.token_ttl_s) == (20.0, 25, 900)
    assert not (p.allow_activate or p.allow_delete or p.allow_irreversible or p.allow_pii)
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.enabled = True  # type: ignore[misc]


# ------------------------------------------------------------------ legacy mode
def test_legacy_context_from_current_variables(env, monkeypatch):
    monkeypatch.setenv("GA4_PROPERTY_ID", "123456789")
    monkeypatch.setenv("GSC_SITE_URL", "sc-domain:example.com")
    monkeypatch.setenv("MERCHANT_ACCOUNT_ID", "42")
    monkeypatch.setenv("GTM_CONTAINER_ID", "GTM-ABCDEF")
    monkeypatch.setenv("GADS_CUSTOMER_ID", "123-456-7890")
    monkeypatch.setenv("GADS_LOGIN_CUSTOMER_ID", "999-000-1111")
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "act_555")
    ctx = resolve(None)
    assert (ctx.client_id, ctx.legacy, ctx.sandbox) == ("default", True, False)
    assert ctx.account("ga4", "property_id") == "123456789"
    assert ctx.account("gsc", "site_url") == "sc-domain:example.com"
    assert ctx.account("merchant", "account_id") == "42"
    assert ctx.account("gtm", "public_id") == "GTM-ABCDEF"
    assert ctx.account("google_ads", "customer_id") == "1234567890"
    assert ctx.account("google_ads", "login_customer_id") == "9990001111"
    assert ctx.account("meta", "ad_account_id") == "555"
    assert resolve("default") == ctx
    assert ctx.policy("google_ads") == WritePolicy()


def test_legacy_reads_environment_at_each_call(env, monkeypatch):
    assert resolve(None).accounts("ga4") == {}
    monkeypatch.setenv("GA4_PROPERTY_ID", "1")
    assert resolve(None).account("ga4", "property_id") == "1"


def test_legacy_missing_id_names_the_variable(env):
    with pytest.raises(ClientError) as info:
        resolve(None).account("ga4", "property_id")
    assert info.value.code == "platform_not_configured"
    assert "GA4_PROPERTY_ID" in info.value.fix


def test_legacy_unknown_client(env):
    with pytest.raises(ClientError) as info:
        resolve("acme")
    assert info.value.code == "unknown_client"
    assert "client add acme" in info.value.fix


def test_legacy_owns_keeps_current_behaviour(env, monkeypatch):
    monkeypatch.setenv("GADS_CUSTOMER_ID", "1234567890")
    ctx = resolve(None)
    assert ctx.owns("google_ads", "999-999-9999")  # tools accepted explicit ids before: unchanged
    assert not ctx.owns("google_ads", "not-an-id")
    assert not ctx.owns("unknown", "1")


def test_legacy_secrets_use_current_entries(env, monkeypatch, tmp_path):
    fake, _ = env
    svc = "google-ecommerce-mcp"
    fake.store.update({(svc, "oauth-token"): '{"g": 1}', (svc, "oauth-token-ads"): '{"a": 1}',
                       (svc, "meta-access-token"): "meta-kr", (svc, "openai-ads-key"): "oa-kr",
                       (svc, "default/google-w"): '{"w": 1}'})
    ctx = resolve(None)
    assert ctx.secret("google") == '{"g": 1}'
    assert ctx.secret("gads") == '{"a": 1}'
    assert ctx.secret("meta") == "meta-kr"
    assert ctx.secret("openai") == "oa-kr"
    assert ctx.secret("google", write=True) == '{"w": 1}'
    monkeypatch.setenv("META_ACCESS_TOKEN", "meta-env")
    monkeypatch.setenv("PAGESPEED_API_KEY", "ps-env")
    assert ctx.secret("meta") == "meta-env"
    assert ctx.secret("pagespeed") == "ps-env"
    token = tmp_path / "tok.json"
    token.write_text('{"file": 1}', encoding="utf-8")
    monkeypatch.setenv("GOOGLE_TOKEN_FILE", str(token))
    assert ctx.secret("google") == '{"file": 1}'
    with pytest.raises(ValueError):
        ctx.secret("password")


def test_secret_without_keyring_backend(env, monkeypatch):
    class Broken:
        @staticmethod
        def get_password(*_):
            raise RuntimeError("no backend")

    monkeypatch.setitem(sys.modules, "keyring", Broken)
    assert resolve(None).secret("meta") is None


# ------------------------------------------------------------------ registry
def test_one_client_is_the_implicit_default_and_env_ids_are_ignored(env, monkeypatch):
    fake, path = env
    write_registry(path, {"acme": ACME})
    monkeypatch.setenv("GA4_PROPERTY_ID", "999")
    monkeypatch.setenv("META_ACCESS_TOKEN", "legacy-token")
    ctx = resolve(None)
    assert (ctx.client_id, ctx.legacy, ctx.label, ctx.sandbox) == ("acme", False, "Acme SA", True)
    assert ctx.account("ga4", "property_id") == "111111111"
    assert ctx.account("meta", "ad_account_id") == "1111"  # act_ normalized
    assert ctx.secret("meta") is None  # META_ACCESS_TOKEN never feeds a registry client
    fake.store[("google-ecommerce-mcp", "acme/meta")] = "acme-meta"
    assert ctx.secret("meta") == "acme-meta"
    monkeypatch.setenv("GEM_SECRET_ACME_META", "acme-env")
    assert ctx.secret("meta") == "acme-env"


def test_two_clients_require_client_id(env):
    _, path = env
    write_registry(path, {"acme": ACME, "beta": BETA})
    with pytest.raises(ClientError) as info:
        resolve(None)
    assert info.value.code == "client_required"
    assert "acme" in info.value.detail and "beta" in info.value.detail
    assert resolve("beta").account("ga4", "property_id") == "222222222"
    assert resolve(" ACME ").client_id == "acme"
    with pytest.raises(ClientError) as info:
        resolve("gamma")
    assert info.value.code == "unknown_client"


def test_empty_registry(env):
    _, path = env
    write_registry(path, {})
    with pytest.raises(ClientError) as info:
        resolve(None)
    assert info.value.code == "unknown_client"


def test_registry_missing_platform(env):
    _, path = env
    write_registry(path, {"solo": {"accounts": {"ga4": {"property_id": "1"}}}})
    with pytest.raises(ClientError) as info:
        resolve(None).account("merchant", "account_id")
    assert info.value.code == "platform_not_configured"
    assert "client add solo --replace" in info.value.fix


def test_pinning(env, monkeypatch):
    _, path = env
    write_registry(path, {"acme": ACME, "beta": BETA})
    monkeypatch.setenv("GEM_CLIENT", "acme")
    assert resolve(None).client_id == "acme"
    assert resolve("acme").client_id == "acme"
    with pytest.raises(ClientError) as info:
        resolve("beta")
    assert info.value.code == "pinned_client"
    monkeypatch.setenv("GEM_CLIENT", "gamma")
    with pytest.raises(ClientError) as info:
        resolve(None)
    assert info.value.code == "unknown_client"


def test_pinning_in_legacy_mode(env, monkeypatch):
    monkeypatch.setenv("GEM_CLIENT", "default")
    assert resolve(None).legacy
    monkeypatch.setenv("GEM_CLIENT", "acme")
    with pytest.raises(ClientError) as info:
        resolve(None)
    assert info.value.code == "unknown_client"


def test_duplicate_json_key_is_refused_not_silently_overwritten(env):
    _, path = env
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"version": 1, "clients": {"acme": {"label": "a", "label": "b"}}}', encoding="utf-8")
    with pytest.raises(ClientError) as info:
        resolve(None)
    assert info.value.code == "registry_invalid" and "duplicate key 'label'" in info.value.detail


@pytest.mark.parametrize("entry, needle", [
    ({"accounts": {}, "token": "x"}, "unknown field"),
    ({"secrets": {"meta": "EAAsecretvalue1234567890"}}, "keyring entry name"),
    ({"secrets": {"meta": "beta/meta"}}, "keyring entry name"),
    ({"secrets": {"meta": "meta-access-token"}}, "keyring entry name"),  # legacy entries: client "default" only
    ({"secrets": {"password": "acme/x"}}, "unknown"),
    ({"accounts": {"ga4": {"property_id": "abc"}}}, "digits"),
    ({"accounts": {"ga4": {"api_key": "1"}}}, "unknown field"),
    ({"accounts": {"tiktok": {}}}, "unknown platform"),
    ({"label": "sk-proj-abcdefghijklmnop"}, "secret"),
    ({"write": {"google_ads": {"enabled": "yes"}}}, "true or false"),
    ({"write": {"google_ads": {"account_allow": ["12"]}}}, "account_allow"),
    ({"write": {"google_ads": {"token_ttl_s": 7200}}}, "token_ttl_s"),
    ({"write": {"google_ads": {"secret": "x"}}}, "unknown field"),
    ({"write": {"bing": {}}}, "unknown write family"),
])
def test_registry_is_validated_and_fails_closed(env, monkeypatch, entry, needle):
    _, path = env
    write_registry(path, {"acme": entry})
    monkeypatch.setenv("GA4_PROPERTY_ID", "1")  # never a fallback to the environment
    with pytest.raises(ClientError) as info:
        resolve(None)
    assert info.value.code == "registry_invalid"
    assert needle in info.value.detail
    assert str(path) in info.value.detail


def test_registry_broken_json_and_bad_ids(env):
    _, path = env
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ClientError) as info:
        resolve(None)
    assert info.value.code == "registry_invalid"
    write_registry(path, {"Acme": {}})
    with pytest.raises(ClientError):
        resolve(None)
    path.write_text(json.dumps({"version": 2, "clients": {}}), encoding="utf-8")
    with pytest.raises(ClientError):
        resolve(None)


@pytest.mark.parametrize("key,name", [("google_write", "oauth-token"), ("google_read", "oauth-token-ads"),
                                      ("meta", "openai-ads-key"), ("google_ads", "oauth-token"),
                                      ("meta_capi", "meta-access-token")])
def test_default_client_legacy_entry_of_another_kind_is_refused_at_load(env, key, name):
    """The kind-coherent pair rule (legacy_pair_ok) applies as soon as the registry is read, not only on use."""
    _, path = env
    write_registry(path, {"default": {"secrets": {key: name}}})
    with pytest.raises(ClientError) as info:
        resolve("default")
    assert info.value.code == "registry_invalid"
    assert f"secrets.{key}" in info.value.detail and str(path) in info.value.detail


def test_default_client_may_keep_legacy_entries(env, monkeypatch):
    fake, path = env
    write_registry(path, {"default": {"secrets": {"meta": "meta-access-token", "google_read": "oauth-token"}},
                          "beta": BETA})
    fake.store[("google-ecommerce-mcp", "meta-access-token")] = "legacy-meta"
    assert resolve("default").secret("meta") == "legacy-meta"
    assert resolve("beta").secret("meta") is None


def test_write_policy_parsed(env):
    _, path = env
    entry = dict(ACME, write={"google_ads": {"enabled": True, "account_allow": ["111-111-1111"],
                                             "max_daily_budget_micros": 50000000, "cap_currency": "EUR",
                                             "max_increase_pct": 10}})
    write_registry(path, {"acme": entry})
    pol = resolve(None).policy("google_ads")
    assert pol.enabled and pol.account_allow == frozenset({"1111111111"})
    assert (pol.max_daily_budget_micros, pol.cap_currency, pol.max_increase_pct) == (50000000, "EUR", 10.0)
    assert resolve(None).policy("meta") == WritePolicy()


def test_list_clients_has_no_ids(env, monkeypatch):
    _, path = env
    rows = clients.list_clients()
    assert rows[0]["legacy"] and rows[0]["client_id"] == "default"
    write_registry(path, {"acme": ACME, "beta": BETA})
    rows = clients.list_clients()
    assert [r["client_id"] for r in rows] == ["acme", "beta"]
    monkeypatch.setenv("GEM_CLIENT", "beta")
    rows = clients.list_clients()  # a pinned process only sees its own client
    assert [(r["client_id"], r["pinned"]) for r in rows] == [("beta", True)]
    rows = clients.list_clients(include_all=True)
    assert [r["client_id"] for r in rows] == ["acme", "beta"]
    assert [r["pinned"] for r in rows] == [False, True]
    monkeypatch.setenv("GEM_CLIENT", "gamma")
    assert clients.list_clients() == []
    dumped = json.dumps(rows)
    for value in ("111111111", "1111111111", "acme.fr", "222222222"):
        assert value not in dumped


def test_mask_id():
    assert clients.mask_id("1234567890") == "*******890"
    assert clients.mask_id("sc-domain:acme.fr") == "sc-domain:***.fr"
    assert clients.mask_id("https://www.acme.fr/shop/") == "https://***.fr/"
    assert clients.mask_id("12") == "**"
    assert clients.mask_id(None) == ""


# ------------------------------------------------------------------ files
def test_save_is_atomic_and_never_leaves_temp_files(env, monkeypatch):
    _, path = env
    clients.add_client("acme", label="Acme", accounts={"ga4": {"property_id": "1"}})
    before = path.read_bytes()

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        clients.add_client("beta", accounts={})
    assert path.read_bytes() == before
    assert [p.name for p in path.parent.iterdir()] == ["clients.json"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_registry_mode_600(env):
    _, path = env
    clients.add_client("acme")
    assert (path.stat().st_mode & 0o777) == 0o600


def test_windows_acl_is_restricted(env, monkeypatch):
    _, path = env
    calls = []
    monkeypatch.setattr(paths, "_IS_WINDOWS", True)
    monkeypatch.setattr(paths, "restrict_windows_acl", lambda p: calls.append(Path(p)) or True)
    assert clients.add_client("acme") is True
    assert len(calls) == 1 and calls[0].parent == path.parent
    monkeypatch.setattr(paths, "restrict_windows_acl", lambda p: False)
    assert clients.add_client("acme", replace=True) is False


def test_windows_acl_reuses_ads_auth(monkeypatch, tmp_path):
    from google_ecommerce_mcp import ads_auth

    seen = []
    monkeypatch.setattr(ads_auth, "_restrict_windows_acl", lambda p: seen.append(p) or True)
    assert paths.restrict_windows_acl(tmp_path / "x") is True
    assert seen == [tmp_path / "x"]


def test_paths(monkeypatch, tmp_path):
    monkeypatch.delenv("GEM_CLIENTS_FILE", raising=False)
    monkeypatch.delenv("GEM_STATE_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    expected = (tmp_path / "roaming") if os.name == "nt" else (tmp_path / "xdg")
    assert paths.clients_file() == expected / "google-ecommerce-mcp" / "clients.json"
    assert paths.state_dir() == expected / "google-ecommerce-mcp" / "state"
    monkeypatch.setenv("GEM_CLIENTS_FILE", str(tmp_path / "c.json"))
    monkeypatch.setenv("GEM_STATE_DIR", str(tmp_path / "s"))
    assert paths.clients_file() == tmp_path / "c.json"
    assert paths.state_dir() == tmp_path / "s"


def test_registry_never_contains_a_secret_value(env):
    _, path = env
    with pytest.raises(ClientError):
        clients.add_client("acme", secrets={"meta": "EAAB-real-token"})
    assert not path.exists()


# ------------------------------------------------------------------ CLI: add (wizard)
def test_wizard_guided_add(env, monkeypatch, capsys):
    _, path = env
    no_getpass(monkeypatch)
    asked = answers(monkeypatch,
                    "Acme SA",          # label
                    "properties/123",   # ga4
                    "acme.fr", "sc-domain:acme.fr",  # gsc: invalid, then valid
                    "",                 # merchant skipped
                    "gtm-abcd123", "", "",  # gtm public id lower case -> normalized
                    "123-456-7890", "",  # google ads
                    "act_42",           # meta
                    "",                 # openai
                    "y", "77", "", "", "", "",  # meta extras: pixel only
                    "n")                # sandbox
    assert main(["client", "add", "acme"]) == 0
    entry = reg(path)["clients"]["acme"]
    assert entry["label"] == "Acme SA" and entry["sandbox"] is False
    assert entry["accounts"]["ga4"] == {"property_id": "123"}
    assert entry["accounts"]["gsc"] == {"site_url": "sc-domain:acme.fr"}
    assert entry["accounts"]["gtm"] == {"public_id": "GTM-ABCD123"}
    assert entry["accounts"]["google_ads"] == {"customer_id": "1234567890"}
    assert entry["accounts"]["meta"] == {"ad_account_id": "42", "pixel_id": "77"}
    assert "merchant" not in entry["accounts"]
    assert entry["secrets"] == {} and entry["write"] == {}
    out = capsys.readouterr().out
    assert "Invalid format" in out
    assert "client set-secret acme meta" in out
    assert "setup-ads --client acme" in out
    assert "Registry created" in out
    assert any("e.g." in q for q in asked)


def test_wizard_french(env, monkeypatch, capsys):
    monkeypatch.setenv("GEM_LANG", "fr_FR.UTF-8")
    answers(monkeypatch)
    assert main(["client", "add", "acme"]) == 0
    out = capsys.readouterr().out
    assert "Nouveau client" in out and "enregistré" in out


def test_wizard_first_registry_imports_env_and_default_keeps_tokens(env, monkeypatch, capsys):
    fake, path = env
    monkeypatch.setenv("GA4_PROPERTY_ID", "555")
    monkeypatch.setenv("GADS_CUSTOMER_ID", "111-222-3333")
    fake.store[("google-ecommerce-mcp", "oauth-token-ads")] = '{"ads": 1}'
    asked = answers(monkeypatch)  # Enter everywhere: accept the proposed values
    assert main(["client", "add", "default"]) == 0
    assert any("[555]" in q for q in asked)
    entry = reg(path)["clients"]["default"]
    assert entry["accounts"]["ga4"] == {"property_id": "555"}
    assert entry["accounts"]["google_ads"] == {"customer_id": "1112223333"}
    assert entry["secrets"]["google_ads"] == "oauth-token-ads"
    ctx = resolve(None)
    assert not ctx.legacy and ctx.secret("gads") == '{"ads": 1}'
    assert "no migration" in capsys.readouterr().out


def test_wizard_second_client_warns_client_id_required(env, monkeypatch, capsys):
    answers(monkeypatch)
    assert main(["client", "add", "acme", "--yes"]) == 0
    assert main(["client", "add", "beta", "--yes", "--ga4", "2"]) == 0
    assert "client_id" in capsys.readouterr().out
    with pytest.raises(ClientError):
        resolve(None)


def test_add_with_flags_non_interactive(env, monkeypatch):
    _, path = env
    no_getpass(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *_: (_ for _ in ()).throw(AssertionError("asked")))
    code = main(["client", "add", "shop-fr", "--yes", "--label", "Shop", "--sandbox", "--ga4", "9",
                 "--gads-customer-id", "123-456-7890", "--meta-account-id", "act_5", "--gtm", "GTM-XYZ1234"])
    assert code == 0
    entry = reg(path)["clients"]["shop-fr"]
    assert entry["sandbox"] is True
    assert entry["accounts"]["google_ads"]["customer_id"] == "1234567890"
    assert entry["accounts"]["meta"]["ad_account_id"] == "5"


def test_add_rejects_bad_flag_and_bad_name(env, capsys):
    assert main(["client", "add", "acme", "--yes", "--ga4", "abc"]) == 2
    err = capsys.readouterr().err
    assert "--ga4" in err and "digits" in err and "Fix" in err
    assert main(["client", "add", "Bad Name!", "--yes"]) == 2


def test_add_existing_needs_replace_and_keeps_write(env, monkeypatch):
    _, path = env
    write_registry(path, {"acme": dict(ACME, write={"google_ads": {"enabled": True,
                                                                    "account_allow": ["1111111111"]}})})
    assert main(["client", "add", "acme", "--yes"]) == 1
    assert main(["client", "add", "acme", "--yes", "--replace", "--ga4", "3"]) == 0
    entry = reg(path)["clients"]["acme"]
    assert entry["accounts"]["ga4"]["property_id"] == "3"
    assert entry["accounts"]["merchant"]["account_id"] == "1111"
    assert entry["write"]["google_ads"]["enabled"] is True
    assert entry["label"] == "Acme SA"


# ------------------------------------------------------------------ CLI: list / show / remove / set-secret
def test_list_and_show_mask_ids(env, monkeypatch, capsys):
    fake, path = env
    write_registry(path, {"acme": ACME, "beta": BETA})
    fake.store[("google-ecommerce-mcp", "acme/meta")] = "SECRET-META-VALUE"
    assert main(["client", "list"]) == 0
    out = capsys.readouterr().out
    assert "acme" in out and "beta" in out and "111111111" not in out
    assert main(["client", "show", "acme"]) == 0
    out = capsys.readouterr().out
    assert "******111" in out and "111111111" not in out and "SECRET-META-VALUE" not in out
    assert main(["client", "show", "acme", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["secrets_present"]["meta"] is True and data["secrets_present"]["gads"] is False
    assert data["secrets"]["meta"] == "acme/meta"
    assert main(["client", "show", "gamma"]) == 1
    assert "unknown_client" in capsys.readouterr().err


def test_list_and_show_in_legacy_mode(env, monkeypatch, capsys):
    monkeypatch.setenv("GA4_PROPERTY_ID", "123456789")
    assert main(["client", "list"]) == 0
    assert "Legacy mode" in capsys.readouterr().out
    assert main(["client", "show", "default"]) == 0
    assert "(legacy)" in capsys.readouterr().out


def test_remove_asks_the_name_and_deletes_only_its_entries(env, monkeypatch, capsys):
    fake, path = env
    write_registry(path, {"acme": ACME, "beta": BETA})
    svc = "google-ecommerce-mcp"
    fake.store.update({(svc, "acme/meta"): "a", (svc, "acme/gads"): "b", (svc, "beta/meta"): "c",
                       (svc, "meta-access-token"): "legacy"})
    tty(monkeypatch)
    answers(monkeypatch, "nope")
    assert main(["client", "remove", "acme"]) == 1
    assert "acme" in reg(path)["clients"]
    answers(monkeypatch, "acme")
    assert main(["client", "remove", "acme"]) == 0
    assert list(reg(path)["clients"]) == ["beta"]
    assert fake.users() == {"beta/meta", "meta-access-token"}
    assert main(["client", "remove", "beta", "--yes", "--keep-secrets"]) == 0
    assert "beta/meta" in fake.users()


def test_set_secret_hidden_prompt(env, monkeypatch, capsys):
    fake, path = env
    write_registry(path, {"acme": ACME})
    tty(monkeypatch)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "  pasted-value  ")
    assert main(["client", "set-secret", "acme", "meta"]) == 0
    assert fake.store[("google-ecommerce-mcp", "acme/meta")] == "pasted-value"
    out = capsys.readouterr().out
    assert "pasted-value" not in out and "acme/meta" in out
    assert main(["client", "set-secret", "acme", "gads"]) == 2
    assert "setup --client acme" in capsys.readouterr().err
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
    assert main(["client", "set-secret", "acme", "openai"]) == 1


def test_set_secret_legacy_default_uses_legacy_entry(env, monkeypatch):
    fake, _ = env
    tty(monkeypatch)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "tok")
    assert main(["client", "set-secret", "default", "meta-access-token"]) == 0
    assert fake.store[("google-ecommerce-mcp", "meta-access-token")] == "tok"


# ------------------------------------------------------------------ CLI: enable-write / disable-write
def test_enable_write_needs_explicit_confirmation(env, monkeypatch, capsys):
    _, path = env
    write_registry(path, {"acme": ACME, "beta": BETA})
    tty(monkeypatch)
    answers(monkeypatch, "yes")  # not the client name: refused
    assert main(["client", "enable-write", "acme", "gads"]) == 1
    assert "google_ads" not in reg(path)["clients"]["acme"].get("write", {})
    out = capsys.readouterr().out
    assert "WARNING" in out and "GEM_WRITE=1" in out
    answers(monkeypatch, "acme")
    assert main(["client", "enable-write", "acme", "gads"]) == 0
    pol = reg(path)["clients"]["acme"]["write"]["google_ads"]
    assert pol["enabled"] is True and pol["account_allow"] == ["1111111111"]
    assert pol["allow_activate"] is False and pol["allow_delete"] is False
    assert resolve("acme").policy("google_ads").enabled
    assert not resolve("beta").policy("google_ads").enabled
    assert main(["client", "disable-write", "acme", "google_ads"]) == 0
    pol = reg(path)["clients"]["acme"]["write"]["google_ads"]
    assert pol["enabled"] is False and pol["account_allow"] == ["1111111111"]


def test_enable_write_other_platforms_defaults(env, monkeypatch):
    _, path = env
    write_registry(path, {"acme": ACME})
    tty(monkeypatch)
    for family, expected in (("meta", ["1111"]), ("meta-eco", ["1112", "1113"]), ("gtm", ["2111", "GTM-ACME111"]),
                             ("openai", ["acct_acme"]), ("gsc", ["sc-domain:acme.fr"])):
        answers(monkeypatch, "acme")
        assert main(["client", "enable-write", "acme", family]) == 0
        key = client_cli._platform(family)
        assert reg(path)["clients"]["acme"]["write"][key]["account_allow"] == sorted(expected)


def test_enable_write_refusals(env, monkeypatch, capsys):
    _, path = env
    write_registry(path, {"acme": ACME, "beta": BETA, "bare": {}})
    tty(monkeypatch)
    answers(monkeypatch, "acme")
    assert main(["client", "enable-write", "acme", "gads", "--account", "2222222222"]) == 1  # beta's account
    assert "id_not_owned" in capsys.readouterr().err
    assert main(["client", "enable-write", "acme", "gads", "--account", "9999999999"]) == 1  # shared MCC
    capsys.readouterr()
    assert main(["client", "enable-write", "bare", "gads"]) == 1
    err = capsys.readouterr().err
    assert "platform_not_configured" in err and "client add bare --replace" in err
    assert main(["client", "enable-write", "acme", "bing"]) == 2
    assert main(["client", "enable-write", "gamma", "gads"]) == 1
    assert all("write" not in e or e.get("write") == {} for e in reg(path)["clients"].values())


def test_enable_write_refused_in_legacy_mode(env, monkeypatch, capsys):
    monkeypatch.setenv("GADS_CUSTOMER_ID", "1234567890")
    tty(monkeypatch)
    assert main(["client", "enable-write", "default", "gads"]) == 1
    assert "client add default" in capsys.readouterr().err


def test_set_write_api_refuses_foreign_ids(env):
    _, path = env
    write_registry(path, {"acme": ACME, "beta": BETA})
    with pytest.raises(ClientError) as info:
        clients.set_write("acme", "google_ads", True, account_allow=["2222222222"])
    assert info.value.code == "id_not_owned"
    assert "2222222222" not in info.value.detail  # masked


# ------------------------------------------------------------------ approve / misc
def test_approve_without_write_core(env, monkeypatch, capsys):
    tty(monkeypatch)
    monkeypatch.setitem(sys.modules, "google_ecommerce_mcp.write_core", None)
    assert main(["approve", "plan-123"]) == 2
    assert "write_core" in capsys.readouterr().err


def test_approve_refused_without_terminal(env, monkeypatch, capsys):
    called = []
    monkeypatch.setattr("builtins.input", lambda *_: called.append(1) or "x")
    assert main(["approve", "pl_0000000000000000", "--client", "acme"]) == 1  # pytest: stdout is not a tty
    assert "interactive terminal" in capsys.readouterr().err and not called


class _Stream:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


@pytest.mark.parametrize("stdin, stdout, expected", [(True, True, True), (True, False, False),
                                                     (False, True, False), (False, False, False)])
def test_is_tty_needs_both_streams(monkeypatch, stdin, stdout, expected):
    monkeypatch.setattr(sys, "stdin", _Stream(stdin))
    monkeypatch.setattr(sys, "stdout", _Stream(stdout))
    assert client_cli._is_tty() is expected


@pytest.mark.parametrize("argv", [["client", "remove", "acme"], ["client", "remove", "acme", "--yes"],
                                  ["client", "enable-write", "acme", "gads"],
                                  ["client", "set-secret", "acme", "meta"]])
def test_sensitive_commands_refused_without_terminal(env, monkeypatch, capsys, argv):
    _, path = env
    write_registry(path, {"acme": ACME})
    fake, _ = env
    monkeypatch.setattr("builtins.input", lambda *_: "acme")
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "value")
    before = path.read_bytes()
    assert main(argv) == 1
    assert "interactive terminal" in capsys.readouterr().err
    assert path.read_bytes() == before and ("google-ecommerce-mcp", "acme/meta") not in fake.store


def test_disable_write_needs_no_terminal(env, monkeypatch):
    _, path = env
    write_registry(path, {"acme": dict(ACME, write={"google_ads": {"enabled": True,
                                                                    "account_allow": ["1111111111"]}})})
    assert main(["client", "disable-write", "acme", "gads"]) == 0
    assert reg(path)["clients"]["acme"]["write"]["google_ads"]["enabled"] is False


def test_client_without_subcommand(env, capsys):
    assert main(["client"]) == 2


def test_broken_registry_is_reported_by_the_cli(env, capsys):
    _, path = env
    path.parent.mkdir(parents=True)
    path.write_text("[]", encoding="utf-8")
    assert main(["client", "list"]) == 1
    err = capsys.readouterr().err
    assert "registry_invalid" in err and str(path) in err and "Fix" in err


def test_lang_detection(monkeypatch):
    for name in ("GEM_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANG", "fr_FR.UTF-8")
    assert client_cli.lang() == "fr"
    monkeypatch.setenv("GEM_LANG", "en")
    assert client_cli.lang() == "en"


def test_secrets_store_names():
    assert secrets_store.env_name("shop-fr", "meta-capi") == "GEM_SECRET_SHOP_FR_META_CAPI"
    assert secrets_store.valid_secret_name("acme", "acme/meta")
    assert not secrets_store.valid_secret_name("acme", "acme")
    assert not secrets_store.valid_secret_name("acme", "acmex/meta")
    assert secrets_store.valid_secret_name("default", "oauth-token")
    with pytest.raises(secrets_store.SecretError):
        secrets_store.set_secret("acme", "meta", "   ")


@pytest.mark.parametrize("module", ["clients.py", "paths.py", "secrets_store.py", "client_cli.py", "__main__.py"])
def test_python_310_syntax(module):
    path = PKG / module
    ast.parse(path.read_text(encoding="utf-8"), filename=module, feature_version=(3, 10))

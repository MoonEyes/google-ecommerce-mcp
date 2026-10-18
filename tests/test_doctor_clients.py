"""`doctor` with a client registry: one compact line per client and platform, --client filter, --json, masked
ids, no secret, no network without --live, exit codes 0/1/2. Offline: fake keyring, Claude config in tmp_path."""

from __future__ import annotations

import json

import pytest

from ads_fakes import no_network  # noqa: F401  (autouse: any real HTTP request fails the test)
from clients_fakes import ACME, BETA, setup_env, write_registry
from google_ecommerce_mcp import doctor
from google_ecommerce_mcp.__main__ import main
from google_ecommerce_mcp.ads_config import ADS_SCOPE

SERVICE = "google-ecommerce-mcp"
A = "https://www.googleapis.com/auth/"
READ = [A + "analytics.readonly", A + "webmasters.readonly", A + "tagmanager.readonly", A + "content"]
WRITE = [A + "analytics.edit", A + "tagmanager.edit.containers", A + "webmasters", A + "content"]
META_SECRET = "EAAmetaSECRETvalue" + "x" * 30


def token(scopes, mark):
    return json.dumps({"token": "ya29." + mark * 30, "refresh_token": "1//0" + mark * 30,
                       "token_uri": "https://oauth2.googleapis.com/token", "client_id": "cid",
                       "client_secret": "csecret-" + mark * 10, "scopes": scopes})


@pytest.fixture
def two(monkeypatch, tmp_path):
    fake, path = setup_env(monkeypatch, tmp_path)
    for name in doctor.KNOWN_ENV:
        monkeypatch.delenv(name, raising=False)
    acme = json.loads(json.dumps(ACME))
    acme["write"] = {"ga4": {"enabled": True, "account_allow": ["111111111"]}}
    write_registry(path, {"acme": acme, "beta": BETA})
    fake.store[(SERVICE, "acme/google")] = token(READ, "r")
    fake.store[(SERVICE, "acme/google-w")] = token(WRITE, "w")
    fake.store[(SERVICE, "acme/gads")] = token([ADS_SCOPE], "a")
    fake.store[(SERVICE, "acme/meta")] = META_SECRET
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"google-ecommerce": {"command": "uvx", "args": ["google-ecommerce-mcp"],
                                                                   "env": {}}}}))
    return fake, path, cfg


def run(capsys, cfg, *extra):
    code = main(["doctor", "--config", str(cfg), *extra])
    return code, capsys.readouterr().out


def rows(result, cid):
    return {r["platform"]: r for c in result["clients"] if c["client_id"] == cid for r in c["rows"]}


def full_ids():
    values = []
    for client in (ACME, BETA):
        for fields in client["accounts"].values():
            values += [v for v in fields.values() if len(v.replace("act_", "")) >= 5]
    return values + ["acme.fr", "beta.com"]


def test_two_clients_json_rows_and_exact_next_commands(two, capsys):
    fake, path, cfg = two
    code, out = run(capsys, cfg, "--json")
    result = json.loads(out)
    assert code == 1 and result["exit_code"] == 1 and result["multi_client"] is True
    assert [c["client_id"] for c in result["clients"]] == ["acme", "beta"]
    acme, beta = rows(result, "acme"), rows(result, "beta")
    assert list(acme) == ["ga4", "gsc", "merchant", "gtm", "google_ads", "meta", "openai_ads"]
    assert acme["ga4"]["status"] == "ok" and acme["ga4"]["write_enabled"] is True
    assert (acme["ga4"]["read_token"], acme["ga4"]["write_token"]) == ("ok", "ok")
    assert acme["google_ads"]["read_token"] == "ok" and acme["google_ads"]["write_token"] == "=read"
    assert acme["meta"]["status"] == "ok"
    assert acme["openai_ads"]["next_step"] == "google-ecommerce-mcp set-secret --client acme openai"
    assert beta["ga4"]["next_step"] == "google-ecommerce-mcp setup --client beta --client-secret <client_secret.json>"
    assert beta["google_ads"]["next_step"] == ("google-ecommerce-mcp setup-ads --client beta --client-secret "
                                               "<client_secret.json>")
    assert beta["meta"]["next_step"] == "google-ecommerce-mcp set-secret --client beta meta"
    assert all(r["status"] == "fix" for r in beta.values())
    assert any("GEM_WRITE=1" in w for w in result["warnings"])


def test_no_secret_nor_full_id_ever_printed(two, capsys):
    fake, path, cfg = two
    for extra in ((), ("--json",)):
        code, out = run(capsys, cfg, *extra)
        for value in full_ids() + ["ya29.", "1//0", "csecret", META_SECRET[:12]]:
            assert value not in out, (extra, value)
    result = json.loads(run(capsys, cfg, "--json")[1])
    assert rows(result, "acme")["ga4"]["account"] == "******111"
    assert rows(result, "beta")["gsc"]["account"] == "https://***.com/"


def test_table_is_compact_and_stable(two, capsys):
    fake, path, cfg = two
    code, first = run(capsys, cfg)
    _, second = run(capsys, cfg)
    assert first == second and code == 1
    lines = first.splitlines()
    header = next(i for i, line in enumerate(lines) if line.startswith("Client"))
    assert lines[header].split() == ["Client", "Platform", "Ids", "Read", "Write", "On", "Status", "Account", "Next"]
    table = lines[header + 2:header + 2 + 14]
    assert [line.split()[0] for line in table] == ["acme"] * 7 + ["beta"] * 7
    assert "2 clients" in lines[0] and "offline" in lines[0]
    assert "Next command: google-ecommerce-mcp set-secret --client acme openai" in first
    assert "Result: 6 OK, 8 to fix, 0 not set up." in first


def test_client_filter_and_unknown_client(two, capsys):
    fake, path, cfg = two
    code, out = run(capsys, cfg, "--client", "acme", "--json")
    result = json.loads(out)
    assert [c["client_id"] for c in result["clients"]] == ["acme"]
    assert code == 1  # acme's ChatGPT Ads id is set but its key is not stored
    assert [r["platform"] for r in result["clients"][0]["rows"] if r["status"] == "fix"] == ["openai_ads"]
    code, out = run(capsys, cfg, "--client", "zeta")
    assert code == 2 and "unknown_client" in out


def test_only_filters_platforms(two, capsys):
    fake, path, cfg = two
    result = json.loads(run(capsys, cfg, "--json", "--only", "gads,meta")[1])
    assert set(rows(result, "beta")) == {"google_ads", "meta"}


def test_write_enabled_without_write_token_points_to_setup_write(two, capsys):
    fake, path, cfg = two
    del fake.store[(SERVICE, "acme/google-w")]
    result = json.loads(run(capsys, cfg, "--json", "--client", "acme")[1])
    ga4 = rows(result, "acme")["ga4"]
    assert ga4["status"] == "fix" and ga4["write_token"] == "missing"
    assert ga4["next_step"] == "google-ecommerce-mcp setup --client acme --write --client-secret <client_secret.json>"
    assert rows(result, "acme")["gsc"]["status"] == "ok"  # writing is off there: the write token is optional


def test_write_token_copied_from_read_token_is_flagged(two, capsys):
    fake, path, cfg = two
    fake.store[(SERVICE, "acme/google-w")] = fake.store[(SERVICE, "acme/google")]
    result = json.loads(run(capsys, cfg, "--json", "--client", "acme")[1])
    assert rows(result, "acme")["ga4"]["write_token"] == "not separate"
    assert rows(result, "acme")["ga4"]["status"] == "fix"


def test_gem_write_in_the_claude_entry_clears_the_warning(two, capsys):
    fake, path, cfg = two
    cfg.write_text(json.dumps({"mcpServers": {"google-ecommerce": {"command": "uvx", "args": ["google-ecommerce-mcp"],
                                                                   "env": {"GEM_WRITE": "1", "GEM_CLIENT": "acme"}}}}))
    result = json.loads(run(capsys, cfg, "--json")[1])
    assert result["claude_desktop"]["gem_write"] is True
    assert not any("GEM_WRITE=1 is not" in w for w in result["warnings"])
    assert result["clients"][0]["pinned_in_claude"] is True and result["clients"][1]["pinned_in_claude"] is False


def test_nothing_configured_exits_2_and_points_to_client_add(monkeypatch, tmp_path, capsys):
    fake, path = setup_env(monkeypatch, tmp_path)
    write_registry(path, {"empty": {}})
    code, out = run(capsys, tmp_path / "none.json")
    assert code == 2 and "google-ecommerce-mcp client add empty --replace" in out


def test_everything_ok_exits_0(monkeypatch, tmp_path, capsys):
    fake, path = setup_env(monkeypatch, tmp_path)
    write_registry(path, {"solo": {"accounts": {"ga4": {"property_id": "123456789"}}}})
    fake.store[(SERVICE, "solo/google")] = token(READ, "s")
    code, out = run(capsys, tmp_path / "none.json")
    assert code == 0 and "Next command: google-ecommerce-mcp doctor --client solo --live" in out


def test_offline_makes_no_tool_call(two, capsys, monkeypatch):
    fake, path, cfg = two
    from google_ecommerce_mcp import google_ads, server

    def refuse(*_a, **_k):
        raise AssertionError("no tool call offline")

    for module, name in ((server, "ga4_properties"), (server, "gsc_sitemaps"), (google_ads, "gads_accounts")):
        monkeypatch.setattr(module, name, refuse)
    assert run(capsys, cfg)[0] == 1


def test_live_calls_each_ok_row_with_its_own_client_id(two, capsys, monkeypatch):
    fake, path, cfg = two
    from google_ecommerce_mcp import google_ads, meta_ads, openai_ads, server

    calls = []

    def recorder(name, answer):
        def call(*_a, client_id=None, **_k):
            calls.append((name, client_id))
            return answer
        return call

    monkeypatch.setattr(server, "ga4_properties", recorder("ga4", {"properties": []}))
    monkeypatch.setattr(server, "gsc_sitemaps", recorder("gsc", {"error": "auth_failed"}))
    monkeypatch.setattr(server, "merchant_data_sources", recorder("merchant", {}))
    monkeypatch.setattr(server, "gtm_inventory", recorder("gtm", {}))
    monkeypatch.setattr(google_ads, "gads_accounts", recorder("gads", {}))
    monkeypatch.setattr(meta_ads, "meta_ad_accounts", recorder("meta", {}))
    monkeypatch.setattr(openai_ads, "openai_ads_account", recorder("openai", {}))
    result = json.loads(run(capsys, cfg, "--json", "--live")[1])
    assert {cid for _, cid in calls} == {"acme"}  # beta's rows are not OK offline: never called
    gsc = rows(result, "acme")["gsc"]
    assert gsc["live"] == "auth_failed"
    assert gsc["next_step"] == "google-ecommerce-mcp setup --client acme --client-secret <client_secret.json>"


def test_legacy_mode_without_registry_is_unchanged(monkeypatch, tmp_path, capsys):
    fake, path = setup_env(monkeypatch, tmp_path)
    code, out = run(capsys, tmp_path / "none.json", "--json")
    result = json.loads(out)
    assert "services" in result and "clients" not in result and code == 2
    code, out = run(capsys, tmp_path / "none.json", "--client", "default", "--json")
    assert "services" in json.loads(out)
    code, out = run(capsys, tmp_path / "none.json", "--client", "acme")
    assert code == 2 and "client add acme" in out

"""`client pilot-check`: offline readiness check for a read-only pilot. Fake keyring, Claude config in tmp_path,
any real HTTP request fails the test. Cases: ready, writing enabled, missing id, GEM_WRITE set (shell and entry)."""

from __future__ import annotations

import json

import pytest

from ads_fakes import no_network  # noqa: F401  (autouse: any real HTTP request fails the test)
from clients_fakes import setup_env, write_registry
from google_ecommerce_mcp import __version__
from google_ecommerce_mcp.__main__ import main

SERVICE = "google-ecommerce-mcp"
A = "https://www.googleapis.com/auth/"
READ = [A + "analytics.readonly", A + "webmasters.readonly", A + "content"]
SECRET = "ya29.PILOTSECRETaccess" + "x" * 20
REFRESH = "1//0PILOTSECRETrefresh" + "y" * 20
READY = "Pilot: ready for read-only use"
PINNED = {"command": "uvx", "args": [f"google-ecommerce-mcp=={__version__}"], "env": {"GEM_CLIENT": "pilot"}}


def pilot_client(**changes):
    entry = {"label": "Pilot Shop", "accounts": {"ga4": {"property_id": "111111111"},
                                                  "gsc": {"site_url": "sc-domain:pilot-shop.example"}}}
    entry.update(changes)
    return entry


def token(scopes=READ):
    return json.dumps({"token": SECRET, "refresh_token": REFRESH, "client_id": "cid", "client_secret": "cs",
                       "token_uri": "https://oauth2.googleapis.com/token", "scopes": scopes})


def claude_config(tmp_path, entry=PINNED, name="google-pilot"):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {name: entry}}), encoding="utf-8")
    return cfg


@pytest.fixture
def env(monkeypatch, tmp_path):
    fake, path = setup_env(monkeypatch, tmp_path)
    monkeypatch.delenv("GEM_WRITE", raising=False)
    write_registry(path, {"pilot": pilot_client()})
    fake.store[(SERVICE, "pilot/google")] = token()
    return fake, path, claude_config(tmp_path)


def run(capsys, cfg, *extra, client="pilot"):
    code = main(["client", "pilot-check", client, "--config", str(cfg), *extra])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_ready_prints_table_and_final_line_exit_0(env, capsys):
    _, _, cfg = env
    code, out, _ = run(capsys, cfg)
    assert code == 0
    assert out.rstrip().splitlines()[-1] == READY
    assert "Writing: OFF for every family" in out
    assert "GEM_WRITE: unset" in out
    assert f"pinned to google-ecommerce-mcp=={__version__}" in out
    ga4 = next(line for line in out.splitlines() if line.startswith("GA4"))
    assert "yes" in ga4 and "ok" in ga4 and "OK" in ga4
    merchant = next(line for line in out.splitlines() if line.startswith("Merchant Center"))
    assert "skipped (optional)" in merchant


def test_ready_json_has_no_secret_and_no_full_id(env, capsys):
    _, _, cfg = env
    code, out, err = run(capsys, cfg, "--json")
    result = json.loads(out)
    assert code == 0 and result["ready"] is True and result["fixes"] == []
    assert {p["platform"]: p["read_token"] for p in result["platforms"]}["ga4"] == "ok"
    for text in (out, err):
        for forbidden in (SECRET, REFRESH, "111111111", "pilot-shop.example"):
            assert forbidden not in text


def test_writing_enabled_is_not_ready_with_the_disable_command(env, capsys):
    _, path, cfg = env
    write_registry(path, {"pilot": pilot_client(write={"ga4": {"enabled": True, "account_allow": ["111111111"]}})})
    code, out, _ = run(capsys, cfg)
    assert code == 1
    assert "Writing: ON for ga4" in out
    assert "Pilot: not ready, 1 thing to fix" in out
    assert "google-ecommerce-mcp client disable-write pilot ga4" in out
    assert READY not in out


def test_missing_id_is_not_ready_with_the_client_add_command(env, capsys):
    _, path, cfg = env
    write_registry(path, {"pilot": {"label": "Pilot Shop", "accounts": {"ga4": {"property_id": "111111111"}}}})
    code, out, _ = run(capsys, cfg)
    assert code == 1
    gsc = next(line for line in out.splitlines() if line.startswith("Search Console"))
    assert "NO" in gsc and "FIX" in gsc
    assert "Search Console: no id set" in out
    assert "google-ecommerce-mcp client add pilot --replace" in out
    assert READY not in out


def test_gem_write_in_the_shell_is_not_ready(env, capsys, monkeypatch):
    _, _, cfg = env
    monkeypatch.setenv("GEM_WRITE", "1")
    code, out, _ = run(capsys, cfg)
    assert code == 1
    assert "GEM_WRITE: set in this shell" in out
    assert "unset GEM_WRITE" in out
    assert READY not in out


def test_gem_write_in_the_claude_entry_is_not_ready(env, capsys, tmp_path):
    cfg = claude_config(tmp_path, {**PINNED, "env": {"GEM_CLIENT": "pilot", "GEM_WRITE": "1"}})
    code, out, _ = run(capsys, cfg)
    assert code == 1
    assert "GEM_WRITE: set in the Claude Desktop entry" in out
    assert "delete the GEM_WRITE line" in out


def test_missing_read_token_gives_the_setup_command(env, capsys):
    fake, _, cfg = env
    fake.store.clear()
    code, out, _ = run(capsys, cfg)
    assert code == 1
    assert "no read token stored" in out
    assert out.count("google-ecommerce-mcp setup --client pilot --client-secret <client_secret.json>") == 1


def test_token_without_the_scope_is_not_ready(env, capsys):
    fake, _, cfg = env
    fake.store[(SERVICE, "pilot/google")] = token([A + "analytics.readonly"])
    code, out, _ = run(capsys, cfg)
    assert code == 1
    assert "Search Console: the stored token does not include this service" in out


def test_unpinned_entry_is_advice_only(env, capsys, tmp_path):
    cfg = claude_config(tmp_path, {"command": "uvx", "args": ["google-ecommerce-mcp"], "env": {"GEM_CLIENT": "pilot"}})
    code, out, _ = run(capsys, cfg)
    assert code == 0
    assert "NOT pinned to an exact version" in out
    assert f'["google-ecommerce-mcp=={__version__}"]' in out
    assert out.rstrip().splitlines()[-1] == READY


def test_missing_claude_config_is_advice_only(env, capsys, tmp_path):
    code, out, _ = run(capsys, tmp_path / "absent.json")
    assert code == 0
    assert "Claude Desktop entry: not checked" in out
    assert out.rstrip().splitlines()[-1] == READY


def test_unknown_client_is_a_structured_error_exit_1(env, capsys):
    _, _, cfg = env
    code, out, err = run(capsys, cfg, client="nobody")
    assert code == 1 and out == ""
    assert "unknown_client" in err


DEMO = __import__("pathlib").Path(__file__).resolve().parent.parent / "examples" / "demo"
DEMO_KEYS = {
    "ga4_report.json": {"rows", "row_count", "truncated", "totals", "metrics", "currency", "date_range", "property_id"},
    "gsc_performance.json": {"rows", "truncated", "totals", "totals_note", "metrics", "date_range"},
    "merchant_product_issues.json": {"products", "scanned", "truncated"},
}


@pytest.mark.parametrize("name", sorted(DEMO_KEYS))
def test_demo_files_are_labelled_and_shaped_like_the_tools(name):
    data = json.loads((DEMO / name).read_text(encoding="utf-8"))
    assert data["_demo"] == "synthetic demo data, shop name Demo Shop"
    assert DEMO_KEYS[name] <= set(data)
    assert {"client_id", "fetched_at", "freshness"} <= set(data)
    assert set(data) - DEMO_KEYS[name] - {"_demo", "client_id", "fetched_at", "freshness"} == set()


def test_demo_totals_match_rows():
    ga4 = json.loads((DEMO / "ga4_report.json").read_text(encoding="utf-8"))
    for metric, total in ga4["totals"].items():
        assert round(sum(r[metric] for r in ga4["rows"]), 2) == round(total, 2)
    gsc = json.loads((DEMO / "gsc_performance.json").read_text(encoding="utf-8"))
    assert gsc["totals"]["clicks"] > sum(r["clicks"] for r in gsc["rows"])  # anonymized queries

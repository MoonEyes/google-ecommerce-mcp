"""ads_overview per client: one client only, only its platforms, never two clients mixed; legacy unchanged."""

from __future__ import annotations

import pytest

from ads_fakes import no_network  # noqa: F401  (autouse)
from clients_fakes import ACME, BETA, setup_env, write_registry
from google_ecommerce_mcp import ads_overview as ov
from google_ecommerce_mcp import google_ads, meta_ads, openai_ads

START, END = "2026-01-01", "2026-01-31"
MODULES = {"google_ads": google_ads, "meta": meta_ads, "openai_ads": openai_ads}
SOLO = {"accounts": {"meta": {"ad_account_id": "4242"}}}


def ok(platform, spend, client_id):
    return {"platform": platform, "spend": spend, "impressions": 10, "clicks": 1, "conversions": 1.0,
            "conversion_value": spend * 2, "roas": 2.0, "currency": "EUR", "attribution": None,
            "date_range": {"start_date": START, "end_date": END}, "totals_complete": True, "client_id": client_id}


@pytest.fixture
def calls(monkeypatch, tmp_path):
    fake, path = setup_env(monkeypatch, tmp_path)
    write_registry(path, {"acme": ACME, "beta": BETA, "solo": SOLO})
    seen = []
    spends = {"acme": 100.0, "beta": 7.0, "solo": 30.0}
    for name, module in MODULES.items():
        def fake(start_date, end_date, client_id=None, _name=name):
            seen.append((_name, client_id))
            return ok(_name, spends[client_id], client_id)

        monkeypatch.setattr(module, "summary", fake)
    return seen


def test_only_the_clients_platforms_are_called_with_its_id(calls):
    out = ov.ads_overview(start_date=START, end_date=END, client_id="solo")
    assert calls == [("meta", "solo")]
    assert out["client_id"] == "solo"
    assert out["platforms"]["meta"]["status"] == "ok"
    assert out["not_configured"] == ["google_ads", "openai_ads"]
    assert "client add solo --replace" in out["platforms"]["google_ads"]["fix"]
    assert out["combined"]["spend"] == 30.0 and out["combined"]["platforms_included"] == ["meta"]


def test_two_clients_are_never_added_up(calls):
    acme = ov.ads_overview(start_date=START, end_date=END, client_id="acme")
    beta = ov.ads_overview(start_date=START, end_date=END, client_id="beta")
    assert {cid for _, cid in calls} == {"acme", "beta"}
    assert acme["combined"]["spend"] == 300.0 and beta["combined"]["spend"] == 21.0
    assert acme["client_id"] == "acme" and beta["client_id"] == "beta"


def test_no_client_with_several_clients_is_refused_without_any_call(calls):
    out = ov.ads_overview(start_date=START, end_date=END)
    assert out["error"] == "client_required" and out["warning"] == ov.WARNING and calls == []
    out = ov.ads_overview(start_date=START, end_date=END, client_id="zeta")
    assert out["error"] == "unknown_client" and calls == []


def test_a_summary_answering_for_another_client_is_dropped(calls, monkeypatch):
    monkeypatch.setattr(meta_ads, "summary", lambda s, e, client_id=None: ok("meta", 999.0, "beta"))
    out = ov.ads_overview(start_date=START, end_date=END, client_id="acme")
    assert out["platforms"]["meta"]["status"] == "error"
    assert out["combined"]["spend"] == 200.0 and "meta" not in out["combined"]["platforms_included"]


def test_pinned_process_refuses_another_client(calls, monkeypatch):
    monkeypatch.setenv("GEM_CLIENT", "acme")
    out = ov.ads_overview(start_date=START, end_date=END, client_id="beta")
    assert out["error"] == "pinned_client" and calls == []
    assert ov.ads_overview(start_date=START, end_date=END)["client_id"] == "acme"


def test_legacy_mode_calls_summaries_exactly_as_before(monkeypatch, tmp_path):
    setup_env(monkeypatch, tmp_path)  # no registry file
    seen = []
    for name, module in MODULES.items():
        def fake(start_date, end_date, _name=name):  # the old two-argument signature
            seen.append(_name)
            return ok(_name, 10.0, None)

        monkeypatch.setattr(module, "summary", fake)
    out = ov.ads_overview(start_date=START, end_date=END)
    assert seen == ["google_ads", "meta", "openai_ads"] and "client_id" not in out
    assert out["combined"]["spend"] == 30.0

"""pagespeed keeps answering when Lighthouse returns a null score, a runtime error or nothing (offline, fake HTTP)."""

import copy

from test_server import PAGESPEED_OK, FakeResponse, server, use  # noqa: F401  (server is a fixture)

URL = "https://shop.example/"
CRUX = {"id": URL, "overall_category": "FAST", "metrics": {
    "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 2300, "category": "FAST"}}}


def run(server, monkeypatch, payload):
    use(server, monkeypatch, [("runPagespeed", FakeResponse(200, payload))])
    return server.pagespeed(URL)


def test_null_performance_score_gives_none_and_keeps_seo(server, monkeypatch):
    payload = copy.deepcopy(PAGESPEED_OK)
    payload["lighthouseResult"]["categories"] = {"performance": {"score": None}, "seo": {"score": 0.9}}
    out = run(server, monkeypatch, payload)
    assert out["performance"] is None and out["seo"] == 90 and "lab_error" not in out


def test_runtime_error_keeps_crux_and_flags_partial(server, monkeypatch):
    payload = {"lighthouseResult": {"runtimeError": {"code": "NO_FCP", "message": "x" * 400},
                                    "categories": {"performance": {"score": None}, "seo": {"score": None}},
                                    "audits": {}},
               "loadingExperience": CRUX}
    out = run(server, monkeypatch, payload)
    assert out["lab_error"]["code"] == "NO_FCP" and len(out["lab_error"]["message"]) == 300
    assert out["partial"] is True and out["field_metrics"]["lcp_ms"] == 2300
    assert out["performance"] is None and out["lcp"] is None and out["field_data"] == "FAST"


def test_no_error_code_is_not_partial(server, monkeypatch):
    payload = copy.deepcopy(PAGESPEED_OK)
    payload["lighthouseResult"]["runtimeError"] = {"code": "NO_ERROR", "message": ""}
    out = run(server, monkeypatch, payload)
    assert "lab_error" not in out and "partial" not in out and out["performance"] == 100


def test_nothing_usable_is_a_structured_error(server, monkeypatch):
    out = run(server, monkeypatch, {"loadingExperience": {"id": URL}})
    assert out["error"] == "lighthouse_failed" and out["fix"] and out["detail"] == "no lighthouseResult"
    assert run(server, monkeypatch, {"error": "quota"})["error"] == "pagespeed_error"
    assert run(server, monkeypatch, {"error": {"message": "Quota exceeded"}})["hint"]


def test_field_data_only_is_partial(server, monkeypatch):
    out = run(server, monkeypatch, {"loadingExperience": CRUX})
    assert out["partial"] is True and out["performance"] is None and out["field_metrics"]["lcp_ms"] == 2300


def test_normal_response_keeps_its_keys(server, monkeypatch):
    out = run(server, monkeypatch, PAGESPEED_OK)
    assert list(out)[:6] == ["performance", "seo", "lcp", "cls", "tbt", "field_data"] and out["performance"] == 100


def test_report_renders_a_partial_pagespeed_output(server, monkeypatch, tmp_path, capsys):
    from test_e2e_pipeline import run_report, section

    out = run(server, monkeypatch, {"lighthouseResult": {"runtimeError": {"code": "NO_FCP", "message": "x"},
                                                          "categories": {"performance": {"score": None}}},
                                    "loadingExperience": CRUX})
    document = {"meta": {"lang": "en"}, "sources": {"web_vitals": {"pages": [
        {"url": URL, "strategy": "mobile", **out}]}}}
    code, page = run_report(tmp_path, document, "en")
    assert code in (None, 0), capsys.readouterr().err
    assert "2.3" in section(page, "cwv")

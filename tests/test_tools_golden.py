"""Smoke golden of the tool list (idea from googleads/google-ads-mcp): names, titles, annotations, and the
parameters of the Google and client tools are frozen in tests/golden_tools.json.

Tolerant to the modules that are not written yet (write_core, gads_write, meta_eco...): any tool beyond the golden
list is accepted, provided it follows the annotation rule: only *_apply tools may write, and they must say so."""

import asyncio
import importlib
import json
from pathlib import Path

import pytest

GOLDEN = json.loads((Path(__file__).parent / "golden_tools.json").read_text(encoding="utf-8"))["tools"]
GOOGLE_TOOLS = {"server_status", "ga4_report", "ga4_realtime", "ga4_properties", "gsc_performance",
                "gsc_inspect_url", "gsc_sitemaps", "merchant_data_sources", "merchant_product_issues",
                "merchant_report_query", "gtm_inventory", "indexing_status", "pagespeed"}
CLIENT_TOOLS = {"clients_list", "client_profile", "client_check"}


@pytest.fixture
def tools(monkeypatch, tmp_path):
    monkeypatch.setenv("GEM_CLIENTS_FILE", str(tmp_path / "no-registry.json"))
    monkeypatch.delenv("GEM_CLIENT", raising=False)
    import google_ecommerce_mcp.server as module

    module = importlib.reload(module)
    return {t.name: t for t in asyncio.run(module.mcp.list_tools())}


def _annotations(tool) -> dict:
    a = tool.annotations
    return {"readOnlyHint": a.readOnlyHint, "destructiveHint": a.destructiveHint,
            "idempotentHint": a.idempotentHint, "openWorldHint": a.openWorldHint}


def test_golden_file_covers_the_22_tools_and_the_client_tools():
    names = [t["name"] for t in GOLDEN]
    assert len(names) == len(set(names)) == 25
    assert GOOGLE_TOOLS | CLIENT_TOOLS <= set(names)


def test_every_golden_tool_is_registered_unchanged(tools):
    for entry in GOLDEN:
        assert entry["name"] in tools, f"{entry['name']} disappeared"
        tool = tools[entry["name"]]
        assert tool.title == entry["title"], entry["name"]
        assert _annotations(tool) == entry["annotations"], entry["name"]
        if "params" in entry:
            assert sorted(tool.inputSchema.get("properties", {})) == entry["params"], entry["name"]


def test_extra_tools_follow_the_annotation_rule(tools):
    """Tools added by future modules: read-only unless they are *_apply, and *_apply is declared destructive."""
    golden = {t["name"] for t in GOLDEN}
    for name, tool in tools.items():
        ann = tool.annotations
        if name.endswith("_apply"):
            assert ann.readOnlyHint is False and ann.destructiveHint is True and ann.idempotentHint is False, name
            assert "client_id" in tool.inputSchema.get("properties", {}), name
        else:
            assert ann.readOnlyHint is True and ann.destructiveHint is False, name
        if name not in golden:
            assert tool.title and tool.description, name


def test_every_google_tool_takes_an_optional_client_id(tools):
    for name in GOOGLE_TOOLS | {"client_profile", "client_check"}:
        schema = tools[name].inputSchema
        assert "client_id" in schema["properties"], name
        assert "client_id" not in schema.get("required", []), name
        assert schema["properties"]["client_id"].get("description"), name


def test_registry_skips_absent_modules_and_reports_what_it_registered(monkeypatch):
    from google_ecommerce_mcp import registry

    assert registry._import_optional("does_not_exist_yet") is None
    calls = []

    class FakeMCP:
        def tool(self, **kwargs):
            def add(fn):
                calls.append((fn.__name__, kwargs["annotations"].readOnlyHint))
                return fn
            return add

    monkeypatch.setattr(registry, "OPTIONAL_MODULES", (("does_not_exist_yet", "register"),))
    monkeypatch.setattr(registry, "ADS_MODULES", ())
    out = registry.register_all(FakeMCP())
    assert out == {"registry": ["clients_list", "client_profile", "client_check"]}
    assert calls == [("clients_list", True), ("client_profile", True), ("client_check", True)]


def test_registry_does_not_hide_a_broken_module(monkeypatch):
    """A module that exists but fails to import (missing dependency inside it) must not vanish silently."""
    from google_ecommerce_mcp import registry

    def import_module(name):
        raise ModuleNotFoundError("No module named 'google_ecommerce_mcp.missing_dep'",
                                  name="google_ecommerce_mcp.missing_dep")

    monkeypatch.setattr(registry.importlib, "import_module", import_module)
    with pytest.raises(ModuleNotFoundError):
        registry._import_optional("broken_write")

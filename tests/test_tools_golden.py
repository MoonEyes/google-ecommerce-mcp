"""Golden of the tool list (idea from googleads/google-ads-mcp): the name, title, annotations and parameter
names of EVERY tool the server registers are frozen in tests/golden_tools.json, in registration order.

Annotation rule: only the four *_apply tools may write (readOnlyHint=False, destructiveHint=True); every plan_*
tool and every read is readOnlyHint=True.

Regenerate after a deliberate change of the tool list, then review the diff:

    python -m pytest tests/test_tools_golden.py --regen-golden
"""

import asyncio
import importlib
import json
from pathlib import Path

import pytest

GOLDEN_PATH = Path(__file__).parent / "golden_tools.json"
GOLDEN = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))["tools"]
GOOGLE_TOOLS = {"server_status", "ga4_report", "ga4_realtime", "ga4_properties", "gsc_performance",
                "gsc_inspect_url", "gsc_sitemaps", "merchant_data_sources", "merchant_product_issues",
                "merchant_report_query", "gtm_inventory", "indexing_status", "pagespeed"}
CLIENT_TOOLS = {"clients_list", "client_profile", "client_check"}
APPLY_TOOLS = {"gads_apply", "meta_apply", "openai_ads_apply", "google_apply"}
COMMENT = ("Frozen tool list (tests/test_tools_golden.py): name, title, annotations and parameter names of every "
           "tool, in registration order. Only the four *_apply tools have readOnlyHint false. Regenerate only on a "
           "deliberate change: python -m pytest tests/test_tools_golden.py --regen-golden")


@pytest.fixture
def tools(monkeypatch, tmp_path):
    monkeypatch.setenv("GEM_CLIENTS_FILE", str(tmp_path / "no-registry.json"))
    monkeypatch.delenv("GEM_CLIENT", raising=False)
    from google_ecommerce_mcp import registry

    real = registry.register_all
    monkeypatch.setattr(registry, "register_all", lambda mcp, **kw: real(mcp, **{**kw, "freeze": False}))
    import google_ecommerce_mcp.server as module

    module = importlib.reload(module)
    return {t.name: t for t in asyncio.run(module.mcp.list_tools())}


def _annotations(tool) -> dict:
    a = tool.annotations
    return {"readOnlyHint": a.readOnlyHint, "destructiveHint": a.destructiveHint,
            "idempotentHint": a.idempotentHint, "openWorldHint": a.openWorldHint}


def _entry(tool) -> dict:
    return {"name": tool.name, "title": tool.title, "annotations": _annotations(tool),
            "params": sorted(tool.inputSchema.get("properties", {}))}


def regen(tools: dict) -> list[dict]:
    """Rewrite golden_tools.json from the registered tools (LF, trailing newline); returns the entries."""
    entries = [_entry(t) for t in tools.values()]
    text = json.dumps({"_comment": COMMENT, "tools": entries}, indent=1, ensure_ascii=False) + "\n"
    GOLDEN_PATH.write_bytes(text.encode("utf-8"))
    return entries


def test_golden_file_covers_the_google_and_client_tools():
    names = [t["name"] for t in GOLDEN]
    assert len(names) == len(set(names))
    assert GOOGLE_TOOLS | CLIENT_TOOLS | APPLY_TOOLS <= set(names)
    assert all("params" in t for t in GOLDEN)


def test_every_registered_tool_matches_the_golden_file(tools, regen_golden):
    expected = regen(tools) if regen_golden else json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))["tools"]
    golden = {e["name"]: e for e in expected}
    assert set(tools) == set(golden), ("only registered", sorted(set(tools) - set(golden)),
                                       "only in the golden file", sorted(set(golden) - set(tools)))
    for name, tool in tools.items():
        assert _entry(tool) == golden[name], name


def test_annotation_rule_only_the_four_apply_tools_write(tools):
    for name, tool in tools.items():
        ann = tool.annotations
        if name in APPLY_TOOLS:
            assert ann.readOnlyHint is False and ann.destructiveHint is True and ann.idempotentHint is False, name
            assert "client_id" in tool.inputSchema.get("properties", {}), name
        else:
            assert not name.endswith("_apply"), f"{name}: a new *_apply tool must be added to APPLY_TOOLS"
            assert ann.readOnlyHint is True and ann.destructiveHint is False, name
        assert tool.title and tool.description, name
    for entry in GOLDEN:
        assert entry["annotations"]["readOnlyHint"] is (entry["name"] not in APPLY_TOOLS), entry["name"]
    assert any("_plan_" in name for name in tools)  # the plan_* tools are part of the frozen list


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
    out = registry.register_all(FakeMCP(), freeze=False)
    assert out == {"registry": ["clients_list", "client_profile", "client_check"]}
    assert calls == [("clients_list", True), ("client_profile", True), ("client_check", True)]


def test_register_all_freezes_the_allowlists_by_default(monkeypatch):
    from google_ecommerce_mcp import registry, write_policy

    class FakeMCP:
        def tool(self, **kwargs):
            return lambda fn: fn

    monkeypatch.setattr(registry, "OPTIONAL_MODULES", ())
    monkeypatch.setattr(registry, "ADS_MODULES", ())
    write_policy._unfreeze_for_tests()
    registry.register_all(FakeMCP(), freeze=False)
    assert not write_policy.allowlists_frozen()
    registry.register_all(FakeMCP())
    assert write_policy.allowlists_frozen()  # conftest restores the previous state after the test
    registry.register_all(FakeMCP())  # already frozen: nothing to do, no error


def test_registry_does_not_hide_a_broken_module(monkeypatch):
    """A module that exists but fails to import (missing dependency inside it) must not vanish silently."""
    from google_ecommerce_mcp import registry

    def import_module(name):
        raise ModuleNotFoundError("No module named 'google_ecommerce_mcp.missing_dep'",
                                  name="google_ecommerce_mcp.missing_dep")

    monkeypatch.setattr(registry.importlib, "import_module", import_module)
    with pytest.raises(ModuleNotFoundError):
        registry._import_optional("broken_write")

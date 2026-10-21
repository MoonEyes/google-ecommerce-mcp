"""End to end over the real MCP protocol: the server runs as a subprocess and a real MCP client talks to it on stdio.

What this proves that the unit tests do not: `python -m google_ecommerce_mcp` starts, answers initialize, lists
every tool of tests/golden_tools.json (same names, same order) with valid JSON schemas and read-only annotations
except the four *_apply tools, and every tool answers a real tools/call with a structured dict (an expected error such as not_configured, or a valid result), never a protocol error nor a
traceback on stderr.

Isolation (zero real network, no real keyring, no secret):
- the environment is built from scratch: no service variable, HOME / APPDATA / LOCALAPPDATA / USERPROFILE /
  HOMEDRIVE+HOMEPATH point to tmp_path, GOOGLE_TOKEN_FILE and GADS_TOKEN_FILE name files that do not exist;
- PYTHON_KEYRING_BACKEND selects keyring's `fail` backend, so the OS keyring is never opened;
- HTTPS_PROXY / HTTP_PROXY point to a local fake proxy (a thread of this test, 127.0.0.1) that refuses every
  CONNECT with 403 and records the target. The only Google tool that needs no configuration, pagespeed, therefore
  ends on a `network` error without leaving the machine. The ads connectors ignore proxy variables by design
  (trust_env False) but none is configured, so they never send anything (asserted below via the proxy log and
  the expected error codes).

The MCP client cannot run a tool "without network" by itself: the isolation above is what makes it offline.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
from pathlib import Path

import pytest

anyio = pytest.importorskip("anyio")
jsonschema = pytest.importorskip("jsonschema")
from mcp import ClientSession  # noqa: E402
from mcp.client import stdio as mcp_stdio  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

SRC = str(Path(__file__).resolve().parents[1] / "src")
TOTAL_TIMEOUT = 180.0  # 82 tools, each called once over stdio

# Minimal valid arguments for every tool (required parameters only, plus dates where the tool needs them to get
# past input validation and reach its configuration check).
PERIOD = {"start_date": "2026-09-01", "end_date": "2026-09-30"}
TOOL_ARGS: dict[str, dict] = {
    "server_status": {},
    "ga4_report": {},
    "ga4_compare": {},
    "ga4_key_event_audit": {},
    "ga4_realtime": {},
    "ga4_properties": {},
    "gsc_performance": {},
    "gsc_moves": {},
    "gsc_opportunities": {},
    "gsc_inspect_url": {"url": "https://shop.example/"},
    "gsc_sitemaps": {},
    "merchant_data_sources": {},
    "merchant_product_issues": {},
    "merchant_report_query": {},
    "gtm_inventory": {},
    "indexing_status": {"url": "https://shop.example/"},
    "pagespeed": {"url": "https://shop.example/"},
    "gads_accounts": {},
    "gads_performance": {"level": "account", **PERIOD},
    "gads_query": {"gaql": "SELECT campaign.name, metrics.clicks FROM campaign LIMIT 10"},
    "meta_ad_accounts": {},
    "meta_insights": PERIOD,
    "openai_ads_account": {},
    "openai_ads_insights": PERIOD,
    "openai_ads_conversions": {**PERIOD, "account_timezone": "Europe/Paris", "aggregation_level": "campaign",
                               "group_by_entity": False},
    "ads_overview": PERIOD,
    "clients_list": {},
    "client_profile": {},
    "client_check": {},
    "gads_apply": {"plan_id": "p1", "confirmation_token": "t", "confirm": "c"},
    "meta_apply": {"plan_id": "p1", "confirmation_token": "t", "confirm": "c"},
    "openai_ads_apply": {"plan_id": "p1", "confirmation_token": "t", "confirm": "c"},
    "google_apply": {"plan_id": "p1", "confirmation_token": "t", "confirm": "c"},
    "plan_get": {"plan_id": "p1"},
    "plan_list": {},
    "audit_log": {},
    "rollback_plan": {"plan_id": "p1"},
    # reads added by the platform waves (B1..B5)
    "gads_conversion_actions": {},
    "meta_campaigns": {},
    "meta_adsets": {},
    "meta_ads_list": {},
    "openai_ads_campaigns": {},
    "openai_ads_ad_groups": {"campaign_id": "cmpn_1"},
    "openai_ads_ads": {"ad_group_id": "adgrp_1"},
    "ga4_custom_definitions": {},
    "ga4_key_events": {},
    "gsc_sites": {},
    "merchant_products": {},
    "merchant_account_issues": {},
    "gtm_workspace_status": {},
    "gtm_get_tag": {"tag_id": "1", "workspace_id": "1"},
    "gtm_get_trigger": {"trigger_id": "1", "workspace_id": "1"},
    "gtm_get_variable": {"variable_id": "1", "workspace_id": "1"},
    "gtm_versions": {},
    "meta_pixel_health": {},
    "meta_dataset_quality": {},
    "meta_catalog_diagnostics": {},
    "meta_catalog_products": {},
    "meta_ig_insights": {},
    "meta_page_insights": {},
    # plan_* tools (readOnlyHint=True: a plan writes nothing; here GEM_WRITE is unset, so every plan is refused)
    "ga4_plan_key_event_create": {"event_name": "purchase_test"},
    "ga4_plan_custom_dimension_create": {"parameter_name": "plan_type", "display_name": "Plan type"},
    "gtm_plan_workspace_create": {"name": "gem test"},
    "gtm_plan_tag_upsert": {"workspace_id": "1", "tag": {"name": "t", "type": "html"}},
    "gtm_plan_trigger_upsert": {"workspace_id": "1", "trigger": {"name": "t", "type": "pageview"}},
    "gtm_plan_variable_upsert": {"workspace_id": "1", "variable": {"name": "v", "type": "c"}},
    "gtm_plan_quick_preview": {"workspace_id": "1"},
    "gsc_plan_sitemap_submit": {"sitemap_url": "https://shop.example/sitemap.xml"},
    "merchant_plan_product_patch": {"product_id": "online~fr~FR~sku1", "update_mask": ["title"],
                                    "product_attributes": {"title": "x"}},
    "gads_plan_pause_entities": {"level": "campaign", "ids": ["1"]},
    "gads_plan_budget_change": {"daily_budget_micros": 1_000_000},
    "gads_plan_add_negatives": {"keywords": ["free"]},
    "gads_plan_create_campaign": {"name": "gem test", "daily_budget_micros": 1_000_000},
    "meta_plan_pause": {"level": "campaign", "entity_ids": ["1"]},
    "meta_plan_budget_change": {"level": "campaign", "entity_id": "1", "daily_budget_minor": 1000},
    "meta_plan_create_campaign": {"name": "gem test", "objective": "OUTCOME_SALES", "special_ad_categories": []},
    "meta_plan_create_adset": {"name": "gem test", "campaign_id": "1", "optimization_goal": "LINK_CLICKS",
                               "billing_event": "IMPRESSIONS", "countries": ["FR"], "advantage_audience": 0},
    "meta_plan_create_ad": {"name": "gem test", "adset_id": "1", "creative_id": "1"},
    "openai_ads_plan_pause": {"level": "campaign", "entity_ids": ["cmpn_1"]},
    "openai_ads_plan_budget_change": {"campaign_id": "cmpn_1", "daily_budget_micros": 1_000_000},
    "openai_ads_plan_create_campaign": {"name": "gem test", "daily_budget_micros": 1_000_000,
                                        "start_time": 2_000_000_000},
    "meta_eco_plan_catalog_item_update": {"product_item_id": "1"},
}
# The frozen tool list (tests/golden_tools.json, regenerated with --regen-golden): its size is the tool count.
GOLDEN_TOOLS = [t["name"] for t in json.loads((Path(__file__).parent / "golden_tools.json").read_text(
    encoding="utf-8"))["tools"]]
WRITE_TOOLS = {"gads_apply", "meta_apply", "openai_ads_apply", "google_apply"}
PLAN_TOOLS = {name for name in TOOL_ARGS if "_plan_" in name}
# Client-registry, write-workflow and plan_* tools carry no freshness block.
NO_FRESHNESS = WRITE_TOOLS | PLAN_TOOLS | {"clients_list", "client_profile", "client_check", "plan_get", "plan_list",
                                           "audit_log", "rollback_plan"}

# What each tool must answer in an empty environment. None = a valid (non-error) result.
EXPECTED_ERROR: dict[str, str | None] = {
    "server_status": None,
    "ga4_report": "not_configured",
    "ga4_compare": "not_configured",
    "ga4_key_event_audit": "not_configured",
    "ga4_realtime": "not_configured",
    "ga4_properties": "not_authenticated",      # needs no id, stops at the missing token
    "gsc_performance": "not_configured",
    "gsc_moves": "not_configured",
    "gsc_opportunities": "not_configured",
    "gsc_inspect_url": "not_configured",
    "gsc_sitemaps": "not_configured",
    "merchant_data_sources": "not_configured",
    "merchant_product_issues": "not_configured",
    "merchant_report_query": "not_configured",
    "gtm_inventory": "not_configured",
    "indexing_status": "not_authenticated",
    "pagespeed": "network",                     # refused by the local fake proxy, never reaches Google
    "gads_accounts": "auth_failed",             # GADS_TOKEN_FILE does not exist
    "gads_performance": "not_configured",
    "gads_query": "not_configured",
    "meta_ad_accounts": "not_configured",
    "meta_insights": "not_configured",
    "openai_ads_account": "not_configured",
    "openai_ads_insights": "not_configured",
    "openai_ads_conversions": "not_configured",
    "ads_overview": None,                       # a partial overview: every platform not_configured
}


class RefusingProxy:
    """A local HTTP proxy that answers every request (CONNECT included) with 403 and records the first line."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.requests: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            with conn:
                conn.settimeout(2)
                try:
                    head = conn.recv(4096).decode("latin-1")
                    self.requests.append(head.split("\r\n", 1)[0])
                    conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                except OSError:
                    pass

    def __enter__(self) -> "RefusingProxy":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self.sock.close()


def isolated_env(tmp_path: Path, proxy_port: int) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    proxy = f"http://127.0.0.1:{proxy_port}"
    drive, rest = os.path.splitdrive(str(home))
    env = {
        "HOME": str(home), "USERPROFILE": str(home), "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"), "HOMEDRIVE": drive or "", "HOMEPATH": rest or str(home),
        "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local" / "share"),
        "GOOGLE_TOKEN_FILE": str(tmp_path / "no-google-token.json"),
        "GADS_TOKEN_FILE": str(tmp_path / "no-gads-token.json"),
        "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
        "HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "https_proxy": proxy, "http_proxy": proxy,
        "NO_PROXY": "", "no_proxy": "",
        "PYTHONPATH": SRC, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
    }
    return env


def _alive(process) -> bool:
    popen = getattr(process, "popen", None)  # mcp's Windows FallbackProcess wraps a subprocess.Popen
    if popen is not None:
        return popen.poll() is None
    return getattr(process, "returncode", 0) is None


def _kill(process) -> None:
    try:
        process.kill()
    except Exception:
        pass


@pytest.fixture
def spawned(monkeypatch):
    """Records every server process the MCP client starts, and kills any still alive at the end (finally)."""
    processes: list = []
    original = mcp_stdio._create_platform_compatible_process

    async def recording(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(mcp_stdio, "_create_platform_compatible_process", recording)
    try:
        yield processes
    finally:
        for process in processes:
            if _alive(process):
                _kill(process)


def _payload(result) -> dict:
    """The tool's dict: structuredContent when the server sends one, else the JSON of the single text content."""
    if result.structuredContent is not None:
        data = result.structuredContent
        return data.get("result", data) if isinstance(data, dict) and set(data) == {"result"} else data
    assert len(result.content) == 1, result.content
    assert result.content[0].type == "text", result.content[0]
    return json.loads(result.content[0].text)


async def _session_run(params: StdioServerParameters, errlog) -> dict:
    out: dict = {}
    with anyio.fail_after(TOTAL_TIMEOUT):
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                out["init"] = init
                listed = await session.list_tools()
                out["tools"] = listed.tools
                out["results"] = {}
                for tool in listed.tools:
                    args = TOOL_ARGS.get(tool.name, {})
                    out["results"][tool.name] = await session.call_tool(tool.name, args)
    return out


def test_server_answers_every_tool_over_real_stdio(tmp_path, spawned):
    from google_ecommerce_mcp import __version__

    errfile = tmp_path / "server-stderr.txt"
    with RefusingProxy() as proxy, open(errfile, "w+", encoding="utf-8") as errlog:
        params = StdioServerParameters(command=sys.executable, args=["-m", "google_ecommerce_mcp"],
                                       env=isolated_env(tmp_path, proxy.port), cwd=str(tmp_path))
        try:
            out = anyio.run(_session_run, params, errlog)
        finally:
            for process in spawned:
                if _alive(process):
                    _kill(process)
        errlog.seek(0)
        stderr = errlog.read()
        proxied = list(proxy.requests)

    assert len(spawned) == 1
    assert not _alive(spawned[0]), "the server process is still running after the session closed"

    # initialize
    info = out["init"].serverInfo
    assert info.name == "google-ecommerce-mcp"
    assert info.version == __version__  # not the mcp library version
    assert out["init"].capabilities.tools is not None

    # tools/list: every tool of the golden list (80 when written), read-only annotations except the four
    # *_apply tools, valid JSON schemas
    tools = {t.name: t for t in out["tools"]}
    assert len(out["tools"]) == len(GOLDEN_TOOLS) == len(tools) == len(TOOL_ARGS)
    assert set(tools) == set(TOOL_ARGS) == set(GOLDEN_TOOLS), set(tools) ^ set(TOOL_ARGS)
    assert [t.name for t in out["tools"]] == GOLDEN_TOOLS
    for name, tool in tools.items():
        ann = tool.annotations
        assert ann is not None, name
        if name in WRITE_TOOLS:
            assert ann.readOnlyHint is False and ann.destructiveHint is True, name
        else:
            assert ann.readOnlyHint is True and ann.destructiveHint is False, name
        assert tool.description and tool.description.strip(), name
        assert tool.inputSchema.get("type") == "object", name
        jsonschema.Draft202012Validator.check_schema(tool.inputSchema)
        # The minimal arguments used below are valid against the published schema.
        jsonschema.validate(TOOL_ARGS[name], tool.inputSchema)
        if tool.outputSchema is not None:
            jsonschema.Draft202012Validator.check_schema(tool.outputSchema)

    # tools/call on every tool: a structured dict, never a protocol error
    for name, result in out["results"].items():
        assert result.isError is False, (name, result.content)
        payload = _payload(result)
        assert isinstance(payload, dict), (name, payload)
        if name not in NO_FRESHNESS:
            assert "fetched_at" in payload and "freshness" in payload, (name, sorted(payload))
        if name in PLAN_TOOLS:  # GEM_WRITE is not set: a plan is refused, never gets a confirmation token
            assert "error" in payload and "confirmation_token" not in payload, (name, payload)
        expected = EXPECTED_ERROR.get(name, "__any__")
        if expected == "__any__":
            assert "Traceback" not in json.dumps(payload), name
            continue
        if expected is None:
            assert "error" not in payload, (name, payload)
        else:
            assert payload.get("error") == expected, (name, payload)
            assert "Traceback" not in json.dumps(payload), name

    status = _payload(out["results"]["server_status"])
    assert status["token_storage"] == "file"
    assert status["ga4"] is status["search_console"] is status["merchant_center"] is status["tag_manager"] is False
    assert status["ads"]["google_ads"] is False and status["ads"]["meta"] is False
    assert status["ads"]["openai_ads"] is False
    assert status["version"] == __version__
    overview = _payload(out["results"]["ads_overview"])
    assert overview["partial"] is True and overview["combined"] is None
    assert sorted(overview["not_configured"]) == ["google_ads", "meta", "openai_ads"]
    assert "surestime" in overview["warning"]

    # zero real network: the only outgoing attempt is pagespeed's, and it stopped at the local proxy
    assert proxied and all(line.startswith("CONNECT pagespeedonline.googleapis.com:443") for line in proxied), proxied
    # no traceback, no unhandled exception on the server's stderr
    assert "Traceback" not in stderr, stderr
    assert "Error" not in stderr.replace("CallToolRequest", ""), stderr


def test_server_process_is_killed_even_when_the_session_fails(tmp_path, spawned):
    """A session that raises mid-way still leaves no server process behind (stdio_client + fixture finally)."""

    class Boom(Exception):
        pass

    async def run(params):
        with anyio.fail_after(TOTAL_TIMEOUT):
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    raise Boom()

    with RefusingProxy() as proxy, open(tmp_path / "stderr.txt", "w+", encoding="utf-8") as errlog:
        params = StdioServerParameters(command=sys.executable, args=["-m", "google_ecommerce_mcp"],
                                       env=isolated_env(tmp_path, proxy.port), cwd=str(tmp_path))
        with pytest.raises(BaseException) as caught:
            anyio.run(run, params)

    def contains(exc: BaseException) -> bool:  # Boom itself, or inside an (anyio) exception group
        return isinstance(exc, Boom) or any(contains(e) for e in getattr(exc, "exceptions", ()))

    assert contains(caught.value), caught.value
    assert len(spawned) == 1
    assert not _alive(spawned[0])

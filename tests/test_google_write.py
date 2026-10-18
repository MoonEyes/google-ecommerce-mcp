"""Shared Google write plumbing (google_write): the write token is the only one used to write, the HTTP clients
pin their hosts and refuse unsigned writes, the pre-checks fail before any network call, registration.

Also holds the offline doubles reused by test_gtm_tools.py and test_ga4_tools.py: a fake Google server (GTM v2 +
GA4 Admin v1beta) behind the AdsClient sessions, and a two-client registry (acme, beta) with separate read and
write tokens. Zero network (ads_fakes.no_network is autouse)."""

from __future__ import annotations

import copy
import json
import re
from urllib.parse import urlsplit

import pytest

from ads_fakes import GOOGLE_ACCESS_PREFIX, GOOGLE_REFRESH_PREFIX, FakeResponse, no_network  # noqa: F401
from clients_fakes import ACME, BETA, setup_env, write_registry
from google_ecommerce_mcp import ads_common, clients, ga4_tools, google_write as gw, gtm_tools, write_core, \
    write_policy
from google_ecommerce_mcp.write_journal import Journal

AUTH = "https://www.googleapis.com/auth/"
WRITE_SCOPES = [AUTH + "tagmanager.edit.containers", AUTH + "tagmanager.edit.containerversions",
                AUTH + "analytics.edit"]
READ_SCOPES = [AUTH + "analytics.readonly", AUTH + "tagmanager.readonly"]
GTM = "https://tagmanager.googleapis.com/tagmanager/v2"
ADMIN = "https://analyticsadmin.googleapis.com/v1beta"


def token_json(token: str, scopes: list[str], **extra) -> str:
    return json.dumps({"token": token, "refresh_token": GOOGLE_REFRESH_PREFIX + "fake-refresh-" + token[-6:],
                       "client_id": "fake.apps.googleusercontent.com", "client_secret": "fake-secret",
                       "token_uri": "https://oauth2.googleapis.com/token", "scopes": scopes,
                       "expiry": "2099-01-01T00:00:00Z", **extra})


def bearer(client_id: str, kind: str) -> str:
    return f"{GOOGLE_ACCESS_PREFIX}{client_id}-{kind}-0123456789"


# ------------------------------------------------------------------ fake Google server
class FakeGoogle:
    """GTM v2 and GA4 Admin v1beta in memory. Records (method, url, authorization, params, json)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.gtm: dict[str, dict] = {}       # container key "acc/cont" -> {"workspaces": {id: ws}, "entities": ...}
        self.ga4: dict[str, dict] = {}       # property id -> {"keyEvents": [...], "customDimensions": [...], ...}
        self.preview_error: int | None = None
        self._seq = 100
        self.versions: dict[str, list] = {}

    # --- setup helpers
    def add_container(self, account: str, container: str, workspaces=(("1", "Default Workspace", ""),)) -> None:
        key = f"{account}/{container}"
        self.gtm[key] = {"public": f"GTM-C{container}", "workspaces": {}, "entities": {}}
        for wid, name, desc in workspaces:
            self.gtm[key]["workspaces"][wid] = {"path": f"accounts/{account}/containers/{container}/workspaces/{wid}",
                                                "accountId": account, "containerId": container,
                                                "workspaceId": wid, "name": name, "description": desc}
        self.versions[key] = [{"containerVersionId": "3", "name": "v3", "numTags": "4"},
                              {"containerVersionId": "7", "name": "v7", "numTags": "5"}]

    def add_entity(self, account: str, container: str, workspace: str, plural: str, entity: dict) -> dict:
        idkey = {"tags": "tagId", "triggers": "triggerId", "variables": "variableId"}[plural]
        self._seq += 1
        eid = str(entity.get(idkey) or self._seq)
        stored = {**copy.deepcopy(entity), idkey: eid, "accountId": account, "containerId": container,
                  "workspaceId": workspace, "fingerprint": f"fp{self._seq}",
                  "path": f"accounts/{account}/containers/{container}/workspaces/{workspace}/{plural}/{eid}"}
        self.gtm[f"{account}/{container}"]["entities"].setdefault((workspace, plural), {})[eid] = stored
        return stored

    def add_property(self, prop: str) -> None:
        self.ga4[prop] = {"keyEvents": [], "customDimensions": [], "customMetrics": []}

    def writes(self) -> list[dict]:
        return [c for c in self.calls if c["method"] in ("POST", "PUT", "PATCH", "DELETE")
                and ":quick_preview" not in c["url"]]

    # --- session API
    def request(self, method, url, **kwargs):
        headers = kwargs.get("headers") or {}
        self.calls.append({"method": method, "url": url, "auth": headers.get("Authorization"),
                           "params": copy.deepcopy(kwargs.get("params")), "json": copy.deepcopy(kwargs.get("json"))})
        path = urlsplit(url).path
        if url.startswith(GTM):
            return self._gtm(method, path[len("/tagmanager/v2"):], kwargs)
        if url.startswith(ADMIN):
            return self._ga4(method, path[len("/v1beta"):], kwargs)
        return FakeResponse(404, {"error": {"message": "unexpected host"}})

    def _gtm(self, method, path, kwargs):
        if path == "/accounts":
            accounts = sorted({k.split("/")[0] for k in self.gtm})
            return FakeResponse(200, {"account": [{"accountId": a, "path": f"accounts/{a}"} for a in accounts]})
        m = re.fullmatch(r"/accounts/(\d+)/containers", path)
        if m:
            return FakeResponse(200, {"container": [{"accountId": m.group(1), "containerId": k.split("/")[1],
                                                     "publicId": v["public"]}
                                                    for k, v in self.gtm.items() if k.startswith(m.group(1) + "/")]})
        m = re.fullmatch(r"/accounts/(\d+)/containers/(\d+)(/.*)?", path)
        if not m or f"{m.group(1)}/{m.group(2)}" not in self.gtm:
            return FakeResponse(404, {"error": {"code": 404, "message": "Not found", "status": "NOT_FOUND"}})
        acc, cont, rest = m.group(1), m.group(2), m.group(3) or ""
        box = self.gtm[f"{acc}/{cont}"]
        if rest == "/workspaces":
            if method == "GET":
                return FakeResponse(200, {"workspace": [copy.deepcopy(w) for w in box["workspaces"].values()]})
            self._seq += 1
            wid = str(self._seq)
            body = kwargs.get("json") or {}
            box["workspaces"][wid] = {"path": f"accounts/{acc}/containers/{cont}/workspaces/{wid}", "accountId": acc,
                                      "containerId": cont, "workspaceId": wid, **body}
            return FakeResponse(200, copy.deepcopy(box["workspaces"][wid]))
        if rest == "/version_headers":
            return FakeResponse(200, {"containerVersionHeader": copy.deepcopy(self.versions[f"{acc}/{cont}"])})
        if rest == "/versions:live":
            return FakeResponse(200, {"containerVersionId": "7", "name": "v7"})
        m = re.fullmatch(r"/workspaces/(\d+)(:quick_preview|/status|/(tags|triggers|variables)(?:/(\d+))?)?", rest)
        if not m or m.group(1) not in box["workspaces"]:
            return FakeResponse(404, {"error": {"code": 404, "message": "Not found"}})
        wid, tail, plural, eid = m.groups()
        if tail == ":quick_preview":
            if self.preview_error:
                return FakeResponse(self.preview_error, {"error": {"code": self.preview_error, "message": "nope"}})
            tags = list(box["entities"].get((wid, "tags"), {}).values())
            return FakeResponse(200, {"compilerError": False, "syncStatus": {"mergeConflict": False},
                                      "containerVersion": {"tag": tags}})
        if tail == "/status":
            changes = [{"changeStatus": "added", "tag": {"name": t["name"], "tagId": t["tagId"]}}
                       for t in box["entities"].get((wid, "tags"), {}).values()]
            return FakeResponse(200, {"workspaceChange": changes})
        if tail is None:
            return FakeResponse(200, copy.deepcopy(box["workspaces"][wid]))
        store = box["entities"].setdefault((wid, plural), {})
        if eid is None:
            if method == "POST":
                return FakeResponse(200, self.add_entity(acc, cont, wid, plural, kwargs.get("json") or {}))
            return FakeResponse(200, {plural[:-1]: list(store.values())})
        if eid not in store:
            return FakeResponse(404, {"error": {"code": 404, "message": "Not found"}})
        if method == "GET":
            return FakeResponse(200, copy.deepcopy(store[eid]))
        if method == "PUT":
            fp = (kwargs.get("params") or {}).get("fingerprint")
            if fp is not None and fp != store[eid]["fingerprint"]:
                return FakeResponse(400, {"error": {"code": 400, "message": "fingerprint mismatch"}})
            self._seq += 1
            idkey = {"tags": "tagId", "triggers": "triggerId", "variables": "variableId"}[plural]
            store[eid] = {**copy.deepcopy(kwargs.get("json") or {}), idkey: eid, "accountId": acc,
                          "containerId": cont, "workspaceId": wid, "fingerprint": f"fp{self._seq}",
                          "path": store[eid]["path"]}
            return FakeResponse(200, copy.deepcopy(store[eid]))
        return FakeResponse(405, {"error": {"code": 405, "message": "method"}})

    def _ga4(self, method, path, kwargs):
        m = re.fullmatch(r"/properties/(\d+)/(keyEvents|customDimensions|customMetrics)", path)
        if not m or m.group(1) not in self.ga4:
            return FakeResponse(404, {"error": {"code": 404, "message": "Not found", "status": "NOT_FOUND"}})
        prop, coll = m.groups()
        items = self.ga4[prop][coll]
        if method == "GET":
            return FakeResponse(200, {coll: copy.deepcopy(items)} if items else {})
        body = copy.deepcopy(kwargs.get("json") or {})
        self._seq += 1
        body["name"] = f"properties/{prop}/{coll}/{self._seq}"
        items.append(body)
        return FakeResponse(200, copy.deepcopy(body))


# ------------------------------------------------------------------ fixtures
def registry_entry(base: dict, *, gtm_allow: list, ga4_allow: list, enabled: bool = True) -> dict:
    entry = copy.deepcopy(base)
    entry["write"] = {"gtm": {"enabled": enabled, "account_allow": gtm_allow},
                      "ga4": {"enabled": enabled, "account_allow": ga4_allow}}
    return entry


@pytest.fixture
def gworld(monkeypatch, tmp_path):
    """Two clients (acme sandbox, beta), each with its own read and write token, a fake Google server behind every
    AdsClient session of ga4_tools / gtm_tools, a fresh journal / handler table / allow-list registry."""
    keyring, path = setup_env(monkeypatch, tmp_path)
    write_registry(path, {"acme": registry_entry(ACME, gtm_allow=["6111"], ga4_allow=["111111111"]),
                          "beta": registry_entry(BETA, gtm_allow=["6222"], ga4_allow=["222222222"])})
    monkeypatch.setenv("GEM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GEM_WRITE", "1")
    monkeypatch.setenv("GEM_WRITE_HMAC_KEY", "ab" * 32)
    monkeypatch.delenv("GEM_LIVE_TEST", raising=False)
    for cid in ("acme", "beta"):
        monkeypatch.setenv(f"GEM_SECRET_{cid.upper()}_GOOGLE", token_json(bearer(cid, "READ"), READ_SCOPES))
        monkeypatch.setenv(f"GEM_SECRET_{cid.upper()}_GOOGLE_W", token_json(bearer(cid, "WRITE"), WRITE_SCOPES))
    monkeypatch.setattr(write_core, "journal", Journal())
    monkeypatch.setattr(write_core, "_HANDLERS", {})
    monkeypatch.setattr(write_policy, "_FIELD_ALLOWLIST", {})
    monkeypatch.setattr(write_policy, "_FROZEN", False)
    monkeypatch.setattr(write_policy, "_TEST_PERMISSIVE", False)
    ads_common._forget_consumed_grants()
    gw._forget_tokens()
    server = FakeGoogle()
    server.add_container("6111", "2111", workspaces=(("1", "Default Workspace", ""), ("5", "Sprint", "")))
    server.add_container("6222", "2222")
    server.add_property("111111111")
    server.add_property("222222222")
    for module in (gtm_tools, ga4_tools):
        for client in (module.READ, module.WRITE):
            monkeypatch.setattr(client, "session", server)
            monkeypatch.setattr(client, "BLOCKED_ATTEMPTS", [])
            client.cache.clear()
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")
    gw.register(mcp)
    ga4_tools.register(mcp)
    gtm_tools.register(mcp)
    yield server
    gw._forget_tokens()
    ads_common._forget_consumed_grants()


def approve_if_needed(client_id: str, plan: dict) -> None:
    """Out-of-band approval injected as test_write_approval does (none of the GA4 / GTM plans needs one)."""
    if plan.get("human_approval_required"):
        assert write_core._write_approval(client_id, plan["plan_id"], approver="test")["status"] == "approved"


def apply(client_id: str, plan: dict, **kw) -> dict:
    assert "error" not in plan, plan
    approve_if_needed(client_id, plan)
    return write_core._apply_tool("google_apply", client_id, plan["plan_id"],
                                  kw.get("token", plan["confirmation_token"]), kw.get("confirm", plan["confirm"]))


# ------------------------------------------------------------------ tokens
class SecretSpy:
    client_id = "acme"
    legacy = False

    def __init__(self, read=None, write=None):
        self.values = {False: read, True: write}
        self.asked: list[tuple[str, bool]] = []

    def secret(self, kind, *, write=False):
        self.asked.append((kind, write))
        return self.values[write]


def test_write_provider_reads_only_the_write_secret():
    spy = SecretSpy(read=token_json(bearer("acme", "READ"), READ_SCOPES),
                    write=token_json(bearer("acme", "WRITE"), WRITE_SCOPES))
    headers = gw.GoogleTokens(spy, write=True).headers()
    assert headers["Authorization"] == "Bearer " + bearer("acme", "WRITE")
    assert spy.asked == [("google", True)]


def test_read_provider_reads_only_the_read_secret():
    spy = SecretSpy(read=token_json(bearer("acme", "READ"), READ_SCOPES), write="never read")
    assert gw.GoogleTokens(spy, write=False).headers()["Authorization"] == "Bearer " + bearer("acme", "READ")
    assert spy.asked == [("google", False)]


def test_missing_write_token_never_falls_back_to_the_read_token():
    spy = SecretSpy(read=token_json(bearer("acme", "READ"), READ_SCOPES), write=None)
    with pytest.raises(gw.WriteTokenMissing) as caught:
        gw.GoogleTokens(spy, write=True).headers()
    assert "write token missing" in str(caught.value)
    assert "google-ecommerce-mcp setup --client acme --write" in str(caught.value)
    assert spy.asked == [("google", True)]


def test_unreadable_token_is_never_echoed():
    spy = SecretSpy(write="{not json " + bearer("acme", "WRITE"))
    with pytest.raises(gw.NotAuthenticated) as caught:
        gw.GoogleTokens(spy, write=True).headers()
    assert bearer("acme", "WRITE") not in str(caught.value)


def test_expired_token_refresh_goes_through_the_hardened_session(monkeypatch):
    import requests

    sessions = []

    class Offline(ads_common.NoRedirectSession):
        def request(self, method, url, *args, **kwargs):
            sessions.append((method, url, self.max_redirects, self.trust_env))
            raise requests.ConnectionError("offline")

    monkeypatch.setattr(gw, "hardened_session", lambda url: Offline())
    raw = token_json(bearer("acme", "WRITE"), WRITE_SCOPES, expiry="2001-01-01T00:00:00Z")
    with pytest.raises(gw.TokenNetworkError):
        gw.GoogleTokens(SecretSpy(write=raw), write=True).headers()
    assert sessions == [("POST", gw.GOOGLE_TOKEN_URI, 0, False)]


def test_write_call_without_write_token_sends_nothing(gworld, monkeypatch):
    monkeypatch.delenv("GEM_SECRET_ACME_GOOGLE_W")
    ctx = clients.resolve("acme")
    out = gw.write_call(gtm_tools.WRITE, ctx, "GET", f"{GTM}/accounts/6111/containers/2111/workspaces")
    assert out["error"] == "write_token_missing" and "--client acme --write" in out["fix"]
    assert gworld.calls == []


def test_write_call_refuses_a_token_without_the_edit_scope(gworld, monkeypatch):
    monkeypatch.setenv("GEM_SECRET_ACME_GOOGLE_W", token_json(bearer("acme", "WRITE"), READ_SCOPES))
    ctx = clients.resolve("acme")
    out = gw.write_call(gtm_tools.WRITE, ctx, "GET", f"{GTM}/accounts/6111/containers/2111/workspaces")
    assert out["error"] == "missing_scope" and out["required_scope"] == AUTH + "tagmanager.edit.containers"
    assert gworld.calls == []


def test_write_and_read_calls_carry_their_own_token(gworld):
    ctx = clients.resolve("acme")
    gw.write_call(gtm_tools.WRITE, ctx, "GET", f"{GTM}/accounts/6111/containers/2111/workspaces")
    gw.read_call(gtm_tools.READ, ctx, "GET", f"{GTM}/accounts/6111/containers/2111/workspaces")
    assert [c["auth"] for c in gworld.calls] == ["Bearer " + bearer("acme", "WRITE"), "Bearer " + bearer("acme", "READ")]


# ------------------------------------------------------------------ HTTP clients
def test_clients_pin_their_hosts():
    with pytest.raises(ValueError):
        gw.google_write_client("gtm", [("POST", r"https://evil\.example\.com/tagmanager/v2/accounts", None, "write")])
    with pytest.raises(ValueError):
        gw.google_read_client("ga4", [("GET", r"https://tagmanager\.googleapis\.com/tagmanager/v2/accounts")])
    with pytest.raises(ValueError):
        gw.google_write_client("unknown", [])
    with pytest.raises(ValueError):  # a joker is refused by WriteAllowList
        gw.google_write_client("gtm", [("PUT", r"https://tagmanager\.googleapis\.com/tagmanager/v2/accounts/.*",
                                        None, "write")])
    ok = gw.google_write_client("gsc", [("PUT", r"https://www\.googleapis\.com/webmasters/v3/sites/[^/]+/sitemaps/"
                                                r"[^/]+", None, "write")])
    assert ok.platform == "gsc" and ok.write_allow_list is not None


def test_read_client_can_never_write(gworld):
    ctx = clients.resolve("acme")
    out = gw.read_call(gtm_tools.READ, ctx, "POST", f"{GTM}/accounts/6111/containers/2111/workspaces/5/tags",
                       json={"name": "x", "type": "html"})
    assert out["error"] == "blocked" and gworld.calls == []


@pytest.mark.parametrize("method, url, reason", [
    ("POST", f"{GTM}/accounts/6111/containers/2111/workspaces/5/tags", "grant_missing"),
    ("POST", f"{GTM}/accounts/6111/containers/2111/workspaces/5:create_version", "not_on_write_allow_list"),
    ("POST", f"{GTM}/accounts/6111/containers/2111/versions/7:publish", "not_on_write_allow_list"),
    ("DELETE", f"{GTM}/accounts/6111/containers/2111/workspaces/5/tags/9", "not_on_write_allow_list"),
    ("POST", f"{ADMIN}/properties/111111111/customDimensions/3:archive", "not_on_write_allow_list"),
    ("DELETE", f"{ADMIN}/properties/111111111/keyEvents/3", "not_on_write_allow_list"),
])
def test_write_client_refuses_unsigned_or_unlisted_writes(gworld, method, url, reason):
    ctx = clients.resolve("acme")
    client = gtm_tools.WRITE if url.startswith(GTM) else ga4_tools.WRITE
    out = gw.write_call(client, ctx, method, url, json={"name": "x"})
    assert out["error"] == "blocked" and out["reason"] == reason
    assert gworld.calls == []


def test_forged_grant_is_refused(gworld):
    ctx = clients.resolve("acme")
    url = f"{GTM}/accounts/6111/containers/2111/workspaces/5/tags"
    forged = ads_common.WriteGrant(client_id="acme", plan_id="pl_0000000000000000", op_id="x", method="POST",
                                   url=url, body_digest=ads_common.body_digest({"name": "x"}))
    out = gw.write_call(gtm_tools.WRITE, ctx, "POST", url, json={"name": "x"}, grant=forged)
    assert out["error"] == "blocked" and out["reason"] == "grant_unsigned" and gworld.calls == []


# ------------------------------------------------------------------ pre-checks and helpers
def test_write_precheck_fails_before_any_network(gworld, monkeypatch):
    ctx = clients.resolve("acme")
    assert gw.write_precheck(ctx, "gtm", "6111") is None
    assert gw.write_precheck(ctx, "gtm", "6222")["error"] == "account_not_owned"
    assert gw.write_precheck(ctx, "gtm", "2111")["error"] == "account_not_allowed"  # owned container, not allowed
    assert gw.write_precheck(ctx, "gsc", "sc-domain:acme.fr")["error"] == "write_not_enabled"
    monkeypatch.setenv("GEM_WRITE", "0")
    assert gw.write_precheck(ctx, "gtm", "6111")["error"] == "write_disabled"
    assert gworld.calls == []


def test_require_bound_refuses_another_account_before_any_request(gworld):
    from google_ecommerce_mcp.write_core import WriteOp

    ctx = clients.resolve("acme")
    op = WriteOp(op_id="o", platform="ga4", account_id="111111111", action="key_event_create", method="POST",
                 url="https://x")
    gw.require_bound(ctx, "ga4", "111111111", op)
    for url_id in ("222222222", "999"):
        with pytest.raises(gw.SnapshotError) as caught:
            gw.require_bound(ctx, "ga4", url_id, op)
        assert caught.value.code == "account_not_bound"
    assert gworld.calls == []


def test_account_binding_helper(gworld):
    ctx = clients.resolve("acme")
    assert gw.google_account_binding(ctx, "ga4", "property_id") == "111111111"
    assert gw.google_account_binding(ctx, "ga4", "property_id", "properties/111111111") == "111111111"
    with pytest.raises(clients.ClientError):
        gw.google_account_binding(ctx, "ga4", "property_id", "222222222")


def test_tool_guard_turns_errors_into_dicts():
    @gw.tool_guard("now")
    def boom(client_id=None):
        raise RuntimeError("secret " + bearer("acme", "WRITE"))

    out = boom(client_id="acme")
    assert out["error"] == "internal_error" and out["client_id"] == "acme" and out["freshness"] == "now"
    assert bearer("acme", "WRITE") not in json.dumps(out)


def test_register_is_idempotent_and_never_freezes(gworld):
    from mcp.server.fastmcp import FastMCP

    snapshot = write_policy.golden_allowlists()
    assert set(snapshot) >= {"ga4", "gtm"}
    assert gw.register(FastMCP("again")) == []
    assert write_policy.golden_allowlists() == snapshot
    assert not write_policy.allowlists_frozen()
    assert write_core.get_handler("ga4") is ga4_tools.HANDLER and write_core.get_handler("gtm") is gtm_tools.HANDLER
    from test_allowlist_registry import _check_shape
    _check_shape(snapshot)


def test_tools_annotations_and_client_id(gworld):
    import asyncio

    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("annotations")
    names = ga4_tools.register(mcp) + gtm_tools.register(mcp)
    assert set(names) == set(ga4_tools.TOOLS) | set(gtm_tools.TOOLS)
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    for name in names:
        tool = tools[name]
        assert tool.annotations.readOnlyHint is True and tool.annotations.destructiveHint is False, name
        schema = tool.inputSchema
        assert "client_id" in schema["properties"] and "client_id" not in schema.get("required", []), name
        assert tool.title and tool.description, name
        if "_plan_" in name:
            assert tool.annotations.idempotentHint is False, name
    assert not any(word in name for name in names for word in ("publish", "create_version", "delete", "archive"))

"""Search Console reads (gsc_sites): multi-site listing per client with the read token, other clients' sites
masked, configured site not visible, two isolated clients. Also holds the offline doubles reused by
test_gsc_write.py: a fake Search Console server behind the AdsClient sessions of gsc_tools / gsc_write, a
two-client registry (acme: sc-domain property, beta: URL-prefix property) with separate read and write tokens.
Zero network (ads_fakes.no_network is autouse)."""

from __future__ import annotations

import copy
import re
from urllib.parse import unquote, urlsplit

import pytest

from ads_fakes import GOOGLE_ACCESS_PREFIX, FakeResponse, no_network  # noqa: F401
from clients_fakes import ACME, BETA, setup_env, write_registry
from google_ecommerce_mcp import ads_common, google_write as gw, gsc_tools, gsc_write, write_core, write_policy
from google_ecommerce_mcp.write_journal import Journal
from test_google_write import AUTH, bearer, token_json

SITES = "https://www.googleapis.com/webmasters/v3/sites"
WRITE_SCOPES = [AUTH + "webmasters", AUTH + "content"]
READ_SCOPES = [AUTH + "webmasters.readonly", AUTH + "content"]
ACME_SITE = "sc-domain:acme.fr"
BETA_SITE = "https://www.beta.com/"


class FakeSearchConsole:
    """webmasters v3 sites list and sitemaps in memory. Records (method, url, auth, params, json)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.sites: dict[str, list[dict]] = {}          # client id -> siteEntry list
        self.sitemaps: dict[str, dict[str, dict]] = {}  # site -> sitemap URL -> entry

    def writes(self) -> list[dict]:
        return [c for c in self.calls if c["method"] in ("POST", "PUT", "PATCH", "DELETE")]

    @staticmethod
    def client_of(auth: str | None) -> str:
        return str(auth or "").removeprefix("Bearer ").removeprefix(GOOGLE_ACCESS_PREFIX).split("-")[0]

    def request(self, method, url, **kwargs):
        headers = kwargs.get("headers") or {}
        auth = headers.get("Authorization")
        self.calls.append({"method": method, "url": url, "auth": auth, "params": copy.deepcopy(kwargs.get("params")),
                           "json": copy.deepcopy(kwargs.get("json"))})
        path = urlsplit(url).path
        if url == SITES and method == "GET":
            return FakeResponse(200, {"siteEntry": copy.deepcopy(self.sites.get(self.client_of(auth), []))})
        m = re.fullmatch(r"/webmasters/v3/sites/([^/]+)/sitemaps(?:/([^/]+))?", path)
        if not m:
            return FakeResponse(404, {"error": {"code": 404, "message": "unexpected url"}})
        site = unquote(m.group(1))
        if site not in self.sitemaps:
            return FakeResponse(403, {"error": {"code": 403, "message": "User does not have sufficient permission",
                                                "status": "PERMISSION_DENIED"}})
        box = self.sitemaps[site]
        if m.group(2) is None and method == "GET":
            return FakeResponse(200, {"sitemap": copy.deepcopy(list(box.values()))} if box else {})
        feed = unquote(m.group(2) or "")
        if method == "PUT":
            box[feed] = {"path": feed, "lastSubmitted": "2026-10-18T10:00:00Z", "isPending": True,
                         "isSitemapsIndex": False, "errors": "0", "warnings": "0"}
            return FakeResponse(200, content=b"")
        if method == "DELETE":
            if feed not in box:
                return FakeResponse(404, {"error": {"code": 404, "message": "Not found"}})
            del box[feed]
            return FakeResponse(204, content=b"")
        return FakeResponse(405, {"error": {"code": 405, "message": "method"}})


def registry_entry(base: dict, *, site: str, allow_delete: bool, enabled: bool = True) -> dict:
    entry = copy.deepcopy(base)
    entry["write"] = {"gsc": {"enabled": enabled, "account_allow": [site], "allow_delete": allow_delete}}
    return entry


@pytest.fixture
def gscworld(monkeypatch, tmp_path):
    """Two clients (acme sandbox: sc-domain:acme.fr, rollback deletes allowed; beta: https://www.beta.com/),
    each with its own read and write token, the fake Search Console behind every AdsClient session of
    gsc_tools / gsc_write, a fresh journal / handler table / allow-list registry."""
    _, path = setup_env(monkeypatch, tmp_path)
    write_registry(path, {"acme": registry_entry(ACME, site=ACME_SITE, allow_delete=True),
                          "beta": registry_entry(BETA, site=BETA_SITE, allow_delete=False)})
    monkeypatch.setenv("GEM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GEM_WRITE", "1")
    monkeypatch.setenv("GEM_WRITE_HMAC_KEY", "cd" * 32)
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
    server = FakeSearchConsole()
    server.sites = {"acme": [{"siteUrl": ACME_SITE, "permissionLevel": "siteOwner"},
                             {"siteUrl": "https://shop.acme.fr/", "permissionLevel": "siteRestrictedUser"},
                             {"siteUrl": BETA_SITE, "permissionLevel": "siteFullUser"}],
                    "beta": [{"siteUrl": BETA_SITE, "permissionLevel": "siteFullUser"}]}
    server.sitemaps = {ACME_SITE: {}, BETA_SITE: {}}
    for client in (gsc_tools.READ, gsc_write.WRITE):
        monkeypatch.setattr(client, "session", server)
        monkeypatch.setattr(client, "BLOCKED_ATTEMPTS", [])
        client.cache.clear()
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")
    gsc_tools.register(mcp)
    gsc_write.register(mcp)
    yield server
    gw._forget_tokens()
    ads_common._forget_consumed_grants()


# ------------------------------------------------------------------ reads
def test_sites_lists_the_clients_own_site_and_masks_the_others(gscworld):
    out = gsc_tools.gsc_sites(client_id="acme")
    assert out["client_id"] == "acme" and out["configured_site"] == ACME_SITE
    first = out["sites"][0]
    assert first == {"site_url": ACME_SITE, "permission_level": "siteOwner", "owned": True, "configured": True,
                     "kind": "domain", "can_submit_sitemaps": True}
    others = out["sites"][1:]
    assert len(others) == 2 and all(o["masked"] and not o["owned"] for o in others)
    assert BETA_SITE not in str(out) and "shop.acme.fr" not in str(out)  # another client's site never shown
    assert out["count"] == 3 and out["owned_count"] == 1
    assert {c["auth"] for c in gscworld.calls} == {"Bearer " + bearer("acme", "READ")}
    assert out["fetched_at"] and out["freshness"] and "untrusted_text" in out


def test_sites_warns_when_the_configured_site_is_not_visible(gscworld):
    gscworld.sites["acme"] = [{"siteUrl": "https://www.acme.fr/", "permissionLevel": "siteOwner"}]
    out = gsc_tools.gsc_sites(client_id="acme")
    assert "not visible" in out["warning"] and "sc-domain:" in out["warning"]


def test_sites_notes_a_level_that_cannot_submit(gscworld):
    gscworld.sites["beta"] = [{"siteUrl": BETA_SITE, "permissionLevel": "siteRestrictedUser"}]
    out = gsc_tools.gsc_sites(client_id="beta")
    assert out["sites"][0]["can_submit_sitemaps"] is False and "cannot submit" in out["note"]


def test_two_clients_see_only_their_token_and_site(gscworld):
    acme = gsc_tools.gsc_sites(client_id="acme")
    beta = gsc_tools.gsc_sites(client_id="beta")
    assert beta["sites"] == [{"site_url": BETA_SITE, "permission_level": "siteFullUser", "owned": True,
                              "configured": True, "kind": "url_prefix", "can_submit_sitemaps": True}]
    assert acme["client_id"] == "acme" and beta["client_id"] == "beta"
    assert [c["auth"] for c in gscworld.calls] == ["Bearer " + bearer("acme", "READ"),
                                                   "Bearer " + bearer("beta", "READ")]
    assert gscworld.writes() == []


def test_read_error_and_unknown_client(gscworld, monkeypatch):
    monkeypatch.delenv("GEM_SECRET_ACME_GOOGLE")
    out = gsc_tools.gsc_sites(client_id="acme")
    assert out["error"] == "auth_failed" and out["client_id"] == "acme"
    assert gsc_tools.gsc_sites(client_id="nobody")["error"] == "unknown_client"
    assert gscworld.calls == []


def test_read_client_has_no_write_list():
    assert gsc_tools.READ.write_allow_list is None
    assert gsc_tools.READ.allow_list.patterns == [("GET", r"https://www\.googleapis\.com/webmasters/v3/sites")]

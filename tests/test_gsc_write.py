"""Search Console sitemap submission: plan -> google_apply on a fake Search Console, sitemap outside the property
refused, transport refusal of encoded paths reported cleanly, rollback (DELETE only of a sitemap this server
added, with allow_delete and a human approval), foreign property without any request, write token only, two
isolated clients, allow-list without sites.add/delete. Offline."""

from __future__ import annotations

import asyncio
import dataclasses
from urllib.parse import quote

import pytest

from ads_fakes import no_network  # noqa: F401
from google_ecommerce_mcp import clients, gsc_write, write_core
from google_ecommerce_mcp.write_core import WriteOp
from test_google_write import apply, bearer
from test_gsc_tools import ACME_SITE, BETA_SITE, SITES, gscworld  # noqa: F401

FEED = "https://www.acme.fr/sitemap.xml"


def put_url(site: str, feed: str) -> str:
    return f"{SITES}/{quote(site, safe='')}/sitemaps/{quote(feed, safe='')}"


# ------------------------------------------------------------------ property and transport checks
@pytest.mark.parametrize("site, feed", [
    (ACME_SITE, "https://evil.example/sitemap.xml"),
    (ACME_SITE, "https://acme.fr.evil.example/sitemap.xml"),
    (ACME_SITE, "https://notacme.fr/sitemap.xml"),
    (ACME_SITE, "ftp://acme.fr/sitemap.xml"),
    (ACME_SITE, "https://user@acme.fr/sitemap.xml"),
    (ACME_SITE, "https://acme.fr/a/../sitemap.xml"),
    (ACME_SITE, "https://acme.fr/sitemap.xml#x"),
    (ACME_SITE, "https://acme.fr/"),
    (ACME_SITE, ""),
    (BETA_SITE, "https://beta.com/sitemap.xml"),
    (BETA_SITE, "http://www.beta.com/sitemap.xml"),
    (BETA_SITE, "https://www.beta.com:8443/sitemap.xml"),
])
def test_sitemap_outside_the_property_is_refused(site, feed):
    assert gsc_write.outside_property(site, feed) is not None


@pytest.mark.parametrize("site, feed", [
    (ACME_SITE, "https://acme.fr/sitemap.xml"), (ACME_SITE, "http://shop.acme.fr/sitemap_index.xml"),
    (ACME_SITE, "https://www.ACME.fr/sitemap.php?page=2"), (BETA_SITE, "https://www.beta.com/sitemaps/products.xml"),
])
def test_sitemap_inside_the_property_is_accepted(site, feed):
    assert gsc_write.outside_property(site, feed) is None


def test_plan_outside_the_property_sends_nothing(gscworld):
    out = gsc_write.gsc_plan_sitemap_submit("https://evil.example/sitemap.xml", client_id="acme")
    assert out["error"] == "sitemap_outside_property" and out["client_id"] == "acme"
    assert gscworld.calls == [] and not write_core.list_plans("acme")


def test_transport_accepts_the_encoded_sitemap_url_only(gscworld):
    """ads_common.bare_url accepts %2F in the property and sitemap segments of Search Console only
    (ENCODED_SLASH_SEGMENTS); a double-encoded URL is still refused, nothing sent."""
    assert gsc_write.transport_problem(put_url(ACME_SITE, FEED)) is None
    assert gsc_write.transport_problem(put_url(BETA_SITE, FEED)) is None
    double = put_url(ACME_SITE, FEED).replace("%2F", "%252F")
    assert gsc_write.transport_problem(double) is not None
    climbing = f"{SITES}/..%2F..%2F/sitemaps/x"
    problem = gsc_write.transport_problem(climbing)
    assert problem is not None and "'.' / '..'" in problem and "refuses a percent-encoded '/' (%2F) in a URL" \
        not in problem  # the stale "every %2F is refused" wording is gone
    assert gscworld.calls == []


@pytest.mark.parametrize("site", ["..", "../..", "x", "ftp://acme.fr/", "sc-domain:", "acme.fr",
                                  "https://www.acme.fr/../", "https://www.acme.fr/./"])
def test_parse_requires_an_http_https_or_sc_domain_property(site):
    assert gsc_write._parse(put_url(site, FEED)) is None
    assert gsc_write._parse(put_url(ACME_SITE, FEED)) == (ACME_SITE, FEED)
    assert gsc_write._parse(put_url("http://www.acme.fr/", FEED)) == ("http://www.acme.fr/", FEED)


def test_parse_refuses_dot_components_in_the_sitemap_url():
    assert gsc_write._parse(put_url(ACME_SITE, "https://acme.fr/a/../sitemap.xml")) is None
    assert gsc_write._parse(f"{SITES}/..%2F..%2F/sitemaps/x") is None


def test_crafted_op_with_a_climbing_property_is_refused_without_any_request(gscworld):
    ctx = clients.resolve("acme")
    op = WriteOp(op_id="s", platform="gsc", account_id=ACME_SITE, action="sitemap_submit", method="PUT",
                 url=f"{SITES}/..%2F..%2F/sitemaps/x", risk="low")
    out = write_core.create_plan(ctx, "gsc", [op], title="t")
    assert "error" in out and "confirmation_token" not in out
    assert gscworld.calls == []


# ------------------------------------------------------------------ plan -> apply
def test_new_sitemap_plan_then_apply_with_the_write_token(gscworld):
    plan = gsc_write.gsc_plan_sitemap_submit(FEED, client_id="acme")
    assert "error" not in plan, plan
    assert plan["platform"] == "gsc" and plan["apply_tool"] == "google_apply"
    assert plan["ops"][0]["risk"] == "low" and plan["ops"][0]["financial"] is False
    assert plan["ops"][0]["rollback"] == "auto" and plan["human_approval_required"] is False
    assert plan["dry_run"][0]["mode"] == "local" and plan["dry_run"][0]["detail"]["already_submitted"] is False
    assert "DELETE" in plan["rollback_note"]
    assert gscworld.writes() == []  # planning wrote nothing
    out = apply("acme", plan)
    assert out["status"] == "applied", out
    [put] = gscworld.writes()
    assert put["method"] == "PUT" and put["url"] == put_url(ACME_SITE, FEED)
    assert put["json"] is None and put["params"] is None
    assert put["auth"] == "Bearer " + bearer("acme", "WRITE")
    assert {c["auth"] for c in gscworld.calls} == {"Bearer " + bearer("acme", "WRITE")}  # read token never used
    assert FEED in gscworld.sitemaps[ACME_SITE]
    assert out["results"][0]["result"]["action"] == "submitted"
    kinds = [e["kind"] for e in write_core.journal.entries("acme", plan_id=plan["plan_id"])]
    assert kinds == ["plan", "apply_intent", "apply_result"]
    assert apply("acme", plan)["error"] == "plan_already_applied"


def test_already_listed_sitemap_has_no_rollback(gscworld):
    gscworld.sitemaps[ACME_SITE][FEED] = {"path": FEED, "lastSubmitted": "2026-01-01T00:00:00Z"}
    plan = gsc_write.gsc_plan_sitemap_submit(FEED, client_id="acme")
    assert "error" not in plan, plan
    assert plan["ops"][0]["rollback"] == "none" and "already listed" in plan["rollback_note"]
    assert apply("acme", plan)["status"] == "applied"
    out = write_core._rollback_tool(plan["plan_id"], client_id="acme")
    assert out["error"] == "no_inverse"
    assert [w["method"] for w in gscworld.writes()] == ["PUT"]


def test_listed_meanwhile_means_no_rollback(gscworld):
    """The rollback follows the snapshot taken at apply time: a sitemap someone listed meanwhile is never
    deleted."""
    plan = gsc_write.gsc_plan_sitemap_submit(FEED, client_id="acme")
    gscworld.sitemaps[ACME_SITE][FEED] = {"path": FEED}
    assert apply("acme", plan)["status"] == "applied"
    assert write_core._rollback_tool(plan["plan_id"], client_id="acme")["error"] == "no_inverse"


def test_rollback_deletes_only_the_sitemap_it_added_with_approval(gscworld):
    plan = gsc_write.gsc_plan_sitemap_submit(FEED, client_id="acme")
    assert apply("acme", plan)["status"] == "applied"
    rollback = write_core._rollback_tool(plan["plan_id"], client_id="acme")
    assert "error" not in rollback, rollback
    assert rollback["ops"][0]["action"] == "sitemap_delete" and rollback["human_approval_required"] is True
    no_approval = write_core._apply_tool("google_apply", "acme", rollback["plan_id"],
                                         rollback["confirmation_token"], rollback["confirm"])
    assert no_approval["error"] == "approval_required"
    assert [w["method"] for w in gscworld.writes()] == ["PUT"]
    out = apply("acme", rollback)  # approval injected as in test_write_approval
    assert out["status"] == "applied", out
    assert [w["method"] for w in gscworld.writes()] == ["PUT", "DELETE"]
    assert gscworld.writes()[-1]["url"] == put_url(ACME_SITE, FEED)
    assert FEED not in gscworld.sitemaps[ACME_SITE]
    # and the rollback of the rollback submits it again
    again = write_core._rollback_tool(rollback["plan_id"], client_id="acme")
    assert "error" not in again and again["ops"][0]["action"] == "sitemap_submit"


def test_rollback_needs_allow_delete(gscworld):
    feed = "https://www.beta.com/sitemap.xml"
    plan = gsc_write.gsc_plan_sitemap_submit(feed, client_id="beta")
    assert "error" not in plan, plan
    assert apply("beta", plan)["status"] == "applied"
    out = write_core._rollback_tool(plan["plan_id"], client_id="beta")
    assert out["error"] == "delete_not_allowed"
    assert [w["method"] for w in gscworld.writes()] == ["PUT"]


def test_crafted_delete_of_a_sitemap_not_added_here_is_refused(gscworld):
    gscworld.sitemaps[ACME_SITE][FEED] = {"path": FEED}
    ctx = clients.resolve("acme")
    op = WriteOp(op_id="d", platform="gsc", account_id=ACME_SITE, action="sitemap_delete", method="DELETE",
                 url=put_url(ACME_SITE, FEED), risk="medium")
    out = write_core.create_plan(ctx, "gsc", [op], title="t")
    assert out["error"] == "dry_run_failed" and "only a sitemap this server submitted" in out["detail"]
    assert gscworld.writes() == []


# ------------------------------------------------------------------ locks: accounts, tokens, clients
def test_foreign_property_is_refused_without_any_request(gscworld):
    out = gsc_write.gsc_plan_sitemap_submit("https://www.beta.com/sitemap.xml", site_url=BETA_SITE,
                                            client_id="acme")
    assert out["error"] == "id_not_owned" and gscworld.calls == []
    ctx = clients.resolve("acme")
    crafted = WriteOp(op_id="s", platform="gsc", account_id=ACME_SITE, action="sitemap_submit", method="PUT",
                      url=put_url(BETA_SITE, "https://www.beta.com/sitemap.xml"), risk="low")
    out = write_core.create_plan(ctx, "gsc", [crafted], title="t")
    assert out["error"] in ("account_not_bound", "snapshot_failed")
    other = dataclasses.replace(crafted, account_id=BETA_SITE)
    assert write_core.create_plan(ctx, "gsc", [other], title="t")["error"] in ("account_not_allowed",
                                                                              "account_not_owned")
    assert gscworld.calls == []  # not even a read of the other property


def test_switches_are_checked_before_any_request(gscworld, monkeypatch):
    monkeypatch.setenv("GEM_WRITE", "0")
    assert gsc_write.gsc_plan_sitemap_submit(FEED, client_id="acme")["error"] == "write_disabled"
    assert gscworld.calls == []


def test_missing_write_token_never_uses_the_read_token(gscworld, monkeypatch):
    monkeypatch.delenv("GEM_SECRET_ACME_GOOGLE_W")
    out = gsc_write.gsc_plan_sitemap_submit(FEED, client_id="acme")
    assert out["error"] == "write_token_missing" and "--client acme --write" in out["fix"]
    assert gscworld.calls == []


def test_write_token_without_webmasters_scope_is_refused(gscworld, monkeypatch):
    from test_google_write import AUTH, token_json

    monkeypatch.setenv("GEM_SECRET_ACME_GOOGLE_W", token_json(bearer("acme", "WRITE"),
                                                              [AUTH + "webmasters.readonly"]))
    out = gsc_write.gsc_plan_sitemap_submit(FEED, client_id="acme")
    assert out["error"] == "missing_scope" and gscworld.calls == []


def test_two_clients_are_isolated(gscworld):
    acme = gsc_write.gsc_plan_sitemap_submit(FEED, client_id="acme")
    beta = gsc_write.gsc_plan_sitemap_submit("https://www.beta.com/sitemap.xml", client_id="beta")
    assert "error" not in acme and "error" not in beta, (acme, beta)
    assert write_core._apply_tool("google_apply", "beta", acme["plan_id"], acme["confirmation_token"],
                                  acme["confirm"])["error"] == "plan_not_found"
    assert apply("acme", acme)["status"] == "applied" and apply("beta", beta)["status"] == "applied"
    by_site: dict[str, set] = {}
    for call in gscworld.calls:
        by_site.setdefault(call["url"].split("/sites/")[1].split("/")[0], set()).add(call["auth"])
    assert by_site == {quote(ACME_SITE, safe=""): {"Bearer " + bearer("acme", "WRITE")},
                       quote(BETA_SITE, safe=""): {"Bearer " + bearer("beta", "WRITE")}}
    assert list(gscworld.sitemaps[BETA_SITE]) == ["https://www.beta.com/sitemap.xml"]
    assert list(gscworld.sitemaps[ACME_SITE]) == [FEED]
    assert write_core.journal.entries("beta", plan_id=acme["plan_id"]) == []


def test_unsigned_write_is_blocked_by_the_client(gscworld):
    from google_ecommerce_mcp import google_write as gw

    ctx = clients.resolve("acme")
    out = gw.write_call(gsc_write.WRITE, ctx, "PUT", put_url(ACME_SITE, FEED))
    assert out["error"] == "blocked" and out["reason"] == "grant_missing"
    out = gw.write_call(gsc_write.WRITE, ctx, "PUT", f"{SITES}/{quote(ACME_SITE, safe='')}")  # sites.add
    assert out["error"] == "blocked" and out["reason"] == "not_on_write_allow_list"
    out = gw.write_call(gsc_write.WRITE, ctx, "DELETE", f"{SITES}/{quote(ACME_SITE, safe='')}")  # sites.delete
    assert out["error"] == "blocked" and out["reason"] == "not_on_write_allow_list"
    assert gscworld.calls == []


def test_write_allow_list_has_only_one_sitemap_resource():
    rules = gsc_write.WRITE.write_allow_list.rules
    assert {m for m, _, _ in rules} == {"PUT", "DELETE"}
    assert all(p.endswith(r"/sitemaps/[^/]+") for _, p, _ in rules)
    assert {"sites.add", "sites.delete"} <= set(gsc_write.VOLUNTARILY_ABSENT)


def test_plan_tool_annotations_and_client_id(gscworld):
    from mcp.server.fastmcp import FastMCP

    from google_ecommerce_mcp import gsc_tools

    mcp = FastMCP("annotations")
    names = gsc_tools.register(mcp) + gsc_write.register(mcp)
    assert set(names) == set(gsc_tools.TOOLS) | set(gsc_write.TOOLS)
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    for name in names:
        tool = tools[name]
        assert tool.annotations.readOnlyHint is True and tool.annotations.destructiveHint is False, name
        assert "client_id" in tool.inputSchema["properties"], name
        assert "client_id" not in tool.inputSchema.get("required", []), name
    assert not any("delete" in name for name in names)

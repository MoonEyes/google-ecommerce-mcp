"""GTM reads and plan -> apply on a fake Tag Manager server: dedicated workspace, tag / trigger / variable
upserts, Default Workspace refused, fingerprint required and re-checked, allow-list, foreign account, two isolated
clients, rollback of an update. Offline (ads_fakes.no_network is autouse)."""

from __future__ import annotations

import dataclasses

import pytest

from ads_fakes import no_network  # noqa: F401
from google_ecommerce_mcp import clients, gtm_tools, write_core
from google_ecommerce_mcp.write_core import WriteOp
from test_google_write import GTM, apply, bearer, gworld  # noqa: F401

BASE = f"{GTM}/accounts/6111/containers/2111"


def add_tag(server, workspace="5", **fields):
    tag = {"name": "GA4 purchase", "type": "gaawe",
           "parameter": [{"type": "template", "key": "eventName", "value": "purchase"}],
           "firingTriggerId": ["2147479553"], **fields}
    return server.add_entity("6111", "2111", workspace, "tags", tag)


def make_gem_workspace(server):
    box = server.gtm["6111/2111"]["workspaces"]
    box["9"] = {"path": f"accounts/6111/containers/2111/workspaces/9", "accountId": "6111", "containerId": "2111",
                "workspaceId": "9", "name": "gem work", "description": "x [gem-managed]"}


# ------------------------------------------------------------------ reads
def test_workspace_status_flags_the_default_workspace_and_uses_the_read_token(gworld):
    add_tag(gworld)
    out = gtm_tools.gtm_workspace_status(client_id="acme")
    assert out["client_id"] == "acme" and out["container"] == {"account_id": "6111", "container_id": "2111"}
    flags = {w["workspace_id"]: w["is_default"] for w in out["workspaces"]}
    assert flags == {"1": True, "5": False}
    sprint = next(w for w in out["workspaces"] if w["workspace_id"] == "5")
    assert sprint["changes"][0]["change"] == "added" and sprint["changes"][0]["kind"] == "tag"
    assert {c["auth"] for c in gworld.calls} == {"Bearer " + bearer("acme", "READ")}
    assert "untrusted_text" in out


def test_lowest_workspace_without_marker_counts_as_default():
    marked = gtm_tools._workspaces([{"workspaceId": "3", "name": "Renamed"}, {"workspaceId": "8", "name": "A"},
                                    {"workspaceId": "2", "name": "mine", "description": "[gem-managed]"}])
    assert {w["workspaceId"]: w["is_default"] for w in marked} == {"3": False, "8": False, "2": False}
    marked = gtm_tools._workspaces([{"workspaceId": "3", "name": "Renamed"}, {"workspaceId": "8", "name": "A"}])
    assert {w["workspaceId"]: w["is_default"] for w in marked} == {"3": True, "8": False}
    assert gtm_tools._workspaces([{"workspaceId": "4", "name": "only one"}])[0]["is_default"] is True


def test_get_tag_returns_the_fingerprint_and_versions_list(gworld):
    tag = add_tag(gworld)
    out = gtm_tools.gtm_get_tag(tag["tagId"], "5", client_id="acme")
    assert out["tag"]["name"] == "GA4 purchase" and out["fingerprint"] == tag["fingerprint"]
    assert out["updatable"] is True
    versions = gtm_tools.gtm_versions(client_id="acme")
    assert [v["version_id"] for v in versions["versions"]] == ["7", "3"]
    assert versions["live_version"]["version_id"] == "7"
    assert gworld.writes() == []


def test_get_tag_cleans_caps_and_masks_secret_parameters(gworld):
    tag = add_tag(gworld, name="Pixel‮​gnp.exe\x07", notes="n" * 9000, parameter=[
        {"type": "template", "key": "eventName", "value": "purchase"},
        {"type": "template", "key": "apiSecret", "value": "s3cr3t-value"},
        {"type": "template", "key": "accessToken", "value": "tok-123"},
        {"type": "template", "key": "measurementApiKey", "value": "k-456"},
        {"type": "template", "key": "PASSWORD", "value": "hunter2"},
        {"type": "list", "key": "userProperties", "list": [{"type": "map", "map": [
            {"type": "template", "key": "name", "value": "api_key"},
            {"type": "template", "key": "value", "value": "row-secret"}]}, {"type": "map", "map": [
            {"type": "template", "key": "name", "value": "plan"},
            {"type": "template", "key": "value", "value": "gold"}]}]}])
    out = gtm_tools.gtm_get_tag(tag["tagId"], "5", client_id="acme")
    text = str(out)
    for secret in ("s3cr3t-value", "tok-123", "k-456", "hunter2", "row-secret"):
        assert secret not in text, secret
    assert "purchase" in text and "gold" in text  # ordinary values stay visible
    assert out["secrets_masked"] == sorted(["apiSecret", "accessToken", "measurementApiKey", "PASSWORD", "api_key"])
    assert "‮" not in out["tag"]["name"] and "​" not in out["tag"]["name"] and "\x07" not in text
    assert len(out["tag"]["notes"]) <= gtm_tools.READ_TEXT_LIMIT and "never send them back" in out["copy_note"]
    assert out["fingerprint"] == tag["fingerprint"]
    # a masked value sent back is refused (it would overwrite the real secret)
    back = gtm_tools.gtm_plan_tag_upsert("5", {"parameter": out["tag"]["parameter"]}, tag_id=tag["tagId"],
                                         fingerprint=tag["fingerprint"], client_id="acme")
    assert back["error"] == "masked_value_sent_back" and gworld.writes() == []


def test_get_tag_without_secrets_has_no_mask_note(gworld):
    tag = add_tag(gworld)
    out = gtm_tools.gtm_get_tag(tag["tagId"], "5", client_id="acme")
    assert "secrets_masked" not in out and "copy_note" not in out


def test_reads_refuse_bad_ids_without_network(gworld):
    out = gtm_tools.gtm_get_tag("../1", "5", client_id="acme")
    assert out["error"] == "bad_request" and gworld.calls == []


# ------------------------------------------------------------------ workspace
def test_workspace_create_plan_then_apply(gworld):
    plan = gtm_tools.gtm_plan_workspace_create("Agent changes", "tracking fixes", client_id="acme")
    assert "error" not in plan, plan
    assert plan["platform"] == "gtm" and plan["apply_tool"] == "google_apply"
    assert plan["human_approval_required"] is False and plan["ops"][0]["risk"] == "low"
    assert gworld.writes() == []  # planning wrote nothing
    out = apply("acme", plan)
    assert out["status"] == "applied", out
    [write] = gworld.writes()
    assert write["method"] == "POST" and write["url"] == f"{BASE}/workspaces"
    assert write["json"] == {"name": "Agent changes", "description": "tracking fixes [gem-managed]"}
    assert write["auth"] == "Bearer " + bearer("acme", "WRITE")
    kinds = [e["kind"] for e in write_core.journal.entries("acme", plan_id=plan["plan_id"])]
    assert kinds == ["plan", "apply_intent", "apply_result"]
    assert apply("acme", plan)["error"] == "plan_already_applied"


def test_workspace_named_like_an_existing_one_or_default_is_refused(gworld):
    assert gtm_tools.gtm_plan_workspace_create("sprint", client_id="acme")["error"] == "invalid_request"
    assert gtm_tools.gtm_plan_workspace_create("Default Workspace",
                                               client_id="acme")["error"] == "default_workspace_refused"
    assert gworld.writes() == []


# ------------------------------------------------------------------ tags
def test_tag_in_the_default_workspace_is_refused(gworld):
    out = gtm_tools.gtm_plan_tag_upsert("1", {"name": "x", "type": "html"}, client_id="acme")
    assert out["error"] == "default_workspace_refused"
    assert gworld.writes() == [] and not write_core.list_plans("acme")


def test_crafted_op_on_the_default_workspace_is_refused_by_the_handler(gworld):
    ctx = clients.resolve("acme")
    op = WriteOp(op_id="t", platform="gtm", account_id="6111", action="tag_create", method="POST",
                 url=f"{BASE}/workspaces/1/tags", body={"name": "x", "type": "gaawe"}, risk="medium")
    out = write_core.create_plan(ctx, "gtm", [op], title="t")
    assert out["error"] == "dry_run_failed" and "Default Workspace" in out["detail"]


def test_tag_create_plan_dry_runs_with_quick_preview_and_applies(gworld):
    plan = gtm_tools.gtm_plan_tag_upsert("5", {"name": "Lead", "type": "gaawe", "parameter": [
        {"type": "template", "key": "eventName", "value": "generate_lead"}], "firingTriggerId": ["2147479553"]},
        client_id="acme")
    assert "error" not in plan, plan
    assert plan["dry_run"][0]["mode"] == "native" and plan["ops"][0]["risk"] == "medium"
    assert plan["ops"][0]["rollback"] == "none"
    assert [c["url"] for c in gworld.calls if c["method"] == "POST"] == [f"{BASE}/workspaces/5:quick_preview"]
    out = apply("acme", plan)
    assert out["status"] == "applied", out
    [write] = gworld.writes()
    assert write["url"] == f"{BASE}/workspaces/5/tags" and write["json"]["name"] == "Lead"
    tags = gworld.gtm["6111/2111"]["entities"][("5", "tags")]
    assert [t["name"] for t in tags.values()] == ["Lead"]


def test_custom_html_tag_is_high_risk(gworld):
    plan = gtm_tools.gtm_plan_tag_upsert("5", {"name": "Pixel", "type": "html", "parameter": [
        {"type": "template", "key": "html", "value": "<script>track()</script>"}]}, client_id="acme")
    assert "error" not in plan, plan
    assert plan["ops"][0]["risk"] == "high" and "runs code" in plan["warning"]
    assert plan["ops"][0]["financial"] is False
    assert plan["human_approval_required"] is True and "approval_command" in plan  # high risk => a human
    refused = write_core._apply_tool("google_apply", "acme", plan["plan_id"], plan["confirmation_token"],
                                     plan["confirm"])
    assert refused["error"] == "approval_required" and gworld.writes() == []


def test_dry_run_falls_back_to_local_when_preview_is_not_allowed(gworld):
    gworld.preview_error = 403
    plan = gtm_tools.gtm_plan_tag_upsert("5", {"name": "Lead", "type": "gaawe"}, client_id="acme")
    assert "error" not in plan, plan
    assert plan["dry_run"][0]["mode"] == "local"


def test_fields_outside_the_allow_list_are_refused(gworld):
    out = gtm_tools.gtm_plan_tag_upsert("5", {"name": "x", "type": "gaawe",
                                             "consentSettings": {"consentStatus": "needed"}}, client_id="acme")
    assert out["error"] == "field_not_allowed" and "consentSettings" in out["detail"]
    deep = {"name": "x", "type": "gaawe",
            "parameter": [{"type": "list", "key": "k", "list": [{"type": "list", "list": [
                {"type": "map", "map": [{"type": "template", "key": "a", "value": "b"}]}]}]}]}
    out = gtm_tools.gtm_plan_tag_upsert("5", deep, client_id="acme")
    assert out["error"] == "field_not_allowed"  # write_policy's own allow-list, nested path
    ctx = clients.resolve("acme")
    op = WriteOp(op_id="t", platform="gtm", account_id="6111", action="tag_create", method="POST",
                 url=f"{BASE}/workspaces/5/tags", body={"name": "x", "type": "gaawe", "monitoringMetadata": {
                     "type": "map", "budget": "1"}}, risk="medium")
    assert write_core.create_plan(ctx, "gtm", [op], title="t")["error"] in ("field_not_allowed",
                                                                            "financial_undeclared")
    assert gworld.writes() == []


def test_tag_update_needs_a_current_fingerprint(gworld):
    tag = add_tag(gworld)
    out = gtm_tools.gtm_plan_tag_upsert("5", {"name": "Renamed"}, tag_id=tag["tagId"], client_id="acme")
    assert out["error"] == "fingerprint_required" and "gtm_get_tag" in out["fix"]
    out = gtm_tools.gtm_plan_tag_upsert("5", {"name": "Renamed"}, tag_id=tag["tagId"], fingerprint="old",
                                        client_id="acme")
    assert out["error"] == "fingerprint_stale"
    out = gtm_tools.gtm_plan_tag_upsert("5", {"name": "GA4 purchase"}, tag_id=tag["tagId"],
                                        fingerprint=tag["fingerprint"], client_id="acme")
    assert out["error"] == "no_change"
    assert gworld.writes() == []


def test_tag_update_apply_then_rollback(gworld):
    tag = add_tag(gworld, notes="v1")
    plan = gtm_tools.gtm_plan_tag_upsert("5", {**tag, "name": "Purchase (GA4)", "notes": "v2"}, tag_id=tag["tagId"],
                                         fingerprint=tag["fingerprint"], client_id="acme")
    assert "error" not in plan, plan
    assert plan["ops"][0]["rollback"] == "auto"
    assert any("name: GA4 purchase -> Purchase (GA4)" in c for c in plan["ops"][0]["changes"])
    out = apply("acme", plan)
    assert out["status"] == "applied", out
    [put] = gworld.writes()
    assert put["method"] == "PUT" and put["params"] == {"fingerprint": tag["fingerprint"]}
    assert put["json"]["name"] == "Purchase (GA4)" and put["json"]["parameter"] == tag["parameter"]
    assert "fingerprint" not in put["json"] and "path" not in put["json"]
    rollback = write_core._rollback_tool(plan["plan_id"], client_id="acme")
    assert "error" not in rollback, rollback
    assert rollback["ops"][0]["action"] == "tag_restore"
    out = apply("acme", rollback)
    assert out["status"] == "applied", out
    stored = gworld.gtm["6111/2111"]["entities"][("5", "tags")][tag["tagId"]]
    assert stored["name"] == "GA4 purchase" and stored["notes"] == "v1"
    restore = gworld.writes()[-1]
    assert restore["method"] == "PUT" and restore["params"] is None


def test_rollback_refused_when_someone_edited_the_tag_since(gworld):
    tag = add_tag(gworld)
    plan = gtm_tools.gtm_plan_tag_upsert("5", {"name": "B"}, tag_id=tag["tagId"], fingerprint=tag["fingerprint"],
                                         client_id="acme")
    assert apply("acme", plan)["status"] == "applied"
    gworld.gtm["6111/2111"]["entities"][("5", "tags")][tag["tagId"]]["name"] = "edited by a human"
    rollback = write_core._rollback_tool(plan["plan_id"], client_id="acme")
    assert rollback["error"] == "dry_run_failed" and "changed since" in rollback["detail"]
    assert len(gworld.writes()) == 1


def test_fingerprint_changed_between_plan_and_apply_sends_nothing(gworld):
    tag = add_tag(gworld)
    plan = gtm_tools.gtm_plan_tag_upsert("5", {"name": "B"}, tag_id=tag["tagId"], fingerprint=tag["fingerprint"],
                                         client_id="acme")
    gworld.gtm["6111/2111"]["entities"][("5", "tags")][tag["tagId"]]["fingerprint"] = "fp-new"
    out = apply("acme", plan)
    assert out["status"] == "failed" and "changed since" in str(out["results"][0])
    assert gworld.writes() == []


def test_unpausing_a_paused_tag_is_refused(gworld):
    tag = add_tag(gworld, paused=True)
    out = gtm_tools.gtm_plan_tag_upsert("5", {"paused": False}, tag_id=tag["tagId"], fingerprint=tag["fingerprint"],
                                        client_id="acme")
    assert out["error"] == "unpause_refused"


def test_entity_with_unsupported_fields_is_not_updatable(gworld):
    tag = add_tag(gworld, consentSettings={"consentStatus": "needed", "consentType": {"type": "list"}})
    assert gtm_tools.gtm_get_tag(tag["tagId"], "5", client_id="acme")["updatable"] is False
    out = gtm_tools.gtm_plan_tag_upsert("5", {"name": "B"}, tag_id=tag["tagId"], fingerprint=tag["fingerprint"],
                                        client_id="acme")
    assert out["error"] == "not_updatable"
    default_consent = add_tag(gworld, name="t2", consentSettings={"consentStatus": "notSet"})
    plan = gtm_tools.gtm_plan_tag_upsert("5", {"name": "t3"}, tag_id=default_consent["tagId"],
                                         fingerprint=default_consent["fingerprint"], client_id="acme")
    assert "error" not in plan, plan


def test_ids_in_the_entity_must_match_the_request(gworld):
    tag = add_tag(gworld)
    out = gtm_tools.gtm_plan_tag_upsert("5", {**tag, "accountId": "6222"}, tag_id=tag["tagId"],
                                        fingerprint=tag["fingerprint"], client_id="acme")
    assert out["error"] == "id_mismatch" and gworld.calls == []


# ------------------------------------------------------------------ triggers, variables, preview
def test_trigger_and_variable_upserts(gworld):
    trigger = gtm_tools.gtm_plan_trigger_upsert("5", {"name": "Lead event", "type": "customEvent",
                                                      "customEventFilter": [{"type": "equals", "parameter": [
                                                          {"type": "template", "key": "arg0", "value": "{{_event}}"},
                                                          {"type": "template", "key": "arg1", "value": "lead"}]}]},
                                                client_id="acme")
    assert "error" not in trigger, trigger
    assert apply("acme", trigger)["status"] == "applied"
    js = gtm_tools.gtm_plan_variable_upsert("5", {"name": "cart value", "type": "jsm", "parameter": [
        {"type": "template", "key": "javascript", "value": "function(){return 1}"}]}, client_id="acme")
    assert "error" not in js and js["ops"][0]["risk"] == "high" and js["human_approval_required"] is True
    assert trigger["human_approval_required"] is False  # medium risk
    assert apply("acme", js)["status"] == "applied"  # the helper approves out of band first
    var = next(iter(gworld.gtm["6111/2111"]["entities"][("5", "variables")].values()))
    upd = gtm_tools.gtm_plan_variable_upsert("5", {"notes": "documented"}, variable_id=var["variableId"],
                                             fingerprint=var["fingerprint"], client_id="acme")
    assert "error" not in upd, upd
    assert apply("acme", upd)["status"] == "applied"
    assert [w["method"] for w in gworld.writes()] == ["POST", "POST", "PUT"]


def test_quick_preview_writes_nothing(gworld):
    add_tag(gworld)
    out = gtm_tools.gtm_plan_quick_preview("5", client_id="acme")
    assert out["preview"]["tags"] == 1 and out["preview"]["compiler_error"] is False
    assert gworld.writes() == [] and not write_core.list_plans("acme")
    assert {c["auth"] for c in gworld.calls} == {"Bearer " + bearer("acme", "WRITE")}


# ------------------------------------------------------------------ locks: switches, accounts, clients
def test_writes_off_means_no_network(gworld, monkeypatch):
    monkeypatch.setenv("GEM_WRITE", "0")
    assert gtm_tools.gtm_plan_workspace_create("x", client_id="acme")["error"] == "write_disabled"
    assert gtm_tools.gtm_plan_quick_preview("5", client_id="acme")["error"] == "write_disabled"
    assert gworld.calls == []


def test_foreign_gtm_account_is_refused(gworld):
    ctx = clients.resolve("acme")
    op = WriteOp(op_id="t", platform="gtm", account_id="6222", action="tag_create", method="POST",
                 url=f"{GTM}/accounts/6222/containers/2222/workspaces/1/tags", body={"name": "x", "type": "gaawe"},
                 risk="medium")
    assert write_core.create_plan(ctx, "gtm", [op], title="t")["error"] in ("account_not_allowed",
                                                                            "account_not_owned")
    # own account in the op, another client's container in the URL: refused by the module lock
    op = dataclasses.replace(op, account_id="6111", url=f"{GTM}/accounts/6111/containers/2222/workspaces/1/tags")
    out = write_core.create_plan(ctx, "gtm", [op], title="t")
    assert out["error"] in ("account_not_bound", "snapshot_failed")
    assert gworld.calls == []  # not even a read of the other container


def test_two_clients_are_isolated(gworld):
    make_gem_workspace(gworld)
    gworld.gtm["6222/2222"]["workspaces"]["4"] = {"accountId": "6222", "containerId": "2222", "workspaceId": "4",
                                                  "name": "beta work", "description": "[gem-managed]"}
    acme = gtm_tools.gtm_plan_tag_upsert("9", {"name": "a", "type": "gaawe"}, client_id="acme")
    beta = gtm_tools.gtm_plan_tag_upsert("4", {"name": "b", "type": "gaawe"}, client_id="beta")
    assert "error" not in acme and "error" not in beta, (acme, beta)
    # beta cannot apply acme's plan, even with acme's token and confirm string
    assert write_core._apply_tool("google_apply", "beta", acme["plan_id"], acme["confirmation_token"],
                                  acme["confirm"])["error"] == "plan_not_found"
    assert apply("acme", acme)["status"] == "applied" and apply("beta", beta)["status"] == "applied"
    by_client = {}
    for call in gworld.calls:
        by_client.setdefault(call["url"].split("/accounts/")[1].split("/")[0], set()).add(call["auth"])
    assert by_client == {"6111": {"Bearer " + bearer("acme", "WRITE")},
                         "6222": {"Bearer " + bearer("beta", "WRITE")}}
    assert write_core.journal.entries("beta", plan_id=acme["plan_id"]) == []


def test_write_allow_list_has_no_publish_version_or_delete():
    rules = gtm_tools.WRITE.write_allow_list.rules
    assert {m for m, _, _ in rules} == {"POST", "PUT"}
    assert not any(word in p for _, p, _ in rules for word in ("publish", "create_version", "versions"))
    assert set(gtm_tools.VOLUNTARILY_ABSENT) >= {"create_version", "publish", "delete"}
    assert gtm_tools.READ.write_allow_list is None

"""write_core: the single plan -> dry-run -> token -> apply -> journal path. Offline: fake client context, fake
platform handler working on an in-memory state, no network, no OS keyring (GEM_WRITE_HMAC_KEY set).

The doubles (WritePolicyDouble, FakeCtx, FakeHandler, budget_op) follow the contracts of PLAN_FULLCONTROL §3 and
are reused by test_write_policy.py."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from google_ecommerce_mcp import write_core, write_journal, write_policy, write_tokens
from google_ecommerce_mcp.write_core import PlatformHandler, WriteOp
from google_ecommerce_mcp.write_journal import Journal, JournalError

ACCOUNT = "1112223333"
URL = f"https://googleads.googleapis.com/v26/customers/{ACCOUNT}/campaignBudgets:mutate"


# ------------------------------------------------------------------ doubles (contracts §3)
@dataclass(frozen=True)
class WritePolicyDouble:
    enabled: bool = False
    account_allow: frozenset = frozenset()
    max_daily_budget_micros: int | None = None
    cap_currency: str | None = None
    max_increase_pct: float = 20.0
    max_ops_per_plan: int = 25
    allow_activate: bool = False
    allow_delete: bool = False
    allow_irreversible: bool = False
    allow_pii: bool = False
    token_ttl_s: int = 900


def open_policy(**overrides: Any) -> WritePolicyDouble:
    base = dict(enabled=True, account_allow=frozenset({ACCOUNT}), max_daily_budget_micros=50_000_000,
                cap_currency="EUR")
    base.update(overrides)
    return WritePolicyDouble(**base)


@dataclass(frozen=True)
class FakeCtx:
    client_id: str = "acme"
    label: str = "Acme"
    legacy: bool = False
    sandbox: bool = False
    owned: frozenset = frozenset({ACCOUNT})
    policies: dict = field(default_factory=dict, compare=False, hash=False)

    def account(self, platform: str, key: str) -> str:
        return next(iter(self.owned))

    def owns(self, platform: str, resource_id: str) -> bool:
        return str(resource_id) in self.owned

    def secret(self, kind: str, *, write: bool = False) -> str | None:
        return None

    def policy(self, platform: str) -> WritePolicyDouble:
        return self.policies.get(platform, WritePolicyDouble())


def ctx(client_id: str = "acme", platform: str = "google_ads", **policy: Any) -> FakeCtx:
    return FakeCtx(client_id=client_id, policies={platform: open_policy(**policy)})


class FakeHandler:
    """In-memory platform: state[url] = entity dict. execute() checks the grant like AdsClient.call does."""

    def __init__(self) -> None:
        self.state: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.grants: list[Any] = []
        self.dry_runs: list[str] = []
        self.fail_on: set[str] = set()
        self.raise_on: set[str] = set()
        self.dry_fail: set[str] = set()

    def dry_run(self, ctx: Any, op: WriteOp) -> dict:
        self.dry_runs.append(op.op_id)
        if op.op_id in self.dry_fail:
            return {"ok": False, "mode": "native", "detail": "INVALID_ARGUMENT"}
        return {"ok": True, "mode": "native", "detail": "validateOnly ok"}

    def snapshot(self, ctx: Any, op: WriteOp) -> dict | None:
        entity = self.state.get(op.url)
        return copy.deepcopy(entity) if entity is not None else None

    def execute(self, ctx: Any, op: WriteOp, grant: Any) -> dict:
        from google_ecommerce_mcp.ads_common import body_digest
        assert grant.client_id == ctx.client_id and grant.op_id == op.op_id
        assert grant.method == op.method.upper() and grant.url == op.url
        assert grant.body_digest == body_digest(op.body)
        self.grants.append(grant)
        self.calls.append((op.op_id, op.method, op.url, copy.deepcopy(op.body)))
        if op.op_id in self.raise_on:
            raise RuntimeError("boom with secret ya29.abcdefghijkl")
        if op.op_id in self.fail_on:
            return {"error": "api_error", "detail": "rejected", "fix": "-"}
        self.state.setdefault(op.url, {}).update(op.body or {})
        return {"ok": True, "request_id": f"req-{op.op_id}"}

    def handler(self, invert: Any = None) -> PlatformHandler:
        return PlatformHandler(dry_run=self.dry_run, snapshot=self.snapshot, execute=self.execute, invert=invert)


def budget_op(op_id: str = "op1", new: int = 11_000_000, old: int | None = 10_000_000, url: str = URL,
              account: str = ACCOUNT, **kw: Any) -> WriteOp:
    before = {"amountMicros": old, "currency": "EUR"} if old is not None else None
    inverse = WriteOp(op_id=f"{op_id}-rb", platform="google_ads", account_id=account, action="budget_change",
                      method="POST", url=url, body={"amountMicros": old}, risk="medium", financial=True,
                      summary=f"restore budget to {old}", before=None) if old is not None else None
    values = dict(op_id=op_id, platform="google_ads", account_id=account, action="budget_change", method="POST",
                  url=url, body={"amountMicros": new}, risk="medium", financial=True,
                  summary=f"set daily budget to {new} micros", before=before, inverse=inverse,
                  rollback="auto" if inverse else "none")
    values.update(kw)
    return WriteOp(**values)


def pause_op(op_id: str = "p1", url: str = URL + "#c1", **kw: Any) -> WriteOp:
    values = dict(op_id=op_id, platform="google_ads", account_id=ACCOUNT, action="pause", method="POST", url=url,
                  body={"status": "PAUSED"}, risk="low", summary="pause campaign")
    values.update(kw)
    return WriteOp(**values)


# ------------------------------------------------------------------ fixtures
@pytest.fixture(autouse=True)
def write_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GEM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GEM_WRITE", "1")
    monkeypatch.setenv("GEM_WRITE_HMAC_KEY", "ab" * 32)
    monkeypatch.delenv("GEM_LIVE_TEST", raising=False)
    monkeypatch.setattr(write_core, "journal", Journal())
    monkeypatch.setattr(write_core, "_HANDLERS", {})
    # these doubles carry no field allow-list: only that requirement of write_policy is relaxed here
    monkeypatch.setattr(write_policy, "_TEST_PERMISSIVE", True, raising=False)
    return tmp_path


@pytest.fixture
def fake():
    handler = FakeHandler()
    handler.state[URL] = {"amountMicros": 10_000_000, "currency": "EUR"}
    write_core.register_handler("google_ads", handler.handler())
    return handler


def approve(c: FakeCtx, plan: dict) -> dict:
    """The out-of-band human approval, injected through the internal function (the CLI needs a terminal)."""
    out = write_core._write_approval(c.client_id, plan["plan_id"], approver="test")
    assert out.get("status") == "approved", out
    return out


def plan_and_apply(c: FakeCtx, ops: list[WriteOp], platform: str = "google_ads") -> tuple[dict, dict]:
    plan = write_core.create_plan(c, platform, ops, title="t")
    assert "error" not in plan, plan
    if plan["human_approval_required"]:
        approve(c, plan)
    return plan, write_core.apply_plan(c, platform, plan["plan_id"], plan["confirmation_token"], plan["confirm"])


def plan_file(c: FakeCtx, plan_id: str):
    return write_journal.state_dir() / "plans" / c.client_id / f"{plan_id}.json"


# ------------------------------------------------------------------ create_plan
def test_create_plan_returns_short_readable_plan_with_token(fake):
    c = ctx()
    plan = write_core.create_plan(c, "google_ads", [budget_op()], title="Raise budget")
    assert plan["client_id"] == "acme" and plan["platform"] == "google_ads"
    assert plan["confirmation_token"].count(".") == 1
    assert plan["confirm"] == f"acme:{plan['plan_id']}:{plan['digest'][:8]}"
    assert plan["requires_confirm"] is True and plan["apply_tool"] == "gads_apply"
    assert plan["risks"] == {"max": "medium", "financial": True, "pii": False, "irreversible": False}
    lines = plan["summary"].splitlines()
    assert len(lines) <= 8 and lines[0].startswith(f"Plan {plan['plan_id']} (acme, google_ads): Raise budget")
    assert "amountMicros: 10000000 -> 11000000" in plan["summary"]
    assert "gads_apply" in lines[-1] and plan["confirm"] in lines[-1]
    assert plan["dry_run"] == [{"op_id": "op1", "ok": True, "mode": "native", "detail": "validateOnly ok"}]
    assert plan_file(c, plan["plan_id"]).is_file()
    kinds = [e["kind"] for e in write_core.journal.entries("acme")]
    assert kinds == ["plan"]


def test_create_plan_refused_without_gem_write(fake, monkeypatch):
    monkeypatch.setenv("GEM_WRITE", "0")
    plan = write_core.create_plan(ctx(), "google_ads", [budget_op()], title="t")
    assert plan["error"] == "write_disabled" and "confirmation_token" not in plan and plan["fix"]
    assert fake.dry_runs == []


def test_policy_is_checked_before_any_dry_run(fake):
    plan = write_core.create_plan(ctx(), "google_ads", [budget_op(new=999_000_000)], title="t")
    assert plan["error"] == "budget_over_cap" and "confirmation_token" not in plan
    assert plan["violations"][0]["op_id"] == "op1"
    assert fake.dry_runs == []


def test_dry_run_failure_means_no_token_and_no_plan(fake):
    fake.dry_fail.add("op1")
    c = ctx()
    plan = write_core.create_plan(c, "google_ads", [budget_op()], title="t")
    assert plan["error"] == "dry_run_failed" and "confirmation_token" not in plan
    assert plan["dry_run"][0]["ok"] is False
    assert not (write_journal.state_dir() / "plans" / "acme").exists() or not list(
        (write_journal.state_dir() / "plans" / "acme").glob("*.json"))


def test_dry_run_exception_or_bad_mode_is_a_failure(fake):
    def boom(c, op):
        raise RuntimeError("x")
    write_core.register_handler("google_ads", PlatformHandler(dry_run=boom, snapshot=fake.snapshot,
                                                              execute=fake.execute))
    assert write_core.create_plan(ctx(), "google_ads", [budget_op()], title="t")["error"] == "dry_run_failed"
    write_core.register_handler("google_ads", PlatformHandler(dry_run=lambda c, op: {"ok": True, "mode": "?"},
                                                              snapshot=fake.snapshot, execute=fake.execute))
    assert write_core.create_plan(ctx(), "google_ads", [budget_op()], title="t")["error"] == "dry_run_failed"


def test_no_handler_and_bad_ops():
    assert write_core.create_plan(ctx(), "google_ads", [budget_op()], title="t")["error"] == "no_handler"
    write_core.register_handler("google_ads", FakeHandler().handler())
    assert write_core.create_plan(ctx(), "google_ads", [{"op_id": "x"}], title="t")["error"] == "bad_op"
    with pytest.raises(TypeError):
        write_core.register_handler("google_ads", object())


def test_plan_time_snapshot_fills_missing_before(fake):
    plan = write_core.create_plan(ctx(), "google_ads", [budget_op(old=None)], title="t")
    assert "error" not in plan, plan
    doc = write_core.load_plan("acme", plan["plan_id"])
    assert doc["ops"][0]["before"] == {"amountMicros": 10_000_000, "currency": "EUR"}


def test_plan_journal_unwritable_means_no_plan(fake, monkeypatch):
    class Broken(Journal):
        def append(self, *a, **k):
            raise JournalError("disk full")
    monkeypatch.setattr(write_core, "journal", Broken())
    plan = write_core.create_plan(ctx(), "google_ads", [budget_op()], title="t")
    assert plan["error"] == "journal_unwritable" and "confirmation_token" not in plan
    assert not list((write_journal.state_dir() / "plans" / "acme").glob("*.json"))


# ------------------------------------------------------------------ apply_plan happy path
def test_apply_executes_with_one_grant_per_op_and_journals_intent_first(fake):
    c = ctx()
    ops = [budget_op(), pause_op()]
    plan, result = plan_and_apply(c, ops)
    assert result["status"] == "applied" and result["client_id"] == "acme"
    assert [r["status"] for r in result["results"]] == ["ok", "ok"]
    assert fake.state[URL]["amountMicros"] == 11_000_000
    from google_ecommerce_mcp.ads_common import WriteGrant
    assert all(isinstance(g, WriteGrant) for g in fake.grants)
    assert [g.op_id for g in fake.grants] == ["op1", "p1"] and {g.plan_id for g in fake.grants} == {plan["plan_id"]}
    entries = write_core.journal.entries("acme", plan_id=plan["plan_id"])
    assert [(e["kind"], e.get("op_id")) for e in entries] == [
        ("plan", None), ("approval", None), ("apply_intent", "op1"), ("apply_result", "op1"),
        ("apply_intent", "p1"), ("apply_result", "p1")]
    intent = entries[2]
    assert intent["approval"] == entries[1]["nonce"]
    assert intent["before"] == {"amountMicros": 10_000_000, "currency": "EUR"} and intent["body_digest"]
    assert entries[3]["request_id"] == "req-op1"
    assert "rollback_plan" in result["rollback"]


def test_apply_refused_without_gem_write(fake, monkeypatch):
    c = ctx()
    plan = write_core.create_plan(c, "google_ads", [budget_op()], title="t")
    monkeypatch.delenv("GEM_WRITE")
    out = write_core.apply_plan(c, "google_ads", plan["plan_id"], plan["confirmation_token"], plan["confirm"])
    assert out["error"] == "write_disabled" and fake.calls == []


# ------------------------------------------------------------------ token locks
def _plan(fake_handler: FakeHandler, c: FakeCtx | None = None) -> tuple[FakeCtx, dict]:
    c = c or ctx()
    plan = write_core.create_plan(c, "google_ads", [budget_op()], title="t")
    assert "error" not in plan, plan
    approve(c, plan)
    return c, plan


def test_altered_token_is_refused(fake):
    c, plan = _plan(fake)
    payload, sig = plan["confirmation_token"].split(".")
    flipped = sig[:-2] + ("AA" if sig[-2:] != "AA" else "BB")
    out = write_core.apply_plan(c, "google_ads", plan["plan_id"], f"{payload}.{flipped}", plan["confirm"])
    assert out["error"] == "token_bad_signature"
    claims = json.loads(write_tokens._b64d(payload))
    claims["exp"] += 99999
    forged = write_tokens._b64e(write_journal.canonical_json(claims).encode()) + "." + sig
    out = write_core.apply_plan(c, "google_ads", plan["plan_id"], forged, plan["confirm"])
    assert out["error"] == "token_bad_signature"
    for junk in ("", "abc", "a.b.c", None, "!!!.###"):
        assert write_core.apply_plan(c, "google_ads", plan["plan_id"], junk, plan["confirm"])["error"] == \
            "token_malformed"
    assert fake.calls == []


def test_expired_token_is_refused(fake, monkeypatch):
    c, plan = _plan(fake)
    real = write_core._now()
    monkeypatch.setattr(write_core, "_now", lambda: real + 901)
    out = write_core.apply_plan(c, "google_ads", plan["plan_id"], plan["confirmation_token"], plan["confirm"])
    assert out["error"] == "token_expired" and fake.calls == []


def test_ttl_follows_policy_and_is_capped_at_60_minutes(fake, monkeypatch):
    c = ctx(token_ttl_s=99999)
    plan = write_core.create_plan(c, "google_ads", [budget_op()], title="t")
    doc = write_core.load_plan("acme", plan["plan_id"])
    assert doc["expires_at"] - doc["created_at"] == 3600


def test_replayed_token_is_refused(fake):
    c, plan = _plan(fake)
    first = write_core.apply_plan(c, "google_ads", plan["plan_id"], plan["confirmation_token"], plan["confirm"])
    assert first["status"] == "applied"
    again = write_core.apply_plan(c, "google_ads", plan["plan_id"], plan["confirmation_token"], plan["confirm"])
    assert again["error"] == "plan_already_applied" and len(fake.calls) == 1


def test_journal_blocks_a_replay_even_without_lock_file(fake):
    c, plan = _plan(fake)
    write_core.apply_plan(c, "google_ads", plan["plan_id"], plan["confirmation_token"], plan["confirm"])
    plan_file(c, plan["plan_id"]).with_suffix(".lock").unlink()
    again = write_core.apply_plan(c, "google_ads", plan["plan_id"], plan["confirmation_token"], plan["confirm"])
    assert again["error"] == "plan_already_applied" and "tokens are single use" in again["detail"]
    assert len(fake.calls) == 1


def test_lock_file_blocks_a_concurrent_apply_even_without_journal_entry(fake):
    c, plan = _plan(fake)
    plan_file(c, plan["plan_id"]).with_suffix(".lock").write_text("")
    out = write_core.apply_plan(c, "google_ads", plan["plan_id"], plan["confirmation_token"], plan["confirm"])
    assert out["error"] == "plan_already_applied" and fake.calls == []


def test_token_of_another_client_is_refused(fake):
    a, plan = _plan(fake)
    b = ctx("beta")
    # the plan is not visible to another client at all
    out = write_core.apply_plan(b, "google_ads", plan["plan_id"], plan["confirmation_token"], plan["confirm"])
    assert out["error"] == "plan_not_found"
    # even if its file is copied into beta's store, the token stays bound to acme
    doc = json.loads(plan_file(a, plan["plan_id"]).read_text(encoding="utf-8"))
    doc["client_id"] = "beta"
    target = plan_file(b, plan["plan_id"])
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(doc), encoding="utf-8")
    out = write_core.apply_plan(b, "google_ads", plan["plan_id"], plan["confirmation_token"],
                                f"beta:{plan['plan_id']}:{plan['digest'][:8]}")
    assert out["error"] == "token_client_mismatch" and out["client_id"] == "beta"
    assert fake.calls == []


def test_token_of_another_platform_is_refused(fake):
    c, plan = _plan(fake)
    write_core.register_handler("meta", FakeHandler().handler())
    out = write_core.apply_plan(c, "meta", plan["plan_id"], plan["confirmation_token"], plan["confirm"])
    assert out["error"] == "platform_mismatch"
    path = plan_file(c, plan["plan_id"])
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["platform"] = "meta"
    path.write_text(json.dumps(doc), encoding="utf-8")
    out = write_core.apply_plan(c, "meta", plan["plan_id"], plan["confirmation_token"], plan["confirm"])
    assert out["error"] == "token_platform_mismatch" and fake.calls == []


def test_plan_modified_after_signing_is_refused(fake):
    c, plan = _plan(fake)
    path = plan_file(c, plan["plan_id"])
    original = path.read_text(encoding="utf-8")
    doc = json.loads(original)
    doc["ops"][0]["body"]["amountMicros"] = 49_000_000
    path.write_text(json.dumps(doc), encoding="utf-8")
    out = write_core.apply_plan(c, "google_ads", plan["plan_id"], plan["confirmation_token"], plan["confirm"])
    assert out["error"] == "plan_modified"
    # attacker also rewrites the stored digest: the signed token no longer matches
    doc["digest"] = write_core.plan_digest("acme", "google_ads", [write_core.op_from_dict(doc["ops"][0])],
                                           doc["policy_hash"])
    path.write_text(json.dumps(doc), encoding="utf-8")
    confirm = write_core.confirm_string("acme", plan["plan_id"], doc["digest"])
    out = write_core.apply_plan(c, "google_ads", plan["plan_id"], plan["confirmation_token"], confirm)
    assert out["error"] == "token_digest_mismatch" and fake.calls == []


def test_wrong_confirm_string_is_refused(fake):
    c, plan = _plan(fake)
    for bad in ("", "yes", plan["confirm"] + "x", plan["confirm"].upper(), None):
        out = write_core.apply_plan(c, "google_ads", plan["plan_id"], plan["confirmation_token"], bad)
        assert out["error"] == "confirm_mismatch" and plan["confirm"] in out["fix"]
    assert fake.calls == []


def test_bad_plan_id_is_not_found(fake):
    c = ctx()
    for pid in ("../../etc/passwd", "pl_zz", "", None):
        assert write_core.apply_plan(c, "google_ads", pid, "a.b", "x")["error"] == "plan_not_found"


# ------------------------------------------------------------------ re-check at apply
def test_policy_change_after_plan_invalidates_it(fake):
    c, plan = _plan(fake)
    disabled = FakeCtx(policies={"google_ads": open_policy(enabled=False)})
    out = write_core.apply_plan(disabled, "google_ads", plan["plan_id"], plan["confirmation_token"],
                                plan["confirm"])
    assert out["error"] == "policy_changed" and fake.calls == []


def test_ownership_is_rechecked_at_apply(fake):
    c, plan = _plan(fake)
    lost = FakeCtx(owned=frozenset({"999"}), policies=c.policies)
    out = write_core.apply_plan(lost, "google_ads", plan["plan_id"], plan["confirmation_token"], plan["confirm"])
    assert out["error"] == "account_not_owned" and fake.calls == []


def test_fresh_snapshot_is_used_at_apply(fake):
    c, plan = _plan(fake)
    fake.state[URL]["amountMicros"] = 5_000_000  # someone lowered it meanwhile: +120 % now
    out = write_core.apply_plan(c, "google_ads", plan["plan_id"], plan["confirmation_token"], plan["confirm"])
    assert out["error"] == "increase_too_large" and fake.calls == []
    assert out["detail"].startswith("Apply refused (re-check)")


def test_increase_is_bounded_over_24_hours_via_the_journal(fake):
    c = ctx()
    _, first = plan_and_apply(c, [budget_op(new=12_000_000)])
    assert first["status"] == "applied"
    plan = write_core.create_plan(c, "google_ads", [budget_op(op_id="op2", new=14_000_000, old=12_000_000)],
                                  title="t")
    assert plan["error"] == "increase_too_large"  # 10 M baseline from the journal, not 12 M


def test_sandbox_required_when_live_test(fake, monkeypatch):
    c, plan = _plan(fake)
    monkeypatch.setenv("GEM_LIVE_TEST", "1")
    out = write_core.apply_plan(c, "google_ads", plan["plan_id"], plan["confirmation_token"], plan["confirm"])
    assert out["error"] == "sandbox_required" and fake.calls == []
    sandbox = dataclasses.replace(c, sandbox=True)
    out = write_core.apply_plan(sandbox, "google_ads", plan["plan_id"], plan["confirmation_token"],
                                plan["confirm"])
    assert out["status"] == "applied"


def test_snapshot_failure_blocks_apply(fake):
    c, plan = _plan(fake)

    def broken(ctx, op):
        raise OSError("down")
    write_core.register_handler("google_ads", PlatformHandler(dry_run=fake.dry_run, snapshot=broken,
                                                              execute=fake.execute))
    out = write_core.apply_plan(c, "google_ads", plan["plan_id"], plan["confirmation_token"], plan["confirm"])
    assert out["error"] == "snapshot_failed" and fake.calls == []


# ------------------------------------------------------------------ journal fail-closed and errors
def test_journal_unwritable_at_apply_means_nothing_is_sent(fake, monkeypatch):
    c, plan = _plan(fake)

    class Broken(Journal):
        def append(self, client_id, kind, **fields):
            if kind == "apply_intent":
                raise JournalError("read-only disk")
            return super().append(client_id, kind, **fields)
    monkeypatch.setattr(write_core, "journal", Broken())
    out = write_core.apply_plan(c, "google_ads", plan["plan_id"], plan["confirmation_token"], plan["confirm"])
    assert out["status"] == "failed" and out["results"][0]["error"] == "journal_unwritable"
    assert fake.calls == []


def test_journal_folder_not_writable_really_blocks(fake, monkeypatch, tmp_path):
    c, plan = _plan(fake)
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where the journal folder should be")
    monkeypatch.setattr(write_core, "journal", Journal(root=blocker))
    out = write_core.apply_plan(c, "google_ads", plan["plan_id"], plan["confirmation_token"], plan["confirm"])
    assert out["results"][0]["error"] == "journal_unwritable" and fake.calls == []


def test_stops_at_first_error(fake):
    fake.fail_on.add("p2")
    c = ctx()
    ops = [pause_op("p1"), pause_op("p2", url=URL + "#c2"), pause_op("p3", url=URL + "#c3")]
    _, out = plan_and_apply(c, ops)
    assert out["status"] == "partial"
    assert [r["status"] for r in out["results"]] == ["ok", "error", "not_run"]
    assert [call[0] for call in fake.calls] == ["p1", "p2"]


def test_execute_exception_becomes_structured_error_without_secret(fake):
    fake.raise_on.add("op1")
    _, out = plan_and_apply(ctx(), [budget_op()])
    assert out["status"] == "failed" and out["results"][0]["result"]["error"] == "execute_failed"
    assert "ya29" not in json.dumps(out)


def test_journal_masks_secrets_and_pii(fake):
    op = pause_op(body={"status": "PAUSED", "access_token": "EAA" + "x" * 30, "email": "jo@example.com"})
    plan = write_core.create_plan(ctx(allow_pii=True), "google_ads", [op], title="t")
    if "error" in plan:  # undeclared PII refused by the policy (stricter): the flagged op goes through
        plan_and_apply(ctx(allow_pii=True), [dataclasses.replace(op, pii=True)])
    text = "".join(p.read_text(encoding="utf-8") for p in write_core.journal.files("acme"))
    assert "jo@example.com" not in text and "EAA" + "x" * 30 not in text


# ------------------------------------------------------------------ rollback
def test_rollback_round_trip(fake):
    c = ctx()
    plan, out = plan_and_apply(c, [budget_op()])
    assert fake.state[URL]["amountMicros"] == 11_000_000
    back = write_core.rollback_plan(c, "google_ads", plan["plan_id"])
    assert "error" not in back, back
    assert back["rollback_of"] == plan["plan_id"] and back["plan_id"] != plan["plan_id"]
    assert back["not_reversible"] == []
    approve(c, back)
    out = write_core.apply_plan(c, "google_ads", back["plan_id"], back["confirmation_token"], back["confirm"])
    assert out["status"] == "applied" and fake.state[URL]["amountMicros"] == 10_000_000
    again = write_core.rollback_plan(c, "google_ads", back["plan_id"])  # rollback of the rollback
    assert "error" not in again, again
    approve(c, again)
    write_core.apply_plan(c, "google_ads", again["plan_id"], again["confirmation_token"], again["confirm"])
    assert fake.state[URL]["amountMicros"] == 11_000_000
    kinds = [e["kind"] for e in write_core.journal.entries("acme", plan_id=plan["plan_id"])]
    assert "rollback" in kinds


def test_rollback_reverses_order_and_reports_irreversible_ops(fake):
    c = ctx()
    second = pause_op("p1", inverse=None, rollback="none")
    plan, _ = plan_and_apply(c, [budget_op(), second])
    back = write_core.rollback_plan(c, "google_ads", plan["plan_id"])
    assert back["not_reversible"] == ["p1"] and [o["op_id"] for o in back["ops"]] == ["op1-rb"]


def test_rollback_only_undoes_ops_that_succeeded(fake):
    fake.fail_on.add("op2")
    c = ctx()
    url2 = URL + "#b2"
    fake.state[url2] = {"amountMicros": 10_000_000, "currency": "EUR"}
    plan, out = plan_and_apply(c, [budget_op(), budget_op("op2", url=url2)])
    assert out["status"] == "partial"
    back = write_core.rollback_plan(c, "google_ads", plan["plan_id"])
    assert [o["op_id"] for o in back["ops"]] == ["op1-rb"]


def test_rollback_errors(fake):
    c = ctx()
    plan = write_core.create_plan(c, "google_ads", [budget_op()], title="t")
    assert write_core.rollback_plan(c, "google_ads", plan["plan_id"])["error"] == "nothing_to_rollback"
    assert write_core.rollback_plan(c, "google_ads", "pl_0000000000000000")["error"] == "plan_not_found"
    assert write_core.rollback_plan(c, "meta", plan["plan_id"])["error"] == "platform_mismatch"
    p2, _ = plan_and_apply(c, [pause_op(inverse=None, rollback="none")])
    out = write_core.rollback_plan(c, "google_ads", p2["plan_id"])
    assert out["error"] == "no_inverse" and out["not_reversible"] == ["p1"]


def test_rollback_uses_handler_invert_with_fresh_before(fake):
    seen = []

    def invert(op, before):
        seen.append(before)
        return dataclasses.replace(op, op_id=op.op_id + "-inv", body={"amountMicros": before["amountMicros"]},
                                   before=None, inverse=None)
    write_core.register_handler("google_ads", fake.handler(invert=invert))
    c = ctx()
    plan, _ = plan_and_apply(c, [budget_op()])
    back = write_core.rollback_plan(c, "google_ads", plan["plan_id"])
    assert seen == [{"amountMicros": 10_000_000, "currency": "EUR"}]
    assert back["ops"][0]["op_id"] == "op1-inv"


# ------------------------------------------------------------------ MCP tools
class ClientErrorDouble(Exception):
    def __init__(self, code, detail, fix):
        super().__init__(detail)
        self.code, self.detail, self.fix = code, detail, fix


@pytest.fixture
def tools(fake, monkeypatch):
    contexts = {"acme": ctx(), "beta": ctx("beta")}

    def resolve(client_id):
        if client_id is None:
            raise ClientErrorDouble("client_required", "2 clients configured.", "pass client_id")
        if client_id not in contexts:
            raise ClientErrorDouble("unknown_client", f"no client {client_id!r}.", "clients_list")
        return contexts[client_id]
    monkeypatch.setattr(write_core, "_resolve", resolve)
    from mcp.server.fastmcp import FastMCP
    server = FastMCP("t")
    names = write_core.register_apply_tools(server)
    listed = {t.name: t for t in asyncio.run(server.list_tools())}
    fns = {name: server._tool_manager.get_tool(name).fn for name in names}
    return names, listed, fns


def test_tools_names_and_annotations(tools):
    names, listed, _ = tools
    assert names == ["gads_apply", "meta_apply", "openai_ads_apply", "google_apply", "plan_get", "plan_list",
                     "audit_log", "rollback_plan"]
    for name in ("gads_apply", "meta_apply", "openai_ads_apply", "google_apply"):
        a = listed[name].annotations
        assert (a.readOnlyHint, a.destructiveHint, a.idempotentHint, a.openWorldHint) == (False, True, False, True)
        assert set(listed[name].inputSchema["properties"]) == {"plan_id", "confirmation_token", "confirm",
                                                               "client_id"}
    for name in ("plan_get", "plan_list", "audit_log", "rollback_plan"):
        assert listed[name].annotations.readOnlyHint is True
        assert listed[name].annotations.destructiveHint is False
        assert "client_id" in listed[name].inputSchema["properties"]


def test_tools_apply_flow_and_errors(tools, fake):
    _, _, fn = tools
    plan = write_core.create_plan(ctx(), "google_ads", [budget_op()], title="t")
    wrong = fn["meta_apply"](plan["plan_id"], plan["confirmation_token"], plan["confirm"], client_id="acme")
    assert wrong["error"] == "wrong_apply_tool" and "gads_apply" in wrong["fix"]
    assert fn["gads_apply"](plan["plan_id"], "x.y", plan["confirm"])["error"] == "client_required"
    assert fn["gads_apply"](plan["plan_id"], "x.y", "c", client_id="zz")["error"] == "unknown_client"
    assert fn["gads_apply"](plan["plan_id"], "x.y", "c", client_id="beta")["error"] == "plan_not_found"
    got = fn["plan_get"](plan["plan_id"], client_id="acme")
    assert got["plan_id"] == plan["plan_id"] and "confirmation_token" not in got and got["requests"]
    assert fn["plan_list"](client_id="acme")["plans"][0]["plan_id"] == plan["plan_id"]
    assert fn["plan_list"](client_id="beta")["plans"] == []
    refused = fn["gads_apply"](plan["plan_id"], plan["confirmation_token"], plan["confirm"], client_id="acme")
    assert refused["error"] == "approval_required" and "approve " + plan["plan_id"] in refused["fix"]
    approve(ctx(), plan)
    out = fn["gads_apply"](plan["plan_id"], plan["confirmation_token"], plan["confirm"], client_id="acme")
    assert out["status"] == "applied"
    log = fn["audit_log"](client_id="acme", plan_id=plan["plan_id"])
    assert [e["kind"] for e in log["entries"]] == ["plan", "approval", "apply_intent", "apply_result"]
    assert log["untrusted_text"]["note"] == write_core.UNTRUSTED_NOTE
    assert fn["audit_log"](client_id="beta")["entries"] == []
    back = fn["rollback_plan"](plan["plan_id"], client_id="acme")
    assert back["rollback_of"] == plan["plan_id"] and back["confirmation_token"]
    assert fn["plan_get"]("pl_0000000000000000", client_id="acme")["error"] == "plan_not_found"


def test_tools_never_raise(tools, monkeypatch):
    _, _, fn = tools

    def explode(*a, **k):
        raise RuntimeError("secret sk-" + "a" * 20)
    monkeypatch.setattr(write_core, "apply_plan", explode)
    monkeypatch.setattr(write_core, "load_plan", lambda *a: {"platform": "google_ads"})
    out = fn["gads_apply"]("pl_0000000000000000", "a.b", "c", client_id="acme")
    assert out["error"] == "internal_error" and "sk-" not in json.dumps(out) and out["fix"]
    monkeypatch.setattr(write_core, "_resolve", lambda cid: (_ for _ in ()).throw(ImportError("no clients")))
    assert fn["plan_list"](client_id="acme")["error"] == "clients_unavailable"


# ------------------------------------------------------------------ integration with the real clients.py
def test_real_client_context_and_account_normalisation(fake):
    clients = pytest.importorskip("google_ecommerce_mcp.clients")
    try:
        real = clients.ClientContext(
            client_id="acme", label="Acme", legacy=False, sandbox=True,
            _accounts={"google_ads": {"customer_id": ACCOUNT}},
            _policies={"google_ads": clients.WritePolicy(enabled=True, account_allow=frozenset({ACCOUNT}),
                                                         max_daily_budget_micros=50_000_000, cap_currency="EUR")})
    except TypeError:
        pytest.skip("ClientContext constructor differs")
    dashed = f"{ACCOUNT[:3]}-{ACCOUNT[3:6]}-{ACCOUNT[6:]}"
    plan, out = plan_and_apply(real, [budget_op(account=dashed)])
    assert out["status"] == "applied"
    stranger = budget_op(account="9998887777")
    refused = write_core.create_plan(real, "google_ads", [stranger], title="t")
    assert refused["error"] == "account_not_allowed"


# ------------------------------------------------------------------ unhashable payloads fail closed
HUGE = 10 ** 5000  # str() / json.dumps of an int over 4300 digits raises ValueError


def test_huge_int_in_body_is_payload_invalid_at_plan(fake):
    for op in (pause_op(body={"status": "PAUSED", "n": HUGE}), budget_op(body={"amountMicros": HUGE}),
               pause_op(params={"x": HUGE})):
        out = write_core.create_plan(ctx(), "google_ads", [op], title="t")
        assert out["error"] == "payload_invalid" and "confirmation_token" not in out, out
        assert out["fix"] and "ValueError" in out["detail"]
    assert fake.dry_runs == [] and fake.calls == []


def test_huge_int_from_the_snapshot_is_payload_invalid(fake):
    fake.state[URL]["extra"] = HUGE
    out = write_core.create_plan(ctx(), "google_ads", [budget_op(old=None)], title="t")
    assert out["error"] == "payload_invalid" and fake.dry_runs == []


def test_huge_int_at_apply_is_payload_invalid_and_nothing_is_sent(fake, monkeypatch):
    c, plan = _plan(fake)
    fake.state[URL]["extra"] = HUGE  # the fresh snapshot of the apply cannot be fingerprinted
    out = write_core.apply_plan(c, "google_ads", plan["plan_id"], plan["confirmation_token"], plan["confirm"])
    assert out["error"] == "payload_invalid" and fake.calls == []
    del fake.state[URL]["extra"]

    def boom(*a, **k):
        raise ValueError("Exceeds the limit (4300 digits) for integer string conversion")
    monkeypatch.setattr(write_core, "plan_digest", boom)
    out = write_core.apply_plan(c, "google_ads", plan["plan_id"], plan["confirmation_token"], plan["confirm"])
    assert out["error"] == "payload_invalid" and fake.calls == []
    assert write_core._write_approval("acme", plan["plan_id"])["error"] == "payload_invalid"
    assert not write_core.journal.plan_consumed("acme", plan["plan_id"])


# ------------------------------------------------------------------ PII of the before snapshot (B1, B4)
_OLD_PII = {"email": "old-a@example.com", "emailAddress": "old-b@example.com",
            "contact_email": "old-c@example.com", "hashedPhoneNumber": "deadbeefcafe0001",
            "userData": {"note": "old-userdata-value"}, "token": "old-secret-token-123456"}
_NEW_PII = {"emailAddress": "new-b@example.com", "contact_email": "new-c@example.com",
            "hashedPhoneNumber": "feedface00000002"}
_CLEAR = [*(v for v in _OLD_PII.values() if isinstance(v, str)), "old-userdata-value", *_NEW_PII.values()]


def _no_clear_pii(where: str, value: Any) -> None:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    leaked = [v for v in _CLEAR if v in text]
    assert not leaked, f"{where} shows {leaked}"


def test_before_and_body_pii_never_shown_in_clear(tools, fake):
    _, _, fn = tools
    url = URL + "#pii"
    before = {"status": "ENABLED", **copy.deepcopy(_OLD_PII)}
    fake.state[url] = copy.deepcopy(before)
    c = ctx(allow_pii=True)
    op = pause_op("pii1", url=url, body={"status": "PAUSED", **_NEW_PII}, pii=True, before=before)
    plan = write_core.create_plan(c, "google_ads", [op], title="t")
    assert "error" not in plan, plan
    _no_clear_pii("create_plan", plan)
    assert any("emailAddress: [pii] -> [pii]" in line for line in plan["ops"][0]["changes"]), plan["ops"]
    _no_clear_pii("plan_get", fn["plan_get"](plan["plan_id"], client_id="acme"))
    preview = write_core.approval_preview("acme", plan["plan_id"])
    assert "error" not in preview, preview
    _no_clear_pii("approve preview", preview)
    approve(c, plan)
    out = write_core.apply_plan(c, "google_ads", plan["plan_id"], plan["confirmation_token"], plan["confirm"])
    assert out["status"] == "applied", out
    _no_clear_pii("apply", out)
    _no_clear_pii("plan_get after apply", fn["plan_get"](plan["plan_id"], client_id="acme"))
    _no_clear_pii("audit_log", fn["audit_log"](client_id="acme"))
    _no_clear_pii("journal on disk", "".join(p.read_text(encoding="utf-8")
                                             for p in write_core.journal.files("acme")))
    intent = write_core.journal.entries("acme", plan_id=plan["plan_id"], kinds=("apply_intent",))[0]
    assert intent["before"]["status"] == "ENABLED"  # non-PII fields stay readable
    for key in ("email", "emailAddress", "contact_email", "hashedPhoneNumber", "userData"):
        assert intent["before"][key] == write_journal.PII_MASK, key
    assert intent["before"]["token"] != _OLD_PII["token"]


def test_audit_log_masks_records_written_by_an_older_masking(tools, fake):
    _, _, fn = tools
    path = write_core.journal.path("acme")
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": "2026-01-01T00:00:00+00:00", "kind": "apply_intent", "client_id": "acme", "plan_id":
              "pl_0000000000000001", "before": copy.deepcopy(_OLD_PII), "body": {"data": [dict(_NEW_PII)]}}
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    log = fn["audit_log"](client_id="acme")
    assert log["entries"][0]["kind"] == "apply_intent"
    _no_clear_pii("audit_log (old record)", log)


# ------------------------------------------------------------------ interrupted apply (fail closed)
class _Crash(BaseException):
    """A process kill / power cut: not an Exception, so nothing in apply_plan catches it."""


@pytest.mark.parametrize("where", ["execute", "apply_result"])
def test_apply_interrupted_after_the_first_send_cannot_be_replayed(fake, where):
    c, plan = _plan(fake)
    journal = write_core.journal
    real_append = journal.append
    real_execute = fake.execute

    def crash_on_result(client_id: str, kind: str, **fields: Any) -> dict:
        if kind == "apply_result":
            raise _Crash()
        return real_append(client_id, kind, **fields)

    def crash_after_send(ctx_: Any, op: WriteOp, grant: Any) -> dict:
        real_execute(ctx_, op, grant)  # the request reached the platform
        raise _Crash()

    if where == "apply_result":
        journal.append = crash_on_result  # type: ignore[method-assign]
    else:
        write_core.register_handler("google_ads", PlatformHandler(
            dry_run=fake.dry_run, snapshot=fake.snapshot, execute=crash_after_send))
    with pytest.raises(_Crash):
        write_core.apply_plan(c, "google_ads", plan["plan_id"], plan["confirmation_token"], plan["confirm"])
    if where == "apply_result":
        del journal.append
    else:
        write_core.register_handler("google_ads", fake.handler())
    assert len(fake.calls) == 1
    lock = plan_file(c, plan["plan_id"]).with_suffix(".lock")
    assert lock.exists()  # the orphan lock stays: only the user removes it
    assert journal.plan_consumed("acme", plan["plan_id"])
    assert not journal.entries("acme", plan_id=plan["plan_id"], kinds=("apply_result",))
    again = write_core.apply_plan(c, "google_ads", plan["plan_id"], plan["confirmation_token"], plan["confirm"])
    assert again["error"] == "plan_already_applied"
    assert write_core._write_approval("acme", plan["plan_id"])["error"] == "plan_already_applied"
    lock.unlink()  # even once the user deletes the lock, the journaled apply_intent refuses the replay
    again = write_core.apply_plan(c, "google_ads", plan["plan_id"], plan["confirmation_token"], plan["confirm"])
    assert again["error"] == "plan_already_applied" and len(fake.calls) == 1

"""write_journal: JSONL append + fsync, one file per client and month, masking, single-use lookup, fail-closed."""

from __future__ import annotations

import datetime
import json
import os

import pytest

from google_ecommerce_mcp import write_journal as wj
from google_ecommerce_mcp.write_journal import Journal, JournalError


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("GEM_STATE_DIR", str(tmp_path / "state"))


def test_state_dir_override_and_fallback(tmp_path, monkeypatch):
    assert wj.state_dir() == tmp_path / "state"
    monkeypatch.delenv("GEM_STATE_DIR")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    expected_root = tmp_path / ("appdata" if os.name == "nt" else "xdg")
    assert wj.state_dir() == expected_root / "google-ecommerce-mcp" / "state"


def test_one_file_per_client_and_month_with_fsync(monkeypatch):
    calls = []
    real = os.fsync
    monkeypatch.setattr(wj.os, "fsync", lambda fd: (calls.append(fd), real(fd)))
    journal = Journal()
    journal.append("acme", "plan", plan_id="pl_1")
    journal.append("beta", "plan", plan_id="pl_2")
    month = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")
    acme = wj.state_dir() / "journal" / "acme" / f"{month}.jsonl"
    assert acme.is_file() and (wj.state_dir() / "journal" / "beta" / f"{month}.jsonl").is_file()
    assert len(calls) == 2
    record = json.loads(acme.read_text(encoding="utf-8").strip())
    assert record["kind"] == "plan" and record["client_id"] == "acme" and record["ts"].endswith("+00:00")
    assert journal.entries("beta")[0]["plan_id"] == "pl_2"
    if os.name != "nt":
        assert (acme.stat().st_mode & 0o777) == 0o600


def test_masking_of_secrets_pii_and_truncation():
    journal = Journal()
    body = {"access_token": "EAA" + "z" * 30, "data": [{"user_data": {"em": "abc"}, "email": "a@b.c"}],
            "phone": "+33600000000", "note": "Bearer abcdef123456", "big": "x" * 5000}
    record = journal.append("acme", "apply_intent", plan_id="pl_1", op_id="o", body=body,
                            url="https://x.test/p?access_token=" + "s" * 20, before={"email": "a@b.c"})
    text = json.dumps(record)
    for leaked in ("EAA" + "z" * 30, "a@b.c", "+33600000000", "abcdef123456", "s" * 20):
        assert leaked not in text
    assert record["body"]["_truncated"] is True and record["body_digest"] == wj.sha256_hex(body)
    assert record["before"] == {"email": "[pii]"}


def test_injected_redactor_is_used():
    journal = Journal(redactor=lambda v: "MASKED" if isinstance(v, str) else v)
    record = journal.append("acme", "plan", plan_id="pl_1")
    assert record["plan_id"] == "MASKED"


def test_entries_filters_and_plan_consumed():
    journal = Journal()
    journal.append("acme", "plan", plan_id="pl_1")
    assert not journal.plan_consumed("acme", "pl_1")
    journal.append("acme", "apply_intent", plan_id="pl_1", op_id="a")
    journal.append("acme", "apply_result", plan_id="pl_1", op_id="a", status="ok")
    journal.append("acme", "plan", plan_id="pl_2")
    assert journal.plan_consumed("acme", "pl_1") and not journal.plan_consumed("acme", "pl_2")
    assert not journal.plan_consumed("beta", "pl_1")
    assert [e["kind"] for e in journal.entries("acme", plan_id="pl_1")] == ["plan", "apply_intent",
                                                                             "apply_result"]
    assert len(journal.entries("acme", kinds=("plan",))) == 2
    assert journal.entries("acme", limit=1)[0]["plan_id"] == "pl_2"


def test_malformed_lines_are_skipped():
    journal = Journal()
    journal.append("acme", "plan", plan_id="pl_1")
    path = journal.files("acme")[0]
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("{broken\n[1,2]\n")
    assert len(journal.entries("acme")) == 1


def test_fail_closed(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a folder")
    with pytest.raises(JournalError):
        Journal(root=blocker).append("acme", "plan", plan_id="pl_1")
    with pytest.raises(JournalError):
        Journal().append("../evil", "plan", plan_id="pl_1")
    with pytest.raises(JournalError):
        Journal().append("acme", "nonsense")
    with pytest.raises(JournalError):
        Journal().append("acme", "plan", body={"x": float("nan")}, extra=object())


def test_safe_id():
    assert wj.safe_id("acme-1.b_2") == "acme-1.b_2"
    for bad in ("", ".", "..", "a/b", "a\\b", "x" * 65, None, "-x"):
        with pytest.raises(ValueError):
            wj.safe_id(bad)


def test_write_private_atomic(tmp_path):
    target = tmp_path / "d" / "f.json"
    wj.write_private_atomic(target, "one")
    wj.write_private_atomic(target, "two")
    assert target.read_text(encoding="utf-8") == "two" and [p.name for p in target.parent.iterdir()] == ["f.json"]


def test_params_are_journaled_masked_with_their_digest():
    journal = Journal(redactor=lambda v: v)
    params = {"partialFailure": "true", "email": "jo@example.com", "data": "x" * 3000}
    record = journal.append("acme", "apply_intent", plan_id="pl_1", body={"a": 1}, params=params)
    assert record["params_digest"] == wj.sha256_hex(params) and record["body_digest"] == wj.sha256_hex({"a": 1})
    assert record["params"]["_truncated"] is True  # long params truncated like a body
    stored = journal.entries("acme", strict=True)[0]
    assert stored["params_digest"] == wj.sha256_hex(params)
    raw = b"".join(p.read_bytes() for p in journal.files("acme"))
    assert b"jo@example.com" not in raw
    small = journal.append("acme", "apply_intent", plan_id="pl_2", params={"partialFailure": "true", "em": "h"})
    assert small["params"] == {"partialFailure": "true", "em": wj.PII_MASK}


_SUBSTRING_PII = {"emailAddress": "b@example.com", "contact_email": "c@example.com",
                  "hashedPhoneNumber": "deadbeef0001", "userData": {"anything": "ud-clear-value"}}


def test_mask_pii_uses_the_policy_detector_at_every_level():
    value = {"status": "ENABLED", "before": dict(_SUBSTRING_PII),
             "operations": [{"create": {"userIdentifiers": [{"hashedEmail": "h1"}], "contact_email": "c2@x.y"}}],
             "form": json.dumps({"user_data": {"em": "e3"}, "emailAddress": "form@example.com"}), "name": "keep"}
    masked = wj.mask_pii(value)
    text = json.dumps(masked)
    for leaked in ("b@example.com", "c@example.com", "deadbeef0001", "ud-clear-value", "h1", "c2@x.y", "e3",
                   "form@example.com"):
        assert leaked not in text, leaked
    assert all(masked["before"][k] == wj.PII_MASK for k in _SUBSTRING_PII)
    assert masked["status"] == "ENABLED" and masked["name"] == "keep"
    assert json.loads(masked["form"])["emailAddress"] == wj.PII_MASK  # JSON strings re-encoded, masked
    assert wj.mask_pii({"form": '{"a": 1}'}) == {"form": '{"a": 1}'}  # untouched when nothing to mask
    assert wj.mask_pii({"emailAddress": ""}) == {"emailAddress": ""}  # empty values kept (nothing to hide)


def test_journal_masks_substring_pii_keys_on_disk():
    journal = Journal()
    journal.append("acme", "apply_intent", plan_id="pl_1", op_id="o", before=dict(_SUBSTRING_PII),
                   body={"data": [dict(_SUBSTRING_PII)]})
    raw = "".join(p.read_text(encoding="utf-8") for p in journal.files("acme"))
    for leaked in ("b@example.com", "c@example.com", "deadbeef0001", "ud-clear-value"):
        assert leaked not in raw, leaked
    stored = journal.entries("acme", strict=True)[0]
    assert stored["before"] == {k: wj.PII_MASK for k in _SUBSTRING_PII}


def test_mask_pii_fails_closed_when_write_policy_cannot_be_imported(monkeypatch):
    import sys
    monkeypatch.setattr(wj, "_POLICY_PII", [])  # nothing cached
    monkeypatch.setitem(sys.modules, "google_ecommerce_mcp.write_policy", None)  # import raises ImportError
    assert wj._policy_pii_key() is wj._every_key_is_pii
    masked = wj.mask_pii({"email": "a@b.c", "emailAddress": "x@y.z", "status": "PAUSED", "amountMicros": 5})
    assert masked == {k: wj.PII_MASK for k in ("email", "emailAddress", "status", "amountMicros")}
    assert wj._POLICY_PII == []  # not cached: a later import may succeed
    monkeypatch.setattr(wj, "_policy_pii_key", lambda: None)  # a missing detector also fails closed
    assert wj.mask_pii({"status": "x"}) == {"status": wj.PII_MASK}


def test_mask_pii_masks_form_strings():
    form = "event_name=Purchase&em=deadbeef01&value=12&client_ip_address=10.0.0.1"
    masked = wj.mask_pii({"data": form})["data"]
    assert "deadbeef01" not in masked and "10.0.0.1" not in masked
    assert "event_name=Purchase" in masked and "value=12" in masked and "em=[pii]" in masked
    assert wj.mask_pii({"data": "a=b&c=d"}) == {"data": "a=b&c=d"}  # untouched when nothing to mask
    assert wj.mask_pii("emailAddress=x%40y.z") == "emailAddress=[pii]"
    # a form value holding JSON is masked too
    nested = wj.mask_pii("user_data=%7B%22em%22%3A%22e9%22%7D&x=1")
    assert "e9" not in nested and "x=1" in nested
    assert wj.mask_pii("https://graph.facebook.com/v23.0/act_1/events") == \
        "https://graph.facebook.com/v23.0/act_1/events"


def test_mask_pii_masks_too_deep_json_whole():
    deep = "[" * 100_000 + "]" * 100_000
    assert wj.mask_pii({"payload": deep}) == {"payload": wj.PII_MASK}
    deep_obj = '{"a":' * 100_000 + '{"email":"leak@x.y"}' + "}" * 100_000
    assert wj.mask_pii(deep_obj) == wj.PII_MASK


def test_mask_pii_detector_error_fails_closed(monkeypatch):
    def broken(key):
        raise RuntimeError("detector down")
    monkeypatch.setattr(wj, "_policy_pii_key", lambda: broken)
    assert wj.mask_pii({"status": "x"}) == {"status": wj.PII_MASK}

"""write_tokens: HMAC-SHA256 confirmation tokens, per-client keys, master key sources (env, keyring, file)."""

from __future__ import annotations

import base64
import os
import sys
import types

import pytest

from google_ecommerce_mcp import write_tokens as wt
from google_ecommerce_mcp.write_journal import canonical_json

NOW = 1_800_000_000
CLAIMS = dict(client_id="acme", plan_id="pl_0123456789abcdef", platform="google_ads", digest="d" * 64)


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("GEM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv(wt.KEY_ENV, "cd" * 32)


def token(**overrides):
    values = dict(CLAIMS, exp=NOW + 900)
    values.update(overrides)
    return wt.issue_token(**values)


def verify(tok, **overrides):
    values = dict(CLAIMS, now=NOW)
    values.update(overrides)
    return wt.verify_token(tok, **values)


def code_of(fn, *args, **kwargs):
    with pytest.raises(wt.TokenError) as info:
        fn(*args, **kwargs)
    assert info.value.detail and info.value.fix
    return info.value.code


def test_round_trip_and_shape():
    tok = token()
    payload, sig = tok.split(".")
    assert "=" not in tok and "+" not in tok and "/" not in tok
    assert verify(tok)["plan_id"] == CLAIMS["plan_id"]
    assert len(wt._b64d(sig)) == 32


def test_each_refusal():
    tok = token()
    assert code_of(verify, tok, client_id="beta") == "token_client_mismatch"
    assert code_of(verify, tok, platform="meta") == "token_platform_mismatch"
    assert code_of(verify, tok, plan_id="pl_ffffffffffffffff") == "token_plan_mismatch"
    assert code_of(verify, tok, digest="e" * 64) == "token_digest_mismatch"
    assert code_of(verify, tok, now=NOW + 900) == "token_expired"
    assert code_of(verify, token(exp=NOW - 1)) == "token_expired"
    for junk in (None, 12, "", "nodot", "a.b.c", "%%%.%%%", "x" * 5000):
        assert code_of(verify, junk) == "token_malformed"
    not_obj = wt._b64e(b"[1]") + "." + wt._b64e(b"x")
    assert code_of(verify, not_obj) == "token_malformed"


def test_altered_payload_or_signature_is_refused():
    payload, sig = token().split(".")
    forged = wt._b64e(canonical_json(dict(CLAIMS, exp=NOW + 99999)).encode()) + "." + sig
    assert code_of(verify, forged) == "token_bad_signature"
    raw = bytearray(wt._b64d(sig))
    raw[0] ^= 1
    assert code_of(verify, payload + "." + wt._b64e(bytes(raw))) == "token_bad_signature"


def test_keys_are_per_client_and_per_master(monkeypatch):
    assert wt.client_key("acme") != wt.client_key("beta")
    # a token re-labelled for another client does not verify with that client's key
    payload_b = wt._b64e(canonical_json(dict(CLAIMS, client_id="beta", exp=NOW + 900)).encode())
    sig_a = token().split(".")[1]
    assert code_of(verify, f"{payload_b}.{sig_a}", client_id="beta") == "token_bad_signature"
    tok = token()
    monkeypatch.setenv(wt.KEY_ENV, "ef" * 32)
    assert code_of(verify, tok) == "token_bad_signature"


def test_master_key_formats(monkeypatch):
    monkeypatch.setenv(wt.KEY_ENV, base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("="))
    assert wt.master_key() == b"k" * 32
    for bad in ("short", "ab" * 10, "!" * 70):
        monkeypatch.setenv(wt.KEY_ENV, bad)
        assert code_of(wt.master_key) == "write_key_invalid"


def test_clamp_ttl():
    assert wt.clamp_ttl(900) == 900
    assert wt.clamp_ttl(5) == wt.MIN_TTL
    assert wt.clamp_ttl(10**9) == wt.MAX_TTL
    assert wt.clamp_ttl("x") == wt.DEFAULT_TTL and wt.clamp_ttl(None) == wt.DEFAULT_TTL


def _fake_keyring(monkeypatch, store, fail=False):
    mod = types.ModuleType("keyring")

    def get_password(service, user):
        if fail:
            raise RuntimeError("no backend")
        return store.get((service, user))

    def set_password(service, user, value):
        if fail:
            raise RuntimeError("no backend")
        store[(service, user)] = value
    mod.get_password, mod.set_password = get_password, set_password
    monkeypatch.setitem(sys.modules, "keyring", mod)


def test_master_key_created_once_in_keyring(monkeypatch):
    monkeypatch.delenv(wt.KEY_ENV)
    store: dict = {}
    _fake_keyring(monkeypatch, store)
    first = wt.master_key()
    assert len(first) == 32 and store[("google-ecommerce-mcp", wt.MASTER_ENTRY)] == first.hex()
    assert wt.master_key() == first


def test_master_key_file_fallback_when_no_keyring(monkeypatch, tmp_path):
    monkeypatch.delenv(wt.KEY_ENV)
    _fake_keyring(monkeypatch, {}, fail=True)
    first = wt.master_key()
    path = tmp_path / "state" / wt.KEY_FILE
    assert path.is_file() and len(first) == 32 and wt.master_key() == first
    if os.name != "nt":
        assert (path.stat().st_mode & 0o777) == 0o600


def test_master_key_unavailable_is_structured(monkeypatch, tmp_path):
    monkeypatch.delenv(wt.KEY_ENV)
    _fake_keyring(monkeypatch, {}, fail=True)
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("GEM_STATE_DIR", str(blocker / "sub"))
    assert code_of(wt.master_key) == "write_key_unavailable"


# ------------------------------------------------------------------ write grants (M1)
def _grant(**over):
    fields = dict(client_id="acme", plan_id="pl_1", op_id="op1", method="POST",
                  url="https://ads.example.com/v1/x:mutate", body_digest="b" * 64, request_digest="r" * 64, mac="")
    fields.update(over)
    return types.SimpleNamespace(**fields)


def test_sign_grant_is_hex_hmac_with_the_client_key():
    import hashlib
    import hmac

    g = _grant()
    material = b"gem-grant-v1|acme|pl_1|op1|POST|https://ads.example.com/v1/x:mutate|" + b"b" * 64 + b"|" + b"r" * 64
    assert wt.grant_material(g) == material
    assert wt.sign_grant(g) == hmac.new(wt.client_key("acme"), material, hashlib.sha256).hexdigest()


def test_verify_grant_mac():
    g = _grant()
    g.mac = wt.sign_grant(g)
    assert wt.verify_grant_mac(g) is True
    for name, value in (("op_id", "op2"), ("request_digest", "s" * 64), ("client_id", "other"),
                        ("mac", ""), ("mac", None), ("mac", g.mac.upper())):
        assert wt.verify_grant_mac(_grant(**{**vars(g), name: value})) is False
    assert wt.verify_grant_mac(object()) is False


def test_grant_mac_changes_with_the_master_key(monkeypatch):
    g = _grant()
    g.mac = wt.sign_grant(g)
    monkeypatch.setenv(wt.KEY_ENV, "ee" * 32)
    assert wt.verify_grant_mac(g) is False


def test_grant_material_refuses_bad_fields():
    with pytest.raises(ValueError):
        wt.grant_material(_grant(plan_id="a|b"))
    with pytest.raises(ValueError):
        wt.grant_material(_grant(request_digest=None))
    assert b"?a=b|c" in wt.grant_material(_grant(url="https://ads.example.com/v1/x?a=b|c"))

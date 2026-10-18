"""Confirmation tokens of write plans: b64url(payload).b64url(hmac), HMAC-SHA256 with a per-client key.

payload = {"plan_id", "client_id", "platform", "digest", "exp"} (canonical JSON). The per-client key is
HMAC-SHA256(master, "gem-write-v1|<client_id>"); the 32-byte master key comes from, in order:
GEM_WRITE_HMAC_KEY (CI; 64 hex chars or base64url of >= 32 bytes), the keyring entry `write-hmac-master`
(service google-ecommerce-mcp, created on first use), else the file <state>/write-hmac-master.key (mode 600).

The token proves integrity (the plan was not changed and comes from this server), NOT human approval: the model
reads it. Single use is enforced by write_core through the journal.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
from typing import Any

from .write_journal import canonical_json, state_dir, write_private_atomic

__all__ = ["TokenError", "MASTER_ENTRY", "KEY_ENV", "DEFAULT_TTL", "MIN_TTL", "MAX_TTL", "master_key",
           "client_key", "clamp_ttl", "issue_token", "verify_token", "grant_material", "sign_grant",
           "verify_grant_mac"]

MASTER_ENTRY = "write-hmac-master"
KEY_ENV = "GEM_WRITE_HMAC_KEY"
KEY_FILE = "write-hmac-master.key"
DEFAULT_TTL = 900
MIN_TTL = 60
MAX_TTL = 3600
_KEY_LABEL = b"gem-write-v1|"


class TokenError(Exception):
    """Token refused: code, detail (where + cause), fix."""

    def __init__(self, code: str, detail: str, fix: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.fix = fix

    def as_dict(self) -> dict:
        return {"error": self.code, "detail": self.detail, "fix": self.fix}


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _decode_key(raw: str, where: str) -> bytes:
    raw = raw.strip()
    key: bytes | None = None
    if len(raw) >= 64 and len(raw) % 2 == 0:
        try:
            key = bytes.fromhex(raw)
        except ValueError:
            key = None
    if key is None:
        try:
            key = _b64d(raw)
        except (binascii.Error, ValueError):
            key = None
    if key is None or len(key) < 32:
        raise TokenError("write_key_invalid", f"{where}: the write signing key is not 32 bytes "
                         "(64 hex chars or base64url).",
                         f"Set {KEY_ENV} to 64 hex characters (python -c \"import secrets;"
                         "print(secrets.token_hex(32))\"), or unset it to use the keyring.")
    return key


def _keyring_master() -> bytes | None:
    """Master key from the keyring (created when absent); None when no keyring backend works."""
    try:
        import keyring

        from .config import KEYRING_SERVICE
        stored = keyring.get_password(KEYRING_SERVICE, MASTER_ENTRY)
        if stored:
            return _decode_key(stored, f"keyring entry {MASTER_ENTRY}")
        fresh = secrets.token_hex(32)
        keyring.set_password(KEYRING_SERVICE, MASTER_ENTRY, fresh)
        again = keyring.get_password(KEYRING_SERVICE, MASTER_ENTRY)
        return _decode_key(again, f"keyring entry {MASTER_ENTRY}") if again else None
    except TokenError:
        raise
    except Exception:
        return None


def _file_master() -> bytes:
    path = state_dir() / KEY_FILE
    if path.is_file():
        return _decode_key(path.read_text(encoding="utf-8"), f"file {path}")
    write_private_atomic(path, secrets.token_hex(32))
    return _decode_key(path.read_text(encoding="utf-8"), f"file {path}")


def master_key() -> bytes:
    """32+ byte master key: GEM_WRITE_HMAC_KEY, else keyring `write-hmac-master`, else the 600 state file."""
    env = (os.environ.get(KEY_ENV) or "").strip()
    if env:
        return _decode_key(env, KEY_ENV)
    key = _keyring_master()
    if key is not None:
        return key
    try:
        return _file_master()
    except OSError as exc:
        raise TokenError("write_key_unavailable", f"Write signing key: no keyring and the state folder is not "
                         f"writable ({type(exc).__name__}).",
                         f"Install a keyring backend, set {KEY_ENV}, or make GEM_STATE_DIR writable.") from exc


def client_key(client_id: str) -> bytes:
    """Per-client signing key derived from the master key (a token for client A never verifies for B)."""
    return hmac.new(master_key(), _KEY_LABEL + client_id.encode("utf-8"), hashlib.sha256).digest()


def clamp_ttl(ttl: Any) -> int:
    """Token lifetime in seconds, clamped to MIN_TTL..MAX_TTL (DEFAULT_TTL when unusable)."""
    try:
        value = int(ttl)
    except (TypeError, ValueError):
        value = DEFAULT_TTL
    return max(MIN_TTL, min(MAX_TTL, value))


def issue_token(*, client_id: str, plan_id: str, platform: str, digest: str, exp: int) -> str:
    """Signed token b64url(payload).b64url(HMAC-SHA256(client_key, payload))."""
    payload = canonical_json({"plan_id": plan_id, "client_id": client_id, "platform": platform,
                              "digest": digest, "exp": int(exp)}).encode("utf-8")
    mac = hmac.new(client_key(client_id), payload, hashlib.sha256).digest()
    return f"{_b64e(payload)}.{_b64e(mac)}"


def _refuse(code: str, cause: str, fix: str) -> TokenError:
    return TokenError(code, f"Confirmation token: {cause}", fix)


_REPLAN = "Create a new plan with the plan_* tool and use the token it returns."


def verify_token(token: Any, *, client_id: str, plan_id: str, platform: str, digest: str, now: float) -> dict:
    """Payload of a valid token, else TokenError (malformed, other client, bad signature, other platform or
    plan, expired, digest different). Signatures and digests are compared with hmac.compare_digest."""
    if not isinstance(token, str) or token.count(".") != 1 or len(token) > 4096:
        raise _refuse("token_malformed", "not of the form payload.signature.", "Pass the confirmation_token "
                      "exactly as the plan returned it.")
    body, sig = token.split(".")
    try:
        payload_raw = _b64d(body)
        mac = _b64d(sig)
        claims = json.loads(payload_raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise _refuse("token_malformed", "cannot be decoded.", "Pass the confirmation_token exactly as the plan "
                      "returned it.") from None
    if not isinstance(claims, dict):
        raise _refuse("token_malformed", "payload is not an object.", _REPLAN)
    if not hmac.compare_digest(str(claims.get("client_id", "")).encode(), client_id.encode()):
        raise _refuse("token_client_mismatch", f"issued for another client, not {client_id!r}.",
                      "Apply the plan with the client_id it was created for.")
    expected = hmac.new(client_key(client_id), payload_raw, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise _refuse("token_bad_signature", "signature does not match (altered or from another server).",
                      _REPLAN)
    if claims.get("platform") != platform:
        raise _refuse("token_platform_mismatch", f"issued for platform {claims.get('platform')!r}, "
                      f"not {platform!r}.", "Use the *_apply tool of the plan's platform.")
    if claims.get("plan_id") != plan_id:
        raise _refuse("token_plan_mismatch", "issued for another plan.", "Pass the token of this plan_id.")
    exp = claims.get("exp")
    if not isinstance(exp, int) or isinstance(exp, bool) or now >= exp:
        raise _refuse("token_expired", "expired.", _REPLAN)
    if not hmac.compare_digest(str(claims.get("digest", "")).encode(), digest.encode()):
        raise _refuse("token_digest_mismatch", "the plan content differs from what was signed.", _REPLAN)
    return claims


# ------------------------------------------------------------------ write grants (ads_common.WriteGrant)
_GRANT_FIELDS = ("client_id", "plan_id", "op_id", "method", "url", "body_digest", "request_digest")
_GRANT_LABEL = b"gem-grant-v1|"


def grant_material(grant: Any) -> bytes:
    """Bytes signed for a WriteGrant: "gem-grant-v1|" + client_id|plan_id|op_id|method|url|body_digest|
    request_digest. Raises ValueError when a field is not a string or when a field other than url holds '|'
    (the split stays unambiguous: url is followed only by two '|'-free digests)."""
    values = [getattr(grant, name, None) for name in _GRANT_FIELDS]
    for name, value in zip(_GRANT_FIELDS, values):
        if not isinstance(value, str):
            raise ValueError(f"grant field {name} must be a string")
        if name != "url" and "|" in value:
            raise ValueError(f"grant field {name} may not hold '|'")
    return _GRANT_LABEL + "|".join(values).encode("utf-8")


def sign_grant(grant: Any) -> str:
    """Hex HMAC-SHA256 of grant_material(grant) with client_key(grant.client_id), the key of the tokens.
    Raises ValueError (bad field) or TokenError (no signing key)."""
    material = grant_material(grant)
    return hmac.new(client_key(grant.client_id), material, hashlib.sha256).hexdigest()


def verify_grant_mac(grant: Any) -> bool:
    """True only if grant.mac is the sign_grant() value (hmac.compare_digest); False for any malformed grant
    or when the signing key is unavailable."""
    mac = getattr(grant, "mac", None)
    if not isinstance(mac, str) or not mac:
        return False
    try:
        expected = sign_grant(grant)
    except (ValueError, TypeError, AttributeError, TokenError):
        return False
    return hmac.compare_digest(mac.encode("utf-8", "replace"), expected.encode("ascii"))

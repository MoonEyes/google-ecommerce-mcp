"""Per-client secrets: environment first, then the OS keyring. Values are never printed, logged nor stored in the
client registry (the registry only holds keyring entry *names*).

Keyring service "google-ecommerce-mcp", user "<client_id>/<kind>". Kinds:

    google     read-only Google token (OAuth JSON)          google-w   separate Google write token (OAuth JSON)
    gads       Google Ads token (OAuth JSON, adwords)       meta       Meta access token
    openai     ChatGPT (OpenAI) Ads API key                 meta-capi  Meta Conversions API token
    pagespeed  PageSpeed Insights API key

Environment, registry mode: GEM_SECRET_<CLIENT>_<KIND> (upper case, "-" -> "_"), e.g. GEM_SECRET_ACME_META.
The legacy variables (META_ACCESS_TOKEN...) are NOT read for registry clients, so one variable can never feed two
clients. Exception, zero migration: the client named "default" may point at the legacy keyring entries.

Legacy mode (no registry): the current entries and variables, unchanged:
    google -> GOOGLE_TOKEN_FILE (file) / oauth-token      gads -> GADS_TOKEN_FILE (file) / oauth-token-ads
    meta   -> META_ACCESS_TOKEN / meta-access-token        openai -> OPENAI_ADS_API_KEY / openai-ads-key
    pagespeed -> PAGESPEED_API_KEY (no keyring entry)
"""

from __future__ import annotations

import os
import re
from pathlib import Path

__all__ = ["KINDS", "PASTEABLE_KINDS", "LEGACY_KEYRING_USERS", "SecretError", "env_name", "keyring_user",
           "get_secret", "set_secret", "delete_client_secrets", "valid_secret_name", "legacy_pair_ok"]

SERVICE = "google-ecommerce-mcp"  # same as config.KEYRING_SERVICE (kept literal: no heavy import here)
KINDS = ("google", "google-w", "gads", "meta", "openai", "meta-capi", "pagespeed")
# Kinds a human pastes (hidden prompt). The Google ones are OAuth JSON written by the setup commands.
PASTEABLE_KINDS = ("meta", "openai", "meta-capi", "pagespeed")

# kind -> (keyring user, environment variable holding the value, environment variable holding a file path)
_LEGACY: dict[str, tuple[str | None, str | None, str | None]] = {
    "google": ("oauth-token", None, "GOOGLE_TOKEN_FILE"),
    "gads": ("oauth-token-ads", None, "GADS_TOKEN_FILE"),
    "meta": ("meta-access-token", "META_ACCESS_TOKEN", None),
    "openai": ("openai-ads-key", "OPENAI_ADS_API_KEY", None),
    "pagespeed": (None, "PAGESPEED_API_KEY", None),
}
LEGACY_KEYRING_USERS = frozenset(user for user, _, _ in _LEGACY.values() if user)
_LEGACY_BY_USER = {user: (env, file_env) for user, env, file_env in _LEGACY.values() if user}
LEGACY_CLIENT = "default"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}/[a-z0-9][a-z0-9-]{0,39}$")


class SecretError(ValueError):
    """Invalid kind / name / value (the message never contains a secret value)."""


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise SecretError(f"unknown secret kind {kind!r}; expected one of {', '.join(KINDS)}")


def env_name(client_id: str, kind: str) -> str:
    """GEM_SECRET_<CLIENT>_<KIND>. Client ids only hold [a-z0-9-], so the mapping is one-to-one."""
    return "GEM_SECRET_" + client_id.upper().replace("-", "_") + "_" + kind.upper().replace("-", "_")


# The only legacy entry each kind of the client "default" may point at (google-w, meta-capi, pagespeed: none).
LEGACY_PAIRS = {kind: user for kind, (user, _, _) in _LEGACY.items() if user}


def legacy_pair_ok(client_id: str, kind: str | None, name: str) -> bool:
    """True when name is not a legacy entry, or is the legacy entry of exactly this kind for the client "default"
    (google -> oauth-token, gads -> oauth-token-ads, meta -> meta-access-token, openai -> openai-ads-key).
    kind=None (caller without the kind): any legacy entry of "default" passes, the per-kind check happens when the
    secret is read or written."""
    if name not in LEGACY_KEYRING_USERS:
        return True
    if client_id != LEGACY_CLIENT:
        return False
    return kind is None or LEGACY_PAIRS.get(kind) == name


def valid_secret_name(client_id: str, name: str, kind: str | None = None) -> bool:
    """A registry secret name must belong to its client: "<client_id>/<something>". Only "default" may also
    name a legacy entry, and only the one of the same kind (see legacy_pair_ok); never for google-w."""
    if not isinstance(name, str):
        return False
    if name in LEGACY_KEYRING_USERS:
        return legacy_pair_ok(client_id, kind, name)
    return bool(_NAME_RE.match(name)) and name.split("/", 1)[0] == client_id


def keyring_user(client_id: str, kind: str, *, legacy: bool = False, name: str | None = None) -> str | None:
    """Keyring user holding this secret. name: the registry's explicit entry name (validated by the caller)."""
    _check_kind(kind)
    if legacy:
        user = _LEGACY.get(kind, (None, None, None))[0]
        return user if user else f"{LEGACY_CLIENT}/{kind}"
    return name or f"{client_id}/{kind}"


def _env(name: str | None) -> str | None:
    if not name:
        return None
    return (os.environ.get(name) or "").strip() or None


def _read_file(path: str | None) -> str | None:
    if not path:
        return None
    try:
        file = Path(path).expanduser()
        return (file.read_text(encoding="utf-8").strip() or None) if file.exists() else None
    except OSError:
        return None


def _keyring_get(user: str) -> str | None:
    try:
        import keyring

        stored = keyring.get_password(SERVICE, user)
    except Exception:  # no backend (headless Linux), locked keychain...
        return None
    return (stored or "").strip() or None


def get_secret(client_id: str, kind: str, *, legacy: bool = False, name: str | None = None) -> str | None:
    """The secret value, or None. Never raises for a missing backend; never logs."""
    _check_kind(kind)
    if legacy:
        _, env, file_env = _LEGACY.get(kind, (None, None, None))
        file_path = _env(file_env)
        if file_path:  # a token file replaces the keyring entry, exactly as today
            return _read_file(file_path)
        value = _env(env)
        if value:
            return value
        user = keyring_user(client_id, kind, legacy=True)
        return _keyring_get(user) if user else None
    value = _env(env_name(client_id, kind))
    if value:
        return value
    if name and not legacy_pair_ok(client_id, kind, name):  # e.g. google-w -> oauth-token: never read
        return None
    user = keyring_user(client_id, kind, name=name)
    if client_id == LEGACY_CLIENT and user in _LEGACY_BY_USER:  # registry "default" kept on the legacy entries
        env, file_env = _LEGACY_BY_USER[user]
        file_path = _env(file_env)
        if file_path:
            return _read_file(file_path)
        value = _env(env)
        if value:
            return value
    return _keyring_get(user) if user else None


def set_secret(client_id: str, kind: str, value: str, *, legacy: bool = False, name: str | None = None) -> str:
    """Store value in the keyring; returns a description of where (never the value)."""
    _check_kind(kind)
    value = (value or "").strip()
    if not value:
        raise SecretError("empty secret: nothing stored")
    if not legacy and name and not legacy_pair_ok(client_id, kind, name):
        raise SecretError(f"the registry maps {kind!r} of client {client_id!r} to the legacy entry {name!r} of "
                          "another kind: nothing stored. Fix the client's \"secrets\" in the registry")
    user = keyring_user(client_id, kind, legacy=legacy, name=name)
    import keyring

    keyring.set_password(SERVICE, user, value)
    override = (_LEGACY.get(kind, (None, None, None))[1] if legacy else env_name(client_id, kind)) or ""
    tail = f"; {override} still takes precedence when set" if override else ""
    return f"operating system keyring (service {SERVICE}, entry {user}){tail}"


def delete_client_secrets(client_id: str, names: dict[str, str] | None = None) -> list[str]:
    """Best-effort removal of every "<client_id>/<kind>" entry (and the registry's own names for it). Legacy entries
    are never deleted here. Returns the entry names actually removed."""
    users = {f"{client_id}/{kind}" for kind in KINDS}
    users.update(n for n in (names or {}).values() if valid_secret_name(client_id, n))
    users -= LEGACY_KEYRING_USERS
    removed = []
    try:
        import keyring
    except Exception:
        return removed
    for user in sorted(users):
        try:
            if keyring.get_password(SERVICE, user) is not None:
                keyring.delete_password(SERVICE, user)
                removed.append(user)
        except Exception:
            continue
    return removed

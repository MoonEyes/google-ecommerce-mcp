"""Client registry and per-client context (multi-client mode), with the legacy single-account mode kept as is.

Registry absent  -> one implicit client "default" built at each call from today's variables (GA4_PROPERTY_ID,
                    GSC_SITE_URL, MERCHANT_ACCOUNT_ID, GTM_CONTAINER_ID, GADS_CUSTOMER_ID, GADS_LOGIN_CUSTOMER_ID,
                    META_AD_ACCOUNT_ID) and today's keyring entries: zero migration.
Registry present -> account-id variables are ignored (never mixed); a client_id is required as soon as there are
                    two clients; with one client it is the implicit default.
GEM_CLIENT=<id>  -> the process is pinned to that client: any other client_id is refused.

The registry (paths.clients_file()) holds ids and keyring entry *names* only, never a secret value: its schema is
closed (unknown keys are refused), it is written atomically and privately (600 / user-only ACL).

Every result exposes ClientContext, an immutable object passed explicitly to the tools; nothing global is mutated.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from . import paths, secrets_store

__all__ = ["ClientError", "WritePolicy", "ClientContext", "resolve", "list_clients", "ERROR_CODES", "PLATFORMS",
           "ACCOUNT_FIELDS", "WRITE_PLATFORMS", "SECRET_KEYS", "PIN_ENV", "LEGACY_CLIENT_ID", "validate_client_id",
           "validate_account", "load_registry", "save_registry", "add_client", "remove_client", "set_write",
           "legacy_accounts_from_env", "registry_path", "default_account_allow", "mask_id"]

PIN_ENV = "GEM_CLIENT"
LEGACY_CLIENT_ID = "default"
REGISTRY_VERSION = 1
ERROR_CODES = ("client_required", "unknown_client", "platform_not_configured", "id_not_owned", "pinned_client",
               "registry_invalid")


# ------------------------------------------------------------------ errors
class ClientError(Exception):
    """Structured error: code (one of ERROR_CODES), detail (where + cause), fix (the exact next step)."""

    def __init__(self, code: str, detail: str, fix: str) -> None:
        super().__init__(f"{detail} Fix: {fix}")
        self.code, self.detail, self.fix = code, detail, fix

    def to_dict(self) -> dict:
        return {"error": self.code, "detail": self.detail, "fix": self.fix}


# ------------------------------------------------------------------ id formats
def _digits(value: str) -> str:
    cleaned = value.strip()
    if not re.fullmatch(r"[0-9]{1,20}", cleaned):
        raise ValueError("digits only (example: 123456789)")
    return cleaned


def _ga4(value: str) -> str:
    cleaned = value.strip()
    if cleaned.startswith("properties/"):
        cleaned = cleaned[len("properties/"):]
    return _digits(cleaned)


def _merchant(value: str) -> str:
    cleaned = value.strip()
    if cleaned.startswith("accounts/"):
        cleaned = cleaned[len("accounts/"):]
    return _digits(cleaned)


def _gsc(value: str) -> str:
    cleaned = value.strip()
    if re.fullmatch(r"sc-domain:[A-Za-z0-9.-]+\.[A-Za-z]{2,}", cleaned):
        return cleaned
    if re.fullmatch(r"https?://[A-Za-z0-9.-]+(:[0-9]+)?/[^\s]*", cleaned):
        return cleaned
    raise ValueError("sc-domain:example.com, or a URL prefix ending with / (https://www.example.com/)")


def _gads(value: str) -> str:
    cleaned = value.strip().replace("-", "").replace(" ", "")
    if not re.fullmatch(r"[0-9]{10}", cleaned):
        raise ValueError("10 digits, dashes allowed (example: 123-456-7890)")
    return cleaned


def _meta_account(value: str) -> str:
    cleaned = value.strip()
    if cleaned.lower().startswith("act_"):
        cleaned = cleaned[4:]
    return _digits(cleaned)


def _gtm_public(value: str) -> str:
    cleaned = value.strip().upper()
    if not re.fullmatch(r"GTM-[A-Z0-9]{4,12}", cleaned):
        raise ValueError("GTM- followed by letters/digits (example: GTM-ABC1234)")
    return cleaned


def _slug(value: str) -> str:
    cleaned = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cleaned):
        raise ValueError("letters, digits, - and _ only")
    return cleaned


Validator = Callable[[str], str]
# platform -> key -> validator (returns the normalized value, raises ValueError with the expected format)
ACCOUNT_FIELDS: Mapping[str, Mapping[str, Validator]] = MappingProxyType({
    "ga4": MappingProxyType({"property_id": _ga4}),
    "gsc": MappingProxyType({"site_url": _gsc}),
    "merchant": MappingProxyType({"account_id": _merchant}),
    "gtm": MappingProxyType({"account_id": _digits, "container_id": _digits, "public_id": _gtm_public}),
    "google_ads": MappingProxyType({"customer_id": _gads, "login_customer_id": _gads}),
    "meta": MappingProxyType({"ad_account_id": _meta_account, "pixel_id": _digits, "catalog_id": _digits,
                              "business_id": _digits, "page_id": _digits, "ig_user_id": _digits}),
    "openai_ads": MappingProxyType({"account_id": _slug}),
    "pagespeed": MappingProxyType({}),
})
PLATFORMS = tuple(ACCOUNT_FIELDS)
# Ids that identify a resource OF the client. A Google Ads manager (MCC) or a Meta business can be shared by an
# agency between clients: they are recorded but never prove ownership.
_NOT_OWNING = {("google_ads", "login_customer_id"), ("meta", "business_id")}
# Write families: their accounts come from this account platform.
WRITE_PLATFORMS: Mapping[str, str] = MappingProxyType({
    "google_ads": "google_ads", "meta": "meta", "meta_eco": "meta", "openai_ads": "openai_ads", "ga4": "ga4",
    "gsc": "gsc", "merchant": "merchant", "gtm": "gtm"})
# Primary ids put in account_allow by default when writing is enabled.
_PRIMARY = {"google_ads": ("customer_id",), "meta": ("ad_account_id",), "meta_eco": ("pixel_id", "catalog_id"),
            "openai_ads": ("account_id",), "ga4": ("property_id",), "gsc": ("site_url",),
            "merchant": ("account_id",), "gtm": ("container_id", "public_id")}
# registry "secrets" key -> secrets_store kind
SECRET_KEYS: Mapping[str, str] = MappingProxyType({
    "google_read": "google", "google_write": "google-w", "google_ads": "gads", "meta": "meta",
    "openai_ads": "openai", "meta_capi": "meta-capi", "pagespeed": "pagespeed"})
_KIND_TO_KEY = {kind: key for key, kind in SECRET_KEYS.items()}
# legacy: platform -> key -> environment variable (read at each resolve)
LEGACY_ENV: Mapping[str, Mapping[str, str]] = MappingProxyType({
    "ga4": {"property_id": "GA4_PROPERTY_ID"},
    "gsc": {"site_url": "GSC_SITE_URL"},
    "merchant": {"account_id": "MERCHANT_ACCOUNT_ID"},
    "gtm": {"public_id": "GTM_CONTAINER_ID"},
    "google_ads": {"customer_id": "GADS_CUSTOMER_ID", "login_customer_id": "GADS_LOGIN_CUSTOMER_ID"},
    "meta": {"ad_account_id": "META_AD_ACCOUNT_ID"},
})
_CLIENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_SECRET_SHAPES = re.compile(r"(EAA[A-Za-z0-9]{10,}|sk-[A-Za-z0-9_-]{10,}|ya29\.|1//0|AIza[0-9A-Za-z_-]{20,})")


def validate_client_id(value: str | None) -> str:
    """Lower-case id of 1-40 characters [a-z0-9-], starting with a letter or digit; raises ValueError."""
    cleaned = (value or "").strip().lower()
    if not _CLIENT_ID_RE.match(cleaned):
        raise ValueError("client id: 1-40 characters a-z, 0-9 and -, starting with a letter or a digit "
                         "(example: acme)")
    return cleaned


def validate_account(platform: str, key: str, value: str) -> str:
    """Normalized id; raises ValueError naming the expected format."""
    try:
        validator = ACCOUNT_FIELDS[platform][key]
    except KeyError:
        raise ValueError(f"unknown account field {platform}.{key}") from None
    return validator(str(value))


def mask_id(value: str | None) -> str:
    """Show only the last 3 characters: 1234567890 -> *******890 (sc-domain:acme.fr -> sc-domain:***.fr)."""
    if not value:
        return ""
    text = str(value)
    if text.startswith("sc-domain:"):
        host = text[len("sc-domain:"):]
        tld = host.rsplit(".", 1)[-1] if "." in host else ""
        return "sc-domain:***" + ("." + tld if tld else "")
    if text.startswith(("http://", "https://")):
        scheme, rest = text.split("://", 1)
        host = rest.split("/", 1)[0]
        tld = host.rsplit(".", 1)[-1] if "." in host else ""
        return scheme + "://***" + ("." + tld if tld else "") + "/"
    if len(text) <= 3:
        return "*" * len(text)
    return "*" * (len(text) - 3) + text[-3:]


# ------------------------------------------------------------------ write policy
@dataclass(frozen=True)
class WritePolicy:
    """Per-client, per-platform write switches and caps. Defaults: writing off, nothing allowed."""

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

    def to_dict(self) -> dict:
        return {"enabled": self.enabled, "account_allow": sorted(self.account_allow),
                "max_daily_budget_micros": self.max_daily_budget_micros, "cap_currency": self.cap_currency,
                "max_increase_pct": self.max_increase_pct, "max_ops_per_plan": self.max_ops_per_plan,
                "allow_activate": self.allow_activate, "allow_delete": self.allow_delete,
                "allow_irreversible": self.allow_irreversible, "allow_pii": self.allow_pii,
                "token_ttl_s": self.token_ttl_s}


_BOOL_FIELDS = ("enabled", "allow_activate", "allow_delete", "allow_irreversible", "allow_pii")


def _parse_policy(platform: str, raw: Any, where: str) -> WritePolicy:
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: must be an object")
    unknown = set(raw) - set(WritePolicy().to_dict())
    if unknown:
        raise ValueError(f"{where}: unknown field(s) {', '.join(sorted(unknown))}")
    values: dict[str, Any] = {}
    for name in _BOOL_FIELDS:
        if name in raw:
            if not isinstance(raw[name], bool):
                raise ValueError(f"{where}.{name}: true or false")
            values[name] = raw[name]
    account_platform = WRITE_PLATFORMS[platform]
    allow = raw.get("account_allow", [])
    if not isinstance(allow, list):
        raise ValueError(f"{where}.account_allow: a list of account ids")
    normalized = set()
    for item in allow:
        norm = _normalize_any(account_platform, str(item))
        if norm is None:
            raise ValueError(f"{where}.account_allow: {mask_id(str(item))} is not a valid {account_platform} id")
        normalized.add(norm)
    values["account_allow"] = frozenset(normalized)
    budget = raw.get("max_daily_budget_micros")
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, int) or budget < 0):
        raise ValueError(f"{where}.max_daily_budget_micros: a positive integer or null")
    values["max_daily_budget_micros"] = budget
    currency = raw.get("cap_currency")
    if currency is not None and not (isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency)):
        raise ValueError(f"{where}.cap_currency: a 3-letter ISO code (EUR) or null")
    values["cap_currency"] = currency
    pct = raw.get("max_increase_pct", 20.0)
    if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not 0 <= pct <= 1000:
        raise ValueError(f"{where}.max_increase_pct: a number between 0 and 1000")
    values["max_increase_pct"] = float(pct)
    ops = raw.get("max_ops_per_plan", 25)
    if isinstance(ops, bool) or not isinstance(ops, int) or not 1 <= ops <= 1000:
        raise ValueError(f"{where}.max_ops_per_plan: an integer between 1 and 1000")
    values["max_ops_per_plan"] = ops
    ttl = raw.get("token_ttl_s", 900)
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not 60 <= ttl <= 3600:
        raise ValueError(f"{where}.token_ttl_s: an integer between 60 and 3600 (15 min default, 60 min max)")
    values["token_ttl_s"] = ttl
    return WritePolicy(**values)


# ------------------------------------------------------------------ client context
def _normalize_any(platform: str, resource_id: str) -> str | None:
    """Normalize resource_id with the first validator of the platform that accepts it (None if none does)."""
    for validator in ACCOUNT_FIELDS.get(platform, {}).values():
        try:
            return validator(resource_id)
        except (ValueError, TypeError):
            continue
    return None


def _empty() -> Mapping:
    return MappingProxyType({})


@dataclass(frozen=True)
class ClientContext:
    """Everything a tool may know about ONE client. Immutable; never holds a secret value (secret() reads it on
    demand from the environment / keyring and never logs it)."""

    client_id: str
    label: str
    legacy: bool
    sandbox: bool
    _accounts: Mapping[str, Mapping[str, str]] = field(default_factory=_empty, repr=False, compare=False)
    _secret_names: Mapping[str, str] = field(default_factory=_empty, repr=False, compare=False)
    _policies: Mapping[str, WritePolicy] = field(default_factory=_empty, repr=False, compare=False)

    def _location(self) -> str:
        return "legacy mode (environment variables)" if self.legacy else f"client {self.client_id!r}"

    def account(self, platform: str, key: str) -> str:
        """The configured id, e.g. account("ga4", "property_id"); raises ClientError(platform_not_configured)."""
        if platform not in ACCOUNT_FIELDS or key not in ACCOUNT_FIELDS[platform]:
            raise ClientError("platform_not_configured", f"unknown account field {platform}.{key}.",
                              f"use one of: {', '.join(f'{p}.{k}' for p in ACCOUNT_FIELDS for k in ACCOUNT_FIELDS[p])}")
        value = self._accounts.get(platform, {}).get(key)
        if value:
            return value
        if self.legacy:
            env = LEGACY_ENV.get(platform, {}).get(key)
            fix = (f"set {env} in the MCP server env of your client config" if env
                   else "create a client registry: google-ecommerce-mcp client add <name>")
        else:
            fix = f"google-ecommerce-mcp client add {self.client_id} --replace (asks the missing ids)"
        raise ClientError("platform_not_configured", f"{self._location()}: {platform}.{key} is not set.", fix)

    def accounts(self, platform: str) -> dict:
        """Copy of the ids configured for platform (empty dict when none)."""
        return dict(self._accounts.get(platform, {}))

    def platforms(self) -> list[str]:
        return [p for p in PLATFORMS if self._accounts.get(p)]

    def owns(self, platform: str, resource_id: str) -> bool:
        """True when resource_id is one of this client's own ids for platform (normalized: act_, dashes,
        properties/...). Legacy mode keeps today's behaviour: any well-formed id is accepted."""
        account_platform = WRITE_PLATFORMS.get(platform, platform)
        if account_platform not in ACCOUNT_FIELDS or resource_id is None:
            return False
        wanted = _normalize_any(account_platform, str(resource_id))
        if wanted is None:
            return False
        if self.legacy:
            return True
        configured = self._accounts.get(account_platform, {})
        return any(value == wanted for key, value in configured.items()
                   if (account_platform, key) not in _NOT_OWNING and value)

    def require_owned(self, platform: str, resource_id: str) -> str:
        """resource_id normalized when owned, else ClientError(id_not_owned) (the id is masked in the message)."""
        if not self.owns(platform, resource_id):
            raise ClientError("id_not_owned",
                              f"{self._location()}: {platform} id {mask_id(str(resource_id))} does not belong to "
                              "this client.",
                              f"use one of the client's own ids (google-ecommerce-mcp client show {self.client_id}), "
                              "or pass the client_id that owns it")
        return _normalize_any(WRITE_PLATFORMS.get(platform, platform), str(resource_id)) or str(resource_id)

    def secret(self, kind: str, *, write: bool = False) -> str | None:
        """Secret value of this client (environment, then keyring), or None. write=True on "google" reads the
        separate write token ("google-w"). Never logged."""
        if write and kind == "google":
            kind = "google-w"
        if kind not in secrets_store.KINDS:
            raise ValueError(f"unknown secret kind {kind!r}; expected one of {', '.join(secrets_store.KINDS)}")
        if self.legacy:
            return secrets_store.get_secret(self.client_id, kind, legacy=True)
        return secrets_store.get_secret(self.client_id, kind, name=self._secret_names.get(kind))

    def secret_entry(self, kind: str) -> str | None:
        """Keyring entry name holding this secret (never its value): for messages and the doctor."""
        return secrets_store.keyring_user(self.client_id, kind, legacy=self.legacy,
                                          name=None if self.legacy else self._secret_names.get(kind))

    def policy(self, platform: str) -> WritePolicy:
        """Write policy for a write family (google_ads, meta, meta_eco, openai_ads, ga4, gsc, merchant, gtm).
        Legacy mode and unknown families: everything off."""
        return self._policies.get(platform, WritePolicy())

    def summary(self, *, masked: bool = True) -> dict:
        """Description without any secret; ids masked unless masked=False."""
        accounts = {p: {k: (mask_id(v) if masked else v) for k, v in self._accounts[p].items() if v}
                    for p in PLATFORMS if self._accounts.get(p)}
        return {"client_id": self.client_id, "label": self.label, "legacy": self.legacy, "sandbox": self.sandbox,
                "accounts": accounts,
                "write": {p: pol.to_dict() if not masked else {**pol.to_dict(), "account_allow":
                          [mask_id(a) for a in sorted(pol.account_allow)]}
                          for p, pol in self._policies.items()},
                "secrets": {kind: self.secret_entry(kind) for kind in secrets_store.KINDS}}


def _freeze_accounts(accounts: dict) -> Mapping[str, Mapping[str, str]]:
    return MappingProxyType({p: MappingProxyType(dict(v)) for p, v in accounts.items()})


# ------------------------------------------------------------------ registry
def registry_path() -> Path:
    return paths.clients_file()


def _invalid(path: Path, cause: str) -> ClientError:
    return ClientError("registry_invalid", f"client registry {path}: {cause}.",
                       "fix or delete that entry (google-ecommerce-mcp client remove <name>, then client add <name>); "
                       "the registry holds ids and keyring entry names only, never a secret")


def _validate_label(label: Any, where: str) -> str:
    if label is None:
        return ""
    if not isinstance(label, str) or len(label) > 80 or "\n" in label:
        raise ValueError(f"{where}: text of 80 characters at most")
    if _SECRET_SHAPES.search(label):
        raise ValueError(f"{where}: looks like a secret; secrets never go in the registry")
    return label.strip()


def _validate_client(client_id: str, raw: Any) -> dict:
    where = f"clients.{client_id}"
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: must be an object")
    unknown = set(raw) - {"label", "sandbox", "accounts", "secrets", "write"}
    if unknown:
        raise ValueError(f"{where}: unknown field(s) {', '.join(sorted(unknown))} (secrets are never stored here)")
    out: dict[str, Any] = {"label": _validate_label(raw.get("label"), f"{where}.label")}
    sandbox = raw.get("sandbox", False)
    if not isinstance(sandbox, bool):
        raise ValueError(f"{where}.sandbox: true or false")
    out["sandbox"] = sandbox
    accounts_raw = raw.get("accounts", {})
    if not isinstance(accounts_raw, dict):
        raise ValueError(f"{where}.accounts: must be an object")
    accounts: dict[str, dict[str, str]] = {}
    for platform, values in accounts_raw.items():
        if platform not in ACCOUNT_FIELDS:
            raise ValueError(f"{where}.accounts.{platform}: unknown platform (known: {', '.join(PLATFORMS)})")
        if not isinstance(values, dict):
            raise ValueError(f"{where}.accounts.{platform}: must be an object")
        clean: dict[str, str] = {}
        for key, value in values.items():
            if key not in ACCOUNT_FIELDS[platform]:
                known = ", ".join(ACCOUNT_FIELDS[platform]) or "none"
                raise ValueError(f"{where}.accounts.{platform}.{key}: unknown field (known: {known})")
            if value in (None, ""):
                continue
            if not isinstance(value, str):
                raise ValueError(f"{where}.accounts.{platform}.{key}: must be a string")
            try:
                clean[key] = validate_account(platform, key, value)
            except ValueError as exc:
                raise ValueError(f"{where}.accounts.{platform}.{key}: {exc}") from None
        accounts[platform] = clean
    out["accounts"] = accounts
    secrets_raw = raw.get("secrets", {})
    if not isinstance(secrets_raw, dict):
        raise ValueError(f"{where}.secrets: must be an object of keyring entry names")
    secrets: dict[str, str] = {}
    for key, name in secrets_raw.items():
        if key not in SECRET_KEYS:
            raise ValueError(f"{where}.secrets.{key}: unknown (known: {', '.join(SECRET_KEYS)})")
        if not secrets_store.valid_secret_name(client_id, name):
            raise ValueError(f"{where}.secrets.{key}: must be a keyring entry name of this client "
                             f"(\"{client_id}/<kind>\"), never a secret value nor another client's entry")
        secrets[key] = name
    out["secrets"] = secrets
    write_raw = raw.get("write", {})
    if not isinstance(write_raw, dict):
        raise ValueError(f"{where}.write: must be an object")
    write: dict[str, dict] = {}
    for platform, policy in write_raw.items():
        if platform not in WRITE_PLATFORMS:
            raise ValueError(f"{where}.write.{platform}: unknown write family (known: {', '.join(WRITE_PLATFORMS)})")
        write[platform] = _parse_policy(platform, policy, f"{where}.write.{platform}").to_dict()
    out["write"] = write
    return out


def _validate_registry(data: Any) -> dict:
    if not isinstance(data, dict):
        raise ValueError("the top level must be an object")
    unknown = set(data) - {"version", "clients"}
    if unknown:
        raise ValueError(f"unknown top-level field(s) {', '.join(sorted(unknown))}")
    if data.get("version") != REGISTRY_VERSION:
        raise ValueError(f"version must be {REGISTRY_VERSION}")
    clients_raw = data.get("clients", {})
    if not isinstance(clients_raw, dict):
        raise ValueError("clients must be an object")
    clients = {}
    for client_id, raw in clients_raw.items():
        try:
            if validate_client_id(client_id) != client_id:
                raise ValueError("not lower case")
        except ValueError:
            raise ValueError(f"clients.{client_id}: invalid client id (a-z, 0-9 and -, lower case)") from None
        clients[client_id] = _validate_client(client_id, raw)
    return {"version": REGISTRY_VERSION, "clients": clients}


def load_registry(path: Path | None = None) -> dict | None:
    """Validated registry, or None when the file does not exist (legacy mode). Fails closed: a broken registry
    raises ClientError(registry_invalid), it never falls back to the environment."""
    path = Path(path) if path else registry_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _invalid(path, f"unreadable ({type(exc).__name__})") from None
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise _invalid(path, f"not valid JSON (line {getattr(exc, 'lineno', '?')})") from None
    try:
        return _validate_registry(data)
    except ValueError as exc:
        raise _invalid(path, str(exc)) from None


def save_registry(data: dict, path: Path | None = None) -> bool:
    """Validate then write atomically, privately. Returns False when the Windows ACL could not be restricted."""
    path = Path(path) if path else registry_path()
    try:
        clean = _validate_registry(data)
    except ValueError as exc:
        raise _invalid(path, str(exc)) from None
    return paths.write_private_atomic(path, json.dumps(clean, indent=2, ensure_ascii=False) + "\n")


def _empty_registry() -> dict:
    return {"version": REGISTRY_VERSION, "clients": {}}


def add_client(client_id: str, *, label: str = "", accounts: dict | None = None, sandbox: bool = False,
               secrets: dict | None = None, replace: bool = False, path: Path | None = None) -> bool:
    """Create (or, with replace=True, update) a client; its write policies are kept on replace."""
    client_id = validate_client_id(client_id)
    data = load_registry(path) or _empty_registry()
    existing = data["clients"].get(client_id)
    if existing and not replace:
        raise ClientError("registry_invalid", f"client {client_id!r} already exists in {path or registry_path()}.",
                          f"google-ecommerce-mcp client add {client_id} --replace, or choose another name")
    entry = {"label": label, "sandbox": sandbox, "accounts": accounts or {},
             "secrets": secrets if secrets is not None else (existing or {}).get("secrets", {}),
             "write": (existing or {}).get("write", {})}
    data["clients"][client_id] = entry
    return save_registry(data, path)


def remove_client(client_id: str, path: Path | None = None) -> dict:
    """Remove a client from the registry; returns its former entry (no secret in it)."""
    data = load_registry(path)
    if not data or client_id not in data["clients"]:
        raise _unknown(client_id, sorted((data or {}).get("clients", {})))
    entry = data["clients"].pop(client_id)
    save_registry(data, path)
    return entry


def default_account_allow(client_id: str, platform: str, path: Path | None = None) -> list[str]:
    """The client's primary ids for a write family (what enable-write puts in account_allow)."""
    data = load_registry(path)
    if not data or client_id not in data["clients"]:
        raise _unknown(client_id, sorted((data or {}).get("clients", {})))
    accounts = data["clients"][client_id]["accounts"].get(WRITE_PLATFORMS[platform], {})
    return [accounts[key] for key in _PRIMARY[platform] if accounts.get(key)]


def set_write(client_id: str, platform: str, enabled: bool, *, account_allow: list[str] | None = None,
              path: Path | None = None) -> WritePolicy:
    """Switch write.<platform>.enabled. Enabling needs at least one own id: account_allow defaults to the client's
    primary ids and may only contain ids the client owns."""
    if platform not in WRITE_PLATFORMS:
        raise ClientError("platform_not_configured", f"unknown write family {platform!r}.",
                          f"use one of: {', '.join(WRITE_PLATFORMS)}")
    data = load_registry(path)
    if not data or client_id not in data["clients"]:
        raise _unknown(client_id, sorted((data or {}).get("clients", {})))
    entry = data["clients"][client_id]
    current = dict(entry["write"].get(platform, {}))
    if enabled:
        allow = account_allow if account_allow is not None else default_account_allow(client_id, platform, path)
        if not allow:
            account_platform = WRITE_PLATFORMS[platform]
            raise ClientError("platform_not_configured",
                              f"client {client_id!r}: no {account_platform} id configured, so no account to allow.",
                              f"google-ecommerce-mcp client add {client_id} --replace, and give the "
                              f"{account_platform} id first")
        ctx = _context_from_entry(client_id, entry)
        for item in allow:
            ctx.require_owned(platform, item)
        current["account_allow"] = sorted({_normalize_any(WRITE_PLATFORMS[platform], a) or a for a in allow})
    current["enabled"] = bool(enabled)
    entry["write"][platform] = current
    save_registry(data, path)
    return _parse_policy(platform, current, f"clients.{client_id}.write.{platform}")


# ------------------------------------------------------------------ resolution
def legacy_accounts_from_env() -> dict[str, dict[str, str]]:
    """Today's ids from the environment, normalized the same way the tools do (dashes, act_). Values that the
    strict validators would refuse are kept as given: legacy behaviour is unchanged."""
    out: dict[str, dict[str, str]] = {}
    for platform, keys in LEGACY_ENV.items():
        for key, env in keys.items():
            value = (os.environ.get(env) or "").strip()
            if not value:
                continue
            if platform == "google_ads":
                value = value.replace("-", "").replace(" ", "")
            elif platform == "meta" and value.lower().startswith("act_"):
                value = value[4:]
            if value:
                out.setdefault(platform, {})[key] = value
    return out


def _legacy_context() -> ClientContext:
    return ClientContext(client_id=LEGACY_CLIENT_ID, label="", legacy=True, sandbox=False,
                         _accounts=_freeze_accounts(legacy_accounts_from_env()))


def _context_from_entry(client_id: str, entry: dict) -> ClientContext:
    names = {SECRET_KEYS[key]: name for key, name in entry.get("secrets", {}).items()}
    policies = {p: _parse_policy(p, raw, f"clients.{client_id}.write.{p}") for p, raw in entry.get("write", {}).items()}
    return ClientContext(client_id=client_id, label=entry.get("label", ""), legacy=False,
                         sandbox=bool(entry.get("sandbox", False)), _accounts=_freeze_accounts(entry.get("accounts", {})),
                         _secret_names=MappingProxyType(names), _policies=MappingProxyType(policies))


def _unknown(client_id: str, known: list[str]) -> ClientError:
    listed = ", ".join(known) if known else "none"
    return ClientError("unknown_client", f"client {client_id!r} is not in the registry {registry_path()} "
                       f"(known: {listed}).",
                       f"google-ecommerce-mcp client list, or create it: google-ecommerce-mcp client add {client_id}")


def _pinned() -> str | None:
    value = (os.environ.get(PIN_ENV) or "").strip().lower()
    return value or None


def resolve(client_id: str | None) -> ClientContext:
    """The ClientContext of client_id (None = the implicit default). Reads the registry at each call.

    Raises ClientError: pinned_client (GEM_CLIENT set to another client), unknown_client, client_required (two
    clients or more and no client_id), registry_invalid (broken registry; never falls back to the environment)."""
    requested = (client_id or "").strip().lower() or None
    pinned = _pinned()
    if pinned and requested and requested != pinned:
        raise ClientError("pinned_client", f"this server process is pinned to client {pinned!r} ({PIN_ENV}); "
                          f"client {requested!r} is refused.",
                          f"omit client_id, or run another server entry with {PIN_ENV}={requested}")
    wanted = requested or pinned
    data = load_registry()
    if data is None:
        if wanted not in (None, LEGACY_CLIENT_ID):
            raise ClientError("unknown_client", f"no client registry ({registry_path()}): only the implicit client "
                              f"{LEGACY_CLIENT_ID!r} exists (environment variables); {wanted!r} is unknown.",
                              f"omit client_id, or create the client: google-ecommerce-mcp client add {wanted}")
        return _legacy_context()
    clients = data["clients"]
    if wanted is None:
        if len(clients) == 1:
            wanted = next(iter(clients))
        elif not clients:
            raise ClientError("unknown_client", f"the client registry {registry_path()} has no client.",
                              "google-ecommerce-mcp client add <name>, or delete the empty file to go back to "
                              "the environment variables")
        else:
            raise ClientError("client_required", f"{len(clients)} clients are registered "
                              f"({', '.join(sorted(clients))}) and no client_id was given.",
                              "pass client_id (see clients_list), or pin the server with "
                              f"{PIN_ENV}=<client_id>")
    if wanted not in clients:
        raise _unknown(wanted, sorted(clients))
    return _context_from_entry(wanted, clients[wanted])


def list_clients(*, include_all: bool = False) -> list[dict]:
    """One row per client: id, label, sandbox, configured platforms, write families enabled. No id, no secret.

    A process pinned with GEM_CLIENT only sees its own client (empty list when it is not registered);
    include_all=True (the administration CLI) lists every client and flags the pinned one."""
    data = load_registry()
    pinned = _pinned()
    if data is None:
        if pinned and pinned != LEGACY_CLIENT_ID and not include_all:
            return []
        ctx = _legacy_context()
        return [{"client_id": ctx.client_id, "label": "", "legacy": True, "sandbox": False,
                 "platforms": ctx.platforms(), "write_enabled": [], "pinned": pinned == LEGACY_CLIENT_ID}]
    rows = []
    for client_id in sorted(data["clients"]):
        if pinned and client_id != pinned and not include_all:
            continue
        ctx = _context_from_entry(client_id, data["clients"][client_id])
        rows.append({"client_id": client_id, "label": ctx.label, "legacy": False, "sandbox": ctx.sandbox,
                     "platforms": ctx.platforms(),
                     "write_enabled": sorted(p for p, pol in ctx._policies.items() if pol.enabled),
                     "pinned": pinned == client_id})
    return rows

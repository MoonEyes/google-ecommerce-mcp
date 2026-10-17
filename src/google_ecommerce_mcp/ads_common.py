"""Shared plumbing for the read-only ad platform connectors (Google Ads, Meta, OpenAI Ads).

This is the only ads module that sends HTTP requests. Every request goes through AdsClient, which checks it
against the platform's AllowList before it leaves the process, refuses any secret in the URL, masks secrets in
every error it returns, and maps platform errors to a small set of codes the assistant can act on:

- not_configured: a required variable is missing (raised as NotConfigured, turned into data by guard());
- auth_failed, missing_scope, rate_limited (retry_after_seconds, usage), not_available, too_many_rows (HTTP
  413), api_error: mapped from the platform answer by map_error();
- blocked: refused before sending (outside the allow-list, secret or `method` in the URL) or a 3xx answer:
  redirects are never followed, so headers never reach another host ("redirect refused");
- response_too_large: the body exceeds max_bytes (MAX_RESPONSE_BYTES, 20 MB); reading stops at the ceiling;
- network: transport failure; the fix mentions ADS_TRUST_ENV=1 for corporate proxies. A local OSError (CA
  bundle path missing under ADS_TRUST_ENV=1, I/O) is also "network", with a safe detail and a CA bundle fix.

The environment (proxy variables, REQUESTS_CA_BUNDLE, .netrc) is ignored unless ADS_TRUST_ENV=1, which enables
the proxy and CA bundle only (never .netrc, never redirects)."""

from __future__ import annotations

import copy
import datetime
import hashlib
import json
import json as _json  # call() has a `json` parameter that shadows the module
import logging
import os
import re
import time
from typing import Any, Callable, Iterable, Sequence, Tuple, Union
from urllib.parse import parse_qsl, urlsplit

import requests
from mcp.types import ToolAnnotations

from . import metrics
from .auth import NotAuthenticated, TokenNetworkError
from .config import NotConfigured

log = logging.getLogger(__name__)

TIMEOUT = 60
MAX_ROWS = 1000
CACHE_TTL_SECONDS = 300
MAX_RESPONSE_BYTES = 20 * 1024 * 1024  # 20 MB: a bigger answer is refused, not loaded into memory
RESPONSE_TOO_LARGE_FIX = "réduire la période ou ajouter LIMIT"
_CHUNK = 64 * 1024
TRUST_ENV_VARIABLE = "ADS_TRUST_ENV"
NETWORK_FIX = ("check the connection and retry; derrière un proxy d'entreprise, définir ADS_TRUST_ENV=1 "
               "(uses HTTPS_PROXY / NO_PROXY and REQUESTS_CA_BUNDLE)")
LOCAL_TRANSPORT_FIX = ("vérifier REQUESTS_CA_BUNDLE / ADS_TRUST_ENV : with ADS_TRUST_ENV=1 the CA bundle path must "
                       "exist; unset ADS_TRUST_ENV to use the default certificates")
REDACTED = "[REDACTED]"

# Every ads tool only reads: declared to MCP clients through the standard annotations (same as server.READ_ONLY).
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

Validator = Callable[[Any], bool]
Rule = Union[Tuple[str, str], Tuple[str, str, Union[Validator, None]]]
Secrets = Union[Iterable[Union[str, None]], Callable[[], Iterable[Union[str, None]]]]


# ------------------------------------------------------------------ redaction
_SECRET_PATTERNS = [
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer " + REDACTED),
    (re.compile(r"(?i)\b(access_token|refresh_token|client_secret|api_key|developer[-_]token)"
                r"([\"']?\s*[:=]\s*[\"']?)[^&\s\"',}]+"), r"\1\2" + REDACTED),
    (re.compile(r"\bEAA[A-Za-z0-9]{10,}"), REDACTED),           # Meta access tokens
    (re.compile(r"\bya29\.[A-Za-z0-9._-]+"), REDACTED),         # Google access tokens
    (re.compile(r"\b1//[A-Za-z0-9._-]{10,}"), REDACTED),        # Google refresh tokens
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), REDACTED),         # OpenAI keys (sk-..., sk-proj-...)
]
_SECRET_KEYS = {"access_token", "refresh_token", "client_secret", "api_key", "token", "authorization",
                "developer-token", "developer_token", "password", "secret"}


def _secret_values(secrets: Secrets | None) -> list[str]:
    if secrets is None:
        return []
    values = secrets() if callable(secrets) else secrets
    # Very short values would mask ordinary words; real tokens are long.
    return sorted({s for s in values if isinstance(s, str) and len(s) >= 6}, key=len, reverse=True)


def _redact_text(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    for rx, replacement in _SECRET_PATTERNS:
        text = rx.sub(replacement, text)
    return text


def redact(value: Any, secrets: Secrets | None = ()) -> Any:
    """Mask secrets in a string, or in every string of a dict/list (returns a new structure).

    Masks the exact values in `secrets` (an iterable or a callable returning one; None items ignored) and the
    token shapes `Bearer ...`, `access_token=...`, `EAA...`, `ya29....`, `1//...`, `sk-...`."""
    known = _secret_values(secrets)

    def walk(item: Any) -> Any:
        if isinstance(item, str):
            return _redact_text(item, known)
        if isinstance(item, dict):
            return {k: REDACTED if isinstance(k, str) and k.lower() in _SECRET_KEYS and item[k] else walk(v)
                    for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [walk(v) for v in item]
        return item

    return walk(value)


# ------------------------------------------------------------------ transport
def trust_env_enabled() -> bool:
    """True when ADS_TRUST_ENV=1: the explicit opt-in to the environment's proxy and CA bundle."""
    return os.getenv(TRUST_ENV_VARIABLE, "").strip() == "1"


class NoRedirectSession(requests.Session):
    """requests.Session that never follows a redirect (allow_redirects forced False, max_redirects 0) and
    ignores the environment (trust_env False: no proxy variables, no .netrc, no CA override)."""

    def __init__(self) -> None:
        super().__init__()
        self.trust_env = False
        self.max_redirects = 0

    def request(self, method, url, *args, **kwargs):  # type: ignore[override]
        kwargs["allow_redirects"] = False
        return super().request(method, url, *args, **kwargs)


def env_transport(url: str) -> dict:
    """Proxy and CA settings taken from the environment for url (honours NO_PROXY), for the ADS_TRUST_ENV=1
    opt-in: {"proxies": {...}, "verify": bundle path or True}. Never .netrc credentials."""
    proxies = requests.utils.get_environ_proxies(url)
    bundle = (os.getenv("REQUESTS_CA_BUNDLE") or os.getenv("CURL_CA_BUNDLE") or "").strip()
    return {"proxies": dict(proxies), "verify": bundle or True}


def hardened_session(url_for_env: str | None = None, trust_env: bool | None = None) -> NoRedirectSession:
    """A NoRedirectSession; with trust_env (default: ADS_TRUST_ENV=1) its proxies/verify come from
    env_transport(url_for_env). Used for the OAuth refresh, whose requests google-auth sends itself."""
    session = NoRedirectSession()
    if (trust_env_enabled() if trust_env is None else trust_env) and url_for_env:
        settings = env_transport(url_for_env)
        session.proxies.update(settings["proxies"])
        session.verify = settings["verify"]
    return session


# ------------------------------------------------------------------ allow-list
_HOST = re.compile(r"[a-z0-9.-]+")
FORBIDDEN_QUERY_KEYS = frozenset({"method", "access_token"})
_PRINTABLE = re.compile(r"[\x21-\x7e]+")


class AllowList:
    """Every request a platform connector may send: (method, URL regex, body validator or None).

    Patterns are full URLs anchored at both ends, must start with https:// and a literal host (pinned host), and
    are matched against scheme://host/path only (query string ignored; secrets in it are refused by AdsClient).
    A rule with a validator also requires validator(body) to return True (e.g. a GAQL guard)."""

    def __init__(self, rules: Sequence[Rule]) -> None:
        self._rules: list[tuple[str, str, re.Pattern[str], Validator | None]] = []
        for rule in rules:
            method, pattern = rule[0].upper(), rule[1]
            validator = rule[2] if len(rule) > 2 else None  # type: ignore[misc]
            if not pattern.startswith("https://"):
                raise ValueError(f"allow-list pattern must start with https://: {pattern}")
            host = pattern[len("https://"):].split("/", 1)[0].replace(r"\.", ".")
            if not _HOST.fullmatch(host):
                raise ValueError(f"allow-list pattern must pin a literal host: {pattern}")
            self._rules.append((method, pattern, re.compile(pattern), validator))

    @property
    def patterns(self) -> list[tuple[str, str]]:
        """[(method, pattern)] for review and tests."""
        return [(m, p) for m, p, _, _ in self._rules]

    @property
    def hosts(self) -> set[str]:
        """The pinned hosts, unescaped."""
        return {p[len("https://"):].split("/", 1)[0].replace(r"\.", ".") for _, p, _, _ in self._rules}

    def is_allowed(self, method: str, url: str, body: Any = None) -> bool:
        """True only if method + scheme://host/path fully match a rule and its validator (if any) accepts body.

        Refused up front: surrounding whitespace, any character outside printable ASCII (\\x21-\\x7e), ';' (also
        as a query separator) or a backslash, and anything not starting literally with lowercase 'https://'."""
        if (not isinstance(url, str) or url != url.strip() or not _PRINTABLE.fullmatch(url) or ";" in url
                or "\\" in url or not url.startswith("https://")):
            return False
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        if parts.scheme != "https" or "\\" in url or parts.username or parts.password:
            return False
        path = parts.path
        if "/../" in path + "/" or "/./" in path + "/" or "%2f" in path.lower() or "%2e" in path.lower():
            return False
        # Graph API honours ?method=delete|post on a GET; a token never travels in the URL (double guard).
        if {k.strip().lower() for k, _ in parse_qsl(parts.query, keep_blank_values=True)} & FORBIDDEN_QUERY_KEYS:
            return False
        bare = f"{parts.scheme}://{parts.netloc}{path}"
        for m, _, rx, validator in self._rules:
            if method.upper() != m or not rx.fullmatch(bare):
                continue
            if validator is None:
                return True
            try:
                if validator(body):
                    return True
            except Exception:  # a validator that crashes refuses
                continue
        return False


class Blocked(Exception):
    """Raised when code tries to send a request outside the allow-list (or with a secret in the URL)."""


# ------------------------------------------------------------------ errors
_FIXES = {
    "google_ads": {
        "auth_failed": "rerun `google-ecommerce-mcp setup-ads --client-secret <file>`; if the token is valid, check "
                       "that the Google account can read this customer (and GADS_LOGIN_CUSTOMER_ID for a manager)",
        "missing_scope": "rerun `google-ecommerce-mcp setup-ads --client-secret <file>` and grant the Google Ads scope",
        "not_available": "enable the Google Ads API in Google Cloud Console for the project of your OAuth client, "
                         "and check the customer id and GADS_API_VERSION",
    },
    "meta": {
        "auth_failed": "create a new access token (System User token with ads_read) and set META_ACCESS_TOKEN, or "
                       "store it with `google-ecommerce-mcp set-secret meta-access-token`",
        "missing_scope": "grant the ads_read permission to the token and give it access to the ad account",
        "not_available": "check META_AD_ACCOUNT_ID and META_API_VERSION, and that the token can see this account",
    },
    "openai_ads": {
        "auth_failed": "set a valid OPENAI_ADS_API_KEY, or store it with "
                       "`google-ecommerce-mcp set-secret openai-ads-key`",
        "missing_scope": "use an API key with read access to the ad account",
        "not_available": "the OpenAI Ads API is not available for this account or endpoint",
    },
}
_DEFAULT_FIXES = {
    "auth_failed": "check the credentials configured for this platform",
    "missing_scope": "grant the missing permission to the credentials",
    "not_available": "check the account id and the API version",
}
RATE_FIX = "wait retry_after_seconds (if given) and retry, or narrow the request (shorter period, smaller limit)"
TOO_MANY_ROWS_FIX = "réduire la période ou ajouter des filtres"
META_RATE_CODES = {4, 17, 32, 613}
META_AUTH_CODES = {102, 190, 463, 467}
ADS_SCOPE_URL = "https://www.googleapis.com/auth/adwords"


def _header(headers: Any, name: str) -> Any:
    if not headers:
        return None
    try:
        for key, value in headers.items():
            if str(key).lower() == name.lower():
                return value
    except AttributeError:
        return None
    return None


def _parse_usage(raw: Any) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return redact(str(raw)[:500])


def _retry_after(headers: Any, usage: dict) -> int | str | None:
    raw = _header(headers, "Retry-After")
    if raw is not None:
        text = str(raw).strip()
        return int(text) if text.isdigit() else text
    # No Retry-After: the longest of the Meta usage delays, so a retry never comes before every limit resets.
    delays: list[int] = []
    account = usage.get("ad_account")
    reset = account.get("reset_time_duration") if isinstance(account, dict) else None
    if isinstance(reset, (int, float)) and not isinstance(reset, bool) and reset > 0:
        delays.append(int(reset))
    business = usage.get("business_use_case")
    if isinstance(business, dict):
        minutes = [entry.get("estimated_time_to_regain_access") for entries in business.values()
                   if isinstance(entries, list) for entry in entries if isinstance(entry, dict)]
        delays.extend(int(m * 60) for m in minutes
                      if isinstance(m, (int, float)) and not isinstance(m, bool) and m > 0)
    return max(delays) if delays else None


def _error_object(data: Any) -> Any:
    if isinstance(data, list):  # Google Ads searchStream answers errors as a list of batches
        for item in data:
            if isinstance(item, dict) and "error" in item:
                return item["error"]
        return data[:3]
    if isinstance(data, dict):
        return data.get("error", data)
    return data


def map_error(platform: str, response: Any, data: Any) -> dict:
    """Turn a failed platform response into {error, detail, fix, platform, http_status, ...}.

    error is one of auth_failed, missing_scope, rate_limited (with retry_after_seconds and usage parsed from
    X-Business-Use-Case-Usage / X-Ad-Account-Usage), not_available, too_many_rows, api_error. Secret shapes in
    detail are masked; AdsClient.call also masks the exact configured secrets."""
    status = getattr(response, "status_code", None)
    headers = getattr(response, "headers", None) or {}
    detail = redact(_error_object(data))
    if isinstance(detail, str):
        detail = detail[:1000]
    text = json.dumps(detail, default=str) if not isinstance(detail, str) else detail
    fixes = _FIXES.get(platform, _DEFAULT_FIXES)
    meta_code = detail.get("code") if isinstance(detail, dict) and platform == "meta" else None
    meta_sub = detail.get("error_subcode") if isinstance(detail, dict) and platform == "meta" else None
    base = {"platform": platform, "http_status": status, "detail": detail}

    def out(code: str, fix: str, **extra: Any) -> dict:
        return {"error": code, **base, "fix": fix, **extra}

    usage = {k: v for k, v in (("business_use_case", _parse_usage(_header(headers, "X-Business-Use-Case-Usage"))),
                               ("ad_account", _parse_usage(_header(headers, "X-Ad-Account-Usage"))),
                               ("app", _parse_usage(_header(headers, "X-App-Usage")))) if v is not None}
    is_meta_rate = isinstance(meta_code, int) and (meta_code in META_RATE_CODES or 80000 <= meta_code <= 80014)
    if status == 429 or is_meta_rate or "RESOURCE_EXHAUSTED" in text:
        return out("rate_limited", RATE_FIX, retry_after_seconds=_retry_after(headers, usage), usage=usage)
    if status == 413:
        return out("too_many_rows", TOO_MANY_ROWS_FIX)
    if status == 401 or meta_code in META_AUTH_CODES or "UNAUTHENTICATED" in text:
        return out("auth_failed", fixes["auth_failed"])
    lowered = text.lower()
    if ("SCOPE_INSUFFICIENT" in text or "insufficient authentication scopes" in lowered
            or (platform == "meta" and isinstance(meta_code, int) and (meta_code == 10 or 200 <= meta_code <= 299))
            or (status == 403 and "scope" in lowered)):
        extra: dict[str, Any] = {}
        if platform == "google_ads":
            extra = {"required_scope": ADS_SCOPE_URL, "missing_scopes": [ADS_SCOPE_URL]}
        elif platform == "meta":
            extra = {"required_permission": "ads_read"}
        return out("missing_scope", fixes["missing_scope"], **extra)
    if ("SERVICE_DISABLED" in text or "has not been used in project" in text or status in (404, 501)
            or "UNIMPLEMENTED" in text or meta_sub == 33):
        return out("not_available", fixes["not_available"])
    if status == 403:
        return out("auth_failed", fixes["auth_failed"])
    if isinstance(status, int) and status >= 500:
        return out("api_error", "the platform failed on its side; retry in a few minutes")
    return out("api_error", "read detail: the request was refused by the platform")


# ------------------------------------------------------------------ client
class _TTLCache:
    """Tiny in-memory cache for discovery calls (account lists), never for reports."""

    def __init__(self, ttl: float = CACHE_TTL_SECONDS, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl = ttl
        self.clock = clock
        self._items: dict[str, tuple[float, dict]] = {}

    def get(self, key: str) -> dict | None:
        hit = self._items.get(key)
        if hit is None:
            return None
        if self.clock() - hit[0] > self.ttl:
            del self._items[key]
            return None
        return copy.deepcopy(hit[1])

    def set(self, key: str, value: dict) -> None:
        self._items[key] = (self.clock(), copy.deepcopy(value))

    def clear(self) -> None:
        self._items.clear()


_SENSITIVE_HEADERS = ("authorization", "developer-token", "x-api-key", "api-key")
Headers = Union[dict, Callable[[], dict], None]


class AdsClient:
    """HTTP client of one ad platform. The only object in the ads connectors that touches the network.

    platform: "google_ads", "meta" or "openai_ads" (selects error fixes). allow_list: the platform AllowList.
    secrets: iterable or zero-argument callable returning the secret values to mask (read at each call, so
    settings changed in the environment are honoured). `session` (requests.Session) is replaceable in tests;
    `BLOCKED_ATTEMPTS` records every refused request of this client; `cache` holds discovery results 300 s.

    Transport hardening: redirects are never followed (allow_redirects=False; a 3xx is returned as a
    "blocked" error, so headers such as developer-token or login-customer-id never reach another host), the
    session ignores the environment (session.trust_env is always False: no proxy variables, no .netrc
    credentials, no REQUESTS_CA_BUNDLE override), and bodies are streamed with a byte ceiling (max_bytes,
    default MAX_RESPONSE_BYTES) so an oversized answer is refused without being loaded into memory.

    trust_env=True, or ADS_TRUST_ENV=1 in the environment (read at each request), is the opt-in for a corporate
    proxy: each request then gets the proxies (honouring NO_PROXY) and the REQUESTS_CA_BUNDLE of the
    environment, passed explicitly; .netrc stays ignored and redirects stay refused."""

    def __init__(self, platform: str, allow_list: AllowList, secrets: Secrets | None = None,
                 timeout: float = TIMEOUT, max_bytes: int = MAX_RESPONSE_BYTES, trust_env: bool = False) -> None:
        self.platform = platform
        self.allow_list = allow_list
        self.secrets = secrets
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.trust_env = trust_env
        self.session: Any = NoRedirectSession()
        self.BLOCKED_ATTEMPTS: list[tuple[str, str]] = []
        self.cache = _TTLCache()

    def is_allowed(self, method: str, url: str, body: Any = None) -> bool:
        """Shortcut to allow_list.is_allowed."""
        return self.allow_list.is_allowed(method, url, body)

    def _secret_list(self, headers: dict | None = None) -> list[str]:
        values = _secret_values(self.secrets)
        for key, value in (headers or {}).items():
            if str(key).lower() in _SENSITIVE_HEADERS and isinstance(value, str):
                values.append(value.split(" ", 1)[1] if value.lower().startswith("bearer ") else value)
        return sorted({v for v in values if len(v) >= 6}, key=len, reverse=True)

    def send(self, method: str, url: str, *, headers: dict | None = None, params: dict | None = None,
             json: Any = None, timeout: float | None = None) -> Any:
        """Send one request if it is on the allow-list, else record it in BLOCKED_ATTEMPTS and raise Blocked.
        Also raises Blocked when a secret, an access_token or a method parameter would travel in the URL.
        Never follows redirects and streams the body (stream=True): returns the raw, unread response."""
        method = method.upper()
        secrets = self._secret_list(headers)
        query_keys = ({k.strip().lower() for k, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True)}
                      | {str(k).strip().lower() for k in (params or {})})
        in_url = url + " " + str(params or "")
        if "method" in query_keys:
            self.BLOCKED_ATTEMPTS.append((method, _redact_text(url, secrets)))
            raise Blocked(f"{method} {_redact_text(url, secrets)}: a 'method' parameter could turn a read into a "
                          "write; request not sent")
        if "access_token" in query_keys or any(s in in_url for s in secrets):
            self.BLOCKED_ATTEMPTS.append((method, _redact_text(url, secrets)))
            raise Blocked(f"{method} {_redact_text(url, secrets)}: a secret may never travel in the URL; "
                          "request not sent")
        if not self.allow_list.is_allowed(method, url, json):
            self.BLOCKED_ATTEMPTS.append((method, _redact_text(url, secrets)))
            log.warning("blocked %s %s (not on the %s read-only allow-list)", method,
                        _redact_text(url, secrets), self.platform)
            raise Blocked(f"{method} {_redact_text(url, secrets)} is not on the {self.platform} read-only "
                          "allow-list; request not sent")
        kwargs: dict[str, Any] = {"headers": headers or {}, "timeout": timeout or self.timeout,
                                  "allow_redirects": False, "stream": True}
        if self.trust_env or trust_env_enabled():
            kwargs.update(env_transport(url))
        if params is not None:
            kwargs["params"] = params
        if json is not None:
            kwargs["json"] = json
        return self.session.request(method, url, **kwargs)

    def _redirect_refused(self, method: str, response: Any, secrets: list[str]) -> dict:
        """A 3xx answer (or TooManyRedirects): recorded in BLOCKED_ATTEMPTS and returned as "blocked"."""
        status = getattr(response, "status_code", None) if response is not None else None
        if response is not None:
            _close(response)
        location = urlsplit(str(_header(getattr(response, "headers", None), "Location") or ""))
        target = f"{location.scheme}://{location.netloc}" if location.netloc else "an unknown location"
        self.BLOCKED_ATTEMPTS.append((method.upper(), f"redirect {status} to {target}"))
        return redact({"error": "blocked", "platform": self.platform, "http_status": status,
                       "detail": f"redirect refused: {status} to {target}; redirects are never followed",
                       "fix": "check the API version and the account id in the configuration"}, secrets)

    def _network_error(self, exc: Exception, secrets: list[str]) -> dict:
        return {"error": "network", "platform": self.platform,
                "detail": redact(f"{type(exc).__name__}: {exc}", secrets), "fix": NETWORK_FIX}

    def _local_error(self, exc: OSError) -> dict:
        """OSError raised before or while reading (TLS CA bundle not found, local I/O): safe detail only."""
        return {"error": "network", "platform": self.platform,
                "detail": f"{type(exc).__name__}: local TLS or I/O configuration error (message withheld)",
                "fix": LOCAL_TRANSPORT_FIX}

    def call(self, method: str, url: str, *, headers: Headers = None, params: dict | None = None,
             json: Any = None, timeout: float | None = None, cache: bool = False,
             max_bytes: int | None = None) -> dict:
        """Send and decode; never raises for HTTP, network, local TLS/OSError, redirect or allow-list problems,
        returns a dict instead.

        headers may be a dict or a zero-argument callable (e.g. a token provider) evaluated here: an
        auth.NotAuthenticated becomes {"error": "auth_failed"}; NotConfigured propagates to guard().
        Success: the JSON object, or {"items": [...]} when the body is a JSON array (Google Ads searchStream).
        Errors: {"error": "blocked"|"network"|map_error codes, ...}, secrets masked. cache=True (discovery calls
        only) reuses a successful result for 300 s and marks it "cached": True. The cache key is a SHA-256 of
        the request and of the resolved headers, so a new token or login-customer-id never gets an old answer
        and no header value is kept in clear. A 3xx answer is {"error": "blocked", "detail": "redirect refused
        ..."}; a body above max_bytes (default self.max_bytes, 20 MB) is {"error": "response_too_large"}."""
        try:
            resolved = headers() if callable(headers) else (headers or {})
        except TokenNetworkError as exc:  # token refresh failed on the network: not an authentication problem
            return redact({"error": "network", "platform": self.platform, "detail": str(exc),
                           "fix": LOCAL_TRANSPORT_FIX if exc.local else NETWORK_FIX}, self._secret_list())
        except NotAuthenticated as exc:
            return redact({"error": "auth_failed", "platform": self.platform, "detail": str(exc),
                           "fix": _FIXES.get(self.platform, _DEFAULT_FIXES)["auth_failed"]}, self._secret_list())
        key = ""
        if cache:
            key = _cache_key(self.platform, method, url, params, json, resolved)
            hit = self.cache.get(key)
            if hit is not None:
                hit["cached"] = True
                return hit
        secrets = self._secret_list(resolved)
        try:
            response = self.send(method, url, headers=resolved, params=params, json=json, timeout=timeout)
        except Blocked as exc:
            return {"error": "blocked", "platform": self.platform, "detail": str(exc)}
        except requests.TooManyRedirects as exc:
            # NoRedirectSession (max_redirects=0) raises this on a 3xx: still a refused redirect, not a network
            # problem. Must come before RequestException, its parent class.
            return self._redirect_refused(method, getattr(exc, "response", None), secrets)
        except requests.RequestException as exc:
            return self._network_error(exc, secrets)
        except OSError as exc:  # e.g. ADS_TRUST_ENV=1 with a REQUESTS_CA_BUNDLE that does not exist
            return self._local_error(exc)
        status = getattr(response, "status_code", None)
        if isinstance(status, int) and 300 <= status < 400:
            return self._redirect_refused(method, response, secrets)
        limit = max_bytes or self.max_bytes
        try:
            body = _read_body(response, limit)
        except _TooLarge:
            return {"error": "response_too_large", "platform": self.platform, "http_status": status,
                    "max_bytes": limit, "detail": f"the answer exceeds {limit} bytes; reading stopped",
                    "fix": RESPONSE_TOO_LARGE_FIX}
        except requests.RequestException as exc:
            return self._network_error(exc, secrets)
        except OSError as exc:
            return self._local_error(exc)
        try:
            data = _json.loads(body) if body is not None else response.json()
        except ValueError:
            text = body.decode("utf-8", "replace") if body is not None else str(getattr(response, "text", ""))
            data = {"text": text[:500]}
        if not getattr(response, "ok", False):
            return redact(map_error(self.platform, response, data), secrets)
        result = data if isinstance(data, dict) else {"items": data if isinstance(data, list) else [data]}
        if cache:
            self.cache.set(key, result)
            result = copy.deepcopy(result)
            result["cached"] = False
        return result


class _TooLarge(Exception):
    """The response body exceeds the byte ceiling."""


def _close(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _read_body(response: Any, max_bytes: int) -> bytes | None:
    """Read a streamed body up to max_bytes, then stop (raise _TooLarge) without reading the rest.
    Returns None for a response object without iter_content (decoded with .json() instead)."""
    try:
        declared = _header(getattr(response, "headers", None), "Content-Length")
        if declared is not None and str(declared).strip().isdigit() and int(str(declared).strip()) > max_bytes:
            raise _TooLarge()
        iter_content = getattr(response, "iter_content", None)
        if not callable(iter_content):
            return None
        chunks, size = [], 0
        for chunk in iter_content(chunk_size=_CHUNK):
            if not chunk:
                continue
            size += len(chunk)
            if size > max_bytes:
                raise _TooLarge()
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        _close(response)


def _cache_key(platform: str, method: str, url: str, params: Any, body: Any, headers: dict) -> str:
    """SHA-256 fingerprint of the request plus every resolved header (name lowercased, value): the token and
    login-customer-id change the key, and the cache never holds a header value in clear."""
    header_pairs = sorted((str(k).lower(), str(v)) for k, v in (headers or {}).items())
    material = _dumps([platform, method.upper(), url, sorted((params or {}).items(), key=str), body, header_pairs])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _dumps(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(value)


# ------------------------------------------------------------------ tool helpers
def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def guard(freshness: str):
    """Decorator (copy of server._guard): NotConfigured becomes {"error": "not_configured", "detail"}; every dict
    result gets fetched_at (UTC ISO) and freshness. Keeps __name__, __doc__, __annotations__ and __wrapped__ so
    FastMCP sees the real signature."""

    def decorate(fn):
        def wrapper(*args, **kwargs):
            try:
                result = fn(*args, **kwargs)
            except NotConfigured as exc:
                result = {"error": "not_configured", "detail": str(exc)}
            if isinstance(result, dict):
                result.setdefault("fetched_at", _now())
                result.setdefault("freshness", freshness)
            return result

        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        wrapper.__annotations__ = fn.__annotations__
        wrapper.__wrapped__ = fn
        return wrapper

    return decorate


def register_tools(mcp: Any, tools: Sequence[tuple[str, Callable]]) -> list[str]:
    """Register each (title, fn) on a FastMCP server as a READ_ONLY tool; returns the tool names."""
    names = []
    for title, fn in tools:
        mcp.tool(title=title, annotations=READ_ONLY)(fn)
        names.append(fn.__name__)
    return names


def cap(limit: int, maximum: int = MAX_ROWS) -> int:
    """Clamp a row limit to 1..maximum."""
    return max(1, min(int(limit), maximum))


def micros(value: Any) -> float | None:
    """Google Ads micros (int64 sent as a string, or a number) to units, rounded to 6 decimals; None if absent
    or not a number. Note Google omits zero fields: use micros(row.get("costMicros", "0")) for a 0 default."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return round(int(str(value).strip()) / 1_000_000, 6)
    except ValueError:
        try:
            return round(float(value) / 1_000_000, 6)
        except (TypeError, ValueError):
            return None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def safe_ratio(numerator: Any, denominator: Any, digits: int = 6) -> float | None:
    """numerator / denominator rounded to `digits`; None when either is missing/not a number or the
    denominator is 0. Use for ctr, cpc, cpa, cvr."""
    num, den = _number(numerator), _number(denominator)
    if num is None or den is None or den == 0:
        return None
    return round(num / den, digits)


def roas(conversion_value: Any, spend: Any) -> float | None:
    """Return on ad spend = conversion value / spend; None if spend is 0/absent or the value is absent."""
    return safe_ratio(conversion_value, spend)


def total_roas(rows: Iterable[dict], value_key: str = "conversion_value", spend_key: str = "spend") -> float | None:
    """Pooled ROAS of several rows: sum of values / sum of spends (never the mean of row ROAS). Rows whose value
    is None are left out of both sums; None when no row has a value or total spend is 0."""
    values, spends, seen = 0.0, 0.0, False
    for row in rows:
        value, spend = _number(row.get(value_key)), _number(row.get(spend_key))
        if value is None:
            continue
        seen = True
        values += value
        spends += spend or 0.0
    return roas(values, spends) if seen else None


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def validate_dates(start_date: str, end_date: str) -> tuple[str, str]:
    """Check two YYYY-MM-DD dates (real calendar dates, start <= end); returns them, raises ValueError otherwise.
    Use before putting dates in a query string (GAQL, time_range) so nothing else can be injected."""
    parsed = []
    for name, value in (("start_date", start_date), ("end_date", end_date)):
        if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
            raise ValueError(f"{name} must be YYYY-MM-DD, got {value!r}")
        try:
            parsed.append(datetime.date.fromisoformat(value))
        except ValueError as exc:
            raise ValueError(f"{name} is not a calendar date: {value!r}") from exc
    if parsed[0] > parsed[1]:
        raise ValueError(f"start_date {start_date} is after end_date {end_date}")
    return start_date, end_date


def date_range(start: str, end: str, timezone: str | None = None, settle_days: int = 1,
               note: str = "the platform can still revise the most recent day; compare complete days only") -> dict:
    """Resolved date range with timezone, data_complete and settling_from (metrics.date_range_info).
    timezone None means unknown and is reported as UTC with that caveat."""
    return metrics.date_range_info(start, end, timezone, settle_days=settle_days, note=note)

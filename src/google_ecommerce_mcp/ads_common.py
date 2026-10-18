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
the proxy and CA bundle only (never .netrc, never redirects).

Write path (opt-in, per client): AllowList and the connectors' ALLOWED_ENDPOINTS stay read-only. A connector may
also give AdsClient a WriteAllowList of (method, pattern, validator, kind) rules: kind="validate" requests (a
dry_run_validator requires the native dry-run flag, with its type) are sent as is; kind="write" requests need
a WriteGrant signed with the per-client HMAC key (issue_grant, called by write_core.apply_plan) matching the
method, the URL, body_digest() of the body sent and request_digest() of the whole request (query, params,
headers), used once per process. On both kinds, headers outside CLIENT_HEADERS (X-HTTP-Method-Override...)
are refused. Anything else is "blocked" with a reason. Google Ads also maps CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION and
ACTION_NOT_PERMITTED to project_not_approved; the "meta_eco" platform shares the Meta error mapping."""

from __future__ import annotations

import copy
import datetime
import hashlib
import hmac
import json
import json as _json  # call() has a `json` parameter that shadows the module
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Sequence, Tuple, Union
from urllib.parse import parse_qsl, unquote, urlsplit

import requests
from mcp.types import ToolAnnotations

from . import metrics
from .ads_config import DRY_RUN_EQUALS, DRY_RUN_FLAGS, DRY_RUN_LIST, DRY_RUN_MODES
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
# Only the *_apply tools (they execute a signed plan) carry these annotations.
WRITE_APPLY = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)

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


# The ONLY place a percent-encoded '/' (%2F) may appear in a path: Search Console (Webmasters v3) takes the
# property (https://www.example.com/ as https%3A%2F%2Fwww.example.com%2F) and the sitemap URL encoded in ONE
# path segment each. host -> (path prefix, names of the segments whose NEXT segment may hold %2F). Any other
# host, or any other segment, keeps the refusal.
ENCODED_SLASH_SEGMENTS: dict[str, tuple[str, frozenset[str]]] = {
    "www.googleapis.com": ("/webmasters/v3/sites/", frozenset({"sites", "sitemaps"})),
}
# Escapes refused everywhere, even in an encoded segment: '.', '\\', '%' itself (double encoding: %252F), NUL
# and the other control characters.
_FORBIDDEN_ESCAPES = re.compile(r"%(?:2e|5c|25|[01][0-9a-f]|7f)", re.IGNORECASE)


def _encoded_slash_ok(host: str, path: str) -> bool:
    """True when every %2F of path sits in a segment that ENCODED_SLASH_SEGMENTS allows for host (the segment
    right after a "sites" or "sitemaps" segment, under the pinned prefix), no forbidden escape is used, and the
    DECODED segment holds no '.' / '..' component (sites/..%2F..%2F/sitemaps/x would climb out once a proxy or
    the server decodes it)."""
    rule = ENCODED_SLASH_SEGMENTS.get(host.lower())
    if rule is None or not path.startswith(rule[0]) or _FORBIDDEN_ESCAPES.search(path):
        return False
    segments = path.split("/")
    for index, segment in enumerate(segments):
        if "%2f" not in segment.lower():
            continue
        if index == 0 or segments[index - 1] not in rule[1]:
            return False
        if any(part in (".", "..") for part in unquote(segment).split("/")):
            return False
    return True


def bare_url(url: Any) -> str | None:
    """scheme://host/path of a URL that passes the allow-list hygiene checks, else None (query dropped).

    Refused: non-string, surrounding whitespace, any character outside printable ASCII, ';', a backslash,
    anything not starting literally with 'https://', user info, '.'/'..' segments, encoded '/', '.', '\\' or
    '%' (double encoding), and a query carrying `method` or `access_token`. One exception (ENCODED_SLASH_SEGMENTS): on
    https://www.googleapis.com/webmasters/v3/sites/..., the segment after "sites" and the one after "sitemaps"
    may hold %2F (Search Console property and sitemap URL); even there %2E, %5C, %25 (double encoding),
    encoded control characters and a '.' / '..' component of the decoded segment are refused."""
    if (not isinstance(url, str) or url != url.strip() or not _PRINTABLE.fullmatch(url) or ";" in url
            or "\\" in url or not url.startswith("https://")):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme != "https" or parts.username or parts.password:
        return None
    path = parts.path
    lowered = path.lower()
    if "/../" in path + "/" or "/./" in path + "/" or "%2e" in lowered or "%25" in lowered or "%5c" in lowered:
        return None  # dot segments, encoded '.', double encoding (%252F), encoded backslash: on every host
    if "%2f" in path.lower() and not _encoded_slash_ok(parts.netloc, path):
        return None
    # Graph API honours ?method=delete|post on a GET; a token never travels in the URL (double guard).
    if {k.strip().lower() for k, _ in parse_qsl(parts.query, keep_blank_values=True)} & FORBIDDEN_QUERY_KEYS:
        return None
    return f"{parts.scheme}://{parts.netloc}{path}"


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
        bare = bare_url(url)
        if bare is None:
            return False
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
    """Raised when code tries to send a request outside the allow-list (or with a secret in the URL).

    reason is None for the read path; on the write path it is one of url_too_long, not_on_write_allow_list,
    header_forbidden, header_unexpected, grant_missing, grant_mismatch, grant_unsigned, grant_mac_invalid,
    body_not_canonical, digest_mismatch, request_not_canonical, request_digest_mismatch, grant_replayed."""

    def __init__(self, message: str, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


# ------------------------------------------------------------------ write path
WRITE_KINDS = ("validate", "write")
WRITE_BLOCKED_FIX = ("writes go through a plan only: build it with a plan_* tool, then apply it with the "
                     "matching *_apply tool; never send a write request directly")


@dataclass(frozen=True)
class WriteGrant:
    """Permission to send ONE write request, issued only by write_core.apply_plan for one op of a signed plan.

    AdsClient.call sends a request matching a kind="write" rule only if: mac is the write_tokens.sign_grant
    HMAC (per-client key of the confirmation tokens) of every other field; method and url (scheme://host/path)
    equal the request's; body_digest is the SHA-256 of the body actually sent; request_digest equals
    request_digest() recomputed on the request actually sent (method, full url, params, body, caller headers);
    and (plan_id, op_id) was never used before in this process. Build one with issue_grant().

    request_digest and mac default to "" so older constructors stay valid, but such a grant is refused
    ("grant_unsigned")."""

    client_id: str
    plan_id: str
    op_id: str
    method: str
    url: str
    body_digest: str
    request_digest: str = ""
    mac: str = field(default="", repr=False)


def canonical_json(obj: Any) -> str:
    """Canonical JSON: keys sorted, no whitespace, UTF-8 kept, NaN/Infinity and non-JSON types refused
    (TypeError/ValueError) so two different bodies can never share a text."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def body_digest(obj: Any) -> str:
    """SHA-256 hex digest of canonical_json(obj) (None digests as "null"). Raises TypeError/ValueError for a
    body that is not plain JSON."""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


# Headers AdsClient connectors set themselves (auth, content negotiation, Google Ads account routing, API
# version). On the write path (validate and write) any other header is refused before sending.
CLIENT_HEADERS = frozenset({"authorization", "user-agent", "content-type", "accept", "developer-token",
                            "login-customer-id", "linked-customer-id", "openai-version"})
# Headers that would let a server rewrite the HTTP method: always refused on the write path.
FORBIDDEN_WRITE_HEADERS = frozenset({"x-http-method-override", "x-http-method", "x-method-override"})
MAX_WRITE_URL_LENGTH = 2048


def caller_headers(headers: Any) -> list[str]:
    """Sorted lowercased names of the headers outside CLIENT_HEADERS (bound in request_digest; on an accepted
    write request this list is always empty, since such headers are refused)."""
    if not headers:
        return []
    return sorted({str(k).strip().lower() for k in dict(headers)} - CLIENT_HEADERS)


def _canonical_params(params: Any) -> list:
    if params is None:
        return []
    items = params.items() if isinstance(params, dict) else list(params)
    out = []
    for key, value in items:
        if isinstance(value, (list, tuple)):
            norm: Any = [None if v is None else str(v) for v in value]
        else:
            norm = None if value is None else str(value)
        out.append([str(key), norm])
    return sorted(out, key=lambda kv: canonical_json(kv))


def request_digest(method: str, url: str, params: Any = None, body: Any = None, headers: Any = None) -> str:
    """SHA-256 hex of the canonical JSON of {method (upper), url (full, query included; a URL never carries a
    secret, AdsClient refuses it), params (sorted [name, value] pairs, values as strings), body, headers
    (caller_headers names)}. Raises TypeError/ValueError for a body or params that are not plain data."""
    material = {"method": str(method).upper(), "url": url, "params": _canonical_params(params), "body": body,
                "headers": caller_headers(headers)}
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


def issue_grant(*, client_id: str, plan_id: str, op_id: str, method: str, url: str, body: Any,
                params: Any = None, headers: Any = None) -> WriteGrant:
    """A signed WriteGrant for exactly this request (body_digest, request_digest, mac via
    write_tokens.sign_grant). For write_core.apply_plan only. Raises ValueError/TypeError (body not plain
    JSON, '|' in an id) or write_tokens.TokenError (no signing key)."""
    from .write_tokens import sign_grant  # lazy: write_tokens needs the state/keyring layer

    unsigned = WriteGrant(client_id=client_id, plan_id=plan_id, op_id=op_id, method=str(method).upper(),
                          url=url, body_digest=body_digest(body),
                          request_digest=request_digest(method, url, params, body, headers))
    return replace(unsigned, mac=sign_grant(unsigned))


_CONSUMED_GRANTS: set[tuple[str, str]] = set()
_CONSUMED_LOCK = threading.Lock()


def _consume_grant(grant: WriteGrant) -> bool:
    """Mark (plan_id, op_id) as used; False if it already was (a grant serves once per process)."""
    key = (grant.plan_id, grant.op_id)
    with _CONSUMED_LOCK:
        if key in _CONSUMED_GRANTS:
            return False
        _CONSUMED_GRANTS.add(key)
        return True


def _forget_consumed_grants() -> None:
    """Tests only: empty the in-process registry of consumed grants."""
    with _CONSUMED_LOCK:
        _CONSUMED_GRANTS.clear()


def dry_run_validator(key: str, expected: Any, inner: Validator | None = None, *,
                      mode: str = DRY_RUN_EQUALS) -> Validator:
    """Validator requiring the dry-run flag in the JSON body, with its exact type; then inner(body) if given.

    mode DRY_RUN_EQUALS: type(body[key]) is type(expected) and body[key] == expected (True only, never 1 nor
    "true"). mode DRY_RUN_LIST: body[key] is a list holding an item of the type and value of expected (Meta
    execution_options=["validate_only"]; the bare string is refused).

    The only validator a kind="validate" rule accepts (WriteAllowList checks its dry_run_flag marker
    (key, expected, mode)). Use platform_dry_run_validator(platform) for the ads_config.DRY_RUN_FLAGS of a
    platform."""
    if not isinstance(key, str) or not key:
        raise ValueError("dry-run flag key must be a non-empty string")
    if mode not in DRY_RUN_MODES:
        raise ValueError(f"dry-run mode must be one of {DRY_RUN_MODES}: {mode!r}")

    def same(value: Any) -> bool:
        return type(value) is type(expected) and value == expected

    def validate(body: Any) -> bool:
        if not isinstance(body, dict) or key not in body:
            return False
        value = body[key]
        if mode == DRY_RUN_LIST:
            flag = isinstance(value, list) and any(same(v) for v in value)
        else:
            flag = same(value)
        return flag and (inner is None or bool(inner(body)))

    validate.dry_run_flag = (key, expected, mode)  # type: ignore[attr-defined]
    return validate


def platform_dry_run_validator(platform: str, inner: Validator | None = None) -> Validator:
    """dry_run_validator built from ads_config.DRY_RUN_FLAGS[platform] (KeyError for an unknown platform)."""
    key, expected, mode = DRY_RUN_FLAGS[platform]
    return dry_run_validator(key, expected, inner, mode=mode)


def _flag_of(validator: Any) -> tuple[str, Any, str]:
    """(key, expected, mode) of a dry_run_validator marker; a 2-tuple marker reads as mode DRY_RUN_EQUALS."""
    flag = getattr(validator, "dry_run_flag", None)
    if not isinstance(flag, tuple) or len(flag) not in (2, 3) or not isinstance(flag[0], str):
        raise ValueError("malformed dry_run_flag marker")
    mode = flag[2] if len(flag) == 3 else DRY_RUN_EQUALS
    if mode not in DRY_RUN_MODES:
        raise ValueError(f"unknown dry-run mode {mode!r}")
    return flag[0], flag[1], mode


def _wrong_type_bodies(key: str, expected: Any, mode: str) -> list[dict]:
    """Bodies carrying the flag with the wrong type: a validate rule's validator must refuse them all."""
    if mode == DRY_RUN_LIST:
        return [{key: expected}, {key: str(expected)}, {key: [str(expected).upper()]}, {key: {}}]
    bodies: list[dict] = [{key: [expected]}, {key: str(expected)}, {key: str(expected).lower()}]
    if isinstance(expected, bool):
        bodies += [{key: int(expected)}, {key: float(expected)}]
    return [b for b in bodies if not (type(b[key]) is type(expected) and b[key] == expected)]


# Regex constructs refused in a write pattern: anything that could span path segments or hosts.
_WILDCARD_ESCAPES = frozenset("SWD")  # \S, \W, \D match '/' and '.'; \d, \w, \s cannot
_QUANTIFIERS = frozenset("+*{?")
_REPEATS = frozenset("+*{")
WriteRule = Tuple[str, str, Union[Validator, None], str]


def _check_write_pattern(pattern: str) -> None:
    """Raise ValueError for a write pattern holding a joker: an unescaped '.', a '|' outside a group, \\S, \\W,
    \\D..., an inline flag/lookaround '(?', a negated class that does not exclude '/', a nested quantifier
    such as (a+)+ or (\\d*){2} (catastrophic backtracking), or more than MAX_WRITE_URL_LENGTH characters."""
    if len(pattern) > MAX_WRITE_URL_LENGTH:
        raise ValueError(f"write pattern longer than {MAX_WRITE_URL_LENGTH} characters")
    # quantified[-1]: the group being read holds a quantifier (directly or in a nested group).
    depth, i, quantified = 0, 0, [False]
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\":
            nxt = pattern[i + 1:i + 2]
            if nxt in _WILDCARD_ESCAPES or nxt == "":
                raise ValueError(f"write pattern may not use \\{nxt}: {pattern}")
            i += 2
            continue
        if ch == ".":
            raise ValueError(f"write pattern may not hold an unescaped '.' (joker): {pattern}")
        if ch == "(":
            if pattern[i + 1:i + 2] == "?":
                raise ValueError(f"write pattern may not use '(?': {pattern}")
            depth += 1
            quantified.append(False)
        elif ch == ")":
            depth -= 1
            inner = quantified.pop() if len(quantified) > 1 else False
            if pattern[i + 1:i + 2] in _REPEATS and inner:
                raise ValueError(f"write pattern may not nest quantifiers (e.g. (a+)+): {pattern}")
            quantified[-1] = quantified[-1] or inner
        elif ch in _QUANTIFIERS:
            quantified[-1] = True
        elif ch == "|" and depth <= 0:
            raise ValueError(f"write pattern may not hold a top-level '|': {pattern}")
        elif ch == "[":
            end = pattern.find("]", i + 2)
            if end < 0:
                raise ValueError(f"unterminated class in write pattern: {pattern}")
            body = pattern[i + 1:end]
            if body.startswith("^") and "/" not in body:
                raise ValueError(f"negated class must exclude '/' in a write pattern: {pattern}")
            i = end + 1
            continue
        i += 1


class WriteAllowList(AllowList):
    """The write requests a platform connector may send: (method, URL regex, validator or None, kind).

    Same constraints as AllowList (https://, pinned literal host, full match on scheme://host/path, same URL
    refusals) plus: no joker ('.*', unescaped '.', top-level '|', \\S..., nested quantifiers), kind in
    WRITE_KINDS, and a kind="validate" rule must carry a dry_run_validator (probed: it refuses None, {} and
    the flag with the wrong type, e.g. Meta execution_options="validate_only" as a string or validateOnly=1).
    With platform (a key of ads_config.DRY_RUN_FLAGS), each validate rule's flag must be that platform's
    (key, expected value, mode). URLs longer than MAX_WRITE_URL_LENGTH never match.

    is_allowed() always returns False, so a WriteAllowList used by mistake as a read list lets nothing through;
    AdsClient uses match() and then requires the dry-run flag (validate) or a WriteGrant (write)."""

    def __init__(self, rules: Sequence[WriteRule], platform: str | None = None) -> None:
        if platform is not None and platform not in DRY_RUN_FLAGS:
            raise ValueError(f"unknown write platform {platform!r}; expected one of {sorted(DRY_RUN_FLAGS)}")
        self.platform = platform
        kinds: list[str] = []
        base: list[Rule] = []
        for rule in rules:
            if len(rule) != 4:
                raise ValueError(f"write rule must be (method, pattern, validator, kind): {rule!r}")
            method, pattern, validator, kind = rule
            if kind not in WRITE_KINDS:
                raise ValueError(f"write rule kind must be one of {WRITE_KINDS}: {kind!r}")
            if not isinstance(pattern, str):
                raise ValueError(f"write pattern must be a string: {pattern!r}")
            _check_write_pattern(pattern)
            if validator is not None and not callable(validator):
                raise ValueError(f"validator must be callable or None: {pattern}")
            if kind == "validate":
                if validator is None or not hasattr(validator, "dry_run_flag"):
                    raise ValueError(f"a validate rule needs a dry_run_validator: {pattern}")
                try:
                    lax = validator(None) or validator({})
                except Exception:
                    lax = False
                if lax:
                    raise ValueError(f"the validator of a validate rule must refuse a body without the flag: "
                                     f"{pattern}")
                key, expected, mode = _flag_of(validator)
                if platform is not None and (key, expected, mode) != DRY_RUN_FLAGS[platform]:
                    raise ValueError(f"the dry-run flag of a {platform} validate rule must be "
                                     f"{DRY_RUN_FLAGS[platform]!r}: {pattern}")
                for wrong in _wrong_type_bodies(key, expected, mode):
                    try:
                        accepted = validator(wrong)
                    except Exception:
                        accepted = False
                    if accepted:
                        raise ValueError(f"the validator of a validate rule must refuse the flag with the wrong "
                                         f"type ({wrong!r}): {pattern}")
            kinds.append(kind)
            base.append((method, pattern, validator))
        super().__init__(base)
        self._kinds = kinds

    @property
    def rules(self) -> list[tuple[str, str, str]]:
        """[(method, pattern, kind)] for review and tests."""
        return [(m, p, k) for (m, p, _, _), k in zip(self._rules, self._kinds)]

    def is_allowed(self, method: str, url: str, body: Any = None) -> bool:
        """Always False: a write is never allowed by the allow-list alone (see match and AdsClient.call)."""
        return False

    def match(self, method: str, url: str, body: Any = None) -> str | None:
        """The kind ("validate" or "write") of the first rule matching method + scheme://host/path whose
        validator (if any) accepts body; None if none matches, the URL fails the hygiene checks or is longer
        than MAX_WRITE_URL_LENGTH."""
        if not isinstance(url, str) or len(url) > MAX_WRITE_URL_LENGTH:
            return None
        bare = bare_url(url)
        if bare is None:
            return None
        for (m, _, rx, validator), kind in zip(self._rules, self._kinds):
            if method.upper() != m or not rx.fullmatch(bare):
                continue
            if validator is None:
                return kind
            try:
                if validator(body):
                    return kind
            except Exception:  # a validator that crashes refuses
                continue
        return None


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
# Meta ecosystem connectors (pixel, catalog, Instagram, Page) use the Graph API: same codes, same fixes.
META_PLATFORMS = frozenset({"meta", "meta_eco"})
_FIXES["meta_eco"] = _FIXES["meta"]
PROJECT_NOT_APPROVED_CODES = ("CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION", "ACTION_NOT_PERMITTED")
PROJECT_NOT_APPROVED_FIX = (
    "the Google Cloud project of your OAuth client is not approved for this Google Ads access level: apply for "
    "Explorer or Basic access in the Google Ads API Center (Tools > API Center of the manager account), or use a "
    "Google Ads test account (a project at Test level only reaches test accounts)")
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


META_WRITE_PERMISSION = "ads_management"
META_WRITE_SCOPE_FIX = ("grant the ads_management permission to the token (ads_read only reads) and give it access "
                        "to the ad account with a role that can edit it")


def map_error(platform: str, response: Any, data: Any, *, method: str | None = None) -> dict:
    """Turn a failed platform response into {error, detail, fix, platform, http_status, ...}.

    error is one of auth_failed, missing_scope, rate_limited (with retry_after_seconds and usage parsed from
    X-Business-Use-Case-Usage / X-Ad-Account-Usage), not_available, too_many_rows, api_error. Secret shapes in
    detail are masked; AdsClient.call also masks the exact configured secrets. method: the HTTP method of the
    request; for Meta (meta, meta_eco) a missing permission on a request other than GET (a write or its
    validate-only dry-run) names ads_management, a GET (or no method) names ads_read."""
    status = getattr(response, "status_code", None)
    headers = getattr(response, "headers", None) or {}
    detail = redact(_error_object(data))
    if isinstance(detail, str):
        detail = detail[:1000]
    text = json.dumps(detail, default=str) if not isinstance(detail, str) else detail
    fixes = _FIXES.get(platform, _DEFAULT_FIXES)
    is_meta = platform in META_PLATFORMS
    meta_code = detail.get("code") if isinstance(detail, dict) and is_meta else None
    meta_sub = detail.get("error_subcode") if isinstance(detail, dict) and is_meta else None
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
    if platform == "google_ads" and any(code in text for code in PROJECT_NOT_APPROVED_CODES):
        return out("project_not_approved", PROJECT_NOT_APPROVED_FIX)
    if status == 401 or meta_code in META_AUTH_CODES or "UNAUTHENTICATED" in text:
        return out("auth_failed", fixes["auth_failed"])
    lowered = text.lower()
    if ("SCOPE_INSUFFICIENT" in text or "insufficient authentication scopes" in lowered
            or (is_meta and isinstance(meta_code, int) and (meta_code == 10 or 200 <= meta_code <= 299))
            or (status == 403 and "scope" in lowered)):
        extra: dict[str, Any] = {}
        if platform == "google_ads":
            extra = {"required_scope": ADS_SCOPE_URL, "missing_scopes": [ADS_SCOPE_URL]}
        elif is_meta and method is not None and str(method).upper() != "GET":
            return out("missing_scope", META_WRITE_SCOPE_FIX, required_permission=META_WRITE_PERMISSION)
        elif is_meta:
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
    environment, passed explicitly; .netrc stays ignored and redirects stay refused.

    Write path (write_allow_list, a WriteAllowList; None = read only): a request off the read allow-list is
    sent only if it matches a kind="validate" rule (its dry_run_validator requires the dry-run flag in the
    body), or a kind="write" rule AND call() receives a signed WriteGrant (see WriteGrant) whose method, url,
    body_digest and request_digest equal those recomputed on the exact request sent, never used before in this
    process. Headers outside CLIENT_HEADERS are refused on both kinds. Otherwise
    {"error": "blocked", "reason": ...}. Write requests are never cached nor retried; redirects, the size
    ceiling and secret masking apply as for reads."""

    def __init__(self, platform: str, allow_list: AllowList, secrets: Secrets | None = None,
                 timeout: float = TIMEOUT, max_bytes: int = MAX_RESPONSE_BYTES, trust_env: bool = False,
                 write_allow_list: WriteAllowList | None = None) -> None:
        if isinstance(allow_list, WriteAllowList):
            raise TypeError("allow_list is the read allow-list; pass a WriteAllowList as write_allow_list")
        if write_allow_list is not None and not isinstance(write_allow_list, WriteAllowList):
            raise TypeError("write_allow_list must be a WriteAllowList")
        if (write_allow_list is not None and write_allow_list.platform is not None
                and DRY_RUN_FLAGS[write_allow_list.platform] != DRY_RUN_FLAGS.get(platform)):
            raise TypeError(f"write_allow_list is for {write_allow_list.platform}, not {platform}")
        self.platform = platform
        self.allow_list = allow_list
        self.write_allow_list = write_allow_list
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

    def _authorize_write(self, method: str, url: str, body: Any, grant: WriteGrant | None,
                         secrets: list[str], params: Any = None, headers: Any = None) -> str:
        """The write kind ("validate" or "write") of a request off the read allow-list, or raise Blocked
        (recorded in BLOCKED_ATTEMPTS) with the reason.

        Order: url_too_long, not_on_write_allow_list, header_forbidden / header_unexpected (both kinds), then
        for a write: grant_missing, grant_mismatch (incomplete), grant_unsigned, grant_mac_invalid,
        grant_mismatch (method/URL), body_not_canonical, digest_mismatch, request_not_canonical,
        request_digest_mismatch, grant_replayed. The grant is consumed only when every check passed."""
        shown = _redact_text(url, secrets)

        def refuse(reason: str, why: str) -> Blocked:
            self.BLOCKED_ATTEMPTS.append((method, shown))
            log.warning("blocked %s %s (%s, %s)", method, shown, self.platform, reason)
            return Blocked(f"{method} {shown}: {why}; request not sent", reason)

        if not isinstance(url, str) or len(url) > MAX_WRITE_URL_LENGTH:
            raise refuse("url_too_long", f"a write URL is limited to {MAX_WRITE_URL_LENGTH} characters")
        kind = self.write_allow_list.match(method, url, body) if self.write_allow_list is not None else None
        if kind is None:
            raise refuse("not_on_write_allow_list",
                         f"not on the {self.platform} read-only allow-list nor on its write allow-list")
        names = [str(k).strip().lower() for k in dict(headers or {})]
        if any(n in FORBIDDEN_WRITE_HEADERS for n in names):
            raise refuse("header_forbidden", "a method-override header is never sent on the write path")
        unexpected = sorted(n for n in names if n not in CLIENT_HEADERS)
        if unexpected:
            raise refuse("header_unexpected", f"header(s) {unexpected} are not set by the connector")
        if kind == "validate":
            return kind
        if not isinstance(grant, WriteGrant):
            raise refuse("grant_missing", "a write needs a WriteGrant issued by an approved plan")
        if not all(isinstance(v, str) and v for v in (grant.client_id, grant.plan_id, grant.op_id,
                                                      grant.method, grant.url, grant.body_digest)):
            raise refuse("grant_mismatch", "the WriteGrant is incomplete")
        if not isinstance(grant.mac, str) or not grant.mac:
            raise refuse("grant_unsigned", "the WriteGrant carries no signature (build it with issue_grant)")
        from .write_tokens import verify_grant_mac  # lazy: write_tokens needs the state/keyring layer

        if not verify_grant_mac(grant):
            raise refuse("grant_mac_invalid", "the WriteGrant signature does not match (forged or altered)")
        if grant.method.upper() != method or bare_url(grant.url) is None or bare_url(grant.url) != bare_url(url):
            raise refuse("grant_mismatch", "the WriteGrant was issued for another method or URL")
        try:
            sent = body_digest(body)
        except (TypeError, ValueError):
            raise refuse("body_not_canonical", "the body is not plain JSON, its digest cannot be checked") from None
        if not hmac.compare_digest(sent.encode("ascii"), grant.body_digest.encode("utf-8", "replace")):
            raise refuse("digest_mismatch", "the body differs from the one approved in the plan")
        try:
            actual = request_digest(method, url, params, body, headers)
        except (TypeError, ValueError):
            raise refuse("request_not_canonical", "the params cannot be digested") from None
        if not isinstance(grant.request_digest, str) or not hmac.compare_digest(
                actual.encode("ascii"), grant.request_digest.encode("utf-8", "replace")):
            raise refuse("request_digest_mismatch",
                         "the URL, query, params or headers differ from the request approved in the plan")
        if not _consume_grant(grant):
            raise refuse("grant_replayed", "this WriteGrant was already used; a grant serves once")
        return kind

    def send(self, method: str, url: str, *, headers: dict | None = None, params: dict | None = None,
             json: Any = None, timeout: float | None = None, grant: WriteGrant | None = None) -> Any:
        """Send one request if it is on the allow-list, else record it in BLOCKED_ATTEMPTS and raise Blocked.
        Also raises Blocked when a secret, an access_token or a method parameter would travel in the URL.
        Off the read allow-list, the write path rules apply (see the class docstring and grant).
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
            if self.write_allow_list is None:
                self.BLOCKED_ATTEMPTS.append((method, _redact_text(url, secrets)))
                log.warning("blocked %s %s (not on the %s read-only allow-list)", method,
                            _redact_text(url, secrets), self.platform)
                raise Blocked(f"{method} {_redact_text(url, secrets)} is not on the {self.platform} read-only "
                              "allow-list; request not sent")
            # Check and send the same object: a deep copy nobody else holds, so the digest is that of the body sent.
            # Same for params and headers: request_digest is computed on the very objects passed to the session.
            json = copy.deepcopy(json)
            params = copy.deepcopy(params)
            headers = dict(headers or {})
            self._authorize_write(method, url, json, grant, secrets, params, headers)
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
             max_bytes: int | None = None, grant: WriteGrant | None = None) -> dict:
        """Send and decode; never raises for HTTP, network, local TLS/OSError, redirect or allow-list problems,
        returns a dict instead.

        headers may be a dict or a zero-argument callable (e.g. a token provider) evaluated here: an
        auth.NotAuthenticated becomes {"error": "auth_failed"}; NotConfigured propagates to guard().
        Success: the JSON object, or {"items": [...]} when the body is a JSON array (Google Ads searchStream).
        Errors: {"error": "blocked"|"network"|map_error codes, ...}, secrets masked. cache=True (discovery calls
        only) reuses a successful result for 300 s and marks it "cached": True. The cache key is a SHA-256 of
        the request and of the resolved headers, so a new token or login-customer-id never gets an old answer
        and no header value is kept in clear. A 3xx answer is {"error": "blocked", "detail": "redirect refused
        ..."}; a body above max_bytes (default self.max_bytes, 20 MB) is {"error": "response_too_large"}.

        grant: the WriteGrant of the op being applied, required for a request matching a kind="write" rule
        (ignored on the read path). A refused write is {"error": "blocked", "reason": ...}; requests off the read
        allow-list are sent once (no retry) and never cached."""
        if cache and not self.allow_list.is_allowed(method, url, json):
            cache = False  # only reads are cached: a dry-run or a write always reaches the platform
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
            response = self.send(method, url, headers=resolved, params=params, json=json, timeout=timeout,
                                 grant=grant)
        except Blocked as exc:
            blocked = {"error": "blocked", "platform": self.platform, "detail": str(exc)}
            if exc.reason:
                blocked["reason"] = exc.reason
                blocked["fix"] = WRITE_BLOCKED_FIX
            return blocked
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
            return redact(map_error(self.platform, response, data, method=method), secrets)
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

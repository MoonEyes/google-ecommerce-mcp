"""Google Ads connector, read-only: two endpoints of the Google Ads REST API and nothing else.

Every request goes through ads_common.AdsClient and its AllowList: GET customers:listAccessibleCustomers and POST
customers/<digits>/googleAds:searchStream whose body must hold a bounded read-only GAQL query (starts with SELECT, no
`;`, no `--` or `/*` comment, a LIMIT of at most 10,001 rows, a FROM on the reporting allow-list and no personal-data
resource, unless GADS_ALLOW_SENSITIVE_QUERIES=1). No :mutate endpoint is on the list, so nothing can be changed even
by a crafted query.

Authentication uses the separate Google Ads OAuth token (scope adwords, ads_auth.AdsTokenProvider) sent as
Authorization: Bearer; TOKENS reads it from GADS_TOKEN_FILE or the OS keyring entry oauth-token-ads. The
developer-token header is sent only when GADS_DEVELOPER_TOKEN is set (Google no longer requires it from 09/09/2026)
and login-customer-id only when GADS_LOGIN_CUSTOMER_ID is set. Settings are read from the environment at each call,
without keyring fallback (Google Ads needs no Meta/OpenAI secret).

Multi-client mode (clients.json): every tool takes an optional client_id, resolved by clients.resolve into a Target
(client context, customer, login customer, token provider). The customer defaults to the client's
google_ads.customer_id; an explicit customer_id must belong to the client (id_not_owned otherwise, no request
sent); login-customer-id comes from the client's google_ads.login_customer_id and the token from the client's own
"gads" secret (ClientAdsTokens), never from the GADS_* variables nor the legacy keyring entry. gads_accounts only
lists the client's own customers. Results carry client_id. Without a registry nothing changes (legacy mode)."""

from __future__ import annotations

import functools
import json
import os
import re
from dataclasses import dataclass
from typing import Annotated, Any, Callable, Literal, Optional

from pydantic import Field

from .ads_auth import AdsTokenProvider
from .ads_common import (MAX_RESPONSE_BYTES, AdsClient, AllowList, cap, date_range, guard, micros, redact,
                         register_tools, roas, safe_ratio, validate_dates)
from .ads_config import AdsSettings
from .auth import NotAuthenticated
from .clients import ClientError
from .config import NotConfigured

PLATFORM = "google_ads"
HOST = "googleads.googleapis.com"
MAX_STREAM_ROWS = 10_000          # rows read at most per searchStream (the query asks for one more to detect it)
READ_LIMIT = MAX_STREAM_ROWS + 1
_MB = MAX_RESPONSE_BYTES // (1024 * 1024)  # byte ceiling of a searchStream answer: ads_common's, never raised
SETTLE_DAYS = 1
SENSITIVE_ENV = "GADS_ALLOW_SENSITIVE_QUERIES"
FRESHNESS = ("Google Ads data, near real time; the most recent day and conversions still inside their conversion "
             "window can change")
SETTLING_NOTE = ("Google Ads can still revise the most recent day, and conversions keep arriving during each "
                 "conversion action's window; compare complete days only")
ATTRIBUTION_NOTE = ("Each conversion action has its own attribution model and lookback window, set in Google Ads "
                    "(réglage par action de conversion). metrics.conversions counts only actions included in "
                    "'Conversions', reported on the date of the ad interaction (not the conversion date). Values "
                    "are as reported by Google Ads, not deduplicated with other platforms.")


def attribution() -> dict:
    """Attribution block, same shape as the Meta and OpenAI connectors (a new dict at each call)."""
    return {"basis": "conversion action settings", "window": "per conversion action", "note": ATTRIBUTION_NOTE}


ACCOUNT_INFO_QUERY = "SELECT customer.currency_code, customer.time_zone FROM customer LIMIT 1"

# gads_query reads only these aggregated reporting resources (FROM and every field prefix besides metrics/segments).
# Placement views (detail_placement_view, group_placement_view) are left out: they name third-party sites, apps and
# channels. Everything else is refused (resource_not_allowed) unless GADS_ALLOW_SENSITIVE_QUERIES=1.
REPORTING_RESOURCES = frozenset({
    "customer", "campaign", "campaign_budget", "ad_group", "ad_group_ad", "ad_group_criterion", "keyword_view",
    "shopping_performance_view", "product_group_view", "asset_group", "asset_group_listing_group_filter",
    "asset_group_asset", "asset", "ad_group_ad_asset_view", "campaign_criterion", "geographic_view", "gender_view",
    "age_range_view", "landing_page_view", "expanded_landing_page_view", "display_keyword_view", "topic_view",
    "bidding_strategy", "conversion_action", "location_view", "distance_view",
})
FIELD_NAMESPACES = frozenset({"metrics", "segments"})
# Second check (defence in depth): names exposing personal data, users, leads, calls, billing or account links,
# refused wherever they appear outside a string literal (sensitive_resource) unless GADS_ALLOW_SENSITIVE_QUERIES=1.
SENSITIVE_RESOURCES = frozenset({
    "change_event", "change_status", "customer_user_access", "customer_user_access_invitation", "click_view",
    "search_term_view", "campaign_search_term_view", "smart_campaign_search_term_view",
    "paid_organic_search_term_view", "user_list", "user_location_view", "customer_client_link", "customer_client",
    "lead_form_submission_data", "local_services_lead", "local_services_lead_conversation", "call_view",
    "access_invitation", "invoice", "billing_setup", "payments_account", "account_budget", "account_link",
    "third_party_app_analytics_link", "batch_job", "detail_placement_view", "group_placement_view",
})
# Any name starting with these is refused too (payments_account_info, payments_profile_id, billing_setup...).
_SENSITIVE_SCAN = re.compile(r"\b(?:" + "|".join(sorted(SENSITIVE_RESOURCES, key=len, reverse=True))
                             + r"|user_[a-z0-9_]*|customer_manager_link[a-z0-9_]*|local_services_[a-z0-9_]*"
                             r"|lead_form_[a-z0-9_]*|payments_[a-z0-9_]*|billing_[a-z0-9_]*)\b",
                             re.IGNORECASE | re.ASCII)
# customer is on the reporting list for its identity only: other customer.* sub-fields (payments_account_info,
# conversion_tracking_setting, remarketing_setting...) are refused unless GADS_ALLOW_SENSITIVE_QUERIES=1.
CUSTOMER_FIELDS = frozenset({"id", "descriptive_name", "currency_code", "time_zone", "status", "manager",
                             "test_account", "resource_name"})
# First segment of every dotted name outside literals (FROM, SELECT, WHERE, ORDER BY): campaign.x.y -> campaign.
_PREFIX = re.compile(r"(?<![a-z0-9_.])([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)?", re.IGNORECASE | re.ASCII)

# Fields reported in micros by Google Ads although their name has no _micros suffix.
MICROS_FIELDS = frozenset({
    "metrics.average_cpc", "metrics.average_cpm", "metrics.average_cost", "metrics.average_cpv",
    "metrics.average_cpe", "metrics.cost_per_conversion", "metrics.cost_per_all_conversions",
    "metrics.cost_per_current_model_attributed_conversion",
})

_GAQL = re.IGNORECASE | re.ASCII
_LITERAL = re.compile(r"'[^']*'|\"[^\"]*\"")
_HEAD = re.compile(r"\s*select\s+(?P<fields>[a-z0-9_.,\s]+?)\s+from\s+(?P<resource>[a-z_][a-z0-9_]*)(?=\s|$)", _GAQL)
_LIMIT_WORD = re.compile(r"\blimit\b", _GAQL)
_LIMIT = re.compile(r"\blimit\s+(?P<n>[0-9]+)(?=\s|$)", _GAQL)
_PARAMETERS = re.compile(r"\bparameters\b", _GAQL)


# ------------------------------------------------------------------ GAQL guard (also the allow-list validator)
class InvalidInput(ValueError):
    """A parameter that cannot be put in a URL or a GAQL query; returned as {"error": code, ...}, never sent."""

    def __init__(self, code: str, detail: str, fix: str) -> None:
        super().__init__(detail)
        self.code, self.detail, self.fix = code, detail, fix

    def as_dict(self) -> dict:
        return {"error": self.code, "platform": PLATFORM, "detail": self.detail, "fix": self.fix}


def is_safe_gaql(query: Any) -> bool:
    """True only for one read-only GAQL statement: starts with SELECT (ASCII, case-insensitive, after strip),
    contains no `;`, no comment (`--`, `/*`, `*/`) and no backslash, even inside a string literal (refusing the backslash
    means an escaped quote can never hide text from the literal masking)."""
    if not isinstance(query, str):
        return False
    text = query.strip()
    if not re.match(r"select\s", text, _GAQL):
        return False
    return not any(token in text for token in (";", "--", "/*", "*/", "\\"))


def sensitive_queries_allowed() -> bool:
    """GADS_ALLOW_SENSITIVE_QUERIES=1 (read now): personal-data resources may be queried by gads_query."""
    return os.getenv(SENSITIVE_ENV, "").strip().lower() in {"1", "true", "yes"}


def _mask_literals(text: str) -> str:
    """Blank the inside of '...' and "..." literals (same length), so keywords inside them are ignored."""
    return _LITERAL.sub(lambda m: m.group(0)[0] + " " * (len(m.group(0)) - 2) + m.group(0)[-1], text)


def _invalid(detail: str) -> InvalidInput:
    return InvalidInput("invalid_gaql", detail, "write a single GAQL query: SELECT <fields> FROM <resource> "
                        "[WHERE ...] [ORDER BY ...] [LIMIT n]; no ';', '--', '/*' or '\\', even inside a string")


def check_gaql(query: Any, bound: bool = True) -> str:
    """Validate a GAQL query and return the query to send; raises InvalidInput with code invalid_gaql,
    sensitive_resource or resource_not_allowed.

    Parses the head strictly (one SELECT of ASCII names, dots and commas, one FROM). Unless
    GADS_ALLOW_SENSITIVE_QUERIES=1: refuses personal-data names anywhere outside literals (sensitive_resource),
    and requires every dotted name prefix outside literals, in SELECT, FROM, WHERE and ORDER BY, to be a
    REPORTING_RESOURCES entry, metrics or segments, with customer.* limited to CUSTOMER_FIELDS
    (resource_not_allowed). Then bounds the read: no LIMIT -> `LIMIT 10001` added (before
    PARAMETERS if present); a LIMIT above 10001 is lowered to 10001. bound=False only checks that the LIMIT is
    present and within the bound (allow-list validator)."""
    if not is_safe_gaql(query):
        raise _invalid("the query must be one GAQL SELECT statement, without ';', comments nor backslash")
    masked = _mask_literals(query)
    if masked.count("'") % 2 or masked.count('"') % 2:  # an unterminated literal is left unmasked
        raise _invalid("unbalanced quote in the query")
    if "\x00" in query:
        raise _invalid("NUL character in the query")
    if any(ord(c) > 126 or (ord(c) < 32 and c not in "\t\n\r") for c in masked):
        raise _invalid("control or non-ASCII character outside a string literal")
    if len(re.findall(r"\bselect\b", masked, _GAQL)) > 1 or len(re.findall(r"\bfrom\b", masked, _GAQL)) > 1:
        raise _invalid("only one SELECT and one FROM are allowed (no subquery)")
    if re.search(r"\bjoin\b", masked, _GAQL):
        raise _invalid("JOIN is not GAQL and is refused")
    head = _HEAD.match(masked)
    if not head:
        raise _invalid("could not read 'SELECT <fields> FROM <resource>' at the start of the query")
    resource = head.group("resource").lower()
    fields = [f.strip() for f in head.group("fields").split(",") if f.strip()]
    if not sensitive_queries_allowed():
        sensitive = sorted({m.group(0).lower() for m in _SENSITIVE_SCAN.finditer(masked)})
        if sensitive:
            raise InvalidInput("sensitive_resource",
                               f"refused: {', '.join(sensitive)} can expose personal data (users, emails, click "
                               "ids, calls, leads, search terms, billing, audience lists or account links)",
                               f"use gads_performance for aggregated figures, or set {SENSITIVE_ENV}=1 in the MCP "
                               "server env to lift this check (données personnelles possibles)")
        dotted = [(m.group(1).lower(), (m.group(2) or "").lower()) for m in _PREFIX.finditer(masked)]
        prefixes = {name for name, _ in dotted} | {f.split(".", 1)[0].lower() for f in fields}
        outside = sorted(({resource} | prefixes) - REPORTING_RESOURCES - FIELD_NAMESPACES)
        customer_extra = sorted({f"customer.{sub}" for name, sub in dotted
                                 if name == "customer" and sub not in CUSTOMER_FIELDS})
        if customer_extra:
            raise InvalidInput("resource_not_allowed",
                               f"{', '.join(customer_extra)}: only customer.{{{', '.join(sorted(CUSTOMER_FIELDS))}}} "
                               "may be read by gads_query",
                               f"{SENSITIVE_ENV}=1 pour lever (données personnelles possibles)")
        if outside:
            raise InvalidInput("resource_not_allowed",
                               f"{', '.join(outside)} is not on the reporting allow-list of gads_query "
                               f"({', '.join(sorted(REPORTING_RESOURCES))})",
                               f"{SENSITIVE_ENV}=1 pour lever (données personnelles possibles)")
    words = list(_LIMIT_WORD.finditer(masked))
    limits = list(_LIMIT.finditer(masked))
    if len(words) > 1 or len(words) != len(limits):
        raise _invalid("LIMIT must appear once, followed by a whole number")
    if limits:
        found = limits[0]
        if int(found.group("n")) <= READ_LIMIT:
            return query
        if not bound:
            raise _invalid(f"LIMIT above {READ_LIMIT}")
        start, end = found.span("n")
        return query[:start] + str(READ_LIMIT) + query[end:]
    if not bound:
        raise _invalid("LIMIT required")
    params = _PARAMETERS.search(masked)
    if params:
        return query[:params.start()].rstrip() + f" LIMIT {READ_LIMIT} " + query[params.start():]
    return query.rstrip() + f" LIMIT {READ_LIMIT}"


def _search_body_ok(body: Any) -> bool:
    """Allow-list validator of searchStream bodies: {"query": <bounded safe GAQL>} (+ optional summaryRowSetting)."""
    if not (isinstance(body, dict) and "query" in body and set(body) <= {"query", "summaryRowSetting"}):
        return False
    try:
        check_gaql(body["query"], bound=False)
    except InvalidInput:
        return False
    return True


# The only requests this connector may send (version: v<digits>, the configured one is validated before use).
ALLOWED_ENDPOINTS = [
    ("GET", r"https://googleads\.googleapis\.com/v[0-9]{1,3}/customers:listAccessibleCustomers"),
    ("POST", r"https://googleads\.googleapis\.com/v[0-9]{1,3}/customers/[0-9]+/googleAds:searchStream",
     _search_body_ok),
]
ALLOW_LIST = AllowList(ALLOWED_ENDPOINTS)


def _settings() -> AdsSettings:
    return AdsSettings.from_env(keyring_fallback=False)


# Raw OAuth access tokens last sent (in memory only, as in TOKENS): "token" = the last one, "client:<id>" = the
# last one of each registered client, so every one is masked even in an exception text that shows it without the
# "Bearer " or "ya29." shapes redact() recognises.
_CURRENT_BEARER: dict[str, str] = {}


def _secrets() -> list[str]:
    """Secret values to mask: GADS_DEVELOPER_TOKEN and the raw Bearer tokens last sent (every client)."""
    values = [_settings().gads_developer_token, *_CURRENT_BEARER.values()]
    return [v for v in values if v]


CLIENT = AdsClient(PLATFORM, ALLOW_LIST, secrets=_secrets)
TOKENS = AdsTokenProvider()  # follows GADS_TOKEN_FILE at each call, else keyring entry oauth-token-ads


# ------------------------------------------------------------------ helpers
def _digits(value: str | None, name: str) -> str:
    cleaned = (value or "").strip().replace("-", "").replace(" ", "")
    if not re.fullmatch(r"[0-9]+", cleaned):
        raise InvalidInput("invalid_customer_id", f"{name} must be a Google Ads customer id (digits, dashes "
                           f"allowed), got {value!r}", "use an id listed by gads_accounts, e.g. 123-456-7890")
    return cleaned


def _customer(customer_id: str, settings: AdsSettings) -> str:
    if (customer_id or "").strip():
        return _digits(customer_id, "customer_id")
    if not settings.gads_customer_id:
        raise NotConfigured("GADS_CUSTOMER_ID")
    return _digits(settings.gads_customer_id, "GADS_CUSTOMER_ID")


def _base(settings: AdsSettings) -> str:
    version = (settings.gads_api_version or "").strip()
    if not re.fullmatch(r"v[0-9]{1,3}", version):
        raise InvalidInput("invalid_api_version", f"GADS_API_VERSION must look like v26, got {version!r}",
                           "set GADS_API_VERSION to a Google Ads API version such as v26, or unset it")
    return f"https://{HOST}/{version}"


def _safe_message(exc: BaseException) -> str:
    return str(redact(f"{type(exc).__name__}: {exc}", _secrets()))[:300]


# ------------------------------------------------------------------ who a call is for (legacy or registered client)
class ClientAdsTokens(AdsTokenProvider):
    """Google Ads token of ONE registered client, read from ctx.secret("gads") (GEM_SECRET_<CLIENT>_GADS, then
    the keyring entry "<client_id>/gads" or the name given in clients.json): never GADS_TOKEN_FILE nor the legacy
    entry oauth-token-ads of another client. Same refresh mechanics as AdsTokenProvider (in memory only)."""

    def __init__(self, ctx: Any) -> None:
        super().__init__(None)
        self._ctx = ctx
        self.client_id = ctx.client_id

    def _load(self) -> bool:
        if self._creds is None:
            try:
                raw = self._ctx.secret("gads")
                if not raw:
                    return False
                from google.oauth2.credentials import Credentials

                self._creds = Credentials.from_authorized_user_info(json.loads(raw))
            except Exception as exc:  # unreadable secret, broken JSON, no keyring backend: never echo the content
                self._creds = None
                raise NotAuthenticated(f"the Google Ads token of client {self.client_id!r} could not be read "
                                       f"({type(exc).__name__}); rerun `google-ecommerce-mcp setup-ads --client "
                                       f"{self.client_id} --client-secret <file>`") from None
        return True

    def headers(self) -> dict[str, str]:
        if not self._load():
            raise NotAuthenticated(f"No Google Ads token for client {self.client_id!r}. Run `google-ecommerce-mcp "
                                   f"setup-ads --client {self.client_id} --client-secret <file>` first.")
        return super().headers()


_CLIENT_TOKENS: dict[str, ClientAdsTokens] = {}  # keyed by client_id: one provider per client, never shared


def client_tokens(ctx: Any) -> Any:
    """The Google Ads token provider of a registered client (created once per client_id)."""
    provider = _CLIENT_TOKENS.get(ctx.client_id)
    if provider is None:
        provider = _CLIENT_TOKENS[ctx.client_id] = ClientAdsTokens(ctx)
    return provider


@dataclass(frozen=True)
class Target:
    """What one Google Ads call may touch: settings (API version, developer token: process-wide), the client
    context (None or legacy = environment variables), the customer, the login customer (manager) and the token
    provider. Built per call by resolve_target / target_for; nothing global changes."""

    settings: AdsSettings
    ctx: Any
    customer: str | None
    login: str | None
    tokens: Any

    @property
    def legacy(self) -> bool:
        return self.ctx is None or bool(getattr(self.ctx, "legacy", False))

    @property
    def client_id(self) -> str | None:
        """Stamped on results in registry mode; None in legacy mode (results unchanged)."""
        return None if self.legacy else self.ctx.client_id

    def stamp(self, out: Any) -> Any:
        if isinstance(out, dict) and self.client_id:
            out.setdefault("client_id", self.client_id)
        return out


def target_for(ctx: Any, customer_id: str = "", *, require_customer: bool = True,
               settings: AdsSettings | None = None) -> Target:
    """Target of an already resolved client context. Legacy (ctx None or legacy): today's variables and TOKENS.
    Registry: the client's customer (an explicit customer_id must be one of its own: ClientError id_not_owned),
    its login customer and its own token. Raises InvalidInput, NotConfigured or ClientError before any request."""
    settings = settings or _settings()
    explicit = (customer_id or "").strip()
    if ctx is None or bool(getattr(ctx, "legacy", False)):
        customer = _customer(customer_id, settings) if (require_customer or explicit) else None
        login = _digits(settings.gads_login_customer_id, "GADS_LOGIN_CUSTOMER_ID") \
            if settings.gads_login_customer_id else None
        return Target(settings, ctx, customer, login, TOKENS)
    accounts = ctx.accounts(PLATFORM)
    if explicit:
        customer: str | None = _digits(customer_id, "customer_id")
        ctx.require_owned(PLATFORM, customer)
    elif require_customer:
        customer = _digits(ctx.account(PLATFORM, "customer_id"), "customer_id")
    else:
        customer = accounts.get("customer_id") or None
    login_raw = accounts.get("login_customer_id")
    login = _digits(login_raw, "login_customer_id") if login_raw else None
    return Target(settings, ctx, customer, login, client_tokens(ctx))


def resolve_target(client_id: str | None, customer_id: str = "", *, require_customer: bool = True) -> Target:
    """clients.resolve(client_id) then target_for (raises ClientError: client_required, unknown_client...)."""
    from .clients import resolve

    return target_for(resolve(client_id), customer_id, require_customer=require_customer)


def _headers(target: Target) -> Callable[[], dict]:
    """Header provider evaluated by AdsClient.call: Bearer token of the target, then the optional Google Ads
    headers. An unreadable stored token (corrupt JSON, no keyring backend) becomes NotAuthenticated, i.e.
    auth_failed."""
    settings, login, tokens, client_id = target.settings, target.login, target.tokens, target.client_id

    def provide() -> dict:
        try:
            headers = dict(tokens.headers())
        except NotAuthenticated:
            raise
        except Exception as exc:
            raise NotAuthenticated(f"the stored Google Ads token could not be read ({_safe_message(exc)}); rerun "
                                   "`google-ecommerce-mcp setup-ads --client-secret <file>`") from exc
        auth = str(headers.get("Authorization", ""))
        if auth.lower().startswith("bearer ") and auth[7:].strip():
            _CURRENT_BEARER["token"] = auth[7:].strip()
            if client_id:
                _CURRENT_BEARER[f"client:{client_id}"] = auth[7:].strip()
        if settings.gads_developer_token:
            headers["developer-token"] = settings.gads_developer_token
        if login:
            headers["login-customer-id"] = login
        return headers

    return provide


def headers_for(target: Target) -> Callable[[], dict]:
    """Public alias of the header provider (used by gads_write for its dry-runs and writes)."""
    return _headers(target)


def run_gaql(target: Target, query: str) -> dict:
    """check_gaql (bounded, reporting resources only) then searchStream for target.customer; {"results",
    "batches"} or an error dict. For gads_write snapshots (never cached: the state must be fresh)."""
    return _search(target, check_gaql(query))


def _search(target: Target, query: str, cache: bool = False) -> dict:
    """POST googleAds:searchStream on a query already returned by check_gaql; returns {"results": [...all
    batches...], "batches": n} or an error dict."""
    if not target.customer:
        raise NotConfigured("GADS_CUSTOMER_ID")
    resp = CLIENT.call("POST", f"{_base(target.settings)}/customers/{target.customer}/googleAds:searchStream",
                       headers=_headers(target), json={"query": query}, cache=cache, max_bytes=MAX_RESPONSE_BYTES)
    if resp.get("error") == "response_too_large":
        return {**resp, "read_complete": False,
                "read_note": f"the answer exceeds {MAX_RESPONSE_BYTES} bytes ({_MB} MB) before {READ_LIMIT} rows: "
                             "nothing was read; add a smaller LIMIT, fewer fields or a shorter period"}
    if "error" in resp:
        return resp
    batches = resp.get("items", [resp] if "results" in resp else [])
    results: list[dict] = []
    for batch in batches:
        if isinstance(batch, dict):
            results.extend(r for r in batch.get("results", []) if isinstance(r, dict))
    return {"results": results, "batches": len(batches)}


def _account_info(target: Target) -> tuple[str | None, str | None, dict | None]:
    """(currency, timezone, error) of the target customer, from a one-row query cached 300 s (the cache key
    includes the token and login-customer-id, so two clients never share an answer)."""
    resp = _search(target, ACCOUNT_INFO_QUERY, cache=True)
    if "error" in resp:
        return None, None, {k: resp.get(k) for k in ("error", "detail", "fix") if k in resp}
    first = resp["results"][0].get("customer", {}) if resp["results"] else {}
    return first.get("currencyCode"), first.get("timeZone"), None


_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")


def _snake(name: str) -> str:
    return _CAMEL.sub("_", name).lower()


def _flatten(item: dict, prefix: str = "") -> dict:
    """{"campaign": {"id": "1"}, "metrics": {"costMicros": "1500000"}} -> {"campaign.id": "1", "metrics.cost": 1.5}.
    Keys become GAQL snake_case; *_micros fields become currency units without the suffix; the micros fields
    without suffix (MICROS_FIELDS, e.g. metrics.average_cpc) become units under their own name; int64 strings
    under metrics become numbers; ids stay strings."""
    out: dict = {}
    for key, value in item.items():
        name = f"{prefix}{_snake(key)}"
        if isinstance(value, dict):
            out.update(_flatten(value, name + "."))
            continue
        if name.endswith("_micros"):
            out[name[: -len("_micros")]] = micros(value)
        elif name in MICROS_FIELDS:
            out[name] = micros(value)
        elif name.startswith("metrics.") and isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value):
            out[name] = int(value)
        else:
            out[name] = value
    return out


def _int(value: Any) -> int:
    try:
        return int(str(value if value is not None else 0))
    except ValueError:
        return 0


def _float(value: Any) -> float:
    try:
        return float(value if value is not None else 0)
    except (TypeError, ValueError):
        return 0.0


def _metric_info(currency: str | None) -> dict:
    money = currency or "account currency (unknown: lookup failed)"
    return {
        "spend": {"unit": money, "definition": "metrics.cost_micros / 1,000,000: cost of the ads in the period"},
        "impressions": {"unit": "count", "definition": "times an ad was shown"},
        "clicks": {"unit": "count", "definition": "clicks on the ads"},
        "conversions": {"unit": "count (can be fractional)", "definition": "metrics.conversions: conversion "
                        "actions included in 'Conversions', attributed per conversion action settings"},
        "conversion_value": {"unit": money, "definition": "metrics.conversions_value: value of those conversions"},
        "ctr": {"unit": "ratio 0..1", "definition": "clicks / impressions; null when no impression"},
        "cpc": {"unit": money, "definition": "spend / clicks; null when no click"},
        "cpa": {"unit": money, "definition": "spend / conversions; null when no conversion"},
        "roas": {"unit": "ratio", "definition": "conversion_value / spend (totals: sum of values / sum of spends, "
                 "never the mean of row ROAS); null when spend is 0"},
    }


def _ratios(row: dict) -> dict:
    row["ctr"] = safe_ratio(row["clicks"], row["impressions"])
    row["cpc"] = safe_ratio(row["spend"], row["clicks"])
    row["cpa"] = safe_ratio(row["spend"], row["conversions"])
    row["roas"] = roas(row["conversion_value"], row["spend"])
    return row


def _contained(fn: Callable[..., dict]) -> Callable[..., dict]:
    """Inside guard(): invalid input becomes its error dict and any unexpected exception becomes a structured
    internal_error (secrets masked) instead of crashing the tool. NotConfigured is left to guard()."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict:
        try:
            return fn(*args, **kwargs)
        except NotConfigured:
            raise
        except InvalidInput as exc:
            return exc.as_dict()
        except ClientError as exc:  # client_required, unknown_client, id_not_owned...: no request was sent
            out = {**exc.to_dict(), "platform": PLATFORM}
            requested = kwargs.get("client_id")
            if isinstance(requested, str) and requested.strip():
                out["client_id"] = requested.strip()
            return out
        except Exception as exc:
            return {"error": "internal_error", "platform": PLATFORM, "detail": _safe_message(exc),
                    "fix": "unexpected failure in the Google Ads connector; retry, and report it if it persists"}

    return wrapper


# ------------------------------------------------------------------ fixed GAQL templates
LEVELS = {
    "account": ("customer", ["customer.id", "customer.descriptive_name"]),
    "campaign": ("campaign", ["campaign.id", "campaign.name", "campaign.status",
                              "campaign.advertising_channel_type"]),
    "ad_group": ("ad_group", ["campaign.id", "campaign.name", "ad_group.id", "ad_group.name", "ad_group.status"]),
}
SEGMENTS = {"none": None, "date": "segments.date", "device": "segments.device", "network": "segments.ad_network_type"}
COMMON_FIELDS = ["customer.currency_code", "customer.time_zone", "metrics.cost_micros", "metrics.impressions",
                 "metrics.clicks", "metrics.conversions", "metrics.conversions_value"]
_LABELS = {"customer.id": "customer_id", "customer.descriptive_name": "customer_name", "campaign.id": "campaign_id",
           "campaign.name": "campaign_name", "campaign.status": "campaign_status",
           "campaign.advertising_channel_type": "channel_type", "ad_group.id": "ad_group_id",
           "ad_group.name": "ad_group_name", "ad_group.status": "ad_group_status", "segments.date": "date",
           "segments.device": "device", "segments.ad_network_type": "network"}


def build_performance_query(level: str, start_date: str, end_date: str, segment: str = "none") -> str:
    """GAQL from fixed templates only: level and segment must be known keys, dates real YYYY-MM-DD dates.
    Raises InvalidInput otherwise, so nothing a caller passes can reach the query text unchecked."""
    if level not in LEVELS:
        raise InvalidInput("invalid_level", f"level must be one of {sorted(LEVELS)}, got {level!r}",
                           "use account, campaign or ad_group")
    if segment not in SEGMENTS:
        raise InvalidInput("invalid_segment", f"segment must be one of {sorted(SEGMENTS)}, got {segment!r}",
                           "use none, date, device or network")
    if not (start_date or "").strip() or not (end_date or "").strip():
        raise InvalidInput("invalid_dates", "start_date and end_date are required (YYYY-MM-DD)",
                           "pass start_date and end_date as YYYY-MM-DD, start <= end")
    try:
        start, end = validate_dates(start_date, end_date)
    except ValueError as exc:
        raise InvalidInput("invalid_dates", str(exc), "pass start_date and end_date as YYYY-MM-DD, start <= end") \
            from exc
    resource, fields = LEVELS[level]
    selected = fields + ([SEGMENTS[segment]] if SEGMENTS[segment] else []) + COMMON_FIELDS
    return (f"SELECT {', '.join(selected)} FROM {resource} "
            f"WHERE segments.date BETWEEN '{start}' AND '{end}' "
            f"ORDER BY metrics.cost_micros DESC LIMIT {READ_LIMIT}")


def _performance_row(result: dict, level: str, segment: str) -> dict:
    flat = _flatten(result)
    _, fields = LEVELS[level]
    keys = fields + ([SEGMENTS[segment]] if SEGMENTS[segment] else [])
    row = {_LABELS[k]: flat.get(k) for k in keys}
    m = result.get("metrics", {}) if isinstance(result.get("metrics"), dict) else {}
    # Google omits zero-valued fields: absent means 0.
    row.update({"spend": micros(m.get("costMicros", "0")), "impressions": _int(m.get("impressions")),
                "clicks": _int(m.get("clicks")), "conversions": round(_float(m.get("conversions")), 6),
                "conversion_value": round(_float(m.get("conversionsValue")), 6)})
    return _ratios(row)


def _totals(rows: list[dict]) -> dict:
    totals = {"spend": round(sum(r["spend"] or 0.0 for r in rows), 6),
              "impressions": sum(r["impressions"] for r in rows),
              "clicks": sum(r["clicks"] for r in rows),
              "conversions": round(sum(r["conversions"] for r in rows), 6),
              "conversion_value": round(sum(r["conversion_value"] for r in rows), 6)}
    return _ratios(totals)


# ------------------------------------------------------------------ tools
CUSTOMER_ID = Annotated[str, Field(description="Google Ads customer id (digits, dashes allowed), as listed by "
                                               "gads_accounts; empty uses the client's (or GADS_CUSTOMER_ID)")]
CLIENT_ID = Annotated[Optional[str], Field(description="Client id from clients_list; empty uses the only client")]


@guard("current state (account list cached 5 minutes)")
@_contained
def gads_accounts(client_id: CLIENT_ID = None) -> dict:
    """Google Ads customer accounts the Google Ads token can read directly (customers:listAccessibleCustomers),
    with the configured customer (GADS_CUSTOMER_ID, or the client's) and login customer (manager account), to pick
    a customer_id for gads_performance or gads_query. Returns {"customers": [{"id"}], "configured_customer_id",
    "login_customer_id", "cached"} (cached: true when the list comes from the 5-minute discovery cache). With a
    client registry only the client's own customers are listed ("hidden_customers" counts the others)."""
    target = resolve_target(client_id, require_customer=False)
    settings = target.settings
    url, headers = f"{_base(settings)}/customers:listAccessibleCustomers", _headers(target)
    resp = CLIENT.call("GET", url, headers=headers, cache=True)
    if "error" in resp:
        return target.stamp(resp)
    customers = [{"id": name.rsplit("/", 1)[-1]} for name in resp.get("resourceNames", []) if isinstance(name, str)]
    if target.legacy:
        return {"customers": customers, "configured_customer_id": settings.gads_customer_id,
                "login_customer_id": settings.gads_login_customer_id, "cached": resp.get("cached", False)}
    own = [c for c in customers if target.ctx.owns(PLATFORM, c["id"])]
    return target.stamp({"customers": own, "hidden_customers": len(customers) - len(own),
                         "configured_customer_id": target.customer, "login_customer_id": target.login,
                         "cached": resp.get("cached", False)})


@guard(FRESHNESS)
@_contained
def gads_performance(
    level: Annotated[Literal["account", "campaign", "ad_group"],
                     Field(description="Row level: account, campaign or ad_group")] = "campaign",
    start_date: Annotated[str, Field(description="Required: first day, YYYY-MM-DD (account timezone)")] = "",
    end_date: Annotated[str, Field(description="Required: last day, YYYY-MM-DD (account timezone)")] = "",
    segment: Annotated[Literal["none", "date", "device", "network"],
                       Field(description="Optional split of each row: none, date, device or network")] = "none",
    limit: Annotated[int, Field(description="Maximum rows returned (1 to 1000), highest spend first; totals "
                                            "cover up to 10,000 rows (see totals_complete)", ge=1, le=1000)] = 50,
    customer_id: CUSTOMER_ID = "",
    client_id: CLIENT_ID = None,
) -> dict:
    """Google Ads performance by account, campaign or ad group over a date range: spend, impressions, clicks,
    conversions, conversion value, CTR, CPC, CPA and ROAS, optionally split by date, device or network. Built from
    fixed aggregated GAQL templates (searchStream), highest spend first. Returns "rows", "totals" (ROAS = sum of
    values / sum of spends) with "totals_complete". start_date and end_date are required (empty -> invalid_dates), the "currency" (looked up when no row), a "date_range" in the
    account timezone with "data_complete", the unit and definition of each metric, and the "attribution" used."""
    query = check_gaql(build_performance_query(level, start_date, end_date, segment))
    target = resolve_target(client_id, customer_id)
    customer = target.customer
    resp = _search(target, query)
    if "error" in resp:
        return target.stamp(resp)
    results = resp["results"]
    totals_complete = len(results) <= MAX_STREAM_ROWS
    results = results[:MAX_STREAM_ROWS]
    rows = [_performance_row(r, level, segment) for r in results]
    first = results[0].get("customer", {}) if results else {}
    currency, timezone = first.get("currencyCode"), first.get("timeZone")
    lookup_error = None
    if not currency or not timezone:
        info_currency, info_timezone, lookup_error = _account_info(target)
        currency, timezone = currency or info_currency, timezone or info_timezone
    limit = cap(limit)
    out = {"customer_id": customer, "level": level, "segment": segment, "rows": rows[:limit],
           "row_count": len(rows), "truncated": len(rows) > limit, "totals": _totals(rows),
           "totals_complete": totals_complete, "currency": currency,
           "date_range": date_range(start_date, end_date, timezone, settle_days=SETTLE_DAYS, note=SETTLING_NOTE),
           "metrics": _metric_info(currency), "attribution": attribution()}
    if not totals_complete:
        out["totals_note"] = (f"more than {MAX_STREAM_ROWS} rows: totals cover the {MAX_STREAM_ROWS} highest-spend "
                              "rows only; use level=account or a shorter period for exact totals")
    if not rows:
        out["note"] = "no row: no activity in this period (or the customer has no matching entity)"
    if not timezone:
        out["timezone_note"] = ("account timezone unknown (lookup failed): date_range is computed in UTC and "
                                "data_complete may be off by one day")
    if lookup_error:
        out["account_info_error"] = lookup_error
    return target.stamp(out)


@guard(FRESHNESS)
@_contained
def gads_query(
    gaql: Annotated[str, Field(description="One read-only GAQL statement starting with SELECT, e.g. SELECT "
                                           "campaign.name, metrics.clicks FROM campaign WHERE segments.date "
                                           "DURING LAST_7_DAYS; no ';', '--' or '/*' anywhere, even in a string")],
    customer_id: CUSTOMER_ID = "",
    limit: Annotated[int, Field(description="Maximum rows returned (1 to 1000); Google is asked for at most "
                                            "10,001 rows (LIMIT added or lowered)", ge=1, le=1000)] = 100,
    client_id: CLIENT_ID = None,
) -> dict:
    """Run a free read-only Google Ads Query Language (GAQL) SELECT on one customer (searchStream), for fields the
    fixed gads_performance templates do not cover. Refused without any network call unless it is a single SELECT
    with no ';' and no '--' or '/*' (refused even inside a string literal). The read is bounded: without LIMIT,
    LIMIT 10001 is added (before PARAMETERS); a larger LIMIT is lowered to 10001; the answer is capped at 20 MB.
    One SELECT and one FROM only, no JOIN, no backslash, ASCII only outside string literals. FROM and every dotted
    name prefix in SELECT, WHERE and ORDER BY (besides metrics and segments) must be a reporting resource: customer, campaign, campaign_budget, ad_group, ad_group_ad,
    ad_group_criterion, keyword_view, shopping_performance_view, product_group_view, asset_group,
    asset_group_listing_group_filter, asset_group_asset, asset, ad_group_ad_asset_view, campaign_criterion,
    geographic_view, gender_view, age_range_view, landing_page_view, expanded_landing_page_view,
    display_keyword_view, topic_view, bidding_strategy, conversion_action, location_view, distance_view, with
    customer.* limited to id, descriptive_name, currency_code, time_zone, status, manager, test_account and
    resource_name; anything else is refused with resource_not_allowed. Names exposing personal data (change_event,
    click_view, call_view, search term views, leads, user_*, payments_*, billing_*, customer_user_access, invoices,
    account links, placements...) are
    refused anywhere in the query with sensitive_resource. Setting GADS_ALLOW_SENSITIVE_QUERIES=1 in the server env
    lifts both lists (personal data possible). Rows are flattened to GAQL field
    names (campaign.name, metrics.clicks); micros fields are converted to currency units: *_micros renamed without
    the suffix (metrics.cost_micros -> metrics.cost), metrics.average_cpc, average_cpm, average_cost and
    cost_per_* kept under their name."""
    target = resolve_target(client_id, customer_id)
    customer = target.customer
    query = check_gaql(gaql)
    resp = _search(target, query)
    if "error" in resp:
        return target.stamp(resp)
    results = resp["results"]
    read_complete = len(results) <= MAX_STREAM_ROWS
    rows = [_flatten(r) for r in results[:MAX_STREAM_ROWS]]
    limit = cap(limit)
    out: dict = {"customer_id": customer, "rows": rows[:limit], "row_count": len(rows),
                 "truncated": len(rows) > limit, "read_complete": read_complete, "batches": resp["batches"],
                 "units_note": "micros converted to currency units (divided by 1,000,000): *_micros fields under "
                               "the name without _micros, and metrics.average_cpc, average_cpm, average_cost, "
                               "average_cpv, average_cpe, cost_per_conversion, cost_per_all_conversions, "
                               "cost_per_current_model_attributed_conversion under their own name; ids are strings",
                 "attribution": attribution()}
    if query != gaql:
        out["query_sent"] = query
        out["limit_note"] = f"the read was bounded to {READ_LIMIT} rows (LIMIT added or lowered)"
    if not read_complete:
        out["read_note"] = f"more than {MAX_STREAM_ROWS} rows matched: only the first {MAX_STREAM_ROWS} were read"
    currencies = {r.get("customer.currency_code") for r in rows if r.get("customer.currency_code")}
    if len(currencies) == 1:
        out["currency"] = currencies.pop()
    return target.stamp(out)


# Conversion actions: their settings decide what metrics.conversions counts (primary_for_goal,
# include_in_conversions_metric), how it is attributed and with which lookback windows.
CONVERSION_FIELDS = (
    "conversion_action.resource_name", "conversion_action.id", "conversion_action.name", "conversion_action.status",
    "conversion_action.type", "conversion_action.category", "conversion_action.origin",
    "conversion_action.primary_for_goal", "conversion_action.include_in_conversions_metric",
    "conversion_action.counting_type", "conversion_action.attribution_model_settings.attribution_model",
    "conversion_action.click_through_lookback_window_days", "conversion_action.view_through_lookback_window_days",
    "conversion_action.value_settings.default_value", "conversion_action.value_settings.default_currency_code",
    "conversion_action.value_settings.always_use_default_value",
)
_CONVERSION_INTS = ("click_through_lookback_window_days", "view_through_lookback_window_days")
CONVERSION_NOTE = ("metrics.conversions counts only the actions with include_in_conversions_metric true (in "
                   "practice the primary ones, primary_for_goal); the others appear in all_conversions only. Each "
                   "action has its own attribution model and lookback windows (days).")


def build_conversion_actions_query(include_removed: bool = False) -> str:
    """Fixed GAQL template of gads_conversion_actions (no caller text reaches it)."""
    where = "" if include_removed else " WHERE conversion_action.status != 'REMOVED'"
    return (f"SELECT {', '.join(CONVERSION_FIELDS)} FROM conversion_action{where} "
            f"ORDER BY conversion_action.name LIMIT {READ_LIMIT}")


def _conversion_row(result: dict) -> dict:
    flat = _flatten(result)
    row = {}
    for field in CONVERSION_FIELDS:
        name = field.rsplit(".", 1)[-1]
        value = flat.get(field)
        if name in _CONVERSION_INTS and isinstance(value, str) and value.lstrip("-").isdigit():
            value = int(value)
        row[name] = value
    return row


@guard("current settings (read now)")
@_contained
def gads_conversion_actions(
    include_removed: Annotated[bool, Field(description="Also list REMOVED conversion actions")] = False,
    limit: Annotated[int, Field(description="Maximum actions returned (1 to 1000), by name", ge=1, le=1000)] = 200,
    customer_id: CUSTOMER_ID = "",
    client_id: CLIENT_ID = None,
) -> dict:
    """Google Ads conversion actions of one customer and the settings that decide what metrics.conversions
    counts: name, status, type, category, origin, primary_for_goal, include_in_conversions_metric, counting type
    (one or every conversion per click), attribution model, click-through and view-through lookback windows (days)
    and default value / currency. Fixed GAQL template on conversion_action (searchStream), sorted by name; REMOVED
    actions are left out unless include_removed. Use it to explain why conversions differ from another platform."""
    query = check_gaql(build_conversion_actions_query(include_removed))
    target = resolve_target(client_id, customer_id)
    resp = _search(target, query)
    if "error" in resp:
        return target.stamp(resp)
    results = resp["results"][:MAX_STREAM_ROWS]
    rows = [_conversion_row(r) for r in results]
    limit = cap(limit)
    out = {"customer_id": target.customer, "conversion_actions": rows[:limit], "count": len(rows),
           "truncated": len(rows) > limit, "counted_in_conversions": sum(1 for r in rows
                                                                         if r.get("include_in_conversions_metric")),
           "note": CONVERSION_NOTE, "attribution": attribution()}
    if not rows:
        out["note_empty"] = "no conversion action: conversions are not tracked in this account"
    return target.stamp(out)


TOOLS = [
    ("Google Ads accounts", gads_accounts),
    ("Google Ads performance", gads_performance),
    ("Google Ads GAQL query", gads_query),
    ("Google Ads conversion actions", gads_conversion_actions),
]


def register(mcp: Any) -> list[str]:
    """Register the Google Ads tools on a FastMCP server (READ_ONLY annotations); returns their names."""
    return register_tools(mcp, TOOLS)


def summary(start_date: str, end_date: str, client_id: str | None = None) -> dict:
    """Account-level totals for the cross-platform overview: {platform, spend, impressions, clicks, conversions,
    conversion_value, roas, currency, attribution, date_range}, or the error dict (not_configured, auth_failed,
    internal_error...) with platform set. client_id: the registered client (None = the implicit one). Never
    raises."""
    try:
        kwargs: dict[str, Any] = {"client_id": client_id} if client_id else {}
        result = gads_performance(level="account", start_date=start_date, end_date=end_date, segment="none",
                                  limit=1, **kwargs)
        if "error" in result:
            return {**result, "platform": PLATFORM}
        totals = result["totals"]
        out = {"platform": PLATFORM, "spend": totals["spend"], "impressions": totals["impressions"],
               "clicks": totals["clicks"], "conversions": totals["conversions"],
               "conversion_value": totals["conversion_value"], "roas": totals["roas"],
               "currency": result["currency"], "attribution": attribution(),
               "date_range": result["date_range"], "totals_complete": result["totals_complete"],
               "customer_id": result["customer_id"]}
        for key in ("timezone_note", "account_info_error"):
            if key in result:
                out[key] = result[key]
        return out
    except Exception as exc:  # summary feeds the overview: never raise
        return {"platform": PLATFORM, "error": "internal_error", "detail": _safe_message(exc),
                "fix": "unexpected failure in the Google Ads connector; retry, and report it if it persists"}

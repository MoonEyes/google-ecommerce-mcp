"""MCP tools for the Google services. Every tool here is read-only: no endpoint that creates, updates, publishes or
deletes is ever called from this module.

The guarantee does not rest on the readOnlyHint annotation (a client may ignore it): every outgoing request is
checked against ALLOWED_ENDPOINTS before it leaves the process, and anything else is refused without a network
call. tests/test_server.py fails the build if a tool needs an endpoint outside that list. Writes, when enabled, go
only through write_core (plan_* then *_apply) and its own allow-list, never through _send.

Multi-client: every tool takes an optional client_id resolved by clients.resolve() into an immutable
ClientContext passed explicitly down to each request (ids, OAuth token). Without a registry (or without the
clients module) the server runs in legacy mode: ids from the environment (SETTINGS), token from TOKENS, results
unchanged byte for byte."""

from __future__ import annotations

import datetime
import inspect
import json
import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlsplit

import requests
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from .auth import NETWORK_FIX, NotAuthenticated, TokenNetworkError, TokenProvider
from . import ads_config, metrics
from .config import (API_BY_HOST, GOOGLE_ACCOUNTS, GOOGLE_READ_SECRET, SCOPE_BY_HOST, WRITE_CAPABLE_SCOPES,
                     NotConfigured, Settings)

SETTINGS = Settings.from_env()
TOKENS = TokenProvider(SETTINGS.token_file)
HTTP = requests.Session()
TIMEOUT = 60
MAX_ROWS = 1000

mcp = FastMCP(
    "google-ecommerce-mcp",
    instructions=(
        "Shop data from Google (GA4, Search Console, Merchant Center, Tag Manager, PageSpeed) and paid media "
        "(Google Ads, Meta Ads, ChatGPT Ads). Start with server_status; clients_list shows the registered clients.\n"
        "Multi-client: pass client_id to every tool (required when several clients exist); a tool only reads the "
        "ids of that client, and an id of another client is refused. Without a registry, client_id can be omitted.\n"
        "Traffic: ga4_report, ga4_realtime, ga4_properties. Search: gsc_performance (site_url picks one of the "
        "client's sites), gsc_inspect_url, gsc_sitemaps. Feeds: merchant_*. Tags: gtm_inventory. Speed: pagespeed.\n"
        "Paid media: gads_*, meta_*, openai_ads_* and ads_overview; each platform attributes conversions its own "
        "way, so never add them up as real orders.\n"
        "Reads never modify anything. A change is a plan: plan_* builds it, dry-runs it and returns a diff with a "
        "confirmation_token; show the diff to the user, then call the family's *_apply (gads_apply, meta_apply, "
        "openai_ads_apply, google_apply) with the plan_id, the token and confirm=\"<client_id>:<plan_id>:<digest8>\" "
        "only after the user agreed. Writes are off unless enabled for the client; rollback_plan builds the undo.\n"
        "Results carry client_id, totals, the unit of each metric and a date_range with data_complete: days from "
        "settling_from can still change, so do not compare them with complete days."
    ),
    website_url="https://github.com/MoonEyes/google-ecommerce-mcp",
)
mcp._mcp_server.version = __version__  # otherwise clients see the mcp library version

# Every tool only reads from Google: declared to MCP clients through the standard annotations.
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)


def tool(title: str):
    return mcp.tool(title=title, annotations=READ_ONLY)


# ------------------------------------------------------------------ clients (multi-client resolution)
CLIENT_ID = Annotated[str | None, Field(description="Client id from clients_list. Required when several clients "
                                                     "are registered; empty uses the only client (or the "
                                                     "environment variables when there is no client registry)")]


class Refused(Exception):
    """A request refused before any network call (an id the client does not own, no client registry...).
    Rendered like a clients.ClientError: {"error": code, "detail", "fix"}."""

    def __init__(self, code: str, detail: str, fix: str) -> None:
        super().__init__(detail)
        self.code, self.detail, self.fix = code, detail, fix


def _clients_module():
    """The clients module, or None when it is not installed (then only the legacy single-account mode exists)."""
    try:
        from . import clients
    except ImportError:
        return None
    return clients


def _client_errors() -> tuple[type[BaseException], ...]:
    clients = _clients_module()
    error = getattr(clients, "ClientError", None) if clients else None
    return (Refused, error) if isinstance(error, type) else (Refused,)


def client_error(exc: BaseException, client_id: str | None = None) -> dict:
    """ClientError / Refused -> {"error", "detail", "fix"} (+ the client_id that was asked for)."""
    out = {"error": getattr(exc, "code", None) or "client_error",
           "detail": getattr(exc, "detail", None) or str(exc),
           "fix": getattr(exc, "fix", None) or "call clients_list and pass one of its client_id values"}
    if client_id:
        out["client_id"] = client_id
    return out


class ClientTokens(TokenProvider):
    """Google OAuth token of one registered client: read from ctx.secret("google") (keyring user
    "<client_id>/google"), never from GOOGLE_TOKEN_FILE nor the legacy "oauth-token" entry."""

    def __init__(self, ctx: Any) -> None:
        super().__init__(None)
        self._ctx = ctx
        self.client_id = ctx.client_id

    def _load(self) -> None:
        if self._creds is not None:
            return
        from google.oauth2.credentials import Credentials

        raw = self._ctx.secret(GOOGLE_READ_SECRET)
        if not raw:
            raise NotAuthenticated(f"No Google token for client {self.client_id!r}. Run `google-ecommerce-mcp setup "
                                   f"--client {self.client_id} --client-secret <file>` first.")
        self._creds = Credentials.from_authorized_user_info(json.loads(raw))

    def headers(self) -> dict[str, str]:
        self._load()
        return super().headers()

    def granted_scopes(self) -> list[str]:
        try:
            self._load()
        except NotAuthenticated:
            return []
        return super().granted_scopes()


_CLIENT_TOKENS: dict[str, ClientTokens] = {}  # keyed by client_id: one token provider per client, never shared


def _client_tokens(ctx: Any) -> ClientTokens:
    provider = _CLIENT_TOKENS.get(ctx.client_id)
    if provider is None:
        provider = _CLIENT_TOKENS[ctx.client_id] = ClientTokens(ctx)
    return provider


@dataclass(frozen=True)
class Scope:
    """What one tool call may touch: the resolved ClientContext (None in legacy mode) and its token provider.
    Passed explicitly to every request; nothing global is changed."""

    ctx: Any = None

    @property
    def legacy(self) -> bool:
        return self.ctx is None or bool(getattr(self.ctx, "legacy", False))

    @property
    def client_id(self) -> str | None:
        """The client id stamped on results; None in legacy mode, where results stay as they were."""
        return None if self.legacy else self.ctx.client_id

    @property
    def tokens(self) -> TokenProvider:
        return TOKENS if self.legacy else _client_tokens(self.ctx)

    def account(self, service: str) -> str:
        """The configured id of a Google service (ga4, gsc, merchant, gtm). Raises NotConfigured (legacy) or
        ClientError (registry) when it is not set."""
        platform, key, variable, attr = GOOGLE_ACCOUNTS[service]
        if self.legacy:
            return _require(getattr(SETTINGS, attr), variable)
        return self.ctx.account(platform, key)

    def configured(self, service: str) -> str | None:
        """Like account(), but None when the id is not set."""
        try:
            return self.account(service)
        except (NotConfigured, *_client_errors()):
            return None

    def owns(self, service: str, value: str) -> bool:
        """In legacy mode any id is accepted (current behaviour); with a registry, only the client's own ids."""
        if self.legacy:
            return True
        return bool(self.ctx.owns(GOOGLE_ACCOUNTS[service][0], value))

    def require_owned(self, service: str, value: str) -> str:
        """value (normalized by the registry) when the client owns it; raises ClientError / Refused otherwise."""
        if self.legacy:
            return value
        check = getattr(self.ctx, "require_owned", None)
        if callable(check):
            return check(GOOGLE_ACCOUNTS[service][0], value)
        if not self.owns(service, value):
            raise Refused("id_not_owned", f"{value!r} is not a {service} id of client {self.client_id!r}",
                          f"call client_profile(client_id={self.client_id!r}) to see its ids, or add it to the "
                          "client with `google-ecommerce-mcp client add`")
        return value


LEGACY = Scope()


def resolve_scope(client_id: str | None) -> Scope:
    """client_id -> Scope through clients.resolve (raises ClientError). Without the clients module only the
    implicit "default" client exists."""
    client_id = (client_id or "").strip() or None
    clients = _clients_module()
    if clients is None:
        if client_id not in (None, "default"):
            raise Refused("unknown_client", f"unknown client {client_id!r}: no client registry is configured",
                          "omit client_id, or register the client with `google-ecommerce-mcp client add`")
        return LEGACY
    return Scope(clients.resolve(client_id))


def _as_scope(value: Any) -> Scope:
    """A tool body receives the Scope resolved by _guard; called directly (tests), it resolves the id itself."""
    return value if isinstance(value, Scope) else resolve_scope(value)


# ------------------------------------------------------------------ allow-list (the real read-only guarantee)
# Every request this server may send. POST appears only on query endpoints that read data.
# Adding a tool that needs another endpoint means adding it here, where review can see it.
ALLOWED_ENDPOINTS = [
    ("POST", r"https://analyticsdata\.googleapis\.com/v1beta/properties/[^/]+:runReport"),
    ("POST", r"https://analyticsdata\.googleapis\.com/v1beta/properties/[^/]+:runRealtimeReport"),
    ("GET", r"https://analyticsadmin\.googleapis\.com/v1beta/accountSummaries"),
    ("POST", r"https://www\.googleapis\.com/webmasters/v3/sites/[^/]+/searchAnalytics/query"),
    ("GET", r"https://www\.googleapis\.com/webmasters/v3/sites/[^/]+/sitemaps"),
    ("POST", r"https://searchconsole\.googleapis\.com/v1/urlInspection/index:inspect"),
    ("GET", r"https://merchantapi\.googleapis\.com/datasources/v1/accounts/[^/]+/dataSources"),
    ("GET", r"https://merchantapi\.googleapis\.com/products/v1/accounts/[^/]+/products"),
    ("GET", r"https://merchantapi\.googleapis\.com/products/v1/accounts/[^/]+/products/[^/]+"),
    ("POST", r"https://merchantapi\.googleapis\.com/reports/v1/accounts/[^/]+/reports:search"),
    ("GET", r"https://tagmanager\.googleapis\.com/tagmanager/v2/accounts"),
    ("GET", r"https://tagmanager\.googleapis\.com/tagmanager/v2/accounts/[^/]+/containers"),
    ("GET", r"https://tagmanager\.googleapis\.com/tagmanager/v2/accounts/[^/]+/containers/[^/]+/workspaces"),
    ("GET", r"https://tagmanager\.googleapis\.com/tagmanager/v2/accounts/[^/]+/containers/[^/]+/workspaces/[^/]+/(tags|triggers|variables)"),
    ("GET", r"https://tagmanager\.googleapis\.com/tagmanager/v2/accounts/[^/]+/containers/[^/]+/versions:live"),
    ("GET", r"https://indexing\.googleapis\.com/v3/urlNotifications/metadata"),
    ("GET", r"https://pagespeedonline\.googleapis\.com/pagespeedonline/v5/runPagespeed"),
]
_ALLOWED = [(m, re.compile(pattern + r"$")) for m, pattern in ALLOWED_ENDPOINTS]


def is_allowed(method: str, url: str) -> bool:
    parts = urlsplit(url)
    bare = f"{parts.scheme}://{parts.netloc}{parts.path}"
    return any(method.upper() == m and rx.match(bare) for m, rx in _ALLOWED)


class Blocked(Exception):
    """Raised when code tries to send a request outside ALLOWED_ENDPOINTS."""


BLOCKED_ATTEMPTS: list[tuple[str, str]] = []  # kept so the test suite can fail the build on any attempt


def _send(method: str, url: str, **kwargs):
    if not is_allowed(method, url):
        BLOCKED_ATTEMPTS.append((method, url))
        raise Blocked(f"{method} {url} is not on the read-only allow-list; request not sent")
    return HTTP.request(method, url, **kwargs)


# ------------------------------------------------------------------ helpers
def _require(value: str | None, variable: str) -> str:
    if not value:
        raise NotConfigured(variable)
    return value


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _scope_for(url: str) -> str | None:
    return next((scope for host, scope in SCOPE_BY_HOST.items() if host in url), None)


def _api_for(url: str) -> str:
    return next((api for host, api in API_BY_HOST.items() if host in url), "the API this tool calls")


def _api_error(response, data, url: str) -> dict:
    """Structured error: the caller learns what is missing and what to do, not just a status code."""
    detail = data.get("error", data) if isinstance(data, dict) else data
    text = str(detail)
    if response.status_code == 403 and ("SCOPE_INSUFFICIENT" in text or "insufficient authentication scopes" in text):
        scope = _scope_for(url)
        return {"error": "missing_scope", "required_scope": scope,
                "missing_scopes": [scope] if scope else [],
                "fix": "rerun `google-ecommerce-mcp setup` and grant this scope"
                       + (" (add --with-merchant or --with-indexing)" if scope in WRITE_CAPABLE_SCOPES else ""),
                "detail": detail}
    if response.status_code == 403 and ("SERVICE_DISABLED" in text or "has not been used in project" in text
                                        or "it is disabled" in text):
        return {"error": "api_disabled", "api": _api_for(url),
                "fix": f"enable {_api_for(url)} in Google Cloud Console (APIs & Services > Library) for the project "
                       "of your OAuth client, wait a few minutes, then retry",
                "detail": detail}
    if response.status_code == 429:
        headers = getattr(response, "headers", None) or {}
        return {"error": "rate_limited", "retry_after_seconds": headers.get("Retry-After"),
                "fix": "wait and retry, or narrow the request (shorter date range, smaller limit)", "detail": detail}
    return {"error": response.status_code, "detail": detail}


def _missing_scopes(errors: list[dict]) -> list[str]:
    """Every scope named by a `missing_scope` error in a partial result, once each, in order."""
    found: list[str] = []
    for error in errors:
        for scope in error.get("missing_scopes", []):
            if scope not in found:
                found.append(scope)
    return found


def _call(method: str, url: str, *, scope: Scope | None = None, **kwargs) -> dict:
    """One Google request with the token of `scope` (the legacy TOKENS when omitted)."""
    try:
        response = _send(method, url, headers=(scope or LEGACY).tokens.headers(), timeout=TIMEOUT, **kwargs)
    except Blocked as exc:
        return {"error": "blocked", "detail": str(exc)}
    except TokenNetworkError as exc:  # token refresh failed on the network: not an authentication problem
        return {"error": "network", "detail": str(exc), "fix": NETWORK_FIX}
    except NotAuthenticated as exc:
        return {"error": "not_authenticated", "detail": str(exc),
                "fix": "rerun `google-ecommerce-mcp setup --client-secret <file>` (or the install line)"}
    except requests.RequestException as exc:
        return {"error": "network", "detail": str(exc)}
    try:
        data = response.json()
    except ValueError:
        data = {"text": response.text[:500]}
    if not response.ok:
        return _api_error(response, data, url)
    return data


def _guard(freshness: str, *, client: bool = True):
    """Turn configuration and client errors into a readable result, and stamp every result with when it was read
    and how fresh the underlying Google data is, so the assistant never has to guess.

    With client=True the tool's client_id argument is resolved once here into a Scope, handed to the tool body in
    place of the string, and stamped on the result (registry mode only: legacy results are unchanged)."""

    def decorate(fn):
        signature = inspect.signature(fn)

        def wrapper(*args, **kwargs):
            scope: Scope | None = None
            requested = None
            try:
                if client and "client_id" in signature.parameters:
                    bound = signature.bind(*args, **kwargs)
                    requested = bound.arguments.get("client_id")
                    scope = _as_scope(requested)
                    bound.arguments["client_id"] = scope
                    args, kwargs = bound.args, bound.kwargs
                result = fn(*args, **kwargs)
            except NotConfigured as exc:
                result = {"error": "not_configured", "detail": str(exc)}
            except _client_errors() as exc:
                result = client_error(exc, scope.client_id if scope and scope.client_id else requested)
            if isinstance(result, dict):
                if scope is not None and scope.client_id:
                    result.setdefault("client_id", scope.client_id)
                result.setdefault("fetched_at", _now())
                result.setdefault("freshness", freshness)
            return result

        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        wrapper.__annotations__ = fn.__annotations__
        wrapper.__wrapped__ = fn
        return wrapper

    return decorate


def _cap(limit: int) -> int:
    return max(1, min(int(limit), MAX_ROWS))


GA4_SETTLING_NOTE = "GA4 can still revise the last 24 to 48 hours; compare complete days only"
GSC_SETTLING_NOTE = "Search Console fills the last 2 days late; recent days may be missing or partial"


def _ga4_rows(resp: dict, limit: int, date_range: dict | None = None) -> dict:
    """Flatten a GA4 response: numbers as numbers, totals, unit and definition per metric, timezone, currency."""
    if "error" in resp:
        return resp
    dims = [d["name"] for d in resp.get("dimensionHeaders", [])]
    headers = resp.get("metricHeaders", [])
    mets = [m["name"] for m in headers]
    types = [m.get("type", "") for m in headers]
    meta = resp.get("metadata", {})

    def values(row: dict) -> dict:
        return {name: metrics.to_number(v.get("value"), kind)
                for name, kind, v in zip(mets, types, row.get("metricValues", []))}

    rows = []
    for row in resp.get("rows", [])[:limit]:
        item = dict(zip(dims, (v["value"] for v in row.get("dimensionValues", []))))
        item.update(values(row))
        rows.append(item)
    row_count = resp.get("rowCount", len(rows))
    out = {"rows": rows, "row_count": row_count, "truncated": row_count > len(rows)}
    if resp.get("totals"):
        out["totals"] = values(resp["totals"][0])
    out["metrics"] = metrics.ga4_metric_info(headers, meta.get("currencyCode"))
    if meta.get("currencyCode"):
        out["currency"] = meta["currencyCode"]
    if date_range:
        out["date_range"] = date_range
    return out


def metrics_date_range(start: str, end: str, timezone: str | None, note: str) -> dict:
    return metrics.date_range_info(start, end, timezone, settle_days=2, note=note)


def _ga4_property(property_id: str, scope: Scope | None = None) -> str:
    """The property to query: the one passed (from ga4_properties) or the configured one (GA4_PROPERTY_ID, or the
    client's ga4.property_id). With a registry, a property passed explicitly must belong to the client."""
    scope = scope or LEGACY
    prop = (property_id or "").strip().removeprefix("properties/")
    if prop:
        if not prop.isdigit():
            raise ValueError(f"property_id must be the numeric GA4 property id, got {property_id!r}")
        return scope.require_owned("ga4", prop)
    return scope.account("ga4")


def _service_status(scope: Scope) -> dict:
    """Same keys as Settings.status(), for one registered client (booleans only)."""
    return {
        "ga4": bool(scope.configured("ga4")),
        "search_console": bool(scope.configured("gsc")),
        "merchant_center": bool(scope.configured("merchant")),
        "tag_manager": bool(scope.configured("gtm")),
        "pagespeed_api_key": bool(_client_secret(scope, "pagespeed") or SETTINGS.pagespeed_api_key),
        "token_storage": "client secret store",
    }


def _client_ads_status(scope: Scope) -> dict:
    """Booleans only: which paid-media accounts the client declares (never an id nor a secret)."""
    ctx = scope.ctx

    def has(platform: str, key: str) -> bool:
        try:
            return bool(ctx.account(platform, key))
        except Exception:
            return False

    return {"google_ads": has("google_ads", "customer_id"), "meta": has("meta", "ad_account_id"),
            "openai_ads": has("openai_ads", "account_id")}


def check(scope: Scope | None = None) -> dict:
    """Configuration status plus a live token test (used by `google-ecommerce-mcp check` and server_status).
    Without a scope (or in legacy mode) the output is the historical one, built from the environment."""
    scope = scope or LEGACY
    status = SETTINGS.status() if scope.legacy else _service_status(scope)
    tokens = scope.tokens
    try:
        tokens.headers()
        status["token"] = "ok"
    except TokenNetworkError as exc:
        status["token"] = f"network error: {exc}"
    except NotAuthenticated as exc:
        status["token"] = str(exc)
    except Exception as exc:  # refresh failures, revoked tokens
        status["token"] = f"error: {exc}"
    try:
        granted = tokens.granted_scopes()
    except Exception:
        granted = []
    status["scopes"] = granted
    status["write_capable_scopes"] = sorted(set(granted) & WRITE_CAPABLE_SCOPES)
    if scope.legacy:
        status["ads"] = ads_config.AdsSettings.from_env().status()  # booleans only, never a secret value
    else:
        status["ads"] = _client_ads_status(scope)
        status["client_id"] = scope.client_id
        status["label"] = getattr(scope.ctx, "label", None)
        status["sandbox"] = bool(getattr(scope.ctx, "sandbox", False))
    status["ads_write_capable_scope"] = ads_config.ADS_SCOPE_NOTE
    status["version"] = __version__
    return status


# ------------------------------------------------------------------ server info
@tool("Server status")
@_guard("live: token tested now")
def server_status(client_id: CLIENT_ID = None) -> dict:
    """Which Google services are configured for this server (or for one client), and whether the stored OAuth
    token works. Call it first when another tool answers not_configured or not_authenticated. Returns one boolean
    per service, the token storage (os-keyring or file), "token": "ok" or the error, and the package version."""
    return check(_as_scope(client_id))


# ------------------------------------------------------------------ GA4
PROPERTY_ID = Annotated[str, Field(description="Numeric GA4 property id, as listed by ga4_properties; "
                                            "empty uses the configured GA4_PROPERTY_ID (or the client's property)")]


@tool("GA4 report")
@_guard("GA4 processed data; the last 24 to 48 hours can still change")
def ga4_report(
    dimensions: Annotated[list[str], Field(description="GA4 API dimension names, e.g. sessionDefaultChannelGroup, "
                                           "landingPage, sessionSource, yearMonth, itemName")] = ["sessionDefaultChannelGroup"],
    metrics: Annotated[list[str], Field(description="GA4 API metric names, e.g. sessions, totalUsers, "
                                        "ecommercePurchases, purchaseRevenue, keyEvents")] = ["sessions", "totalUsers"],
    start_date: Annotated[str, Field(description="YYYY-MM-DD, NdaysAgo, yesterday or today")] = "28daysAgo",
    end_date: Annotated[str, Field(description="YYYY-MM-DD, NdaysAgo, yesterday or today")] = "yesterday",
    limit: Annotated[int, Field(description="Maximum rows returned (1 to 1000); totals always cover every row",
                                ge=1, le=1000)] = 50,
    channel_group: Annotated[str, Field(description="Optional exact filter on sessionDefaultChannelGroup, "
                                        "e.g. 'Organic Search'; empty for all channels")] = "",
    property_id: PROPERTY_ID = "",
    client_id: CLIENT_ID = None,
) -> dict:
    """Run a Google Analytics 4 report (Data API runReport): traffic, conversions or revenue split by any dimensions
    over a date range. Returns the top rows ({"rows": [{dimension: value, metric: number}]}), "totals" over every
    row, "row_count" and "truncated", the unit and definition of each metric under "metrics", the currency, and a
    "date_range" resolved to calendar dates in the property's timezone with "data_complete". Use ga4_realtime for the
    last 30 minutes."""
    scope = _as_scope(client_id)
    try:
        prop = _ga4_property(property_id, scope)
    except ValueError as exc:
        return {"error": "invalid_property_id", "detail": str(exc)}
    limit = _cap(limit)
    body = {"dateRanges": [{"startDate": start_date, "endDate": end_date}],
            "dimensions": [{"name": d} for d in dimensions], "metrics": [{"name": m} for m in metrics],
            "limit": limit, "metricAggregations": ["TOTAL"]}
    if channel_group:
        body["dimensionFilter"] = {"filter": {"fieldName": "sessionDefaultChannelGroup",
                                              "stringFilter": {"value": channel_group}}}
    resp = _call("POST", f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport", json=body,
                 scope=scope)
    if "error" in resp:
        return resp
    timezone = resp.get("metadata", {}).get("timeZone")
    out = _ga4_rows(resp, limit, metrics_date_range(start_date, end_date, timezone, GA4_SETTLING_NOTE))
    out["property_id"] = prop
    return out


@tool("GA4 realtime")
@_guard("realtime: last 30 minutes")
def ga4_realtime(
    dimensions: Annotated[list[str], Field(description="GA4 realtime dimensions, e.g. unifiedScreenName, "
                                           "country, deviceCategory")] = ["unifiedScreenName"],
    metrics: Annotated[list[str], Field(description="GA4 realtime metrics, e.g. activeUsers, screenPageViews, "
                                        "eventCount")] = ["activeUsers", "screenPageViews"],
    property_id: PROPERTY_ID = "",
    client_id: CLIENT_ID = None,
) -> dict:
    """GA4 realtime report for the last 30 minutes. Useful to check a tracking change, for example that one page
    view is counted once and not twice. Returns {"rows": [...], "row_count": n, "totals": {...}, "metrics": {...}}."""
    scope = _as_scope(client_id)
    try:
        prop = _ga4_property(property_id, scope)
    except ValueError as exc:
        return {"error": "invalid_property_id", "detail": str(exc)}
    body = {"dimensions": [{"name": d} for d in dimensions], "metrics": [{"name": m} for m in metrics],
            "metricAggregations": ["TOTAL"]}
    out = _ga4_rows(_call("POST", f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runRealtimeReport",
                          json=body, scope=scope), 100, {"start": "30 minutes ago", "end": "now", "data_complete": False,
                                                         "settling_note": "realtime counts keep moving"})
    if "error" not in out:
        out["property_id"] = prop
    return out


@tool("GA4 properties")
@_guard("current state")
def ga4_properties(
    max_pages: Annotated[int, Field(description="Pages of 200 accounts to read at most", ge=1, le=20)] = 5,
    client_id: CLIENT_ID = None,
) -> dict:
    """GA4 accounts and properties the authorized Google account can read (Admin API accountSummaries), so the
    assistant can find a property id itself and pass it as property_id to ga4_report or ga4_realtime. Returns
    {"properties": [{property_id, property_name, property_type, account_id, account_name, configured}],
    "configured_property_id"}. Needs the Google Analytics Admin API enabled; no extra OAuth scope. With a client
    registry each property also says whether it belongs to the client ("owned"): only those can be queried."""
    scope = _as_scope(client_id)
    configured = SETTINGS.ga4_property_id if scope.legacy else scope.configured("ga4")
    url = "https://analyticsadmin.googleapis.com/v1beta/accountSummaries"
    found, token, pages = [], None, 0
    while pages < max_pages:
        params = {"pageSize": 200}
        if token:
            params["pageToken"] = token
        resp = _call("GET", url, params=params, scope=scope)
        if "error" in resp:
            if not pages:
                return resp
            return {"properties": found, "configured_property_id": configured,
                    "truncated": True, "partial": True, "partial_error": resp}
        pages += 1
        for account in resp.get("accountSummaries", []):
            account_id = account.get("account", "").removeprefix("accounts/")
            for prop in account.get("propertySummaries", []):
                prop_id = prop.get("property", "").removeprefix("properties/")
                item = {"property_id": prop_id, "property_name": prop.get("displayName"),
                        "property_type": prop.get("propertyType"), "account_id": account_id,
                        "account_name": account.get("displayName"), "configured": prop_id == configured}
                if not scope.legacy:
                    item["owned"] = scope.owns("ga4", prop_id)
                found.append(item)
        token = resp.get("nextPageToken")
        if not token:
            break
    return {"properties": found, "configured_property_id": configured, "truncated": bool(token)}


# ------------------------------------------------------------------ Search Console
SITE_URL = Annotated[str, Field(description="Search Console property, e.g. sc-domain:example.com or "
                                         "https://www.example.com/; empty uses the configured GSC_SITE_URL (or the "
                                         "client's site). With a client registry it must be one of the client's sites")]
SEARCH_TYPE = Annotated[Literal["web", "image", "video", "news", "discover", "googleNews"],
                        Field(description="Search type (sent to Google as `type`): web, image, video, news, discover "
                                          "or googleNews")]


def site_path(site_url: str) -> str:
    """Percent-encode a Search Console property for a URL path segment: every reserved character, including ':' and
    '/', so sc-domain:example.com becomes sc-domain%3Aexample.com and https://x/ becomes https%3A%2F%2Fx%2F."""
    return quote(site_url, safe="")


def _site_url(site_url: str, scope: Scope) -> str:
    """The raw (not encoded) property: the one passed, checked against the client's sites, or the configured one."""
    site = (site_url or "").strip()
    if site:
        return scope.require_owned("gsc", site)
    return scope.account("gsc")


def _site(scope: Scope | None = None, site_url: str = "") -> str:
    return site_path(_site_url(site_url, scope or LEGACY))


@tool("Search Console performance")
@_guard("Search Console data lags about 2 days")
def gsc_performance(
    dimensions: Annotated[list[Literal["query", "page", "country", "device", "date", "searchAppearance"]],
                          Field(description="How to split the results")] = ["query"],
    start_date: Annotated[str, Field(description="YYYY-MM-DD; empty means 30 days ago (Pacific Time)")] = "",
    end_date: Annotated[str, Field(description="YYYY-MM-DD; empty means 2 days ago (Search Console data lag)")] = "",
    limit: Annotated[int, Field(description="Maximum rows returned (1 to 1000); totals always cover the whole "
                                "property", ge=1, le=1000)] = 50,
    page_contains: Annotated[str, Field(description="Optional: keep only pages whose URL contains this text")] = "",
    search_type: SEARCH_TYPE = "web",
    site_url: SITE_URL = "",
    client_id: CLIENT_ID = None,
) -> dict:
    """Google Search Console search performance for the configured property: clicks, impressions, CTR and average
    position, split by query, page, country, device or date. Returns the top rows, "totals" for the whole period
    (same filter, no split), "truncated", the unit and definition of each metric under "metrics", and a
    "date_range" in Pacific Time (Search Console's timezone) with "data_complete"."""
    scope = _as_scope(client_id)
    site = _site(scope, site_url)
    limit = _cap(limit)
    today, _ = metrics.today_in(metrics.GSC_TIMEZONE)
    start = start_date or (today - datetime.timedelta(days=30)).isoformat()
    end = end_date or (today - datetime.timedelta(days=2)).isoformat()
    body = {"startDate": start, "endDate": end, "dimensions": dimensions, "rowLimit": limit}
    if search_type and search_type != "web":
        body["type"] = search_type  # `searchType` is deprecated; web is Google's default, so it is not sent
    if page_contains:
        body["dimensionFilterGroups"] = [{"filters": [{"dimension": "page", "operator": "contains",
                                                       "expression": page_contains}]}]
    url = f"https://www.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query"
    resp = _call("POST", url, json=body, scope=scope)
    if "error" in resp:
        return resp
    rows = [dict(zip(dimensions, r["keys"]), clicks=r["clicks"], impressions=r["impressions"],
                 ctr=round(r["ctr"], 4), position=round(r["position"], 1)) for r in resp.get("rows", [])]
    out = {"rows": rows, "truncated": len(rows) >= limit}
    # Totals come from the same query without dimensions: summing the rows would miss the rows beyond the limit and
    # the queries Google anonymizes, so row sums are always lower than the real total.
    totals = _call("POST", url, json={k: v for k, v in body.items() if k not in ("dimensions", "rowLimit")},
                   scope=scope)
    if "error" in totals:
        out.update(partial=True, partial_error={"step": "totals", **totals})
    else:
        t = (totals.get("rows") or [{"clicks": 0, "impressions": 0, "ctr": 0, "position": 0}])[0]
        out["totals"] = {"clicks": t["clicks"], "impressions": t["impressions"], "ctr": round(t["ctr"], 4),
                         "position": round(t["position"], 1)}
        if "query" in dimensions:
            out["totals_note"] = "totals include anonymized queries, so they exceed the sum of query rows"
    out["metrics"] = metrics.GSC_METRICS
    out["date_range"] = metrics_date_range(start, end, metrics.GSC_TIMEZONE, GSC_SETTLING_NOTE)
    if site_url:
        out["site_url"] = _site_url(site_url, scope)
    return out


@tool("Search Console URL inspection")
@_guard("Google's current index state; see lastCrawlTime for the crawl date")
def gsc_inspect_url(
    url: Annotated[str, Field(description="Full URL to inspect; must belong to the configured Search Console property")],
    site_url: SITE_URL = "",
    client_id: CLIENT_ID = None,
) -> dict:
    """Google index status of one URL (URL Inspection API): verdict, coverage state, user and Google-selected
    canonical, robots.txt state, page fetch state and last crawl time. Quota: 2,000 inspections per day."""
    scope = _as_scope(client_id)
    site = _site_url(site_url, scope)  # sent in the JSON body, so not percent-encoded
    resp = _call("POST", "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect",
                 json={"inspectionUrl": url, "siteUrl": site}, scope=scope)
    return resp if "error" in resp else resp.get("inspectionResult", {}).get("indexStatusResult", resp)


@tool("Search Console sitemaps")
@_guard("current state")
def gsc_sitemaps(site_url: SITE_URL = "", client_id: CLIENT_ID = None) -> dict:
    """Sitemaps declared in Search Console, with last submission, last download, errors and warnings."""
    scope = _as_scope(client_id)
    return _call("GET", f"https://www.googleapis.com/webmasters/v3/sites/{_site(scope, site_url)}/sitemaps",
                 scope=scope)


# ------------------------------------------------------------------ Merchant Center (Merchant API v1)
MERCHANT = "https://merchantapi.googleapis.com"


@tool("Merchant Center data sources")
@_guard("current state")
def merchant_data_sources(client_id: CLIENT_ID = None) -> dict:
    """Merchant Center data sources (feeds): labels, countries, languages, fetch URLs."""
    scope = _as_scope(client_id)
    acc = scope.account("merchant")
    return _call("GET", f"{MERCHANT}/datasources/v1/accounts/{acc}/dataSources", scope=scope)


@tool("Merchant Center product issues")
@_guard("current state; issues update a few hours after a feed fetch")
def merchant_product_issues(
    limit: Annotated[int, Field(description="Maximum products returned (1 to 1000)", ge=1, le=1000)] = 50,
    only_with_issues: Annotated[bool, Field(description="True: only products with at least one issue; "
                                            "False: every product")] = True,
    max_pages: Annotated[int, Field(description="Pages of 250 products to scan at most", ge=1, le=200)] = 20,
    client_id: CLIENT_ID = None,
) -> dict:
    """Merchant Center products with their item-level issues (disapprovals, demotions, warnings) per destination,
    with the attribute to fix. Scans every page of products up to max_pages. Returns {"products": [{offer_id,
    feed_label, title, link, issues: [{code, severity, description, attribute, destination}]}], "scanned": n,
    "truncated": bool}."""
    scope = _as_scope(client_id)
    acc = scope.account("merchant")
    limit = _cap(limit)
    out, token, pages, scanned = [], None, 0, 0
    while pages < max(1, max_pages):
        params = {"pageSize": 250}
        if token:
            params["pageToken"] = token
        resp = _call("GET", f"{MERCHANT}/products/v1/accounts/{acc}/products", params=params, scope=scope)
        if "error" in resp:
            if not pages:
                return resp
            # Keep what was already scanned: a quota hit on page 7 should not throw away pages 1 to 6.
            return {"products": out, "scanned": scanned, "truncated": True, "partial": True, "partial_error": resp}
        pages += 1
        for product in resp.get("products", []):
            scanned += 1
            issues = [{"code": i.get("code"), "severity": i.get("severity"), "description": i.get("description"),
                       "attribute": i.get("attribute"), "destination": i.get("reportingContext")}
                      for i in product.get("productStatus", {}).get("itemLevelIssues", [])]
            if only_with_issues and not issues:
                continue
            attrs = product.get("productAttributes", product.get("attributes", {}))
            out.append({"offer_id": product.get("offerId"), "feed_label": product.get("feedLabel"),
                        "title": attrs.get("title"), "link": attrs.get("link"), "issues": issues})
            if len(out) >= limit:
                return {"products": out, "scanned": scanned, "truncated": True}
        token = resp.get("nextPageToken")
        if not token:
            break
    return {"products": out, "scanned": scanned, "truncated": bool(token)}


@tool("Merchant Center report query")
@_guard("performance tables lag about 1 day; product_view is current")
def merchant_report_query(
    query: Annotated[str, Field(description="Merchant Center Query Language (MCQL) statement. Tables include "
                                "product_view (must select id) and product_performance_view (clicks, impressions "
                                "by date or offer)")] =
        "SELECT id, offer_id, title, aggregated_reporting_context_status FROM product_view LIMIT 50",
    client_id: CLIENT_ID = None,
) -> dict:
    """Run a Merchant Center report (Merchant API reports:search) with an MCQL query, for example product status
    or free-listing performance. Returns Google's raw result rows."""
    scope = _as_scope(client_id)
    acc = scope.account("merchant")
    return _call("POST", f"{MERCHANT}/reports/v1/accounts/{acc}/reports:search", json={"query": query}, scope=scope)


# ------------------------------------------------------------------ Tag Manager (read only)
GTM_API = "https://tagmanager.googleapis.com/tagmanager/v2"
BUILT_IN_TRIGGERS = {"2147479553": "All Pages (built-in)", "2147479573": "Initialization - All Pages (built-in)",
                     "2147479572": "Consent Initialization - All Pages (built-in)"}


def _gtm_target(scope: Scope) -> tuple[str, str | None]:
    """(container id, GTM account id or None). Legacy: GTM_CONTAINER_ID, a public id GTM-XXXX. Registry: the
    client's gtm.public_id (GTM-XXXX), else its numeric gtm.container_id, and its gtm.account_id when set."""
    if scope.legacy:
        return scope.account("gtm"), None

    def get(key: str) -> str | None:
        try:
            return scope.ctx.account("gtm", key) or None
        except Exception:
            return None

    container = get("public_id") or scope.account("gtm")  # account() raises platform_not_configured
    return container, get("account_id")


def _gtm_matches(container: dict, wanted: str, scope: Scope) -> bool:
    if scope.legacy:
        return container.get("publicId") == wanted
    return wanted in (container.get("publicId"), container.get("containerId"))


@tool("Tag Manager inventory")
@_guard("current workspace state, which may differ from the live version")
def gtm_inventory(client_id: CLIENT_ID = None) -> dict:
    """Tags (type, paused, firing triggers, parameters), triggers and variables of the configured Google Tag Manager
    container's default workspace, plus the id of the live published version. Built-in trigger ids are translated,
    e.g. All Pages. Useful to spot a tag configured twice."""
    scope = _as_scope(client_id)
    public_id, account_id = _gtm_target(scope)
    accounts = _call("GET", f"{GTM_API}/accounts", scope=scope)
    if "error" in accounts:
        return accounts
    errors: list[dict] = []

    def fetch(step: str, url: str, key: str) -> list:
        """One sub-request; on failure record which part is missing instead of returning an empty list silently."""
        resp = _call("GET", url, scope=scope)
        if "error" in resp:
            errors.append({"step": step, **resp})
            return []
        return resp.get(key, [])

    for account in accounts.get("account", []):
        if account_id and account.get("accountId", account.get("path", "").removeprefix("accounts/")) != account_id:
            continue  # registry mode: never even list the containers of another GTM account
        for container in fetch(f"containers of {account['path']}", f"{GTM_API}/{account['path']}/containers", "container"):
            if not _gtm_matches(container, public_id, scope):
                continue
            workspaces = fetch("workspaces", f"{GTM_API}/{container['path']}/workspaces", "workspace")
            if not workspaces:
                return {"error": "no_workspace", "partial_errors": errors}
            base = f"{GTM_API}/{workspaces[0]['path']}"
            triggers = dict(BUILT_IN_TRIGGERS)
            triggers.update({t["triggerId"]: t["name"] for t in fetch("triggers", f"{base}/triggers", "trigger")})
            tags = [{"name": t["name"], "type": t["type"], "paused": t.get("paused", False),
                     "firing_triggers": [triggers.get(i, i) for i in t.get("firingTriggerId", [])],
                     "parameters": {p["key"]: p.get("value") for p in t.get("parameter", []) if "value" in p}}
                    for t in fetch("tags", f"{base}/tags", "tag")]
            variables = [v["name"] for v in fetch("variables", f"{base}/variables", "variable")]
            live = _call("GET", f"{GTM_API}/{container['path']}/versions:live", scope=scope)
            if "error" in live:
                errors.append({"step": "live version", **live})
                live = {}
            result = {"container": public_id, "workspace": workspaces[0].get("name"), "tags": tags,
                      "triggers": sorted(set(triggers.values())), "variables": variables,
                      "live_version": {"id": live.get("containerVersionId"), "name": live.get("name")}}
            if errors:
                result.update(partial=True, partial_errors=errors, missing_scopes=_missing_scopes(errors))
            return result
    if errors:
        return {"error": "container_not_found", "detail": f"{public_id} not found; some accounts could not be read",
                "partial_errors": errors}
    return {"error": "container_not_found", "detail": f"{public_id} is not visible to the authorized Google account"}


# ------------------------------------------------------------------ Indexing API + PageSpeed
def _client_secret(scope: Scope, kind: str) -> str | None:
    try:
        return scope.ctx.secret(kind)
    except Exception:  # unknown kind, no keyring backend: behave as if absent
        return None


PAGESPEED_URL = "https://pagespeedonline.googleapis.com/pagespeedonline/v5/runPagespeed"


@tool("Indexing API status")
@_guard("current state")
def indexing_status(
    url: Annotated[str, Field(description="Full URL to look up")],
    client_id: CLIENT_ID = None,
) -> dict:
    """Latest Indexing API notifications (URL_UPDATED / URL_DELETED) Google holds for a URL. Read only: nothing is
    submitted. Most URLs were never notified, which is reported as such."""
    scope = _as_scope(client_id)
    resp = _call("GET", "https://indexing.googleapis.com/v3/urlNotifications/metadata", params={"url": url},
                 scope=scope)
    if resp.get("error") == 404:
        return {"status": "no Indexing API notification has been sent for this URL"}
    return resp


@tool("PageSpeed Insights")
@_guard("lab test run now; field_data covers the last 28 days")
def pagespeed(
    url: Annotated[str, Field(description="Full public URL to test")],
    strategy: Annotated[Literal["mobile", "desktop"], Field(description="Device profile Lighthouse emulates")] = "mobile",
    client_id: CLIENT_ID = None,
) -> dict:
    """Lighthouse performance and SEO scores (0 to 100) plus LCP, CLS and TBT for a URL, and the Chrome UX Report
    field category when Google has real-user data. Set PAGESPEED_API_KEY to avoid the shared anonymous quota."""
    scope = _as_scope(client_id)  # PageSpeed reads public pages with an API key, never an OAuth token
    params = {"url": url, "strategy": strategy, "category": ["performance", "seo"]}
    key = SETTINGS.pagespeed_api_key if scope.legacy else (_client_secret(scope, "pagespeed")
                                                            or SETTINGS.pagespeed_api_key)
    if key:
        params["key"] = key
    try:
        data = _send("GET", PAGESPEED_URL, params=params, timeout=120).json()
    except Blocked as exc:
        return {"error": "blocked", "detail": str(exc)}
    except (requests.RequestException, ValueError) as exc:
        return {"error": "network", "detail": str(exc)}
    if "error" in data:
        message = data["error"].get("message", "")
        hint = "set PAGESPEED_API_KEY (Google Cloud API key with the PageSpeed Insights API enabled)" if "Quota" in message else ""
        return {"error": message, "hint": hint}
    lh, audits = data["lighthouseResult"], data["lighthouseResult"]["audits"]
    out = {"performance": round(lh["categories"]["performance"]["score"] * 100),
           "seo": round(lh["categories"]["seo"]["score"] * 100),
           "lcp": audits["largest-contentful-paint"]["displayValue"],
           "cls": audits["cumulative-layout-shift"]["displayValue"],
           "tbt": audits["total-blocking-time"]["displayValue"],
           "field_data": data.get("loadingExperience", {}).get("overall_category")}
    field = _field_metrics(data.get("loadingExperience"))
    if field:
        out["field_metrics"] = field
    return out


# CrUX field metrics of the PageSpeed Insights API v5 (loadingExperience.metrics.<NAME>.percentile, the 75th
# percentile over the last 28 days). LCP and INP percentiles are in milliseconds (assumed from the metric names).
# CUMULATIVE_LAYOUT_SHIFT_SCORE is assumed to be sent multiplied by 100 (percentile 5 means CLS 0.05): the API
# reference types every percentile as an integer, which only makes sense for CLS if it is scaled, but neither the
# reference page nor the "about" page states the scale, and no live response was checked (the shared anonymous
# quota was exhausted on 2026-10-17). Divided by 100 here; confirm against a real response with PAGESPEED_API_KEY.
CRUX_METRICS = (("lcp", "LARGEST_CONTENTFUL_PAINT_MS", "lcp_ms", 1), ("inp", "INTERACTION_TO_NEXT_PAINT", "inp_ms", 1),
                ("cls", "CUMULATIVE_LAYOUT_SHIFT_SCORE", "cls", 100))


def _field_metrics(experience) -> dict | None:
    """{lcp_ms, inp_ms, cls, lcp_category, inp_category, cls_category, origin_fallback} from loadingExperience, only
    the values Google returned; None when the API has no field data (not enough real-user traffic for the URL nor
    for its origin)."""
    metrics = experience.get("metrics") if isinstance(experience, dict) else None
    if not isinstance(metrics, dict):
        return None
    out: dict = {}
    for short, name, key, divisor in CRUX_METRICS:
        entry = metrics.get(name)
        if not isinstance(entry, dict):
            continue
        value = entry.get("percentile")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = round(value / divisor, 4) if divisor != 1 else value
        if isinstance(entry.get("category"), str) and entry["category"]:
            out[f"{short}_category"] = entry["category"]
    if not any(k in out for _, _, k, _ in CRUX_METRICS):
        return None
    # origin_fallback: Google had too little data for this URL and answered for the whole origin instead.
    out["origin_fallback"] = experience.get("origin_fallback") is True
    return out


# ------------------------------------------------------------------ every other tool (paid media, clients, writes)
# Each module owns its allow-list and its HTTP client (see ads_common.py); nothing here sends a request.
from . import registry  # noqa: E402  (imported last: registry reads this module's helpers)

REGISTERED = registry.register_all(mcp)

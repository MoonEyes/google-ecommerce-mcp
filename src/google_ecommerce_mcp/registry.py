"""Every tool outside server.py, registered in one place.

register_all(mcp) registers, in this order:
1. the paid-media read modules that always ship (google_ads, meta_ads, openai_ads, ads_overview);
2. the optional platform and write modules, each only if it is installed (a module that is absent is skipped; a
   module that exists but fails to import raises, so a broken write module never disappears silently);
3. write_core.register_apply_tools(mcp): plan_get, plan_list, audit_log, rollback_plan and the *_apply tools;
4. the cross-cutting client tools: clients_list, client_profile, client_check (never a secret value);
5. write_policy.freeze_allowlists() (unless freeze=False): no field allow-list can change afterwards.

Only the *_apply tools may be annotated readOnlyHint=False (tests/test_tools_golden.py enforces it)."""

from __future__ import annotations

import importlib
from dataclasses import asdict, is_dataclass
from typing import Annotated, Any, Callable

from pydantic import Field

ADS_MODULES = ("google_ads", "meta_ads", "openai_ads", "ads_overview")
# (module, function to call with mcp). Absent modules are skipped; their tools appear once the file exists.
OPTIONAL_MODULES: tuple[tuple[str, str], ...] = (
    ("ga4_tools", "register"),
    ("gsc_tools", "register"),
    ("merchant_tools", "register"),
    ("gtm_tools", "register"),
    ("google_write", "register"),
    ("gsc_write", "register"),
    ("merchant_write", "register"),
    ("gads_write", "register"),
    ("meta_write", "register"),
    ("openai_write", "register"),
    ("meta_eco", "register"),
    ("meta_eco_write", "register"),
    ("write_core", "register_apply_tools"),  # last: the platform modules register their handlers first
)
CLIENT_TOOLS = ("clients_list", "client_profile", "client_check")

# Accounts shown by client_profile when the context has no `accounts` mapping: (platform, key). Ids, never secrets.
PROFILE_ACCOUNTS = (
    ("ga4", "property_id"), ("gsc", "site_url"), ("merchant", "account_id"), ("gtm", "account_id"),
    ("gtm", "container_id"), ("gtm", "public_id"), ("google_ads", "customer_id"), ("google_ads", "login_customer_id"),
    ("meta", "ad_account_id"), ("meta", "pixel_id"), ("meta", "catalog_id"), ("meta", "business_id"),
    ("meta", "page_id"), ("meta", "ig_user_id"), ("openai_ads", "account_id"),
)
POLICY_PLATFORMS = ("google_ads", "meta", "openai_ads", "ga4", "gsc", "merchant", "gtm")
SECRET_KINDS = ("google", "google-w", "gads", "meta", "openai", "meta-capi", "pagespeed")


def _import_optional(name: str):
    """The module google_ecommerce_mcp.<name>, or None when that file does not exist."""
    full = f"{__package__}.{name}"
    try:
        return importlib.import_module(full)
    except ModuleNotFoundError as exc:
        if exc.name == full:
            return None
        raise


def register_all(mcp: Any, *, freeze: bool = True) -> dict[str, list[str]]:
    """Register every tool that is not defined in server.py. Returns {module: [tool names]} (skipped modules are
    absent from the result).

    freeze (default True, the server): once every module has registered its field allow-lists, close the
    registry (write_policy.freeze_allowlists, once; already frozen = nothing to do), so no code running later
    can add or change an allow-list. Tests that rebuild the registry pass freeze=False."""
    registered: dict[str, list[str]] = {}
    for name in ADS_MODULES:
        module = importlib.import_module(f"{__package__}.{name}")
        registered[name] = list(module.register(mcp) or [])
    for name, function in OPTIONAL_MODULES:
        module = _import_optional(name)
        hook: Callable | None = getattr(module, function, None) if module else None
        if hook is not None:
            registered[name] = list(hook(mcp) or [])
    registered["registry"] = _register_client_tools(mcp)
    if freeze:
        from . import write_policy

        if not write_policy.allowlists_frozen():
            write_policy.freeze_allowlists()
    return registered


# ------------------------------------------------------------------ cross-cutting client tools
def _plain(value: Any) -> Any:
    """JSON-friendly copy of a policy (frozensets as sorted lists)."""
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _accounts(ctx: Any) -> dict:
    accounts = getattr(ctx, "accounts", None)
    if callable(accounts):  # clients.ClientContext.accounts(platform) -> dict
        platforms = sorted({p for p, _ in PROFILE_ACCOUNTS} | {"gtm"})
        out = {}
        for platform in platforms:
            try:
                ids = accounts(platform)
            except Exception:
                continue
            if ids:
                out[platform] = {k: v for k, v in dict(ids).items() if v}
        return out
    if isinstance(accounts, dict):
        return {p: dict(v) for p, v in accounts.items() if isinstance(v, dict)}
    found: dict[str, dict] = {}
    for platform, key in PROFILE_ACCOUNTS:
        try:
            value = ctx.account(platform, key)
        except Exception:
            continue
        if value:
            found.setdefault(platform, {})[key] = value
    return found


def _legacy_accounts() -> dict:
    from . import server
    from .ads_config import AdsSettings

    s, ads = server.SETTINGS, AdsSettings.from_env(keyring_fallback=False)
    pairs = {("ga4", "property_id"): s.ga4_property_id, ("gsc", "site_url"): s.gsc_site_url,
             ("merchant", "account_id"): s.merchant_account_id, ("gtm", "container_id"): s.gtm_container_id,
             ("google_ads", "customer_id"): ads.gads_customer_id,
             ("google_ads", "login_customer_id"): ads.gads_login_customer_id,
             ("meta", "ad_account_id"): ads.meta_ad_account_id}
    found: dict[str, dict] = {}
    for (platform, key), value in pairs.items():
        if value:
            found.setdefault(platform, {})[key] = value
    return found


def clients_list() -> dict:
    """Clients this server can work on, from the client registry (clients.json): client_id, label, sandbox and
    which platforms are configured. Never a secret. Without a registry there is one implicit client, "default",
    built from the environment variables. Pass client_id to the other tools when several clients are listed."""
    from . import server

    clients = server._clients_module()
    if clients is None or not hasattr(clients, "list_clients"):
        items = [{"client_id": "default", "label": "environment variables", "legacy": True, "sandbox": False}]
    else:
        items = list(clients.list_clients())
    return {"clients": items, "count": len(items), "client_id_required": len(items) > 1}


def client_profile(client_id: Annotated[str | None, Field(description="Client id from clients_list; empty uses "
                                                                   "the only client")] = None) -> dict:
    """One client's profile: label, sandbox flag, the account ids of each platform (GA4 property, Search Console
    site, Merchant account, GTM container, Google Ads customer, Meta ad account...) and its write policy per platform
    (enabled, allowed accounts, budget caps). Never a secret value."""
    from . import server

    scope = server._as_scope(client_id)
    if scope.ctx is None:
        return {"client_id": "default", "label": "environment variables", "legacy": True, "sandbox": False,
                "accounts": _legacy_accounts(), "write": {}}
    ctx = scope.ctx
    write: dict[str, Any] = {}
    for platform in POLICY_PLATFORMS:
        try:
            write[platform] = _plain(ctx.policy(platform))
        except Exception:
            continue
    return {"client_id": ctx.client_id, "label": getattr(ctx, "label", ctx.client_id),
            "legacy": bool(getattr(ctx, "legacy", False)), "sandbox": bool(getattr(ctx, "sandbox", False)),
            "accounts": _accounts(ctx), "write": write}


def client_check(client_id: Annotated[str | None, Field(description="Client id from clients_list; empty uses "
                                                                 "the only client")] = None) -> dict:
    """Health of one client: which services are configured, whether its Google token works and which scopes it
    holds (like server_status), plus whether each of its secrets is stored ("secrets": {kind: true/false}; the
    values are never read out)."""
    from . import server

    scope = server._as_scope(client_id)
    status = server.check(scope)
    if scope.ctx is not None:
        stored: dict[str, bool] = {}
        for kind in SECRET_KINDS:
            try:
                stored[kind] = bool(scope.ctx.secret(kind))
            except Exception:
                stored[kind] = False
        status["secrets"] = stored
        status["client_id"] = scope.ctx.client_id
    else:
        status["client_id"] = "default"
    return status


def _register_client_tools(mcp: Any) -> list[str]:
    from . import server

    tools = (
        ("Clients list", server._guard("current registry", client=False)(clients_list)),
        ("Client profile", server._guard("current registry", client=False)(client_profile)),
        ("Client check", server._guard("live: token tested now", client=False)(client_check)),
    )
    for title, fn in tools:
        mcp.tool(title=title, annotations=server.READ_ONLY)(fn)
    return [fn.__name__ for _, fn in tools]

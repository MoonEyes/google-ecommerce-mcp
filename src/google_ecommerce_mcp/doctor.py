"""`google-ecommerce-mcp doctor`: one line per service, what works, what does not, and the exact command to fix it.

Offline (default): reads the configuration (this shell's environment, overlaid by the server entry of the Claude
Desktop config, as the server sees it at run time), checks that each secret is present (booleans only) and that the
stored tokens hold the right scopes, the Google Ads token being kept apart from the read-only Google token.
--live: one small read per service through the existing tools (short timeout), errors translated into advice.

With a client registry (clients.json), or --client: one compact line per client and platform (ids configured,
read token, separate write token, write switch of the client's policy, status, masked id, next command), every
client of the registry unless --client filters it; GEM_WRITE / GEM_CLIENT of the Claude Desktop entry are checked
too. Without a registry and without --client (or with --client default) the single-account output is unchanged.

Never printed: a secret, a token, a full account id (masked as 123***890), a URL returned by a platform, an error
detail sent back by a platform (it may contain ids). Exit code: 0 when everything configured works, 1 when
something configured needs fixing, 2 when nothing is configured."""

from __future__ import annotations

import contextlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from . import setup_guide as guide
from .ads_common import redact
from .ads_config import ADS_SCOPE, KEYRING_USER_META, KEYRING_USER_OPENAI, SECRET_ENV
from .config import INDEXING_SCOPE, KEYRING_SERVICE, MERCHANT_SCOPE

LIVE_TIMEOUT = 15  # seconds per request in --live
SERVER_NAME = "google-ecommerce"

GOOGLE_ENV = ("GA4_PROPERTY_ID", "GSC_SITE_URL", "MERCHANT_ACCOUNT_ID", "GTM_CONTAINER_ID", "PAGESPEED_API_KEY",
              "GOOGLE_TOKEN_FILE")
ADS_ENV = ("GADS_CUSTOMER_ID", "GADS_LOGIN_CUSTOMER_ID", "GADS_DEVELOPER_TOKEN", "GADS_TOKEN_FILE", "GADS_API_VERSION",
           "GADS_ALLOW_SENSITIVE_QUERIES", "META_ACCESS_TOKEN", "META_AD_ACCOUNT_ID", "META_API_VERSION",
           "OPENAI_ADS_API_KEY", "ADS_TRUST_ENV")
KNOWN_ENV = GOOGLE_ENV + ADS_ENV
SECRET_VARIABLES = ("META_ACCESS_TOKEN", "OPENAI_ADS_API_KEY", "GADS_DEVELOPER_TOKEN", "PAGESPEED_API_KEY")

GOOGLE_SCOPE = {
    "ga4": "https://www.googleapis.com/auth/analytics.readonly",
    "gsc": "https://www.googleapis.com/auth/webmasters.readonly",
    "merchant": MERCHANT_SCOPE,
    "gtm": "https://www.googleapis.com/auth/tagmanager.readonly",
}
GOOGLE_ID = {  # service -> (variable, install flag, placeholder, label)
    "ga4": ("GA4_PROPERTY_ID", "ga4", "<property id>", "property"),
    "gsc": ("GSC_SITE_URL", "gsc", "<property>", "site"),
    "merchant": ("MERCHANT_ACCOUNT_ID", "merchant", "<account id>", "account"),
    "gtm": ("GTM_CONTAINER_ID", "gtm", "<container id>", "container"),
}
GOOGLE_API = {"ga4": "Google Analytics Admin API", "gsc": "Google Search Console API", "merchant": "Merchant API",
              "gtm": "Tag Manager API"}

OK, FIX, NOT_SET_UP, OPTIONAL = "ok", "fix", "not_set_up", "optional"
_LABEL = {OK: "OK", FIX: "FIX", NOT_SET_UP: "not set up", OPTIONAL: "optional"}


# ------------------------------------------------------------------ helpers
# Known id formats: a value that does not match is fully hidden (it may be a secret pasted in the wrong place).
ID_FORMATS = {
    "ga4": re.compile(r"(?:properties/)?[0-9]{1,20}"),
    "merchant": re.compile(r"[0-9]{1,20}"),
    "gtm": re.compile(r"GTM-[A-Z0-9]{4,12}", re.IGNORECASE),
    "gads": re.compile(r"[0-9]{10}"),
    "meta": re.compile(r"[0-9]{3,20}"),
}


def valid_id(kind: str, value: str | None) -> bool:
    """True when value has the documented shape of this id (Google Ads dashes and Meta act_ tolerated)."""
    text = (value or "").strip()
    if kind == "gads":
        text = _digits(text)
    elif kind == "meta":
        text = _meta_digits(text)
    return bool(ID_FORMATS[kind].fullmatch(text))


def mask(value: Any, kind: str | None = None) -> str:
    """'1234567890' -> '123***890'. Short values, and values of a known kind with the wrong shape, are fully
    hidden. Never returns the full value."""
    text = str(value or "").strip()
    if kind in ID_FORMATS and not valid_id(kind, text):
        return "***"
    if kind == "gads":
        text = _digits(text)
    elif kind == "meta":
        text = _meta_digits(text)
    if len(text) < 6:
        return "***"
    keep = 3 if len(text) >= 9 else 1
    return f"{text[:keep]}***{text[-keep:]}"


def _digits(value: str | None) -> str:
    return (value or "").strip().replace("-", "").replace(" ", "")


def _meta_digits(value: str | None) -> str:
    text = (value or "").strip()
    return text[4:] if text.lower().startswith("act_") else text


def _same_file(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    try:
        return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()
    except (OSError, RuntimeError):
        return os.path.normcase(a) == os.path.normcase(b)


def _scopes_of(raw: str | None) -> list[str] | None:
    """Scopes recorded in a stored token; None when the token is not readable JSON or records none."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    scopes = data.get("scopes") if isinstance(data, dict) else None
    if isinstance(scopes, str):
        scopes = scopes.split()
    return [str(s) for s in scopes] if isinstance(scopes, list) else None


def _read(read: Callable[[str | None], str | None], source: str | None) -> tuple[str | None, str | None]:
    """(raw token, error kind). Never raises, never returns the content of an error."""
    try:
        return read(source), None
    except Exception as exc:  # unreadable file, no keyring backend, locked keychain
        return None, type(exc).__name__


def _keyring_secret(name: str) -> tuple[str | None, str | None]:
    try:
        import keyring

        value = keyring.get_password(KEYRING_SERVICE, name)
    except Exception as exc:
        return None, type(exc).__name__
    return ((value or "").strip() or None), None


def claude_entry_env(config_path: Path | None, name: str = SERVER_NAME) -> tuple[dict[str, str], bool, str]:
    """(env of the server entry, entry found, problem). Tolerant: a missing or broken file is reported, not raised."""
    if config_path is None:
        return {}, False, ""
    from .installer import ConfigError, load_config

    if not config_path.exists():
        return {}, False, "Claude Desktop config not found"
    try:
        data = load_config(config_path)  # BOM, size cap and shape checks shared with install
    except ConfigError as exc:
        kind = type(exc.__cause__).__name__ if exc.__cause__ else "wrong shape or too large"
        return {}, False, f"Claude Desktop config could not be read ({kind})"
    entry = data.get("mcpServers", {}).get(name)
    if not isinstance(entry, dict):
        return {}, False, ""
    env = entry.get("env") or {}
    return ({str(k): str(v) for k, v in env.items() if v is not None} if isinstance(env, dict) else {}), True, ""


def effective_env(entry_env: dict[str, str], base: dict[str, str] | None = None) -> dict[str, str]:
    """What the server sees: this environment, overlaid by the Claude Desktop entry (known variables only)."""
    source = os.environ if base is None else base
    env = {k: source[k].strip() for k in KNOWN_ENV if (source.get(k) or "").strip()}
    env.update({k: v.strip() for k, v in entry_env.items() if k in KNOWN_ENV and (v or "").strip()})
    return env


# ------------------------------------------------------------------ report
@dataclass
class ServiceReport:
    service: str
    name: str
    configured: bool = False
    secret_present: bool = False
    token_check: str = "-"
    status: str = NOT_SET_UP
    account: str = ""
    problem: str = ""
    next_step: str = ""
    live: str = ""
    notes: list[str] = field(default_factory=list)

    def fix(self, problem: str, next_step: str) -> "ServiceReport":
        self.status, self.problem, self.next_step = FIX, problem, next_step
        return self


@dataclass
class Facts:
    """Everything read once from the machine (tokens, keyring), shared by the per-service checks."""

    env: dict[str, str]
    google_raw: str | None = None
    google_error: str | None = None
    ads_raw: str | None = None
    ads_error: str | None = None
    meta_secret: bool = False
    openai_secret: bool = False
    keyring_error: str | None = None

    @property
    def google_scopes(self) -> list[str] | None:
        return _scopes_of(self.google_raw)

    @property
    def ads_scopes(self) -> list[str] | None:
        return _scopes_of(self.ads_raw)


def gather(env: dict[str, str], services: tuple[str, ...]) -> Facts:
    """Read only what the selected services need."""
    facts = Facts(env=env)
    if set(services) & (set(guide.GOOGLE_SERVICES) | {"gads"}):
        from .auth import _read_raw as read_google

        facts.google_raw, facts.google_error = _read(read_google, env.get("GOOGLE_TOKEN_FILE"))
    if "gads" in services:
        from .ads_auth import _read_raw as read_ads

        facts.ads_raw, facts.ads_error = _read(read_ads, env.get("GADS_TOKEN_FILE"))
    for name, attr, wanted in ((KEYRING_USER_META, "meta_secret", "meta"),
                               (KEYRING_USER_OPENAI, "openai_secret", "openai")):
        if wanted not in services:
            continue
        if env.get(SECRET_ENV[name]):
            setattr(facts, attr, True)
            continue
        value, error = _keyring_secret(name)
        setattr(facts, attr, bool(value))
        facts.keyring_error = facts.keyring_error or error
    return facts


def _google_setup(env: dict[str, str], facts: Facts) -> str:
    scopes = facts.google_scopes or []
    return guide.google_setup_command(env.get("GOOGLE_TOKEN_FILE"), merchant=bool(env.get("MERCHANT_ACCOUNT_ID")),
                                      indexing=INDEXING_SCOPE in scopes)


def check_google(service: str, facts: Facts) -> ServiceReport:
    env = facts.env
    variable, flag, placeholder, label = GOOGLE_ID[service]
    report = ServiceReport(service, guide.service(service).name, secret_present=bool(facts.google_raw))
    value = env.get(variable, "")
    report.configured = bool(value)
    if value:
        report.account = f"{label} {mask(value, service if service in ID_FORMATS else None)}"
    scopes = facts.google_scopes
    if facts.google_error:
        report.token_check = "unreadable"
    elif not facts.google_raw:
        report.token_check = "missing"
    elif scopes is not None and GOOGLE_SCOPE[service] not in scopes:
        report.token_check = "missing scope"
    else:
        report.token_check = "ok"
    if not value:
        # A Google token that already holds this service's scope only needs the id; otherwise authorize again.
        report.next_step = guide.install_id_command(flag, placeholder) if report.token_check == "ok" \
            else guide.first_command(service)
        report.token_check = "-"
        return report
    if service == "ga4" and not valid_id("ga4", value):
        return report.fix("GA4_PROPERTY_ID must be digits only (not G-XXXX)",
                          guide.install_id_command(flag, placeholder, force=True))
    if service == "merchant" and not valid_id("merchant", value):
        return report.fix("MERCHANT_ACCOUNT_ID must be digits only",
                          guide.install_id_command(flag, placeholder, force=True))
    if service == "gtm" and not valid_id("gtm", value):
        return report.fix("GTM_CONTAINER_ID must look like GTM-XXXXXXX",
                          guide.install_id_command(flag, placeholder, force=True))
    if report.token_check == "unreadable":
        return report.fix(f"The stored Google token cannot be read ({facts.google_error})", _google_setup(env, facts))
    if report.token_check == "missing":
        return report.fix("No Google token is stored yet", _google_setup(env, facts))
    if report.token_check == "missing scope":
        extra = " (Merchant Center needs the extra content permission)" if service == "merchant" else ""
        return report.fix(f"The Google token does not include {report.name}{extra}", _google_setup(env, facts))
    if scopes is None:
        report.notes.append("the stored Google token records no scopes; run with --live to test it")
    report.status = OK
    return report


def check_pagespeed(facts: Facts) -> ServiceReport:
    present = bool(facts.env.get("PAGESPEED_API_KEY"))
    report = ServiceReport("pagespeed", "PageSpeed", configured=present, secret_present=present)
    report.status = OK if present else OPTIONAL
    if not present:
        report.notes.append("works without a key on Google's shared quota")
        report.next_step = guide.first_command("pagespeed")
    return report


def check_gads(facts: Facts) -> ServiceReport:
    env = facts.env
    customer, login = _digits(env.get("GADS_CUSTOMER_ID")), _digits(env.get("GADS_LOGIN_CUSTOMER_ID"))
    ads_file, google_file = env.get("GADS_TOKEN_FILE"), env.get("GOOGLE_TOKEN_FILE")
    report = ServiceReport("gads", "Google Ads", secret_present=bool(facts.ads_raw))
    report.configured = bool(customer or facts.ads_raw or login)
    if customer:
        report.account = f"customer {mask(customer, 'gads')}" + (f", manager {mask(login, 'gads')}" if login else "")
    scopes = facts.ads_scopes
    same_file = _same_file(ads_file, google_file)
    same_token = bool(facts.ads_raw) and facts.ads_raw == facts.google_raw
    if same_file or same_token:
        report.token_check = "not separate"
    elif facts.ads_error:
        report.token_check = "unreadable"
    elif not facts.ads_raw:
        report.token_check = "missing"
    elif scopes is not None and ADS_SCOPE not in scopes:
        report.token_check = "missing scope"
    elif scopes is not None and set(scopes) != {ADS_SCOPE}:
        report.token_check = "not separate"
    else:
        report.token_check = "ok"
    if not report.configured:
        report.token_check = "-" if report.token_check == "missing" else report.token_check
        report.next_step = guide.first_command("gads")
        return report
    if same_file:
        return report.fix("GADS_TOKEN_FILE is the same file as GOOGLE_TOKEN_FILE; the Google Ads token must be kept "
                          "apart. Remove GADS_TOKEN_FILE from the server env in the Claude Desktop config, then run",
                          guide.setup_ads_command())
    if same_token:
        return report.fix("The Google Ads token is a copy of the read-only Google token; authorize Google Ads "
                          "separately", guide.setup_ads_command(ads_file))
    for value, variable, flag, placeholder in ((env.get("GADS_CUSTOMER_ID"), "GADS_CUSTOMER_ID", "gads-customer-id",
                                                "<customer id>"),
                                               (env.get("GADS_LOGIN_CUSTOMER_ID"), "GADS_LOGIN_CUSTOMER_ID",
                                                "gads-login-customer-id", "<manager id>")):
        if value and not valid_id("gads", value):
            return report.fix(f"{variable} must be 10 digits (dashes allowed)",
                              guide.install_id_command(flag, placeholder, force=True))
    if report.token_check == "unreadable":
        return report.fix(f"The stored Google Ads token cannot be read ({facts.ads_error})",
                          guide.setup_ads_command(ads_file))
    if report.token_check == "missing":
        return report.fix("No Google Ads token is stored yet (it is separate from the Google token)",
                          guide.setup_ads_command(ads_file))
    if report.token_check == "missing scope":
        return report.fix("The Google Ads token does not include the Google Ads permission",
                          guide.setup_ads_command(ads_file))
    if report.token_check == "not separate":
        return report.fix("The Google Ads token holds other Google permissions; authorize Google Ads alone",
                          guide.setup_ads_command(ads_file))
    if not customer:
        return report.fix("The Google Ads token is stored but no customer id is set (ask Claude to run "
                          "gads_accounts to list them)", guide.install_id_command("gads-customer-id", "<customer id>"))
    report.status = OK
    return report


def check_meta(facts: Facts) -> ServiceReport:
    env = facts.env
    account = _meta_digits(env.get("META_AD_ACCOUNT_ID"))
    report = ServiceReport("meta", "Meta Ads", secret_present=facts.meta_secret)
    report.configured = bool(account or facts.meta_secret)
    report.token_check = "ok" if facts.meta_secret else "missing"
    if account:
        report.account = f"ad account {mask(account, 'meta')}"
    if not report.configured:
        report.token_check = "-"
        report.next_step = guide.first_command("meta")
        return report
    if not facts.meta_secret:
        extra = f" (keyring: {facts.keyring_error})" if facts.keyring_error else ""
        return report.fix(f"The Meta ad account is set but no Meta token is stored{extra}", guide.SET_META)
    if not account:
        return report.fix("A Meta token is stored but no ad account id is set (ask Claude to run meta_ad_accounts "
                          "to list them)", guide.install_id_command("meta-account-id", "<ad account id>"))
    if not valid_id("meta", account):
        return report.fix("META_AD_ACCOUNT_ID must be digits, optionally starting with act_",
                          guide.install_id_command("meta-account-id", "<ad account id>", force=True))
    report.status = OK
    return report


def check_openai(facts: Facts) -> ServiceReport:
    report = ServiceReport("openai", "ChatGPT Ads", configured=facts.openai_secret, secret_present=facts.openai_secret)
    if not facts.openai_secret:
        report.next_step = guide.first_command("openai")
        if facts.keyring_error:
            report.notes.append(f"keyring not readable ({facts.keyring_error})")
        return report
    report.token_check = "ok"
    report.status = OK
    return report


def offline(services: tuple[str, ...], env: dict[str, str]) -> tuple[list[ServiceReport], Facts]:
    facts = gather(env, services)
    checks: dict[str, Callable[[], ServiceReport]] = {
        **{s: (lambda s=s: check_google(s, facts)) for s in guide.GOOGLE_SERVICES},
        "pagespeed": lambda: check_pagespeed(facts),
        "gads": lambda: check_gads(facts),
        "meta": lambda: check_meta(facts),
        "openai": lambda: check_openai(facts),
    }
    return [checks[s]() for s in services], facts


def warnings_for(env: dict[str, str], entry_env: dict[str, str], registered: bool, config_problem: str,
                 facts_google_scopes: list[str] | None) -> list[str]:
    out: list[str] = []
    if config_problem:
        out.append(f"{config_problem}: only this shell's environment was read")
    elif not registered:
        out.append(f"the server is not registered in Claude Desktop yet: run `{guide.CLI} install`")
    if env.get("ADS_TRUST_ENV") == "1":
        out.append("ADS_TRUST_ENV=1: the Ads connectors use this machine's proxy and CA settings; keep it only "
                   "behind a company proxy")
    if (env.get("GADS_ALLOW_SENSITIVE_QUERIES") or "").strip().lower() in {"1", "true", "yes"}:
        out.append("GADS_ALLOW_SENSITIVE_QUERIES is on: gads_query can read personal data (user lists, "
                   "customer details); remove it unless you need it")
    plain = sorted(v for v in ("META_ACCESS_TOKEN", "OPENAI_ADS_API_KEY", "GADS_DEVELOPER_TOKEN") if entry_env.get(v))
    if plain:
        out.append(f"{', '.join(plain)} is written in plain text in the Claude Desktop config: move it to the "
                   f"keyring (`{guide.CLI} set-secret ...`) and delete it from the config")
    if facts_google_scopes and ADS_SCOPE in facts_google_scopes:
        out.append("the read-only Google token also holds the Google Ads permission: rerun "
                   f"`{guide.google_setup_command(env.get('GOOGLE_TOKEN_FILE'))}` to drop it")
    return out


# ------------------------------------------------------------------ live
@contextlib.contextmanager
def applied_env(env: dict[str, str]) -> Iterator[Any]:
    """Run the existing tools with the effective configuration and a short timeout, then restore everything."""
    from . import google_ads, meta_ads, openai_ads, server
    from .auth import TokenProvider
    from .config import Settings

    saved_env = {k: os.environ.get(k) for k in KNOWN_ENV}
    saved = (server.SETTINGS, server.TOKENS, server.TIMEOUT)
    clients = [google_ads.CLIENT, meta_ads.CLIENT, openai_ads.CLIENT]
    timeouts = [c.timeout for c in clients]
    saved_ads_tokens, saved_bearer = google_ads.TOKENS, dict(google_ads._CURRENT_BEARER)
    try:
        # A fresh Google Ads token provider and empty caches: nothing from an earlier call is reused.
        google_ads.TOKENS = google_ads.AdsTokenProvider()
        google_ads._CURRENT_BEARER.clear()
        for client in clients:
            client.cache.clear()
        for key in KNOWN_ENV:
            if key in env:
                os.environ[key] = env[key]
        server.SETTINGS = Settings.from_env()
        server.TOKENS = TokenProvider(server.SETTINGS.token_file)
        server.TIMEOUT = LIVE_TIMEOUT
        for client in clients:
            client.timeout = LIVE_TIMEOUT
        yield server
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        server.SETTINGS, server.TOKENS, server.TIMEOUT = saved
        google_ads.TOKENS = saved_ads_tokens
        google_ads._CURRENT_BEARER.clear()
        google_ads._CURRENT_BEARER.update(saved_bearer)
        for client, timeout in zip(clients, timeouts):
            client.timeout = timeout
            client.cache.clear()


def _live_call(service: str) -> dict:
    from . import google_ads, meta_ads, openai_ads, server

    calls: dict[str, Callable[[], dict]] = {
        "ga4": lambda: server.ga4_properties(max_pages=1),
        "gsc": server.gsc_sitemaps,
        "merchant": server.merchant_data_sources,
        "gtm": server.gtm_inventory,
        "gads": google_ads.gads_accounts,
        "meta": meta_ads.meta_ad_accounts,
        "openai": openai_ads.openai_ads_account,
    }
    try:
        result = calls[service]()
    except Exception as exc:  # never crash the doctor on a tool bug
        return {"error": "internal_error", "kind": type(exc).__name__}
    return result if isinstance(result, dict) else {"error": "internal_error"}


def _reauth(service: str, env: dict[str, str], facts_scopes: list[str] | None) -> str:
    if service == "gads":
        return guide.setup_ads_command(env.get("GADS_TOKEN_FILE"))
    if service == "meta":
        return guide.SET_META
    if service == "openai":
        return guide.SET_OPENAI
    return guide.google_setup_command(env.get("GOOGLE_TOKEN_FILE"), merchant=bool(env.get("MERCHANT_ACCOUNT_ID")),
                                      indexing=INDEXING_SCOPE in (facts_scopes or []))


def advice(service: str, result: dict, env: dict[str, str], google_scopes: list[str] | None = None) -> tuple[str, str]:
    """(problem, next command) for a structured error returned by a tool. Never echoes the platform's detail."""
    code = result.get("error")
    retest = guide.doctor_command(service)
    name = guide.service(service).name
    is_ads = service in guide.ADS_SERVICES
    if code == "not_configured":
        return f"{name}: a required setting is missing", guide.first_command(service)
    if code in ("auth_failed", "not_authenticated", 401):
        what = {"gads": "The Google Ads token was refused (expired, revoked, or no access to this customer)",
                "meta": "The Meta token was refused (expired, revoked, or no access to this ad account)",
                "openai": "The ChatGPT Ads key was refused (revoked or wrong key)"}.get(
            service, "The Google token was refused (expired or revoked)")
        return what, _reauth(service, env, google_scopes)
    if code == "missing_scope":
        what = {"gads": "The Google Ads token lacks the Google Ads permission",
                "meta": "The Meta token lacks ads_read or access to this ad account: fix it in Business Settings, "
                        "create a new token, then store it",
                "openai": "The ChatGPT Ads key cannot read this ad account: create a key with read access, then "
                          "store it"}.get(service, f"The Google token does not include {name}")
        return what, _reauth(service, env, google_scopes)
    if code == 403:
        return (f"The authorized Google account has no access to this {name} id: check the id, or authorize with "
                "an account that can read it"), _reauth(service, env, google_scopes)
    if code in ("api_disabled", "not_available"):
        if service == "gads":
            return ("Google Ads API not available: turn it on in Google Cloud Console (APIs & Services > Library) "
                    "and check the customer id, then retest"), retest
        if service == "meta":
            return "Meta answered not available: check the ad account id and META_API_VERSION, then retest", retest
        if service == "openai":
            return "The ChatGPT Ads API is not available for this account yet", retest
        return (f"Turn on the {GOOGLE_API.get(service, 'API')} in Google Cloud Console (APIs & Services > Library), "
                "wait 5 minutes, then retest"), retest
    if code == "rate_limited":
        try:
            wait = max(1, int(float(str(result.get("retry_after_seconds")))))
        except (TypeError, ValueError):
            wait = 60  # absent, or an HTTP date: never echo the platform's value
        return f"Too many requests right now; wait {wait} seconds, then retest", retest
    if code == "network":
        if is_ads:
            return ("The service could not be reached: check the internet connection; behind a company proxy run "
                    "this, then retest"), guide.CORPORATE_PROXY
        return "Google could not be reached: check the internet connection (and the proxy settings), then retest", \
            retest
    if code == "response_too_large":
        return "The answer was unexpectedly large and was not read; retry later", retest
    if code == "blocked":
        return "The request was refused by the read-only safety list; check GADS_API_VERSION / META_API_VERSION", \
            retest
    if code == "container_not_found":
        return ("The Tag Manager container is not visible to this Google account: check the id, or authorize with "
                "an account that can read it"), _reauth(service, env, google_scopes)
    if code == 404:
        variable, flag, placeholder, _ = GOOGLE_ID.get(service, ("", service, "<id>", ""))
        return f"{name}: not found; check {variable}", guide.install_id_command(flag, placeholder, force=True)
    if isinstance(code, str) and code.startswith("invalid_"):
        return f"{name}: a setting has the wrong format ({code})", guide.first_command(service)
    return f"{name}: unexpected answer ({code}); see docs/TROUBLESHOOTING.md", f"{retest} --json"


def _visible(service: str, result: dict, env: dict[str, str]) -> tuple[str, str] | None:
    """After a successful call: is the configured account among those the credentials can read?"""
    if service == "ga4":
        properties = result.get("properties") or []
        if not result.get("truncated") and not any(p.get("configured") for p in properties if isinstance(p, dict)):
            return ("The GA4 property is not visible to the authorized Google account: check the id, or authorize "
                    "with an account that can read it"), guide.install_id_command("ga4", "<property id>", force=True)
    if service == "meta":
        wanted = _meta_digits(env.get("META_AD_ACCOUNT_ID"))
        ids = {str(a.get("account_id")) for a in result.get("accounts") or [] if isinstance(a, dict)}
        if wanted and result.get("complete", True) and wanted not in ids:
            return ("The Meta token cannot see this ad account: give the system user access to it in Business "
                    "Settings, or fix the id"), guide.install_id_command("meta-account-id", "<ad account id>", True)
    if service == "gads":
        wanted = _digits(env.get("GADS_CUSTOMER_ID"))
        ids = {str(c.get("id")) for c in result.get("customers") or [] if isinstance(c, dict)}
        if wanted and wanted not in ids and not env.get("GADS_LOGIN_CUSTOMER_ID"):
            return ("This Google account cannot read the customer directly; if you reach it through a manager "
                    "account, add the manager id"), guide.install_id_command("gads-login-customer-id", "<manager id>")
    return None


def live(reports: list[ServiceReport], env: dict[str, str], google_scopes: list[str] | None = None) -> None:
    """One small read per service whose offline check passed. Updates the reports in place."""
    targets = [r for r in reports if r.status == OK and r.service != "pagespeed"]
    for report in reports:
        if report.service == "pagespeed" and report.status in (OK, OPTIONAL):
            report.live = "skipped"
            report.notes.append("not tested live: PageSpeed needs a page address; ask Claude to run pagespeed")
    if not targets:
        return
    with applied_env(env):
        for report in targets:
            result = _live_call(report.service)
            if "error" in result:
                code = result.get("error")
                report.live = str(code)
                problem, command = advice(report.service, result, env, google_scopes)
                report.fix(problem, command)
                continue
            report.live = "ok"
            hidden = _visible(report.service, result, env)
            if hidden:
                report.live = "not_visible"
                report.fix(*hidden)


# ------------------------------------------------------------------ run
def exit_code(reports: list[ServiceReport]) -> int:
    configured = [r for r in reports if r.configured]
    if not configured:
        return 2
    return 1 if any(r.status == FIX for r in configured) else 0


def diagnose(services: tuple[str, ...], env: dict[str, str], entry_env: dict[str, str] | None = None,
             registered: bool = True, config_problem: str = "", run_live: bool = False) -> dict:
    """The whole diagnosis as data (what --json prints). Secrets are never part of it."""
    reports, facts = offline(services, env)
    google_scopes = facts.google_scopes
    if run_live:
        live(reports, env, google_scopes)
    warnings = warnings_for(env, entry_env or {}, registered, config_problem, google_scopes)
    return {"mode": "live" if run_live else "offline",
            "claude_desktop": {"registered": registered},
            "services": [asdict(r) for r in reports],
            "warnings": warnings,
            "exit_code": exit_code(reports)}


def _secrets(env: dict[str, str]) -> list[str]:
    values = [env.get(k) for k in SECRET_VARIABLES]
    for name in (KEYRING_USER_META, KEYRING_USER_OPENAI):
        values.append(_keyring_secret(name)[0])
    return [v for v in values if v]


def _yes(flag: bool) -> str:
    return "yes" if flag else "no"


def render(result: dict) -> str:
    """Plain text: one line per service, then the numbered list of what to do."""
    live_mode = result["mode"] == "live"
    lines = [f"{guide.CLI} doctor ({'live test' if live_mode else 'offline check; add --live to test each service'})",
             ""]
    header = f"{'Service':<16}{'Set up':<8}{'Secret':<8}{'Token':<15}" + (f"{'Live':<16}" if live_mode else "") \
        + f"{'Status':<12}Account"
    lines.append(header)
    lines.append("-" * len(header))
    for s in result["services"]:
        lines.append(f"{s['name']:<16}{_yes(s['configured']):<8}{_yes(s['secret_present']):<8}{s['token_check']:<15}"
                     + (f"{(s['live'] or '-'):<16}" if live_mode else "")
                     + f"{_LABEL[s['status']]:<12}{s['account'] or '-'}")
    todo = [s for s in result["services"] if s["status"] == FIX]
    later = [s for s in result["services"] if s["status"] in (NOT_SET_UP,)]
    if todo:
        lines += ["", "To fix (one at a time, then run the check again):"]
        for number, s in enumerate(todo, 1):
            lines.append(f"  {number}. {s['name']}: {s['problem']}")
            lines.append(f"     {s['next_step']}")
            lines.append(f"     then: {guide.doctor_command(s['service'])}")
    notes = [(s["name"], n) for s in result["services"] for n in s["notes"]]
    if notes:
        lines += ["", "Notes:"] + [f"  - {name}: {note}" for name, note in notes]
    if result["warnings"]:
        lines += ["", "Warnings:"] + [f"  - {w}" for w in result["warnings"]]
    if later:
        lines += ["", "Not set up (optional, one service at a time):"]
        lines += [f"  - {s['name']}: {s['next_step']}" for s in later]
    ok = sum(1 for s in result["services"] if s["status"] == OK)
    code = result["exit_code"]
    lines += ["", f"Result: {ok} OK, {len(todo)} to fix, {len(later)} not set up."]
    if code == 2:
        lines.append(f"Nothing is set up yet. Start with one service: {guide.first_command('ga4')}")
    elif code == 1:
        lines.append(f"Next command: {todo[0]['next_step']}")
    elif not live_mode:
        lines.append(f"Next command: {guide.CLI} doctor --live")
    return "\n".join(lines)


def parse_only(text: str | None) -> tuple[str, ...]:
    """'gads,meta' -> ('gads', 'meta') in the canonical order; raises ValueError on an unknown name."""
    if text is None:
        return guide.SERVICE_IDS
    wanted = [w.strip().lower() for w in re.split(r"[,\s]+", text) if w.strip()]
    if not wanted:
        raise ValueError(f"--only needs at least one service; choose from {','.join(guide.SERVICE_IDS)}")
    unknown = sorted(set(wanted) - set(guide.SERVICE_IDS))
    if unknown:
        raise ValueError(f"unknown service {', '.join(unknown)}; choose from {','.join(guide.SERVICE_IDS)}")
    return tuple(s for s in guide.SERVICE_IDS if s in wanted)


# ------------------------------------------------------------------ multi-client mode (registry)
# (platform of the registry, doctor service id, display name)
CLIENT_PLATFORMS = (("ga4", "ga4", "GA4"), ("gsc", "gsc", "Search Console"), ("merchant", "merchant", "Merchant"),
                    ("gtm", "gtm", "Tag Manager"), ("google_ads", "gads", "Google Ads"), ("meta", "meta", "Meta Ads"),
                    ("openai_ads", "openai", "ChatGPT Ads"))
# A write token holding one of these scopes can write the platform (GA4 also accepts the full analytics scope).
ACCEPTED_WRITE_SCOPES = {"ga4": ("https://www.googleapis.com/auth/analytics.edit",
                                 "https://www.googleapis.com/auth/analytics"),
                         "gtm": ("https://www.googleapis.com/auth/tagmanager.edit.containers",),
                         "gsc": ("https://www.googleapis.com/auth/webmasters",),
                         "merchant": (MERCHANT_SCOPE,)}
CLIENT_ENV = ("GEM_CLIENT", "GEM_WRITE", "GEM_CLIENTS_FILE", "GEM_STATE_DIR")


@dataclass
class ClientRow:
    """One client x one platform. Booleans, token states and a masked id only: never a secret nor a full id."""

    client_id: str
    platform: str
    name: str
    configured: bool = False
    account: str = ""
    read_token: str = "-"
    write_token: str = "-"
    write_enabled: bool = False
    status: str = NOT_SET_UP
    problem: str = ""
    next_step: str = ""
    live: str = ""

    def fix(self, problem: str, next_step: str) -> "ClientRow":
        self.status, self.problem, self.next_step = FIX, problem, next_step
        return self


def _client_secret(ctx: Any, kind: str, seen: list[str]) -> tuple[str | None, str | None]:
    """(value, error kind) of one secret of the client; the value is only kept to be redacted from the output."""
    try:
        value = ctx.secret(kind)
    except Exception as exc:  # no keyring backend, locked keychain
        return None, type(exc).__name__
    if value:
        seen.append(value)
    return value, None


def _token_state(raw: str | None, error: str | None, wanted: tuple[str, ...]) -> str:
    if error:
        return "unreadable"
    if not raw:
        return "missing"
    scopes = _scopes_of(raw)
    if scopes is None:
        try:
            json.loads(raw)
        except ValueError:
            return "unreadable"
        return "ok"
    return "ok" if any(s in scopes for s in wanted) else "scope"


def _primary_account(ctx: Any, platform: str) -> str:
    from .clients import mask_id

    values = ctx.accounts(platform)
    for key in ("property_id", "site_url", "account_id", "public_id", "customer_id", "ad_account_id"):
        if values.get(key):
            return mask_id(values[key])
    return mask_id(next(iter(values.values()))) if values else ""


def client_rows(ctx: Any, platforms: tuple[str, ...] | None = None, seen: list[str] | None = None) -> list[ClientRow]:
    """Offline rows of one client: ids configured, read token, write token, write switch, next command."""
    seen = seen if seen is not None else []
    cid = ctx.client_id
    google_raw, google_err = _client_secret(ctx, "google", seen)
    write_raw, write_err = (None, None) if ctx.legacy else _client_secret(ctx, "google-w", seen)
    indexing = INDEXING_SCOPE in (_scopes_of(google_raw) or [])
    rows: list[ClientRow] = []
    for platform, service, name in CLIENT_PLATFORMS:
        if platforms is not None and platform not in platforms:
            continue
        row = ClientRow(cid, platform, name, configured=bool(ctx.accounts(platform)))
        row.account = _primary_account(ctx, platform)
        row.write_enabled = bool(ctx.policy(platform).enabled) or (
            platform == "meta" and bool(ctx.policy("meta_eco").enabled))
        if platform in GOOGLE_SCOPE:
            row.read_token = _token_state(google_raw, google_err, (GOOGLE_SCOPE[platform],))
            read_fix = guide.client_setup_command(cid, indexing=indexing)
            if not ctx.legacy:
                row.write_token = _token_state(write_raw, write_err, ACCEPTED_WRITE_SCOPES[platform])
                if write_raw and write_raw == google_raw:
                    row.write_token = "not separate"
        elif platform == "google_ads":
            raw, err = _client_secret(ctx, "gads", seen)
            row.read_token = _token_state(raw, err, (ADS_SCOPE,))
            if raw and raw in (google_raw, write_raw):
                row.read_token = "not separate"
            row.write_token = "=read" if row.read_token == "ok" else "-"
            read_fix = guide.client_setup_ads_command(cid)
        else:
            kind = guide.SECRET_KIND[platform]
            raw, err = _client_secret(ctx, kind, seen)
            row.read_token = "unreadable" if err else ("ok" if raw else "missing")
            row.write_token = "=read" if raw else "-"
            read_fix = guide.client_secret_command(cid, kind)
        if not row.configured:
            row.read_token = row.read_token if row.read_token == "ok" else "-"
            row.write_token = row.write_token if row.write_token in ("ok", "=read") else "-"
            row.next_step = "-" if ctx.legacy else guide.client_add_command(cid, replace=True)
            rows.append(row)
            continue
        if row.read_token == "unreadable":
            row.fix("the stored token cannot be read", read_fix)
        elif row.read_token == "missing":
            row.fix("no token stored for this client yet", read_fix)
        elif row.read_token == "scope":
            row.fix("the client's token does not include this platform", read_fix)
        elif row.read_token == "not separate":
            row.fix("the Google Ads token is a copy of another Google token", read_fix)
        elif row.write_enabled and platform in GOOGLE_SCOPE and row.write_token != "ok":
            row.fix(f"writing is enabled but the separate write token is {row.write_token}",
                    guide.client_setup_command(cid, write=True))
        else:
            row.status = OK
            row.next_step = "-"
        if not row.write_enabled and row.write_token == "missing":
            row.write_token = "-"  # writing off: no write token is expected
        rows.append(row)
    return rows


def _client_live(rows: list[ClientRow], env: dict[str, str]) -> None:
    """One small read per OK row, with the row's client_id (never another client's)."""
    from . import google_ads, meta_ads, openai_ads, server

    calls: dict[str, Callable[[str], dict]] = {
        "ga4": lambda c: server.ga4_properties(max_pages=1, client_id=c),
        "gsc": lambda c: server.gsc_sitemaps(client_id=c),
        "merchant": lambda c: server.merchant_data_sources(client_id=c),
        "gtm": lambda c: server.gtm_inventory(client_id=c),
        "google_ads": lambda c: google_ads.gads_accounts(client_id=c),
        "meta": lambda c: meta_ads.meta_ad_accounts(client_id=c),
        "openai_ads": lambda c: openai_ads.openai_ads_account(client_id=c),
    }
    targets = [r for r in rows if r.status == OK]
    if not targets:
        return
    service_of = {p: s for p, s, _ in CLIENT_PLATFORMS}
    with applied_env(env):
        for row in targets:
            try:
                result = calls[row.platform](row.client_id)
            except Exception as exc:  # never crash the doctor on a tool bug
                result = {"error": "internal_error", "kind": type(exc).__name__}
            if not isinstance(result, dict):
                result = {"error": "internal_error"}
            if "error" not in result:
                row.live = "ok"
                continue
            row.live = str(result.get("error"))
            problem, command = advice(service_of[row.platform], result, env)
            if command.startswith((f"{guide.CLI} setup", f"{guide.CLI} set-secret")):
                command = {"google_ads": guide.client_setup_ads_command(row.client_id),
                           "meta": guide.client_secret_command(row.client_id, "meta"),
                           "openai_ads": guide.client_secret_command(row.client_id, "openai")}.get(
                    row.platform, guide.client_setup_command(row.client_id))
            elif command.startswith(f"{guide.CLI} install"):
                command = guide.client_add_command(row.client_id, replace=True)
            elif command.startswith(f"{guide.CLI} doctor"):
                command = guide.client_doctor_command(row.client_id, live=True)
            row.fix(problem, command)


def _client_warnings(contexts: list[Any], rows: list[ClientRow], entry_env: dict[str, str], registered: bool,
                     config_problem: str, known: list[str]) -> list[str]:
    out: list[str] = []
    first = contexts[0].client_id if contexts else guide.CLIENT
    if config_problem:
        out.append(f"{config_problem}: the server entry could not be checked")
    elif not registered:
        out.append(f"the server is not registered in Claude Desktop yet: run `{guide.client_install_command(first)}`")
    pinned = (entry_env.get("GEM_CLIENT") or "").strip().lower()
    if pinned and pinned not in known:
        out.append(f"the Claude Desktop entry is pinned (GEM_CLIENT) to a client that is not in the registry: "
                   f"run `{guide.client_install_command(first)} --pin --force`")
    if len(known) >= 2 and not pinned:
        out.append(f"{len(known)} clients registered: every tool call names its client_id (or pin a server entry "
                   "with GEM_CLIENT)")
    writing = sorted({r.client_id for r in rows if r.write_enabled})
    if writing and entry_env.get("GEM_WRITE") != "1":
        out.append(f"writing is enabled for {', '.join(writing)} but GEM_WRITE=1 is not in the server entry: write "
                   f"plans are refused (`{guide.client_install_command(writing[0], skip_auth=True, write=True)}`)")
    legacy_ids = sorted(k for k in GOOGLE_ENV[:4] + ("GADS_CUSTOMER_ID", "META_AD_ACCOUNT_ID") if entry_env.get(k))
    if legacy_ids and contexts and not contexts[0].legacy:
        out.append(f"{', '.join(legacy_ids)} in the server entry are ignored now that a client registry exists")
    plain = sorted(v for v in ("META_ACCESS_TOKEN", "OPENAI_ADS_API_KEY", "GADS_DEVELOPER_TOKEN") if entry_env.get(v))
    if plain:
        out.append(f"{', '.join(plain)} is written in plain text in the Claude Desktop config: store it with "
                   f"`{guide.client_secret_command(first, 'meta')}` (or openai) and delete it from the config")
    return out


def client_exit_code(rows: list[ClientRow]) -> int:
    configured = [r for r in rows if r.configured]
    if not configured:
        return 2
    return 1 if any(r.status == FIX for r in configured) else 0


def diagnose_clients(contexts: list[Any], platforms: tuple[str, ...] | None, env: dict[str, str],
                     entry_env: dict[str, str] | None = None, registered: bool = True, config_problem: str = "",
                     run_live: bool = False, known: list[str] | None = None) -> tuple[dict, list[str]]:
    """(result as data, secret values seen: to redact). Never a secret value nor a full id in the result."""
    seen: list[str] = []
    entry_env = entry_env or {}
    rows: list[ClientRow] = []
    for ctx in contexts:
        rows.extend(client_rows(ctx, platforms, seen))
    if run_live:
        _client_live(rows, env)
    known = known if known is not None else [c.client_id for c in contexts]
    clients_out = []
    for ctx in contexts:
        mine = [asdict(r) for r in rows if r.client_id == ctx.client_id]
        clients_out.append({"client_id": ctx.client_id, "legacy": ctx.legacy, "sandbox": ctx.sandbox,
                            "pinned_in_claude": (entry_env.get("GEM_CLIENT") or "").strip().lower() == ctx.client_id,
                            "rows": mine})
    result = {"mode": "live" if run_live else "offline", "multi_client": True,
              "claude_desktop": {"registered": registered, "gem_write": entry_env.get("GEM_WRITE") == "1"},
              "clients": clients_out,
              "warnings": _client_warnings(contexts, rows, entry_env, registered, config_problem, known),
              "exit_code": client_exit_code(rows)}
    return result, seen


def _short(state: str) -> str:
    return {"not separate": "shared", "unreadable": "bad"}.get(state, state)


def render_clients(result: dict) -> str:
    """Compact table: one line per client and platform, then what to run next."""
    live_mode = result["mode"] == "live"
    n = len(result["clients"])
    lines = [f"{guide.CLI} doctor ({n} client{'s' if n != 1 else ''}, "
             + ("live test)" if live_mode else "offline check; add --live to test each platform)"), ""]
    header = (f"{'Client':<14}{'Platform':<16}{'Ids':<5}{'Read':<9}{'Write':<9}{'On':<5}"
              + (f"{'Live':<14}" if live_mode else "") + f"{'Status':<12}{'Account':<19}Next")
    lines += [header, "-" * len(header)]
    todo = []
    for client in result["clients"]:
        for r in client["rows"]:
            lines.append(f"{r['client_id'][:13]:<14}{r['name']:<16}{_yes(r['configured']):<5}"
                         f"{_short(r['read_token']):<9}{_short(r['write_token']):<9}"
                         f"{('on' if r['write_enabled'] else 'off'):<5}"
                         + (f"{(r['live'] or '-')[:13]:<14}" if live_mode else "")
                         + f"{_LABEL[r['status']]:<12}{(r['account'] or '-')[:18]:<19}"
                         + (r["next_step"] if r["status"] == FIX else "-"))
            if r["status"] == FIX:
                todo.append(r)
    if todo:
        # One step per (client, command): the four Google platforms of a client share one consent.
        grouped: dict[tuple[str, str], list[dict]] = {}
        for r in todo:
            grouped.setdefault((r["client_id"], r["next_step"]), []).append(r)
        lines += ["", "To fix (one at a time, then run the check again):"]
        for number, ((cid, command), group) in enumerate(grouped.items(), 1):
            names = ", ".join(r["name"] for r in group)
            problems = "; ".join(dict.fromkeys(r["problem"] for r in group))
            lines += [f"  {number}. {cid} / {names}: {problems}", f"     {command}"]
    if result["warnings"]:
        lines += ["", "Warnings:"] + [f"  - {w}" for w in result["warnings"]]
    rows = [r for c in result["clients"] for r in c["rows"]]
    ok = sum(1 for r in rows if r["status"] == OK)
    later = sum(1 for r in rows if r["status"] == NOT_SET_UP)
    lines += ["", f"Result: {ok} OK, {len(todo)} to fix, {later} not set up."]
    code = result["exit_code"]
    first = result["clients"][0]["client_id"] if result["clients"] else guide.CLIENT
    if code == 2:
        lines.append(f"Nothing is set up yet. Start with: {guide.client_add_command(first, replace=True)}")
    elif code == 1:
        lines.append(f"Next command: {todo[0]['next_step']}")
    elif not live_mode:
        lines.append(f"Next command: {guide.client_doctor_command(first, live=True)}" if n == 1
                     else f"Next command: {guide.CLI} doctor --live")
    return "\n".join(lines)


def _client_platforms(text: str | None) -> tuple[str, ...] | None:
    if text is None:
        return None
    services = parse_only(text)
    by_service = {s: p for p, s, _ in CLIENT_PLATFORMS}
    return tuple(by_service[s] for s in services if s in by_service)


def _run_clients(args: Any, data: dict | None, entry_env: dict[str, str], registered: bool, problem: str,
                 env: dict[str, str]) -> int:
    from . import clients

    wanted = (getattr(args, "client", None) or "").strip().lower() or None
    try:
        platforms = _client_platforms(getattr(args, "only", None))
    except ValueError as exc:
        print(str(exc))
        return 2
    if data is None:
        contexts = [clients._legacy_context()]
        known = [clients.LEGACY_CLIENT_ID]
    else:
        known = sorted(data["clients"])
        if wanted and wanted not in known:
            exc = clients._unknown(wanted, known)
            print(f"Error [{exc.code}]: {exc.detail}\n  Fix: {exc.fix}")
            return 2
        contexts = [clients._context_from_entry(cid, data["clients"][cid]) for cid in known
                    if not wanted or cid == wanted]
    result, seen = diagnose_clients(contexts, platforms, env, entry_env, registered, problem,
                                    run_live=bool(getattr(args, "live", False)), known=known)
    result = redact(result, seen + _secrets(env))
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(render_clients(result))
    return int(result["exit_code"])


def run(args: Any) -> int:
    from .installer import claude_desktop_config_path

    config = getattr(args, "config", None)
    config_path = Path(config).expanduser() if config else claude_desktop_config_path()
    entry_env, registered, problem = claude_entry_env(config_path, getattr(args, "name", None) or SERVER_NAME)
    wanted = (getattr(args, "client", None) or "").strip().lower() or None
    from . import clients

    try:
        data = clients.load_registry()
    except clients.ClientError as exc:
        print(f"Error [{exc.code}]: {exc.detail}\n  Fix: {exc.fix}")
        return 1
    if data is not None or (wanted and wanted != clients.LEGACY_CLIENT_ID):
        if data is None:
            print(f"Error [unknown_client]: no client registry ({clients.registry_path()}); only the implicit "
                  f"client 'default' exists.\n  Fix: {guide.client_add_command(wanted)}")
            return 2
        return _run_clients(args, data, entry_env, registered, problem, effective_env(entry_env))
    try:
        services = parse_only(getattr(args, "only", None))
    except ValueError as exc:
        print(str(exc))
        return 2
    env = effective_env(entry_env)
    result = diagnose(services, env, entry_env, registered, problem, run_live=bool(getattr(args, "live", False)))
    result = redact(result, _secrets(env))
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(render(result))
    return int(result["exit_code"])

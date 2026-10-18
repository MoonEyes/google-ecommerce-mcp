"""OAuth token handling.

The token is stored in the operating system keyring (Windows Credential Manager, macOS Keychain,
Secret Service on Linux) unless GOOGLE_TOKEN_FILE points to a file. Refreshed access tokens stay
in memory; the stored refresh token is only written by `google-ecommerce-mcp setup`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import requests
from google.auth.exceptions import TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from .config import (INDEXING_SCOPE, KEYRING_SERVICE, KEYRING_USER, MERCHANT_SCOPE, READ_ONLY_SCOPES,
                     WRITE_CAPABLE_SCOPES, scopes_for)


class NotAuthenticated(Exception):
    def __init__(self, message: str = "No Google token found. Run `google-ecommerce-mcp setup --client-secret <file>` "
                                      "first.") -> None:
        super().__init__(message)


class TokenNetworkError(NotAuthenticated):
    """The access token could not be refreshed because the network failed (TransportError, SSL, timeout...).

    Not an authentication problem: callers that know it (server._call, ads_common.AdsClient.call) answer
    {"error": "network"}; it subclasses NotAuthenticated so older callers still treat it as "no token now" and never
    let it escape. The message names the exception type only, never its text (it can hold a URL or a token)."""

    def __init__(self, message: str, kind: str = "", local: bool = False) -> None:
        super().__init__(message)
        self.kind, self.local = kind, local


REFRESH_NETWORK = ("The Google token could not be refreshed: network error ({kind}). Check the internet connection "
                   "(and the proxy settings), then retry; the stored token is unchanged.")
NETWORK_FIX = "check the internet connection (and the proxy settings), then retry"
# What a refresh can raise when the network or TLS fails: google-auth wraps requests errors in TransportError, but
# a raw requests error (SSLError...) or an OSError (CA bundle, socket) is caught too.
NETWORK_ERRORS: tuple[type[BaseException], ...] = (TransportError, requests.RequestException, OSError)

EXPIRED = ("The stored Google token has expired or was revoked. Run `google-ecommerce-mcp setup --client-secret "
           "<file>` again (rerun the install line also works). Apps whose OAuth consent screen is in Testing get "
           "tokens that expire after 7 days: publish the app (In production) to avoid this.")


def _read_raw(token_file: str | None) -> str | None:
    if token_file:
        path = Path(token_file).expanduser()
        return path.read_text(encoding="utf-8") if path.exists() else None
    import keyring

    return keyring.get_password(KEYRING_SERVICE, KEYRING_USER)


def save_token(raw_json: str, token_file: str | None) -> str:
    """Store the token in token_file (created mode 600, Windows ACL restricted to the user, no window where the
    file is readable by others) or in the keyring entry oauth-token; returns where (never the value)."""
    if token_file:
        from .ads_auth import WINDOWS_ACL_WARNING, _write_private  # lazy: ads_auth imports this module

        path = Path(token_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        private = _write_private(path, raw_json)
        return f"file {path}" + ("" if private else f" ({WINDOWS_ACL_WARNING})")
    import keyring

    keyring.set_password(KEYRING_SERVICE, KEYRING_USER, raw_json)
    return "operating system keyring"


class TokenProvider:
    """Lazily loads credentials and refreshes the access token when it expires."""

    def __init__(self, token_file: str | None) -> None:
        self._token_file = token_file
        self._creds: Credentials | None = None

    def headers(self) -> dict[str, str]:
        if self._creds is None:
            raw = _read_raw(self._token_file)
            if not raw:
                raise NotAuthenticated()
            self._creds = Credentials.from_authorized_user_info(json.loads(raw))
        if not self._creds.valid:
            from google.auth.exceptions import RefreshError

            try:
                self._creds.refresh(Request())
            except NETWORK_ERRORS as exc:  # the stored token stays loaded: the next call retries the refresh
                raise TokenNetworkError(REFRESH_NETWORK.format(kind=type(exc).__name__),
                                        kind=type(exc).__name__) from None
            except RefreshError as exc:
                self._creds = None  # read the token again next call, in case setup was rerun meanwhile
                raise NotAuthenticated(EXPIRED if "invalid_grant" in str(exc) else f"Token refresh failed: {exc}") from exc
        return {"Authorization": f"Bearer {self._creds.token}", "Content-Type": "application/json"}

    def granted_scopes(self) -> list[str]:
        """Scopes recorded with the stored token (empty when no token is stored)."""
        if self._creds is None:
            raw = _read_raw(self._token_file)
            if not raw:
                return []
            self._creds = Credentials.from_authorized_user_info(json.loads(raw))
        return sorted(getattr(self._creds, "granted_scopes", None) or self._creds.scopes or [])


def run_setup(client_secret: str, token_file: str | None, scopes: list[str] | None = None) -> str:
    """Interactive OAuth consent in the browser, then store the token. Returns where it was stored.
    Only read-only scopes are requested unless the caller passes write-capable ones explicitly. The client file is
    validated first (validate_client_secret, like consent()): a hostile file never starts a flow nor opens a browser."""
    config = validate_client_secret(client_secret)
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_config(config, scopes or READ_ONLY_SCOPES)  # the validated content
    creds = flow.run_local_server(port=0, open_browser=True, prompt="consent select_account")
    return save_token(creds.to_json(), token_file)


# ------------------------------------------------------------------ multi-client (registry) tokens
_AUTH = "https://www.googleapis.com/auth/"
GOOGLE_PLATFORMS = ("ga4", "gsc", "merchant", "gtm")
# Scope of the read-only token each Google platform needs (Merchant has no read-only scope).
READ_SCOPE_BY_PLATFORM = {"ga4": _AUTH + "analytics.readonly", "gsc": _AUTH + "webmasters.readonly",
                          "merchant": MERCHANT_SCOPE, "gtm": _AUTH + "tagmanager.readonly"}
# Scope of the separate write token (`setup --client X --write`), one per Google platform the client has.
WRITE_SCOPE_BY_PLATFORM = {"ga4": _AUTH + "analytics.edit", "gtm": _AUTH + "tagmanager.edit.containers",
                           "gsc": _AUTH + "webmasters", "merchant": MERCHANT_SCOPE}
GOOGLE_WRITE_SCOPES = tuple(WRITE_SCOPE_BY_PLATFORM[p] for p in ("ga4", "gtm", "gsc", "merchant"))
# Never requested in v1: publishing a Tag Manager version, or creating versions.
FORBIDDEN_WRITE_SCOPES = (_AUTH + "tagmanager.publish", _AUTH + "tagmanager.edit.containerversions")
CLIENT_SECRET_GLOB = "client_secret*.json"
# Shown before any consent that widens the rights (setup --client X --write, install --client X): (fr, en).
WRITE_TOKEN_WARNING = (
    "ATTENTION : --write demande à Google un SECOND jeton, distinct du jeton de lecture, avec des droits de "
    "MODIFICATION pour le client {cid!r} sur : {platforms}.\n"
    "  Portées demandées : {scopes} (jamais tagmanager.publish ni edit.containerversions).\n"
    "  Rangé à part dans l'entrée du trousseau {entry} ; le jeton de lecture n'est pas modifié.\n"
    "  Rien n'est modifié sans : client enable-write, GEM_WRITE=1 sur le serveur, un plan validé "
    "(et `google-ecommerce-mcp approve` hors du chat pour les dépenses et l'irréversible).",
    "WARNING: --write asks Google for a SECOND token, separate from the read token, with EDIT rights for "
    "client {cid!r} on: {platforms}.\n"
    "  Scopes requested: {scopes} (never tagmanager.publish nor edit.containerversions).\n"
    "  Stored apart in the keyring entry {entry}; the read token is not changed.\n"
    "  Nothing changes without: client enable-write, GEM_WRITE=1 on the server, a confirmed plan "
    "(and `google-ecommerce-mcp approve` outside the chat for spending and irreversible changes).")


def write_token_warning(ctx: object, lang: str = "en") -> str:
    """The warning text for this client's write token (platforms and scopes it would get, its keyring entry)."""
    accounts = getattr(ctx, "accounts")
    platforms = [p for p in GOOGLE_PLATFORMS if accounts(p)]
    scopes = write_scopes_for(platforms)
    entry_of = getattr(ctx, "secret_entry", None)
    entry = entry_of("google-w") if callable(entry_of) else f"{getattr(ctx, 'client_id')}/google-w"
    text = WRITE_TOKEN_WARNING[0 if lang == "fr" else 1]
    return text.format(cid=getattr(ctx, "client_id"), platforms=", ".join(platforms) or "-", entry=entry,
                       scopes=", ".join(s.rsplit("/", 1)[-1] for s in scopes) or "-")


class SetupRefused(ValueError):
    """setup --client cannot run (legacy mode with --write, no Google platform, forbidden scope...)."""


def read_scopes_for(ctx: object, *, merchant: bool = False, indexing: bool = False) -> list[str]:
    """Read-only scopes for a client: the three read-only ones, plus content when the client has a Merchant
    Center id (or merchant=True), plus indexing only when asked."""
    accounts = getattr(ctx, "accounts", None)
    has_merchant = bool(accounts("merchant")) if callable(accounts) else False
    return scopes_for(merchant=merchant or has_merchant, indexing=indexing)


def write_scopes_for(platforms: list[str] | tuple[str, ...]) -> list[str]:
    """Write scopes for the given Google platforms only (least privilege), in a stable order; never publish."""
    wanted = [WRITE_SCOPE_BY_PLATFORM[p] for p in ("ga4", "gtm", "gsc", "merchant") if p in platforms]
    return list(dict.fromkeys(wanted))


WRITE_NEEDS_EXPLICIT_FILE = ("--write needs an explicit --client-secret <file>: the OAuth client file is never "
                             "picked automatically for a write token")


def find_client_secret(given: str | None, search_dirs: list[Path] | None = None, *, write: bool = False,
                       confirm: Callable[[Path], bool] | None = None) -> tuple[Path | None, str]:
    """(OAuth client file, problem). given wins. With write=True nothing is ever discovered: an explicit file is
    required. Otherwise exactly one client_secret*.json in the current folder, else in ~/Downloads, is only used
    when confirm(path) says yes (the caller asks [y/N] in a terminal); confirm=None (not interactive) refuses it
    and names the file to pass. Several candidates or none: (None, why) so the caller asks for --client-secret."""
    if given:
        path = Path(given.strip().strip('"')).expanduser()
        return (path, "") if path.is_file() else (None, f"{path} not found")
    if write:
        return None, WRITE_NEEDS_EXPLICIT_FILE
    dirs = search_dirs if search_dirs is not None else [Path.cwd(), Path.home() / "Downloads"]
    for folder in dirs:
        try:
            found = sorted(p for p in folder.glob(CLIENT_SECRET_GLOB) if p.is_file())
        except OSError:
            continue
        if len(found) == 1:
            if confirm is None:
                return None, (f"{found[0]} was found but is not used without your confirmation outside an "
                              f"interactive terminal: pass --client-secret \"{found[0]}\"")
            if confirm(found[0]):
                return found[0], ""
            return None, f"{found[0]} not used (not confirmed): pass --client-secret <file>"
        if len(found) > 1:
            return None, f"{len(found)} client_secret*.json files in {folder}: pass --client-secret <file>"
    return None, "no client_secret*.json found in the current folder nor in Downloads: pass --client-secret <file>"


# What an OAuth client file must look like before any consent flow starts (Desktop app downloaded from Google).
GOOGLE_AUTH_HOST = "accounts.google.com"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
OOB_REDIRECT = "urn:ietf:wg:oauth:2.0:oob"  # in older Google downloads; not a network destination
MAX_CLIENT_SECRET_BYTES = 64 * 1024
_CLIENT_FILE_FIX = ("download it again from Google Cloud Console > APIs & Services > Credentials, OAuth client of "
                    "type Desktop app, and pass it with --client-secret <file>")


class ClientSecretRefused(SetupRefused):
    """The OAuth client file is not a genuine Google Desktop app client: refused before any flow or browser."""


def _refuse_client_file(path: str, cause: str) -> ClientSecretRefused:
    return ClientSecretRefused(f"OAuth client file {path}: {cause}. Nothing was opened nor stored. Fix: "
                               f"{_CLIENT_FILE_FIX}")


def _is_loopback(uri: object) -> bool:
    if uri == OOB_REDIRECT:
        return True
    if not isinstance(uri, str):
        return False
    try:
        parts = urlsplit(uri)
        _ = parts.port  # raises ValueError for a malformed port
    except ValueError:
        return False
    return parts.scheme == "http" and not parts.username and not parts.password and parts.hostname in LOOPBACK_HOSTS


def validate_client_secret(client_secret: str) -> dict:
    """The client config of an OAuth client file, or ClientSecretRefused (location, cause, fix; never a value).

    Accepted only when: the single root key is "installed" (Desktop app, never "web"), auth_uri is on
    https://accounts.google.com/, token_uri is exactly https://oauth2.googleapis.com/token, client_id and
    client_secret are present, and every redirect_uri is a local loopback (http://localhost, 127.0.0.1, [::1])."""
    path = str(client_secret)
    try:
        file = Path(path).expanduser()
        if file.stat().st_size > MAX_CLIENT_SECRET_BYTES:
            raise _refuse_client_file(path, "too large to be an OAuth client file")
        data = json.loads(file.read_text(encoding="utf-8-sig"))
    except ClientSecretRefused:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise _refuse_client_file(path, f"cannot be read ({type(exc).__name__})") from None
    except ValueError:
        raise _refuse_client_file(path, "is not valid JSON") from None
    if not isinstance(data, dict) or list(data) != ["installed"] or not isinstance(data["installed"], dict):
        keys = ", ".join(sorted(map(str, data))) if isinstance(data, dict) else type(data).__name__
        raise _refuse_client_file(path, f"the root key must be \"installed\" only (Desktop app), found: {keys or '-'}")
    info = data["installed"]
    auth_uri = info.get("auth_uri")
    try:
        auth_parts = urlsplit(auth_uri) if isinstance(auth_uri, str) else None
    except ValueError:
        auth_parts = None
    if (auth_parts is None or auth_parts.scheme != "https" or auth_parts.netloc != GOOGLE_AUTH_HOST
            or not auth_uri.startswith(f"https://{GOOGLE_AUTH_HOST}/")):
        raise _refuse_client_file(path, f"auth_uri must start with https://{GOOGLE_AUTH_HOST}/")
    if info.get("token_uri") != GOOGLE_TOKEN_URI:
        raise _refuse_client_file(path, f"token_uri must be exactly {GOOGLE_TOKEN_URI}")
    for key in ("client_id", "client_secret"):
        if not isinstance(info.get(key), str) or not info[key].strip():
            raise _refuse_client_file(path, f"{key} is missing")
    redirects = info.get("redirect_uris")
    if not isinstance(redirects, list) or not redirects or not all(_is_loopback(u) for u in redirects):
        raise _refuse_client_file(path, "redirect_uris must only list local loopback addresses (http://localhost)")
    return data


def consent(client_secret: str, scopes: list[str]) -> str:
    """Browser consent (installed app, localhost redirect) for exactly these scopes; returns the token JSON, whose
    "scopes" are the ones Google actually granted. The client file is validated BEFORE anything else
    (validate_client_secret): a hostile file never starts a flow nor opens a browser. Tests replace this function."""
    config = validate_client_secret(client_secret)
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_config(config, scopes)  # the validated content, not a second read
    creds = flow.run_local_server(port=0, open_browser=True, prompt="consent select_account")
    data = json.loads(creds.to_json())
    granted = getattr(creds, "granted_scopes", None)
    if isinstance(granted, str):
        granted = granted.split()
    if isinstance(granted, (list, tuple, set)) and granted:
        data["scopes"] = [str(s) for s in granted]
    return json.dumps(data)


def check_granted(raw: str, requested: list[str] | tuple[str, ...]) -> list[str]:
    """The scopes Google granted, or SetupRefused when the token must not be stored: no readable scope list, a
    forbidden scope (tagmanager.publish, edit.containerversions), or any scope that was not requested."""
    granted = recorded_scopes(raw)
    if granted is None:
        raise SetupRefused("the token returned by Google lists no scope; it was not stored. Rerun the same command")
    bad = sorted(set(granted) & set(FORBIDDEN_WRITE_SCOPES))
    if bad:
        raise SetupRefused(f"Google granted a forbidden scope ({', '.join(bad)}); the token was not stored. Revoke "
                           "the app's access at https://myaccount.google.com/permissions, then rerun")
    extra = sorted(set(granted) - set(requested))
    if extra:
        raise SetupRefused(f"Google granted scope(s) that were not requested ({', '.join(extra)}); the token was "
                           "not stored. Revoke the app's access at https://myaccount.google.com/permissions, then "
                           "rerun")
    return granted


# Read tokens that are not read-only: (fr, en). Google has no read-only scope for Merchant Center nor Indexing.
WRITE_CAPABLE_NAMES = {MERCHANT_SCOPE: "content (Merchant Center)", INDEXING_SCOPE: "indexing (Indexing API)"}
READ_TOKEN_WARNING = (
    "ATTENTION : le jeton de LECTURE du client {cid!r} contiendra aussi {names}. Google n'a pas de portée en lecture "
    "seule pour ces services : ce jeton PEUT les modifier (ce serveur ne l'utilise qu'en lecture). Ce n'est donc "
    "PAS un jeton « lecture seule ».",
    "WARNING: the READ token of client {cid!r} will also hold {names}. Google has no read-only scope for these "
    "services: this token CAN change them (this server only uses it to read). It is therefore NOT a read-only "
    "token.")


def write_capable(scopes: list[str] | tuple[str, ...]) -> list[str]:
    """The write-capable scopes (content, indexing) among scopes requested for a read token."""
    return [s for s in scopes if s in WRITE_CAPABLE_SCOPES]


def read_token_warning(cid: str, scopes: list[str] | tuple[str, ...], lang: str = "en") -> str:
    """'' for a truly read-only scope list; otherwise the explicit warning naming the write-capable scopes."""
    capable = write_capable(scopes)
    if not capable:
        return ""
    names = ", ".join(WRITE_CAPABLE_NAMES[s] for s in capable)
    return READ_TOKEN_WARNING[0 if lang == "fr" else 1].format(cid=cid, names=names)


def recorded_scopes(raw: str | None) -> list[str] | None:
    """Scopes recorded in a token JSON (None when unreadable or absent)."""
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


def store_client_token(ctx: object, kind: str, raw_json: str) -> str:
    """Store an OAuth token JSON in THIS client's own keyring entry for kind ("google", "google-w" or "gads"):
    the entry the server reads for it (ctx.secret_entry), never another one. Returns where (never the value)."""
    from . import secrets_store

    if kind not in ("google", "google-w", "gads"):
        raise SetupRefused(f"unknown token kind {kind!r}")
    legacy = bool(getattr(ctx, "legacy", False))
    names = getattr(ctx, "_secret_names", {}) or {}
    return secrets_store.set_secret(str(getattr(ctx, "client_id")), kind, raw_json, legacy=legacy,
                                    name=None if legacy else names.get(kind))


def run_client_setup(ctx: object, client_secret: str, *, write: bool = False,
                     scopes: list[str] | None = None) -> tuple[str, list[str]]:
    """Consent then store in the client's entry: "<client_id>/google" (read) or, with write=True, the separate
    "<client_id>/google-w" (the read token is never touched). Returns (where, scopes requested but not granted)."""
    if write:
        if getattr(ctx, "legacy", False):
            raise SetupRefused("writing needs a registered client (legacy mode has none)")
        if scopes is None:
            accounts = getattr(ctx, "accounts")
            scopes = write_scopes_for([p for p in GOOGLE_PLATFORMS if accounts(p)])
        if not scopes:
            raise SetupRefused("the client has no Google id (GA4, Search Console, Merchant Center, Tag Manager)")
        bad = sorted(set(scopes) & set(FORBIDDEN_WRITE_SCOPES))
        if bad:
            raise SetupRefused(f"scope(s) not allowed in this version: {', '.join(bad)}")
    elif scopes is None:
        scopes = read_scopes_for(ctx)
    raw = consent(client_secret, list(scopes))
    granted = check_granted(raw, list(scopes))  # forbidden or unrequested scope: nothing stored
    missing = [s for s in scopes if s not in granted]
    where = store_client_token(ctx, "google-w" if write else "google", raw)
    return where, missing

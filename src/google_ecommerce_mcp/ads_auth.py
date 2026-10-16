"""Google Ads OAuth token and the other ad platform secrets.

The Google Ads token is a separate token holding only the adwords scope. It lives in the OS keyring under
KEYRING_USER_ADS ("oauth-token-ads") or in GADS_TOKEN_FILE, never in the "oauth-token" entry (or
GOOGLE_TOKEN_FILE) of the read-only Google token, which `setup-ads` never touches."""

from __future__ import annotations

import getpass
import json
import ntpath
import os
import subprocess
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from .ads_common import hardened_session
from .ads_config import ADS_SCOPE, KEYRING_USER_ADS, SECRET_ENV, get_secret
from .auth import NotAuthenticated
from .config import KEYRING_SERVICE

__all__ = ["AdsTokenProvider", "NotAuthenticated", "run_setup_ads", "save_ads_token", "get_secret", "set_secret"]

NO_TOKEN = ("No Google Ads token found. Run `google-ecommerce-mcp setup-ads --client-secret <file>` first "
            "(it asks only for the Google Ads scope and leaves the other Google token untouched).")
EXPIRED = ("The stored Google Ads token has expired or was revoked. Run `google-ecommerce-mcp setup-ads "
           "--client-secret <file>` again. Apps whose OAuth consent screen is in Testing get tokens that expire "
           "after 7 days: publish the app (In production) to avoid this.")
UNREADABLE = ("The stored Google Ads token could not be read ({kind}). Run `google-ecommerce-mcp setup-ads "
              "--client-secret <file>` again.")
BAD_TOKEN_URI = ("The stored Google Ads token names an unexpected token endpoint; it was not refreshed so the "
                 "refresh token is never sent elsewhere. Run `google-ecommerce-mcp setup-ads --client-secret "
                 "<file>` again.")
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
REFRESH_NETWORK = ("The Google Ads token could not be refreshed (network: {kind}). Check the connection; behind a "
                   "corporate proxy set ADS_TRUST_ENV=1.")
REFRESH_LOCAL = ("The Google Ads token could not be refreshed (local TLS or I/O error: {kind}). Vérifier "
                 "REQUESTS_CA_BUNDLE / ADS_TRUST_ENV: with ADS_TRUST_ENV=1 the CA bundle path must exist.")
WINDOWS_ACL_WARNING = ("warning: the file permissions could not be restricted to your Windows account; keep the "
                       "file inside your user profile")
_IS_WINDOWS = os.name == "nt"


def _token_file_from_env() -> str | None:
    return os.getenv("GADS_TOKEN_FILE", "").strip() or None


def _same_file(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()


def _read_raw(token_file: str | None) -> str | None:
    if token_file:
        path = Path(token_file).expanduser()
        return path.read_text(encoding="utf-8") if path.exists() else None
    import keyring

    return keyring.get_password(KEYRING_SERVICE, KEYRING_USER_ADS)


def _stored_scopes(path: Path) -> list[str] | None:
    """Scopes recorded in an existing token file; None when it is not a readable token."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    scopes = data.get("scopes") if isinstance(data, dict) else None
    if isinstance(scopes, str):
        scopes = scopes.split()
    return list(scopes) if isinstance(scopes, list) else None


def _icacls_path() -> str:
    """Absolute path of icacls.exe (%SystemRoot%\\System32, C:\\Windows when unset): never resolved via PATH."""
    root = (os.environ.get("SystemRoot") or "").strip() or r"C:\Windows"
    return ntpath.join(root, "System32", "icacls.exe")


def _restrict_windows_acl(path: Path) -> bool:
    """Best effort on Windows, where mode 600 has no effect: drop inherited ACL entries and grant only the
    current user (icacls.exe by absolute path under %SystemRoot%, argument list, no shell, 15 s timeout).
    Never raises; True when icacls succeeded."""
    try:
        user = os.environ.get("USERNAME") or getpass.getuser()
        result = subprocess.run([_icacls_path(), str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                                check=False, timeout=15, capture_output=True)
        return result.returncode == 0
    except Exception:
        return False


def _write_private(path: Path, text: str) -> bool:
    """Write text to path created with mode 600 (no window where the token is world-readable on POSIX).
    On Windows the mode bits do nothing: the ACL is restricted best effort (_restrict_windows_acl).
    Returns False only when that Windows restriction failed."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(text.encode("utf-8"))
    try:
        os.chmod(path, 0o600)  # O_CREAT's mode only applies to a new file: tighten an existing one too
    except OSError:
        pass
    return _restrict_windows_acl(path) if _IS_WINDOWS else True


def save_ads_token(raw_json: str, token_file: str | None) -> str:
    """Store the Google Ads token in token_file (created mode 600) or the keyring entry oauth-token-ads;
    returns where. Refuses GOOGLE_TOKEN_FILE, and refuses to overwrite an existing file that is not a Google
    Ads token (scopes exactly [adwords]), so a read-only Google token passed by mistake is never lost.
    On Windows, when the file ACL cannot be restricted, the returned text ends with WINDOWS_ACL_WARNING."""
    if token_file:
        if _same_file(token_file, os.getenv("GOOGLE_TOKEN_FILE", "").strip() or None):
            raise ValueError("GADS_TOKEN_FILE must differ from GOOGLE_TOKEN_FILE: the Google Ads token is kept "
                             "apart from the read-only Google token")
        path = Path(token_file).expanduser()
        if path.exists() and _stored_scopes(path) != [ADS_SCOPE]:
            raise ValueError(f"{path} already exists and is not a Google Ads token (scopes other than adwords); "
                             "refusing to overwrite it. Choose another --token-file")
        path.parent.mkdir(parents=True, exist_ok=True)
        private = _write_private(path, raw_json)
        return f"file {path}" + ("" if private else f" ({WINDOWS_ACL_WARNING})")
    import keyring

    keyring.set_password(KEYRING_SERVICE, KEYRING_USER_ADS, raw_json)
    return "operating system keyring (entry oauth-token-ads)"


class AdsTokenProvider:
    """Same mechanics as auth.TokenProvider for the Google Ads token: lazy load, in-memory refresh.

    token_file: explicit file, or None to follow GADS_TOKEN_FILE read at each call (keyring entry
    oauth-token-ads when unset). Raises auth.NotAuthenticated with a setup-ads hint."""

    def __init__(self, token_file: str | None = None) -> None:
        self._explicit = token_file
        self._loaded_from: str | None = None
        self._creds: Any = None

    def _file(self) -> str | None:
        return self._explicit or _token_file_from_env()

    def _load(self) -> bool:
        source = self._file()
        if self._creds is not None and source != self._loaded_from:
            self._creds = None  # GADS_TOKEN_FILE changed: read the new token
        if self._creds is None:
            try:
                raw = _read_raw(source)
                if not raw:
                    return False
                self._creds = Credentials.from_authorized_user_info(json.loads(raw))
            except Exception as exc:  # unreadable file, broken JSON, no keyring backend: never echo the content
                self._creds = None
                raise NotAuthenticated(UNREADABLE.format(kind=type(exc).__name__)) from None
            self._loaded_from = source
        return True

    def headers(self) -> dict[str, str]:
        """{"Authorization": "Bearer <token>", "Content-Type": "application/json"}, refreshing if needed."""
        if not self._load():
            raise NotAuthenticated(NO_TOKEN)
        if not self._creds.valid:
            from google.auth.exceptions import RefreshError

            if getattr(self._creds, "token_uri", None) != GOOGLE_TOKEN_URI:
                self._creds = None
                raise NotAuthenticated(BAD_TOKEN_URI)

            from google.auth.exceptions import TransportError

            # Own session: no redirects followed, environment ignored unless ADS_TRUST_ENV=1 (proxy/CA only).
            try:
                self._creds.refresh(Request(session=hardened_session(GOOGLE_TOKEN_URI)))
            except TransportError as exc:
                raise NotAuthenticated(REFRESH_NETWORK.format(kind=type(exc).__name__)) from None
            except OSError as exc:  # CA bundle missing under ADS_TRUST_ENV=1, local I/O: no message echoed
                raise NotAuthenticated(REFRESH_LOCAL.format(kind=type(exc).__name__)) from None
            except RefreshError as exc:
                self._creds = None
                raise NotAuthenticated(EXPIRED if "invalid_grant" in str(exc)
                                       else f"Google Ads token refresh failed: {exc}") from exc
        return {"Authorization": f"Bearer {self._creds.token}", "Content-Type": "application/json"}

    def granted_scopes(self) -> list[str]:
        """Scopes recorded with the stored Google Ads token (empty when none is stored or readable)."""
        try:
            if not self._load():
                return []
        except NotAuthenticated:
            return []
        return sorted(getattr(self._creds, "granted_scopes", None) or self._creds.scopes or [])


def run_setup_ads(client_secret: str, token_file: str | None = None) -> str:
    """Browser consent asking ONLY for the adwords scope, then store the token apart (oauth-token-ads or
    token_file). Never writes the "oauth-token" entry nor GOOGLE_TOKEN_FILE. Returns where it was stored."""
    if token_file and _same_file(token_file, os.getenv("GOOGLE_TOKEN_FILE", "").strip() or None):
        raise ValueError("the Google Ads token file must differ from GOOGLE_TOKEN_FILE")
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(client_secret, [ADS_SCOPE])
    creds = flow.run_local_server(port=0, open_browser=True, prompt="consent select_account")
    return save_ads_token(creds.to_json(), token_file)


def set_secret(name: str, value: str) -> str:
    """Store a platform secret ("meta-access-token" or "openai-ads-key") in the OS keyring; returns where.
    The value is never printed nor logged."""
    if name not in SECRET_ENV:
        raise ValueError(f"unknown secret {name!r}; expected one of {sorted(SECRET_ENV)}")
    value = (value or "").strip()
    if not value:
        raise ValueError("empty secret")
    import keyring

    keyring.set_password(KEYRING_SERVICE, name, value)
    return f"operating system keyring (entry {name}); {SECRET_ENV[name]} still takes precedence when set"

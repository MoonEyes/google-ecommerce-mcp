"""OAuth token handling.

The token is stored in the operating system keyring (Windows Credential Manager, macOS Keychain,
Secret Service on Linux) unless GOOGLE_TOKEN_FILE points to a file. Refreshed access tokens stay
in memory; the stored refresh token is only written by `google-ecommerce-mcp setup`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import requests
from google.auth.exceptions import TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from .config import KEYRING_SERVICE, KEYRING_USER, READ_ONLY_SCOPES


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
    if token_file:
        path = Path(token_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw_json, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return f"file {path}"
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
    Only read-only scopes are requested unless the caller passes write-capable ones explicitly."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(client_secret, scopes or READ_ONLY_SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True, prompt="consent select_account")
    return save_token(creds.to_json(), token_file)

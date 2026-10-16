"""Configuration of the ad platform connectors, read from the environment at each use (never frozen at import).

Google Ads uses its own OAuth token (scope adwords), stored apart from the Google token of the other tools: the
adwords scope can write and Google offers no read-only variant, so it is never added to config.scopes_for()."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .config import KEYRING_SERVICE

ADS_SCOPE = "https://www.googleapis.com/auth/adwords"
# Google has no read-only Google Ads scope: a token holding it could write, even though this server only reads.
ADS_WRITE_CAPABLE = frozenset({ADS_SCOPE})
ADS_SCOPE_NOTE = ("The Google Ads scope (adwords) is write-capable and cannot be limited to reading; this server "
                  "only ever sends read requests on its allow-list.")

KEYRING_USER_ADS = "oauth-token-ads"           # never "oauth-token", which holds the Google token of other tools
KEYRING_USER_META = "meta-access-token"
KEYRING_USER_OPENAI = "openai-ads-key"
# Secret name (keyring user under KEYRING_SERVICE) -> environment variable read first.
SECRET_ENV = {KEYRING_USER_META: "META_ACCESS_TOKEN", KEYRING_USER_OPENAI: "OPENAI_ADS_API_KEY"}

DEFAULT_GADS_API_VERSION = "v26"
DEFAULT_META_API_VERSION = "v26.0"

__all__ = ["ADS_SCOPE", "ADS_WRITE_CAPABLE", "KEYRING_SERVICE", "KEYRING_USER_ADS", "KEYRING_USER_META",
           "KEYRING_USER_OPENAI", "SECRET_ENV", "AdsSettings", "get_secret"]


def _env(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def get_secret(name: str) -> str | None:
    """Secret by name ("meta-access-token" or "openai-ads-key"): its environment variable first, then the OS
    keyring entry (service google-ecommerce-mcp). None when absent or when no keyring backend works."""
    if name not in SECRET_ENV:
        raise ValueError(f"unknown secret {name!r}; expected one of {sorted(SECRET_ENV)}")
    value = _env(SECRET_ENV[name])
    if value:
        return value
    try:
        import keyring

        stored = keyring.get_password(KEYRING_SERVICE, name)
    except Exception:  # no backend (headless Linux), locked keychain...
        return None
    return (stored or "").strip() or None


def _digits_id(value: str | None) -> str | None:
    """123-456-7890 -> 1234567890; other values kept as given (validated by the tool)."""
    if not value:
        return None
    cleaned = value.replace("-", "").replace(" ", "")
    return cleaned or None


def _meta_account(value: str | None) -> str | None:
    """act_123 or 123 -> 123."""
    if not value:
        return None
    return value[4:] if value.lower().startswith("act_") else value


@dataclass(frozen=True)
class AdsSettings:
    gads_customer_id: str | None = None
    gads_login_customer_id: str | None = None
    # Optional: Google announced the developer token is no longer required from 09/09/2026; sent only if set.
    gads_developer_token: str | None = field(default=None, repr=False)
    gads_token_file: str | None = None
    gads_api_version: str = DEFAULT_GADS_API_VERSION
    meta_access_token: str | None = field(default=None, repr=False)
    meta_ad_account_id: str | None = None
    meta_api_version: str = DEFAULT_META_API_VERSION
    openai_ads_api_key: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, keyring_fallback: bool = True) -> "AdsSettings":
        """Read the environment now (call at each use). Meta token and OpenAI key fall back to the keyring
        (get_secret) unless keyring_fallback is False."""
        def secret(name: str) -> str | None:
            return get_secret(name) if keyring_fallback else _env(SECRET_ENV[name])

        return cls(
            gads_customer_id=_digits_id(_env("GADS_CUSTOMER_ID")),
            gads_login_customer_id=_digits_id(_env("GADS_LOGIN_CUSTOMER_ID")),
            gads_developer_token=_env("GADS_DEVELOPER_TOKEN"),
            gads_token_file=_env("GADS_TOKEN_FILE"),
            gads_api_version=_env("GADS_API_VERSION") or DEFAULT_GADS_API_VERSION,
            meta_access_token=secret(KEYRING_USER_META),
            meta_ad_account_id=_meta_account(_env("META_AD_ACCOUNT_ID")),
            meta_api_version=_env("META_API_VERSION") or DEFAULT_META_API_VERSION,
            openai_ads_api_key=secret(KEYRING_USER_OPENAI),
        )

    def secrets(self) -> list[str]:
        """Secret values to mask in any output (for AdsClient)."""
        return [s for s in (self.gads_developer_token, self.meta_access_token, self.openai_ads_api_key) if s]

    def status(self) -> dict:
        """Booleans only: what is configured. Never a secret value nor an id."""
        return {
            "google_ads": bool(self.gads_customer_id),
            "google_ads_login_customer_id": bool(self.gads_login_customer_id),
            "google_ads_developer_token": bool(self.gads_developer_token),
            "google_ads_token_file": bool(self.gads_token_file),
            "meta": bool(self.meta_access_token and self.meta_ad_account_id),
            "meta_access_token": bool(self.meta_access_token),
            "meta_ad_account_id": bool(self.meta_ad_account_id),
            "openai_ads": bool(self.openai_ads_api_key),
        }

"""Configuration read from environment variables. Nothing is hard-coded: every account id comes from the user."""

from __future__ import annotations

import os
from dataclasses import dataclass

# Scopes requested by `google-ecommerce-mcp setup`. All read-only, except Merchant Center:
# Google offers no read-only scope for the Merchant API, so `content` is required. This server
# never calls a write endpoint, but users should know the token itself could write.
SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/content",
    "https://www.googleapis.com/auth/tagmanager.readonly",
    "https://www.googleapis.com/auth/indexing",
]

KEYRING_SERVICE = "google-ecommerce-mcp"
KEYRING_USER = "oauth-token"


@dataclass(frozen=True)
class Settings:
    ga4_property_id: str | None
    gsc_site_url: str | None
    merchant_account_id: str | None
    gtm_container_id: str | None
    pagespeed_api_key: str | None
    token_file: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        def get(name: str) -> str | None:
            value = os.getenv(name, "").strip()
            return value or None

        return cls(
            ga4_property_id=get("GA4_PROPERTY_ID"),
            gsc_site_url=get("GSC_SITE_URL"),
            merchant_account_id=get("MERCHANT_ACCOUNT_ID"),
            gtm_container_id=get("GTM_CONTAINER_ID"),
            pagespeed_api_key=get("PAGESPEED_API_KEY"),
            token_file=get("GOOGLE_TOKEN_FILE"),
        )

    def status(self) -> dict:
        return {
            "ga4": bool(self.ga4_property_id),
            "search_console": bool(self.gsc_site_url),
            "merchant_center": bool(self.merchant_account_id),
            "tag_manager": bool(self.gtm_container_id),
            "pagespeed_api_key": bool(self.pagespeed_api_key),
            "token_storage": "file" if self.token_file else "os-keyring",
        }


class NotConfigured(Exception):
    """Raised when a tool is called for a service whose environment variable is missing."""

    def __init__(self, variable: str):
        super().__init__(f"{variable} is not set. Add it to the MCP server env in your client config.")
        self.variable = variable

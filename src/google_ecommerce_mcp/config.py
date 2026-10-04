"""Configuration read from environment variables. Nothing is hard-coded: every account id comes from the user."""

from __future__ import annotations

import os
from dataclasses import dataclass

# Scopes requested by `google-ecommerce-mcp setup`. The three below are read-only and always requested.
READ_ONLY_SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/tagmanager.readonly",
]
# Google offers no read-only scope for these two APIs: a token holding them could write, even though this
# server never does. They are requested only when the user opts in to the service (least privilege).
MERCHANT_SCOPE = "https://www.googleapis.com/auth/content"
INDEXING_SCOPE = "https://www.googleapis.com/auth/indexing"
WRITE_CAPABLE_SCOPES = {MERCHANT_SCOPE, INDEXING_SCOPE}

# Scope each API host needs, used to tell the user exactly which scope is missing.
SCOPE_BY_HOST = {
    "analyticsdata.googleapis.com": "https://www.googleapis.com/auth/analytics.readonly",
    "analyticsadmin.googleapis.com": "https://www.googleapis.com/auth/analytics.readonly",
    "www.googleapis.com/webmasters": "https://www.googleapis.com/auth/webmasters.readonly",
    "searchconsole.googleapis.com": "https://www.googleapis.com/auth/webmasters.readonly",
    "tagmanager.googleapis.com": "https://www.googleapis.com/auth/tagmanager.readonly",
    "merchantapi.googleapis.com": MERCHANT_SCOPE,
    "indexing.googleapis.com": INDEXING_SCOPE,
}


# Google Cloud API each host belongs to, used when Google answers that the API is not enabled in the project.
API_BY_HOST = {
    "analyticsdata.googleapis.com": "Google Analytics Data API",
    "analyticsadmin.googleapis.com": "Google Analytics Admin API",
    "www.googleapis.com/webmasters": "Google Search Console API",
    "searchconsole.googleapis.com": "Google Search Console API",
    "tagmanager.googleapis.com": "Tag Manager API",
    "merchantapi.googleapis.com": "Merchant API",
    "indexing.googleapis.com": "Web Search Indexing API",
    "www.googleapis.com/pagespeedonline": "PageSpeed Insights API",
}


def scopes_for(merchant: bool = False, indexing: bool = False) -> list[str]:
    """Read-only scopes, plus the write-capable ones only for services the user enabled."""
    scopes = list(READ_ONLY_SCOPES)
    if merchant:
        scopes.append(MERCHANT_SCOPE)
    if indexing:
        scopes.append(INDEXING_SCOPE)
    return scopes


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

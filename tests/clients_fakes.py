"""Shared offline fixtures for the client registry tests: a fake keyring and a clean environment."""

from __future__ import annotations

import json
import types

LEGACY_VARS = ("GA4_PROPERTY_ID", "GSC_SITE_URL", "MERCHANT_ACCOUNT_ID", "GTM_CONTAINER_ID", "GADS_CUSTOMER_ID",
               "GADS_LOGIN_CUSTOMER_ID", "META_AD_ACCOUNT_ID", "META_ACCESS_TOKEN", "OPENAI_ADS_API_KEY",
               "PAGESPEED_API_KEY", "GOOGLE_TOKEN_FILE", "GADS_TOKEN_FILE", "GEM_CLIENT", "GEM_CLIENTS_FILE",
               "GEM_STATE_DIR", "LC_ALL", "LC_MESSAGES", "LANG")


class FakeKeyring(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("keyring")
        self.store: dict[tuple[str, str], str] = {}
        self.reads: list[str] = []

    def get_password(self, service: str, user: str):
        self.reads.append(user)
        return self.store.get((service, user))

    def set_password(self, service: str, user: str, value: str) -> None:
        self.store[(service, user)] = value

    def delete_password(self, service: str, user: str) -> None:
        self.store.pop((service, user), None)

    def users(self) -> set[str]:
        return {user for _, user in self.store}


def setup_env(monkeypatch, tmp_path) -> tuple[FakeKeyring, object]:
    for name in LEGACY_VARS:
        monkeypatch.delenv(name, raising=False)
    for name in list(__import__("os").environ):
        if name.startswith("GEM_SECRET_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEM_LANG", "en")
    path = tmp_path / "cfg" / "clients.json"
    monkeypatch.setenv("GEM_CLIENTS_FILE", str(path))
    fake = FakeKeyring()
    monkeypatch.setitem(__import__("sys").modules, "keyring", fake)
    return fake, path


def write_registry(path, clients: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "clients": clients}), encoding="utf-8")


ACME = {"label": "Acme SA", "sandbox": True,
        "accounts": {"ga4": {"property_id": "111111111"}, "gsc": {"site_url": "sc-domain:acme.fr"},
                     "merchant": {"account_id": "1111"},
                     "gtm": {"account_id": "6111", "container_id": "2111", "public_id": "GTM-ACME111"},
                     "google_ads": {"customer_id": "1111111111", "login_customer_id": "9999999999"},
                     "meta": {"ad_account_id": "act_1111", "pixel_id": "1112", "catalog_id": "1113",
                              "business_id": "8888", "page_id": "1114", "ig_user_id": "1115"},
                     "openai_ads": {"account_id": "acct_acme"}}}
BETA = {"label": "Beta SARL",
        "accounts": {"ga4": {"property_id": "222222222"}, "gsc": {"site_url": "https://www.beta.com/"},
                     "merchant": {"account_id": "2222"},
                     "gtm": {"account_id": "6222", "container_id": "2222", "public_id": "GTM-BETA222"},
                     "google_ads": {"customer_id": "2222222222", "login_customer_id": "9999999999"},
                     "meta": {"ad_account_id": "2221", "pixel_id": "2222", "catalog_id": "2223",
                              "business_id": "8888", "page_id": "2224", "ig_user_id": "2225"},
                     "openai_ads": {"account_id": "acct_beta"}}}

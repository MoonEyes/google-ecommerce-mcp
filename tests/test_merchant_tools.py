"""Merchant API v1 reads (merchant_products, merchant_account_issues): full product ids lang~feedLabel~offerId,
bounded pagination, base64url ids, account issues by severity, foreign account refused without any request, two
isolated clients, read token only. Also holds the offline doubles reused by test_merchant_write.py: a fake Merchant
API behind the AdsClient sessions of merchant_tools / merchant_write and a two-client registry (acme 1111, beta
2222) with separate read and write tokens. Zero network (ads_fakes.no_network is autouse)."""

from __future__ import annotations

import base64
import copy
import re
from urllib.parse import urlsplit

import pytest

from ads_fakes import GOOGLE_ACCESS_PREFIX, FakeResponse, no_network  # noqa: F401
from clients_fakes import ACME, BETA, setup_env, write_registry
from google_ecommerce_mcp import ads_common, google_write as gw, merchant_tools, merchant_write, write_core, \
    write_policy
from google_ecommerce_mcp.write_journal import Journal
from test_google_write import AUTH, bearer, token_json

MAPI = "https://merchantapi.googleapis.com"
WRITE_SCOPES = [AUTH + "content"]
READ_SCOPES = [AUTH + "content"]


def decode_segment(segment: str) -> str:
    if "~" in segment:
        return segment
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)).decode("utf-8")


class FakeMerchant:
    """Merchant API v1 products, productInputs.patch, data sources and account issues in memory."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.products: dict[str, dict[str, dict]] = {}     # account -> product id -> processed product
        self.sources: dict[str, dict[str, dict]] = {}      # account -> data source id -> data source
        self.issues: dict[str, list[dict]] = {}
        self.process = True  # False: the processed product keeps its old values (Merchant Center latency)

    def writes(self) -> list[dict]:
        return [c for c in self.calls if c["method"] in ("POST", "PUT", "PATCH", "DELETE")]

    def add_product(self, account: str, offer: str, *, lang="fr", label="FR", ds="77", **attrs) -> dict:
        pid = f"{lang}~{label}~{offer}"
        product = {"name": f"accounts/{account}/products/{pid}", "offerId": offer, "contentLanguage": lang,
                   "feedLabel": label, "dataSource": f"accounts/{account}/dataSources/{ds}",
                   "productAttributes": copy.deepcopy(attrs),
                   "productStatus": {"itemLevelIssues": [{"code": "missing_gtin"}],
                                     "destinationStatuses": [{"reportingContext": "SHOPPING_ADS",
                                                              "approvedCountries": ["FR"]}]}}
        self.products.setdefault(account, {})[pid] = product
        return product

    def add_source(self, account: str, ds: str, input_type: str = "API", kind: str = "primary") -> None:
        key = "primaryProductDataSource" if kind == "primary" else "supplementalProductDataSource"
        self.sources.setdefault(account, {})[ds] = {"name": f"accounts/{account}/dataSources/{ds}",
                                                    "dataSourceId": ds, "input": input_type, key: {}}

    def request(self, method, url, **kwargs):
        headers = kwargs.get("headers") or {}
        params = kwargs.get("params") or {}
        self.calls.append({"method": method, "url": url, "auth": headers.get("Authorization"),
                           "params": copy.deepcopy(kwargs.get("params")), "json": copy.deepcopy(kwargs.get("json"))})
        path = urlsplit(url).path
        m = re.fullmatch(r"/products/v1/accounts/(\d+)/products", path)
        if m and method == "GET":
            items = list(self.products.get(m.group(1), {}).values())
            start = int(params.get("pageToken") or 0)
            size = int(params.get("pageSize") or 25)
            page = items[start:start + size]
            out = {"products": copy.deepcopy(page)}
            if start + size < len(items):
                out["nextPageToken"] = str(start + size)
            return FakeResponse(200, out)
        m = re.fullmatch(r"/products/v1/accounts/(\d+)/products/([^/]+)", path)
        if m and method == "GET":
            product = self.products.get(m.group(1), {}).get(decode_segment(m.group(2)))
            if product is None:
                return FakeResponse(404, {"error": {"code": 404, "message": "Not found", "status": "NOT_FOUND"}})
            return FakeResponse(200, copy.deepcopy(product))
        m = re.fullmatch(r"/datasources/v1/accounts/(\d+)/dataSources/(\d+)", path)
        if m and method == "GET":
            source = self.sources.get(m.group(1), {}).get(m.group(2))
            if source is None:
                return FakeResponse(404, {"error": {"code": 404, "message": "Not found"}})
            return FakeResponse(200, copy.deepcopy(source))
        m = re.fullmatch(r"/accounts/v1/accounts/(\d+)/issues", path)
        if m and method == "GET":
            return FakeResponse(200, {"accountIssues": copy.deepcopy(self.issues.get(m.group(1), []))})
        m = re.fullmatch(r"/products/v1/accounts/(\d+)/productInputs/([^/]+)", path)
        if m and method == "PATCH":
            return self._patch(m.group(1), decode_segment(m.group(2)), params, kwargs.get("json") or {})
        return FakeResponse(404, {"error": {"code": 404, "message": "unexpected url"}})

    def _patch(self, account, pid, params, body):
        source = str(params.get("dataSource", ""))
        ds = source.rsplit("/", 1)[-1]
        if not source.startswith(f"accounts/{account}/") or self.sources.get(account, {}).get(ds, {}).get(
                "input") != "API":
            return FakeResponse(400, {"error": {"code": 400, "message": "data source must be of type API"}})
        product = self.products.get(account, {}).get(pid)
        if product is None:
            return FakeResponse(404, {"error": {"code": 404, "message": "Not found"}})
        attrs = body.get("productAttributes") or {}
        if self.process:
            for path in str(params.get("updateMask", "")).split(","):
                name = path.split(".", 1)[1]
                if name in attrs:
                    product["productAttributes"][name] = copy.deepcopy(attrs[name])
                else:  # Google's semantics: a masked field without a value is deleted
                    product["productAttributes"].pop(name, None)
        lang, label, offer = pid.split("~", 2)
        return FakeResponse(200, {"name": f"accounts/{account}/productInputs/{pid}", "offerId": offer,
                                  "contentLanguage": lang, "feedLabel": label, "productAttributes": attrs})


def registry_entry(base: dict, *, account: str, enabled: bool = True) -> dict:
    entry = copy.deepcopy(base)
    entry["write"] = {"merchant": {"enabled": enabled, "account_allow": [account],
                                   "max_daily_budget_micros": 500_000_000, "cap_currency": "EUR"}}
    return entry


@pytest.fixture
def mworld(monkeypatch, tmp_path):
    """Two clients (acme: Merchant 1111, beta: 2222), each with its own read and write token, the fake Merchant
    API behind every AdsClient session of merchant_tools / merchant_write, a fresh journal / handler table /
    allow-list registry. acme has products SKU1 (title, price 12.99 EUR, in stock) and SKU2 (no description),
    data sources 77 (API, primary) and 88 (FILE)."""
    _, path = setup_env(monkeypatch, tmp_path)
    write_registry(path, {"acme": registry_entry(ACME, account="1111"),
                          "beta": registry_entry(BETA, account="2222")})
    monkeypatch.setenv("GEM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GEM_WRITE", "1")
    monkeypatch.setenv("GEM_WRITE_HMAC_KEY", "ef" * 32)
    monkeypatch.delenv("GEM_LIVE_TEST", raising=False)
    for cid in ("acme", "beta"):
        monkeypatch.setenv(f"GEM_SECRET_{cid.upper()}_GOOGLE", token_json(bearer(cid, "READ"), READ_SCOPES))
        monkeypatch.setenv(f"GEM_SECRET_{cid.upper()}_GOOGLE_W", token_json(bearer(cid, "WRITE"), WRITE_SCOPES))
    monkeypatch.setattr(write_core, "journal", Journal())
    monkeypatch.setattr(write_core, "_HANDLERS", {})
    monkeypatch.setattr(write_policy, "_FIELD_ALLOWLIST", {})
    monkeypatch.setattr(write_policy, "_FROZEN", False)
    monkeypatch.setattr(write_policy, "_TEST_PERMISSIVE", False)
    ads_common._forget_consumed_grants()
    gw._forget_tokens()
    server = FakeMerchant()
    server.add_source("1111", "77")
    server.add_source("1111", "88", input_type="FILE")
    server.add_source("2222", "99")
    server.add_product("1111", "SKU1", title="Blue mug", link="https://acme.fr/p/sku1", availability="IN_STOCK",
                       price={"amountMicros": "12990000", "currencyCode": "EUR"}, description="A mug")
    server.add_product("1111", "SKU2", title="Red mug", availability="IN_STOCK",
                       price={"amountMicros": "9990000", "currencyCode": "EUR"})
    server.add_product("2222", "B1", lang="en", label="US", ds="99", title="Beta thing",
                       price={"amountMicros": "5000000", "currencyCode": "USD"})
    for client in (merchant_tools.READ, merchant_write.WRITE):
        monkeypatch.setattr(client, "session", server)
        monkeypatch.setattr(client, "BLOCKED_ATTEMPTS", [])
        client.cache.clear()
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")
    merchant_tools.register(mcp)
    merchant_write.register(mcp)
    yield server
    gw._forget_tokens()
    ads_common._forget_consumed_grants()


# ------------------------------------------------------------------ ids
@pytest.mark.parametrize("text, parts", [
    ("fr~FR~SKU1", ("fr", "FR", "SKU1")),
    ("accounts/1111/products/fr~FR~SKU1", ("fr", "FR", "SKU1")),
    ("en~US~a~b", ("en", "US", "a~b")),
    (base64.urlsafe_b64encode(b"en~US~a/b 1").decode().rstrip("="), ("en", "US", "a/b 1")),
])
def test_product_ids_parse(text, parts):
    assert merchant_tools.parse_product_id(text) == parts


@pytest.mark.parametrize("text", ["online~fr~FR~SKU1", "SKU1", "FR~fr", "fr~FR~", "fr~bad label~x", "f~FR~x"])
def test_bad_product_ids_are_refused(text):
    with pytest.raises(ValueError):
        merchant_tools.parse_product_id(text)


def test_product_segment_is_plain_or_base64url():
    assert merchant_tools.product_segment("fr", "FR", "SKU-1.a_b") == "fr~FR~SKU-1.a_b"
    segment = merchant_tools.product_segment("en", "US", "a/b%c~d")
    assert "~" not in segment and "/" not in segment and "=" not in segment and "%" not in segment
    assert merchant_tools.parse_product_id(segment) == ("en", "US", "a/b%c~d")


# ------------------------------------------------------------------ reads
def test_products_have_full_ids_and_use_the_read_token(mworld):
    out = merchant_tools.merchant_products(client_id="acme")
    assert out["client_id"] == "acme" and out["account_id"] == "1111" and out["count"] == 2
    first = out["products"][0]
    assert first["id"] == "fr~FR~SKU1" and first["name"] == "accounts/1111/products/fr~FR~SKU1"
    assert first["price"] == {"amount_micros": "12990000", "currency_code": "EUR", "amount": "12.99"}
    assert first["data_source"] == "accounts/1111/dataSources/77" and first["issues"] == 1
    assert first["destinations"][0]["reporting_context"] == "SHOPPING_ADS"
    assert out["next_page_token"] is None and "untrusted_text" in out
    assert {c["auth"] for c in mworld.calls} == {"Bearer " + bearer("acme", "READ")}
    assert mworld.writes() == []


def test_products_are_paged_and_bounded(mworld):
    for i in range(5):
        mworld.add_product("1111", f"X{i}", title=f"x{i}")
    page = merchant_tools.merchant_products(limit=3, client_id="acme")
    assert page["count"] == 3 and page["next_page_token"] == "3"
    rest = merchant_tools.merchant_products(limit=3, page_token=page["next_page_token"], client_id="acme")
    assert rest["count"] == 3 and rest["next_page_token"] == "6"
    merchant_tools.merchant_products(limit=10_000, client_id="acme")
    assert mworld.calls[-1]["params"]["pageSize"] == merchant_tools.MAX_PRODUCTS
    assert merchant_tools.merchant_products(page_token="a b", client_id="acme")["error"] == "bad_request"


def test_account_issues_by_severity(mworld):
    mworld.issues["1111"] = [
        {"name": "accounts/1111/issues/1", "title": "Website not claimed", "severity": "CRITICAL",
         "impactedDestinations": [{"reportingContext": "SHOPPING_ADS",
                                   "impacts": [{"regionCode": "FR", "severity": "CRITICAL"}]}],
         "documentationUri": "https://support.google.com/merchants/answer/1"},
        {"name": "accounts/1111/issues/2", "title": "Shipping", "severity": "ERROR"},
        {"name": "accounts/1111/issues/3", "title": "Tip", "severity": "SUGGESTION"}]
    out = merchant_tools.merchant_account_issues(client_id="acme")
    assert out["count"] == 3 and out["by_severity"] == {"CRITICAL": 1, "ERROR": 1, "SUGGESTION": 1}
    assert out["issues"][0]["impacted_destinations"][0]["impacts"] == [{"region_code": "FR", "severity": "CRITICAL"}]
    assert mworld.calls[0]["url"] == f"{MAPI}/accounts/v1/accounts/1111/issues"
    assert mworld.calls[0]["params"] == {"pageSize": 100, "languageCode": "en-US"}
    assert merchant_tools.merchant_account_issues(language_code="x;y", client_id="acme")["error"] == "bad_request"


def test_foreign_account_is_refused_without_any_request(mworld):
    assert merchant_tools.merchant_products(account_id="2222", client_id="acme")["error"] == "id_not_owned"
    assert merchant_tools.merchant_account_issues(account_id="2222", client_id="acme")["error"] == "id_not_owned"
    assert merchant_tools.merchant_products(account_id="abc", client_id="acme")["error"] == "bad_request"
    assert mworld.calls == []


def test_two_clients_are_isolated(mworld):
    acme = merchant_tools.merchant_products(client_id="acme")
    beta = merchant_tools.merchant_products(client_id="beta")
    assert [p["id"] for p in beta["products"]] == ["en~US~B1"]
    assert {p["id"] for p in acme["products"]} == {"fr~FR~SKU1", "fr~FR~SKU2"}
    by_account = {}
    for call in mworld.calls:
        by_account.setdefault(call["url"].split("/accounts/")[1].split("/")[0], set()).add(call["auth"])
    assert by_account == {"1111": {"Bearer " + bearer("acme", "READ")}, "2222": {"Bearer " + bearer("beta", "READ")}}


def test_read_client_has_no_write_list():
    assert merchant_tools.READ.write_allow_list is None
    assert {m for m, _ in merchant_tools.READ.allow_list.patterns} == {"GET"}

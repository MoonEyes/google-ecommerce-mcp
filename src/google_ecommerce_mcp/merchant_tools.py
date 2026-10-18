"""Merchant API v1: product list and account issues (read only), and the product id helpers of merchant_write.

Read (read token, scope content): merchant_products, merchant_account_issues. server.py already offers
merchant_data_sources, merchant_product_issues and merchant_report_query; this module adds only what it lacks.

Product ids (Merchant API v1): "contentLanguage~feedLabel~offerId" (en~FR~SKU123), WITHOUT the "online~" prefix of
v1beta. When the offerId holds a character outside [A-Za-z0-9._-] (for example '/', '%', '~' or a space) the id
travels as the unpadded base64url encoding of that same text, which the API accepts in place of the plain form
(product_segment / parse_product_id below). The account is the client's Merchant account (clients.json
merchant.account_id); another account is refused before any request.
"""

from __future__ import annotations

import base64
import binascii
import re
from decimal import Decimal
from typing import Annotated, Any, Optional

from pydantic import Field

from . import google_write as gw

PLATFORM = "merchant"
MAPI = "https://merchantapi.googleapis.com"
_A = r"https://merchantapi\.googleapis\.com"
READ_RULES = [("GET", _A + r"/products/v1/accounts/[0-9]+/products"),
              ("GET", _A + r"/accounts/v1/accounts/[0-9]+/issues")]
READ = gw.google_read_client(PLATFORM, READ_RULES)

TOOLS = ("merchant_products", "merchant_account_issues")
MAX_PRODUCTS = 250
ISSUE_PAGES = 5
_LANG = re.compile(r"[a-z]{2,3}")
_LABEL = re.compile(r"[A-Za-z0-9_-]{1,20}")
_PLAIN_OFFER = re.compile(r"[A-Za-z0-9._-]{1,50}")
_B64 = re.compile(r"[A-Za-z0-9_-]{4,400}")
_OFFER_CHARS = re.compile(r"[\x20-\x7e -￿]{1,50}")

ACCOUNT_ID = Annotated[str, Field(description="Merchant Center account id of the client (empty: the configured "
                                                "one)")]
CLIENT_ID = Annotated[Optional[str], Field(description="Client id from clients_list. Required when several clients "
                                                       "are registered; empty uses the only client")]


# ------------------------------------------------------------------ ids
def account_of(ctx: Any, account_id: str = "") -> str:
    """The client's Merchant account id (digits); a given id must be the client's own (ClientError)."""
    value = (account_id or "").strip()
    if value.startswith("accounts/"):
        value = value[len("accounts/"):]
    if value and not value.isdigit():
        raise ValueError(f"account_id must be the numeric Merchant Center account id, got {account_id!r}")
    return gw.google_account_binding(ctx, PLATFORM, "account_id", value)


def product_id(lang: str, label: str, offer: str) -> str:
    return f"{lang}~{label}~{offer}"


def _check_parts(lang: str, label: str, offer: str) -> tuple[str, str, str]:
    if not _LANG.fullmatch(lang):
        raise ValueError(f"contentLanguage {lang!r}: a two-letter ISO 639-1 code such as en or fr")
    if not _LABEL.fullmatch(label):
        raise ValueError(f"feedLabel {label!r}: 1 to 20 letters, digits, - or _")
    if not _OFFER_CHARS.fullmatch(offer) or offer != offer.strip():
        raise ValueError("offerId: 1 to 50 printable characters, no leading or trailing space")
    return lang, label, offer


def parse_product_id(text: Any) -> tuple[str, str, str]:
    """(contentLanguage, feedLabel, offerId) of "lang~feedLabel~offerId", of a resource name
    accounts/{a}/products/{id} or productInputs/{id}, or of the unpadded base64url form. Raises ValueError."""
    raw = str(text or "").strip()
    match = re.fullmatch(r"accounts/[0-9]+/(?:products|productInputs)/(.+)", raw)
    if match:
        raw = match.group(1)
    if raw.startswith("online~"):
        raise ValueError("Merchant API v1 product ids have no 'online~' prefix: use contentLanguage~feedLabel~offerId")
    if "~" not in raw:
        if not _B64.fullmatch(raw):
            raise ValueError(f"product id {str(text)[:80]!r}: expected contentLanguage~feedLabel~offerId "
                             "(e.g. en~FR~SKU123)")
        try:
            raw = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            raise ValueError("product id: not a valid base64url-encoded id") from None
        if raw.count("~") < 2:
            raise ValueError("product id: the decoded id is not contentLanguage~feedLabel~offerId")
    parts = raw.split("~", 2)
    if len(parts) != 3:
        raise ValueError(f"product id {str(text)[:80]!r}: expected contentLanguage~feedLabel~offerId")
    return _check_parts(*parts)


def product_segment(lang: str, label: str, offer: str) -> str:
    """URL path segment of a product: the plain id, or its unpadded base64url form when the offerId holds a
    character the plain form cannot carry."""
    _check_parts(lang, label, offer)
    if _PLAIN_OFFER.fullmatch(offer):
        return product_id(lang, label, offer)
    return base64.urlsafe_b64encode(product_id(lang, label, offer).encode("utf-8")).decode("ascii").rstrip("=")


def money(price: Any) -> dict | None:
    """{"amount_micros", "currency_code", "amount"} of a Merchant Price, None when absent."""
    if not isinstance(price, dict) or "amountMicros" not in price:
        return None
    micros = str(price.get("amountMicros"))
    try:
        amount = f"{(Decimal(micros) / Decimal(1_000_000)).normalize():f}"
    except ArithmeticError:
        amount = None
    return {"amount_micros": micros, "currency_code": price.get("currencyCode"), "amount": amount}


def _product(p: dict) -> dict:
    attrs = p.get("productAttributes") if isinstance(p.get("productAttributes"), dict) else {}
    status = p.get("productStatus") if isinstance(p.get("productStatus"), dict) else {}
    lang, label, offer = p.get("contentLanguage"), p.get("feedLabel"), p.get("offerId")
    destinations = [{"reporting_context": d.get("reportingContext"),
                     "approved": d.get("approvedCountries", []), "pending": d.get("pendingCountries", []),
                     "disapproved": d.get("disapprovedCountries", [])}
                    for d in status.get("destinationStatuses", []) if isinstance(d, dict)]
    return {"id": product_id(lang, label, offer) if lang and label and offer else None, "name": p.get("name"),
            "offer_id": offer, "content_language": lang, "feed_label": label, "data_source": p.get("dataSource"),
            "title": attrs.get("title"), "link": attrs.get("link"), "availability": attrs.get("availability"),
            "price": money(attrs.get("price")), "sale_price": money(attrs.get("salePrice")),
            "issues": len(status.get("itemLevelIssues", []) or []), "destinations": destinations}


# ------------------------------------------------------------------ read tools
@gw.tool_guard("current state; Merchant Center applies product changes with up to 30 minutes of delay")
def merchant_products(
    limit: Annotated[int, Field(description=f"Products in this page (1 to {MAX_PRODUCTS})", ge=1,
                                le=MAX_PRODUCTS)] = 50,
    page_token: Annotated[str, Field(description="next_page_token of the previous page; empty = first page")] = "",
    account_id: ACCOUNT_ID = "",
    client_id: CLIENT_ID = None,
) -> dict:
    """One page of the client's Merchant Center products (processed view): full product id
    contentLanguage~feedLabel~offerId (the id merchant_plan_product_patch takes), title, link, availability,
    price, primary data source, number of item issues and status per destination. Pass next_page_token for the
    next page. Read only."""
    ctx = gw.resolve(client_id)
    account = account_of(ctx, account_id)
    size = max(1, min(int(limit), MAX_PRODUCTS))
    params: dict[str, Any] = {"pageSize": size}
    token = str(page_token or "").strip()
    if token:
        if len(token) > 2000 or not re.fullmatch(r"[A-Za-z0-9_=+/.-]+", token):
            raise ValueError("page_token: pass next_page_token exactly as returned")
        params["pageToken"] = token
    resp = gw.read_call(READ, ctx, "GET", f"{MAPI}/products/v1/accounts/{account}/products", params=params)
    if "error" in resp:
        return {**resp, "client_id": ctx.client_id}
    products = [_product(p) for p in resp.get("products", []) if isinstance(p, dict)][:size]
    return {"client_id": ctx.client_id, "account_id": account, "products": products, "count": len(products),
            "next_page_token": resp.get("nextPageToken") or None,
            "untrusted_text": gw.untrusted("products[].title", "products[].link", "products[].offer_id")}


def _issue(i: dict) -> dict:
    return {"name": i.get("name"), "title": i.get("title"), "severity": i.get("severity"),
            "detail": i.get("detail"), "documentation_uri": i.get("documentationUri"),
            "impacted_destinations": [{"reporting_context": d.get("reportingContext"),
                                       "impacts": [{"region_code": x.get("regionCode"), "severity": x.get("severity")}
                                                   for x in d.get("impacts", []) if isinstance(x, dict)]}
                                      for d in i.get("impactedDestinations", []) if isinstance(d, dict)]}


@gw.tool_guard("current state of the account")
def merchant_account_issues(
    language_code: Annotated[str, Field(description="Language of the issue texts (BCP-47, e.g. en-US, fr)")] =
    "en-US",
    account_id: ACCOUNT_ID = "",
    client_id: CLIENT_ID = None,
) -> dict:
    """Account-level issues of the client's Merchant Center account (suspensions, policy, shipping, tax, website
    claim...): title, severity (CRITICAL, ERROR, SUGGESTION), detail, documentation link and impacted
    destinations per country, with counts by severity. Read only."""
    ctx = gw.resolve(client_id)
    account = account_of(ctx, account_id)
    lang = str(language_code or "en-US").strip()
    if not re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?", lang):
        raise ValueError(f"language_code {language_code!r}: a BCP-47 code such as en-US")
    items: list[dict] = []
    token = None
    partial = None
    for page in range(ISSUE_PAGES):
        params: dict[str, Any] = {"pageSize": 100, "languageCode": lang, **({"pageToken": token} if token else {})}
        resp = gw.read_call(READ, ctx, "GET", f"{MAPI}/accounts/v1/accounts/{account}/issues", params=params)
        if "error" in resp:
            if page == 0:
                return {**resp, "client_id": ctx.client_id}
            partial = resp
            break
        items.extend(i for i in resp.get("accountIssues", []) if isinstance(i, dict))
        token = resp.get("nextPageToken")
        if not token:
            break
    issues = [_issue(i) for i in items]
    counts: dict[str, int] = {}
    for issue in issues:
        key = str(issue.get("severity") or "UNSPECIFIED")
        counts[key] = counts.get(key, 0) + 1
    out = {"client_id": ctx.client_id, "account_id": account, "issues": issues, "count": len(issues),
           "by_severity": counts, "truncated": bool(token) and partial is None,
           "untrusted_text": gw.untrusted("issues[].title", "issues[].detail")}
    if partial:
        out.update(partial=True, partial_errors=[{"step": "next page", **partial}], truncated=True)
    return out


def register(mcp: Any) -> list[str]:
    """Register the Merchant read tools (read-only)."""
    return gw.register_tools(mcp, [("Merchant Center products", merchant_products),
                                   ("Merchant Center account issues", merchant_account_issues)], gw.READ_ONLY)

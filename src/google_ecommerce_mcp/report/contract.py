"""Input contract of the report generator, its sanitising and the accessors sections use.

The input is ONE JSON document built from the MCP tool results; the generator never calls a platform::

    {"meta": {"client": "...", "lang": "fr", "currency": "EUR", "generated_at": "YYYY-MM-DD",
              "prepared_by": "...", "sample": false,
              "brand": {"name": "...", "accent": "#1F6F5B", "logo_data_uri": "data:image/png;base64,..."}},
     "periods": {"current": ["YYYY-MM-DD", "YYYY-MM-DD"], "previous": [...], "yoy": [...]},
     "goals": {"revenue": 25000, "orders": 175, "aov": 150, "conversion_rate": 0.02, "sessions": 9000,
               "new_customers": 120},
     "sources": {"ga4": {}, "search_console": {}, "merchant_center": {}, "google_ads": {}, "meta_ads": {},
                 "openai_ads": {}, "ads_overview": {}, "web_vitals": {}},
     "errors": [{"source": "...", "code": "...", "message": "..."}],
     "events": [{"date": "YYYY-MM-DD", "label": "..."}],
     "notes": "operator text in mini-markdown"}

Every source is optional. A source absent (missing, null or {}) is shown as "not provided"; a source that is
a tool error (an object with an ``error`` key) is shown as "failed"; neither is ever turned into a zero.

Shape of each source. A *tool output* is the unmodified JSON of the MCP tool. A *periodic* value is either a
bare tool output (taken as the current period) or ``{"current": <output>, "previous": <output>,
"yoy": <output>}`` (each key optional):

=================  =========================================================================================
source             parts (``part`` argument of :func:`view`); the first one is the default part
=================  =========================================================================================
ga4                ``kpi``: periodic ``ga4_report`` whose ``totals`` hold purchaseRevenue, ecommercePurchases,
                   sessions (optionally totalUsers, newUsers, firstTimePurchasers); run with
                   ``dimensions: ["date"]`` its rows also feed the KPI sparklines.
                   ``funnel``: periodic ``ga4_report`` by ``eventName`` with ``eventCount``.
                   ``channels``: periodic ``ga4_report`` by ``sessionDefaultChannelGroup``.
                   ``products``: periodic ``ga4_report`` by ``itemName``.
search_console     ``totals``: periodic ``gsc_performance`` (any dimension; its ``totals`` are used; with
                   ``dimensions: ["date"]`` the rows give the daily curve). ``queries``, ``pages``: periodic
                   ``gsc_performance`` by ``query`` / ``page``.
merchant_center    ``product_issues``: ``merchant_product_issues``. ``performance``: ``merchant_report_query``.
                   ``statuses``: optional ``{"approved": n, "limited": n, "disapproved": n}`` counted by the
                   assistant from ``merchant_report_query`` (``aggregated_reporting_context_status``).
google_ads         periodic ``gads_performance`` (level account or campaign).
meta_ads           periodic ``meta_insights``.
openai_ads         periodic ``openai_ads_insights``.
ads_overview       periodic ``ads_overview``.
web_vitals         ``pages``: list of ``{"url": ..., "strategy": "mobile", **pagespeed output}``; or a bare
                   list / a single ``pagespeed`` output. Field p75 values: see sections/cwv.py.
gtm_inventory      ``gtm_inventory`` output (data-quality checks only).
ga4_realtime       ``ga4_realtime`` output (data-quality checks only).
=================  =========================================================================================

Sanitising (:func:`sanitize`) removes obvious personal data before anything is rendered: keys such as
``email``, ``transaction_id``, ``user_id`` or ``client_id`` are dropped wherever they appear, e-mail
addresses inside strings are masked, and each removal is reported in ``errors`` (code
``personal_data_removed``). The report shows aggregates only.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from .numbers import is_num as _numbers_is_num
from .period import PERIOD_KEYS

SOURCES: tuple[str, ...] = ("ga4", "search_console", "merchant_center", "google_ads", "meta_ads", "openai_ads",
                            "ads_overview", "web_vitals", "gtm_inventory", "ga4_realtime")

DEFAULT_PART: dict[str, str] = {"ga4": "kpi", "search_console": "totals", "merchant_center": "product_issues",
                                "web_vitals": "pages"}
PARTS: dict[str, tuple[str, ...]] = {
    "ga4": ("kpi", "funnel", "channels", "products"),
    "search_console": ("totals", "queries", "pages"),
    "merchant_center": ("product_issues", "performance", "statuses"),
    "web_vitals": ("pages",),
}

#: Keys dropped wherever they appear (compared lower-case, without "_", "-" or spaces).
PII_KEYS = frozenset({
    "email", "emailaddress", "mail", "customeremail", "useremail",
    "transactionid", "orderid", "userid", "userpseudoid", "clientid",
    "phone", "phonenumber", "telephone", "ipaddress", "firstname", "lastname", "fullname", "streetaddress",
    "ip", "useragent", "gclid", "fbclid", "msclkid", "key", "token",
})
#: URL query parameters whose value is masked wherever a URL appears in a text value.
URL_PII_PARAMS = ("gclid", "fbclid", "msclkid", "ip", "user_agent", "useragent", "key", "token", "order_id",
                  "orderid", "order", "email", "user_id", "uid", "client_id", "transaction_id", "session_id",
                  "sessionid", "phone")
URL_PARAM_RE = re.compile(r"([?&;](?:" + "|".join(URL_PII_PARAMS) + r")=)[^&#\s;]*", re.IGNORECASE)
URL_MASK = "[…]"
SURROGATE_RE = re.compile("[\ud800-\udfff]")
#: Deepest nesting accepted in the input, checked without recursion before anything else.
MAX_DEPTH = 100
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
EMAIL_MASK = "[e-mail]"
MAX_ERRORS = 200
MAX_EVENTS = 100
MAX_NOTES = 20_000


class ContractError(ValueError):
    """The input is not a report document."""


def is_num(value: Any) -> bool:
    """A real, finite number of plausible size (bool excluded); see numbers.py."""
    return _numbers_is_num(value)


def num(value: Any) -> float | None:
    """``value`` when it is a finite number, else None. Numeric strings are NOT accepted (no guessing)."""
    return value if is_num(value) else None


def _norm_key(key: str) -> str:
    return re.sub(r"[\s_\-]", "", key).lower()


def clean_text(text: str) -> str:
    """Lone UTF-16 surrogates (a JSON "\\ud800") replaced by U+FFFD: they cannot be written as UTF-8."""
    return SURROGATE_RE.sub("�", text) if SURROGATE_RE.search(text) else text


def check_depth(raw: Any, limit: int = MAX_DEPTH) -> None:
    """Raise ContractError when ``raw`` nests deeper than ``limit`` levels (iterative: no recursion)."""
    stack = [(raw, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > limit:
            raise ContractError(f"the input nests deeper than {limit} levels")
        children = node.values() if isinstance(node, dict) else node
        stack.extend((child, depth + 1) for child in children if isinstance(child, (dict, list)))


def _scrub(node: Any, path: str, found: dict[str, int], masked: list[int]) -> Any:
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            key = clean_text(key) if isinstance(key, str) else key
            if isinstance(key, str) and _norm_key(key) in PII_KEYS:
                label = f"{path}.{key}" if path else key
                found[re.sub(r"\[\d+\]", "[]", label)] = found.get(re.sub(r"\[\d+\]", "[]", label), 0) + 1
                continue
            out[key] = _scrub(value, f"{path}.{key}" if path else str(key), found, masked)
        return out
    if isinstance(node, list):
        return [_scrub(v, f"{path}[{i}]", found, masked) for i, v in enumerate(node)]
    if isinstance(node, str):
        node = clean_text(node)
        if node.startswith("data:"):
            return node
        if "@" in node:
            node, count = EMAIL_RE.subn(EMAIL_MASK, node)
            masked[0] += count
        if "=" in node:
            node, count = URL_PARAM_RE.subn(lambda m: m.group(1) + URL_MASK, node)
            masked[1] += count
    return node


def sanitize(raw: Any) -> dict:
    """A deep copy of ``raw`` with the structure checked, personal data removed and removals logged in errors.

    Raises ContractError when ``raw`` is not an object. Wrong-typed blocks are replaced by empty ones and
    reported in errors (code ``invalid_input``)."""
    if not isinstance(raw, dict):
        raise ContractError("the report input must be a JSON object")
    check_depth(raw)
    data = copy.deepcopy(raw)
    errors: list[dict] = []
    expected = {"meta": dict, "periods": dict, "goals": dict, "sources": dict, "errors": list, "events": list}
    for key, kind in expected.items():
        value = data.get(key)
        if value is None:
            data[key] = kind()
        elif not isinstance(value, kind):
            errors.append({"source": "report", "code": "invalid_input",
                           "message": f"'{key}' must be a {'list' if kind is list else 'object'}; ignored"})
            data[key] = kind()
    notes = data.get("notes")
    data["notes"] = notes[:MAX_NOTES] if isinstance(notes, str) else ""

    logo = None
    brand = data["meta"].get("brand")
    if isinstance(brand, dict) and "logo_data_uri" in brand:
        logo = brand.pop("logo_data_uri")
    found: dict[str, int] = {}
    masked = [0, 0]
    for key in ("meta", "goals", "sources", "events", "errors"):
        data[key] = _scrub(data[key], "" if key in ("sources",) else key, found, masked)
    data["notes"] = _scrub(data["notes"], "notes", found, masked)
    if logo is not None and isinstance(data["meta"].get("brand"), dict):
        data["meta"]["brand"]["logo_data_uri"] = logo

    data["errors"] = [e for e in data["errors"] if isinstance(e, dict)][:MAX_ERRORS]
    data["events"] = [e for e in data["events"] if isinstance(e, dict)][:MAX_EVENTS]
    for label, count in sorted(found.items()):
        errors.append({"source": "report", "code": "personal_data_removed",
                       "message": f"personal data key removed: {label} ({count}x)"})
    if masked[0]:
        errors.append({"source": "report", "code": "personal_data_removed",
                       "message": f"{masked[0]} e-mail address(es) masked in text values"})
    if masked[1]:
        errors.append({"source": "report", "code": "personal_data_removed",
                       "message": f"{masked[1]} identifying URL parameter value(s) masked (gclid, order_id...)"})
    data["errors"].extend(errors)
    unknown = sorted(k for k in data["sources"] if k not in SOURCES)
    if unknown:
        data["errors"].append({"source": "report", "code": "unknown_source",
                               "message": f"unknown source(s) ignored: {', '.join(map(str, unknown))}"})
    return data


def is_tool_output(node: Any) -> bool:
    """A non-empty object that is not a {current, previous, yoy} wrapper."""
    return isinstance(node, dict) and bool(node) and not any(k in node for k in PERIOD_KEYS)


def periodic(node: Any) -> dict[str, Any]:
    """{current, previous, yoy} -> value for a periodic node; a bare tool output is the current period."""
    if isinstance(node, dict) and any(k in node for k in PERIOD_KEYS):
        return {k: node.get(k) for k in PERIOD_KEYS if node.get(k) not in (None, {}, [])}
    if node in (None, {}, []):
        return {}
    return {"current": node}


def part_of(source_value: Any, source: str, part: str | None) -> Any:
    """The ``part`` of a source (see the table above); a source without parts answers itself."""
    if source not in PARTS or part is None:
        return source_value
    if isinstance(source_value, dict) and any(p in source_value for p in PARTS[source]):
        return source_value.get(part)
    # A bare value stands for the default part only.
    return source_value if part == DEFAULT_PART[source] else None


def view(data: dict, source: str, part: str | None = None, period: str = "current") -> Any:
    """One tool output (or list, for web_vitals) for ``source`` / ``part`` / ``period``; None when absent.

    ``part=None`` means the source's default part. Non-periodic parts (merchant_center, web_vitals) ignore
    ``period`` except that only "current" returns them."""
    sources = data.get("sources") if isinstance(data, dict) else None
    value = sources.get(source) if isinstance(sources, dict) else None
    if value in (None, {}, []):
        return None
    if source in DEFAULT_PART and part is None:
        part = DEFAULT_PART[source]
    node = part_of(value, source, part)
    if node in (None, {}, []):
        return None
    if source in ("merchant_center", "web_vitals"):
        return node if period == "current" else None
    return periodic(node).get(period)


def state(node: Any) -> str:
    """'missing' (None or empty), 'error' (a tool error object) or 'ok'."""
    if node in (None, {}, []):
        return "missing"
    if isinstance(node, dict) and "error" in node:
        return "error"
    return "ok"


def error_code(node: Any) -> str:
    """The ``error`` value of a tool error object, as text."""
    if isinstance(node, dict) and "error" in node:
        return str(node.get("error"))[:60]
    return ""


def totals(node: Any) -> dict:
    """The ``totals`` object of a tool output, or {}."""
    if state(node) != "ok":
        return {}
    t = node.get("totals")
    return t if isinstance(t, dict) else {}


def rows(node: Any) -> list[dict]:
    """The ``rows`` of a tool output (dicts only), or []."""
    if state(node) != "ok":
        return []
    r = node.get("rows")
    return [x for x in r if isinstance(x, dict)] if isinstance(r, list) else []


def currency_of(node: Any, metric: str | None = None) -> str | None:
    """The currency of a tool output: its ``currency`` field, else the ``unit`` of ``metric`` in ``metrics``
    when it looks like an ISO code. Never guessed."""
    if state(node) != "ok":
        return None
    cur = node.get("currency")
    if isinstance(cur, str) and re.fullmatch(r"[A-Z]{3}", cur.strip()):
        return cur.strip()
    if metric and isinstance(node.get("metrics"), dict):
        unit = (node["metrics"].get(metric) or {}).get("unit") if isinstance(node["metrics"].get(metric), dict) \
            else None
        if isinstance(unit, str) and re.fullmatch(r"[A-Z]{3}", unit.strip()):
            return unit.strip()
    return None


def provided_sources(data: dict) -> list[str]:
    """The sources present in the input (not empty), in SOURCES order."""
    sources = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(sources, dict):
        return []
    return [s for s in SOURCES if sources.get(s) not in (None, {}, [])]

"""Search Console: sitemap submission through the write path (plan_* -> google_apply).

Write plan (write token, scope webmasters; the read token is never used here):
    gsc_plan_sitemap_submit   PUT webmasters/v3/sites/{siteUrl}/sitemaps/{feedpath} (no body)
Low risk and idempotent: submitting a sitemap asks Google to fetch it; resubmitting one that is already listed
asks for a new fetch. The sitemap URL must belong to the client's own property (clients.json gsc.site_url):
    sc-domain:example.com      http(s) URL on example.com or one of its subdomains
    https://www.example.com/   URL starting with that exact prefix (scheme, host, port, path)
Rollback: when the sitemap was NOT listed before the apply (fresh snapshot taken at apply time), rollback_plan
builds the DELETE of that sitemap ("sitemap_delete"; a removal, so the policy also needs write.gsc.allow_delete
and an out-of-band human approval). When it was already listed, rollback is "none": this server never removes a
sitemap it did not add.

Deliberately ABSENT in v1: deleting any other sitemap (no sitemap delete tool), sites.add / sites.delete (adding
or removing a property), URL Inspection or Indexing API writes. The `webmasters` scope would allow some of them:
only the allow-list below (PUT / DELETE on one sitemap URL) lets a request out.

Handler "gsc": snapshot = the property's sitemap list read with the write token (binding first: the URL must name
the op's own site, owned by the client, before any request); dry_run = local (Search Console has no dry-run):
URL shape, property domain, no body, no params; execute re-checks on the snapshot taken at apply time. Account of
an op = the site URL (sc-domain:... or https://.../), carried by the snapshot as account_id (write_policy binds
on it: the site is percent-encoded in the path).

Transport note: Search Console takes the property and the sitemap URL percent-encoded in the path
(https%3A%2F%2F...). ads_common.bare_url accepts an encoded '/' (%2F) ONLY in the segment right after "sites" and
the one right after "sitemaps" on www.googleapis.com/webmasters/v3/sites/ (ENCODED_SLASH_SEGMENTS); even there it
refuses %2E, %5C, %25 (double encoding), encoded control characters and any '.' / '..' component once decoded.
_parse also requires the property to start with http://, https:// or sc-domain:. A URL the shared client would
still refuse is reported up front as "transport_blocked" (nothing is sent) instead of failing at apply time.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Annotated, Any, Optional
from urllib.parse import quote, unquote, urlsplit

from pydantic import Field

from . import google_write as gw
from .write_core import PlatformHandler, WriteOp

PLATFORM = "gsc"
SITES = "https://www.googleapis.com/webmasters/v3/sites"
_S = r"https://www\.googleapis\.com/webmasters/v3/sites"
SNAPSHOT_RULES = [("GET", _S + r"/[^/]+/sitemaps")]
WRITE_RULES = [("PUT", _S + r"/[^/]+/sitemaps/[^/]+", None, "write"),
               ("DELETE", _S + r"/[^/]+/sitemaps/[^/]+", None, "write")]
WRITE = gw.google_write_client(PLATFORM, WRITE_RULES, read_rules=SNAPSHOT_RULES)

SUBMIT = "sitemap_submit"
WITHDRAW = "sitemap_delete"  # only ever built by rollback_plan, for a sitemap this server added
# The PUT and the DELETE carry no body and no params (dry_run and execute refuse any). write_policy's registry
# shape needs a non-empty list, so the single entry names the path parameter, which the builder never sends.
ALLOWLISTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {SUBMIT: (("feedpath",), ()),
                                                                   WITHDRAW: (("feedpath",), ())}
TOOLS = ("gsc_plan_sitemap_submit",)
VOLUNTARILY_ABSENT = ("sitemap_delete tool", "sites.add", "sites.delete", "indexing publish")
MAX_URL = 2048
_URL_RX = re.compile(re.escape(SITES) + r"/([^/?#]+)/sitemaps/([^/?#]+)")
_PRINTABLE = re.compile(r"[\x21-\x7e]+")
_PROPERTY_PREFIXES = ("http://", "https://", "sc-domain:")
LATENCY_NOTE = "Google fetches a submitted sitemap later; its status in Search Console can take hours or days"

CLIENT_ID = Annotated[Optional[str], Field(description="Client id from clients_list. Required when several clients "
                                                       "are registered; empty uses the only client")]


# ------------------------------------------------------------------ helpers
def sitemap_api_url(site: str, feed: str) -> str:
    """The sitemap resource URL: property and sitemap URL percent-encoded (every reserved character)."""
    return f"{SITES}/{quote(site, safe='')}/sitemaps/{quote(feed, safe='')}"


def _parse(url: Any) -> tuple[str, str] | None:
    """(site, sitemap URL) of a sitemap resource URL in canonical encoding, else None."""
    match = _URL_RX.fullmatch(url) if isinstance(url, str) else None
    if not match:
        return None
    site, feed = unquote(match.group(1)), unquote(match.group(2))
    if quote(site, safe="") != match.group(1) or quote(feed, safe="") != match.group(2):
        return None  # another encoding of the same text: refused, one URL per sitemap
    if not site.startswith(_PROPERTY_PREFIXES) or len(site) <= len("sc-domain:"):
        return None  # a property is http://..., https://... or sc-domain:... (never ../ or a bare name)
    if any(part in (".", "..") for text in (site, feed) for part in text.split("/")):
        return None
    return site, feed


def outside_property(site: str, feed: Any) -> str | None:
    """Why the sitemap URL is not inside the property site, or None when it is."""
    if not isinstance(feed, str) or not feed:
        return "the sitemap URL is empty"
    if len(feed) > MAX_URL or not _PRINTABLE.fullmatch(feed):
        return f"the sitemap URL must be printable ASCII without spaces, {MAX_URL} characters at most"
    try:
        parts = urlsplit(feed)
        port = parts.port
    except ValueError:
        return "the sitemap URL cannot be parsed"
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        return "the sitemap URL must be an http(s) URL with a host"
    if parts.username or parts.password or "@" in parts.netloc:
        return "the sitemap URL may not carry user information"
    if parts.fragment or "#" in feed:
        return "the sitemap URL may not carry a fragment (#...)"
    path = unquote(parts.path)
    if "\\" in path or any(seg in (".", "..") for seg in path.split("/")) or len(path) < 2:
        return "the sitemap URL needs a file path without dot segments (e.g. /sitemap.xml)"
    if site.startswith("sc-domain:"):
        domain = site[len("sc-domain:"):].lower()
        if host != domain and not host.endswith("." + domain):
            return f"host {host} is not {domain} nor one of its subdomains (property {site})"
        return None
    try:
        prefix = urlsplit(site)
        prefix_port = prefix.port
    except ValueError:
        return f"the property {site} is not a valid URL prefix"
    if (parts.scheme, host, port) != (prefix.scheme, (prefix.hostname or "").lower(), prefix_port):
        return f"the sitemap URL is not on {prefix.scheme}://{prefix.netloc} (property {site})"
    if not parts.path.startswith(prefix.path or "/"):
        return f"the sitemap URL is not under the property prefix {site}"
    return None


def transport_problem(url: str) -> str | None:
    """Why the shared HTTP client (ads_common) would refuse this URL before sending it, or None."""
    from . import ads_common

    if ads_common.bare_url(url) is None:
        return ("the shared HTTP client refuses this URL: a percent-encoded '/' (%2F) is accepted only in the "
                "property and sitemap segments, without %2E / %5C / %25 escapes, control characters or '.' / '..' "
                "components once decoded, so this request cannot be sent")
    return None


def _summary(entry: dict | None) -> dict | None:
    if not isinstance(entry, dict):
        return None
    return {"path": entry.get("path"), "last_submitted": entry.get("lastSubmitted"),
            "last_downloaded": entry.get("lastDownloaded"), "is_pending": entry.get("isPending"),
            "is_index": entry.get("isSitemapsIndex"), "kind": entry.get("type"),
            "errors": entry.get("errors"), "warnings": entry.get("warnings")}


# ------------------------------------------------------------------ handler
def _snapshot(ctx: Any, op: WriteOp) -> dict:
    """The property's sitemaps, read with the write token after the binding check. Raises gw.SnapshotError."""
    parts = _parse(op.url)
    if parts is None:
        raise gw.SnapshotError("bad_url", "the op URL is not a Search Console sitemap URL")
    site, feed = parts
    gw.require_bound(ctx, PLATFORM, site, op)  # before any request: never read another client's property
    blocked = transport_problem(op.url)
    if blocked:
        raise gw.SnapshotError("transport_blocked", blocked, "Submit the sitemap in Search Console > Sitemaps.")
    listed = gw.write_call(WRITE, ctx, "GET", f"{SITES}/{quote(site, safe='')}/sitemaps")
    if "error" in listed:
        raise gw.SnapshotError.from_result(listed, f"sitemaps of {site}")
    items = [s for s in listed.get("sitemap", []) if isinstance(s, dict)]
    current = next((s for s in items if s.get("path") == feed), None)
    return {"account_id": site, "feedpath": feed, "submitted": current is not None, "sitemap": _summary(current),
            "sitemaps_count": len(items)}


def _preflight(ctx: Any, op: WriteOp) -> list[str]:
    """The module locks, on op.before (the trusted snapshot). Empty list = go on."""
    parts = _parse(op.url)
    if parts is None:
        return ["the URL is not a Search Console sitemap URL in canonical encoding"]
    site, feed = parts
    problems: list[str] = []
    if site != str(op.account_id) or not ctx.owns(PLATFORM, site):
        problems.append(f"property {site} is not the op's own property of client {ctx.client_id!r}")
    if op.body is not None or op.params is not None:
        problems.append("a sitemap request carries no body and no parameters")
    before = op.before if isinstance(op.before, dict) else {}
    if before.get("account_id") != site or before.get("feedpath") != feed:
        problems.append("the snapshot names another property or sitemap than the URL")
    outside = outside_property(site, feed)
    if outside:
        problems.append(outside)
    blocked = transport_problem(op.url)
    if blocked:
        problems.append(blocked)
    if op.action == SUBMIT:
        if op.method.upper() != "PUT":
            problems.append("a sitemap is submitted with PUT")
    elif op.action == WITHDRAW:
        if op.method.upper() != "DELETE":
            problems.append("a sitemap is removed with DELETE")
        origin = op.inverse
        if not isinstance(origin, WriteOp) or origin.action != SUBMIT or origin.url != op.url:
            problems.append("only a sitemap this server submitted can be removed (rollback of its submission)")
        if before.get("submitted") is not True:
            problems.append(f"sitemap {feed} is no longer listed on {site}: nothing to remove")
    else:
        problems.append(f"unknown Search Console action {op.action!r}")
    return problems


def _dry_run(ctx: Any, op: WriteOp) -> dict:
    problems = _preflight(ctx, op)
    if problems:
        return gw.local_result(False, "; ".join(problems))
    before = op.before or {}
    detail = {"checked": "property, sitemap URL inside the property, current sitemap list (Search Console has no "
                         "dry-run)", "already_submitted": bool(before.get("submitted")),
              "sitemaps_on_property": before.get("sitemaps_count")}
    if op.action == SUBMIT:
        detail["note"] = LATENCY_NOTE
    return gw.local_result(True, detail)


def _execute(ctx: Any, op: WriteOp, grant: Any) -> dict:
    problems = _preflight(ctx, op)  # op.before is the snapshot taken at apply time
    if problems:
        return {"error": "precondition_failed", "detail": "; ".join(problems),
                "fix": "Check the property with gsc_sitemaps, then plan again; nothing was sent."}
    result = gw.write_call(WRITE, ctx, op.method, op.url, grant=grant)
    if "error" in result:
        return result
    site, feed = _parse(op.url) or ("", "")
    out = {"ok": True, "site_url": site, "sitemap": feed,
           "action": "submitted" if op.action == SUBMIT else "removed"}
    if op.action == SUBMIT:
        out["note"] = LATENCY_NOTE
    return out


def _withdraw_op(op: WriteOp) -> WriteOp:
    site, feed = _parse(op.url) or (str(op.account_id), "")
    return WriteOp(op_id=f"{op.op_id}-rb", platform=PLATFORM, account_id=op.account_id, action=WITHDRAW,
                   method="DELETE", url=op.url, risk="medium", financial=False,
                   summary=f"Remove sitemap {feed} from {site} (it was added by plan, rollback)",
                   inverse=dataclasses.replace(op, before=None, inverse=None, rollback="none"), rollback="auto")


def _invert(op: WriteOp, before: dict | None) -> WriteOp | None:
    """Rollback of a submission: DELETE, only when the snapshot taken at apply time did not list the sitemap.
    Rollback of a removal: submit it again."""
    if op.action == SUBMIT:
        if isinstance(before, dict) and before.get("submitted") is False and _parse(op.url):
            return _withdraw_op(op)
        return None
    if op.action == WITHDRAW and isinstance(op.inverse, WriteOp) and op.inverse.action == SUBMIT:
        again = dataclasses.replace(op.inverse, op_id=f"{op.op_id}-rb", before=None, inverse=None,
                                    rollback="auto")
        return dataclasses.replace(again, inverse=_withdraw_op(again))
    return None


def _entity_key(ctx: Any, op: WriteOp) -> str:
    before = op.before if isinstance(op.before, dict) else {}
    return f"gsc/{before.get('account_id')}/sitemap/{before.get('feedpath')}"


HANDLER = PlatformHandler(dry_run=_dry_run, snapshot=_snapshot, execute=_execute, invert=_invert,
                          entity_key=_entity_key)


# ------------------------------------------------------------------ plan tool
def _error(ctx: Any, code: str, detail: str, fix: str) -> dict:
    return {"error": code, "detail": detail, "fix": fix, "client_id": ctx.client_id}


@gw.tool_guard()
def gsc_plan_sitemap_submit(
    sitemap_url: Annotated[str, Field(description="Full URL of the sitemap, inside the client's property, e.g. "
                                                  "https://www.example.com/sitemap.xml")],
    site_url: Annotated[str, Field(description="Search Console property of the client (empty: the configured "
                                               "one), e.g. sc-domain:example.com")] = "",
    client_id: CLIENT_ID = None,
) -> dict:
    """PLAN (nothing is written): submit a sitemap to the client's Search Console property. The sitemap URL must
    be inside the property (same domain or a subdomain for sc-domain:, same prefix for a URL-prefix property).
    Low risk. Apply with google_apply after the user agreed. A sitemap that was not listed before can be removed
    again with rollback_plan (needs write.gsc.allow_delete and a human approval); one that was already listed
    has no rollback."""
    ctx = gw.resolve(client_id)
    site = gw.google_account_binding(ctx, PLATFORM, "site_url", site_url)
    blocked = gw.write_precheck(ctx, PLATFORM, site)
    if blocked:
        return blocked
    feed = str(sitemap_url or "").strip()
    outside = outside_property(site, feed)
    if outside:
        return _error(ctx, "sitemap_outside_property", f"sitemap {feed[:200]!r}: {outside}.",
                      "Pass a sitemap URL of the client's own property; nothing was planned or sent.")
    url = sitemap_api_url(site, feed)
    transport = transport_problem(url)
    if transport:
        return _error(ctx, "transport_blocked", transport + "; nothing was sent.",
                      "Use a plain sitemap URL of the property (no dot segments, no encoded '.', '\\' or '%'), "
                      "or submit it in Search Console > Sitemaps.")
    op = WriteOp(op_id=f"{SUBMIT}-1", platform=PLATFORM, account_id=site, action=SUBMIT, method="PUT", url=url,
                 risk="low", financial=False, summary=f"Submit sitemap {feed} to Search Console property {site}")
    try:
        before = _snapshot(ctx, op)
    except gw.SnapshotError as exc:
        return {**exc.as_dict(), "client_id": ctx.client_id}
    # rollback "auto" only while the sitemap is unlisted. No inverse is stored: rollback_plan must build the
    # DELETE from the snapshot taken at APPLY time (_invert), never from this plan-time one, so a sitemap someone
    # listed between plan and apply is never removed.
    inverse = _invert(op, before)
    op = dataclasses.replace(op, before=before, rollback="auto" if inverse else "none",
                             summary=op.summary + (" (already listed: asks Google to fetch it again)"
                                                   if before.get("submitted") else ""))
    problems = _preflight(ctx, op)
    if problems:
        return _error(ctx, "invalid_request", "; ".join(problems), "Nothing was planned; fix the request.")
    from . import write_core

    out = write_core.create_plan(ctx, PLATFORM, [op], title=f"Search Console: sitemap {feed[:120]}")
    if "error" not in out:
        out["rollback_note"] = (
            "rollback_plan builds the DELETE of this sitemap if it was still unlisted when the plan was applied "
            "(needs write.gsc.allow_delete and a human approval)" if inverse
            else "no rollback: the sitemap was already listed, this server never removes it")
    return out


# ------------------------------------------------------------------ registration
def register_write() -> None:
    """Field allow-lists and the "gsc" PlatformHandler (idempotent: the same lists again are a no-op)."""
    from . import write_core, write_policy

    for action, (fields, money_ok) in ALLOWLISTS.items():
        write_policy.register_field_allowlist(PLATFORM, action, fields, money_ok=money_ok)
    write_core.register_handler(PLATFORM, HANDLER)


def register(mcp: Any) -> list[str]:
    """Register the Search Console plan tool (read-only hint: it only plans)."""
    register_write()
    return gw.register_tools(mcp, [("Plan a Search Console sitemap submission", gsc_plan_sitemap_submit)],
                             gw.PLAN_ONLY)

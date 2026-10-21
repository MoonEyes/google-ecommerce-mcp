"""Search Console: the sites a client's Google token can see (read only).

Read (read token, webmasters.readonly): gsc_sites. server.py already offers gsc_performance, gsc_inspect_url and
gsc_sitemaps; this module adds only what it lacks. The write plan (sitemap submission) lives in gsc_write.py.

gsc_sites lists every Search Console property the client's READ token sees (GET webmasters/v3/sites), with its
permission level and whether this client owns it (clients.json gsc.site_url). With a client registry, a property
that is not the client's is never shown in full (its address is masked, only its permission level is given): an
agency often authorizes the same Google account for several clients, and one client's answer must not list
another client's sites. Legacy mode (no registry) shows every property, as before.
"""

from __future__ import annotations

import datetime
from typing import Annotated, Any, Optional

from pydantic import Field

from . import google_write as gw

PLATFORM = "gsc"
WEBMASTERS = "https://www.googleapis.com/webmasters/v3"
READ_RULES = [("GET", r"https://www\.googleapis\.com/webmasters/v3/sites")]
READ = gw.google_read_client(PLATFORM, READ_RULES)

TOOLS = ("gsc_sites",)
# Permission levels that may submit a sitemap (Search Console "full" users and owners).
WRITE_LEVELS = frozenset({"siteOwner", "siteFullUser"})
CLIENT_ID = Annotated[Optional[str], Field(description="Client id from clients_list. Required when several clients "
                                                       "are registered; empty uses the only client")]


def _configured_site(ctx: Any) -> str | None:
    try:
        return str(ctx.account(PLATFORM, "site_url"))
    except Exception:  # platform_not_configured: still list what the token sees
        return None


def _mask(site: str) -> str:
    from .clients import mask_id

    return mask_id(site)


def _entry(ctx: Any, raw: dict, configured: str | None) -> dict | None:
    site = raw.get("siteUrl")
    if not isinstance(site, str) or not site:
        return None
    level = str(raw.get("permissionLevel") or "")
    owned = bool(configured and site == configured) or (not ctx.legacy and ctx.owns(PLATFORM, site))
    if ctx.legacy or owned:
        return {"site_url": site, "permission_level": level, "owned": True if ctx.legacy else owned,
                "configured": bool(configured and site == configured),
                "kind": "domain" if site.startswith("sc-domain:") else "url_prefix",
                "can_submit_sitemaps": level in WRITE_LEVELS}
    return {"site_url": _mask(site), "permission_level": level, "owned": False, "configured": False,
            "masked": True}


@gw.tool_guard("current Search Console access")
def gsc_sites(client_id: CLIENT_ID = None) -> dict:
    """Search Console properties the client's Google token can see: address (sc-domain:example.com or a URL
    prefix), permission level (siteOwner, siteFullUser, siteRestrictedUser, siteUnverifiedUser), whether it is
    the client's configured site and whether that level may submit sitemaps. With several clients, sites that are
    not this client's are masked. Read only."""
    ctx = gw.resolve(client_id)
    configured = _configured_site(ctx)
    listed = gw.read_call(READ, ctx, "GET", f"{WEBMASTERS}/sites")
    if "error" in listed:
        return {**listed, "client_id": ctx.client_id}
    raw = [s for s in listed.get("siteEntry", []) if isinstance(s, dict)]
    entries = [e for e in (_entry(ctx, s, configured) for s in raw) if e is not None]
    entries.sort(key=lambda e: (not e.get("configured"), not e.get("owned"), str(e.get("site_url"))))
    visible = {s.get("siteUrl") for s in raw}
    out: dict[str, Any] = {"client_id": ctx.client_id, "configured_site": configured, "sites": entries,
                           "count": len(entries), "owned_count": sum(1 for e in entries if e.get("owned")),
                           "untrusted_text": gw.untrusted("sites[].site_url")}
    if configured is not None and configured not in visible:
        out["warning"] = (f"the configured site {configured!r} is not visible to this Google token: check the "
                          "property type (sc-domain:example.com for a Domain property, https://www.example.com/ "
                          "with its trailing slash for a URL-prefix property) and that the authorized account "
                          "has access to it")
    elif configured is not None:
        level = next((str(s.get("permissionLevel") or "") for s in raw if s.get("siteUrl") == configured), "")
        if level not in WRITE_LEVELS:
            out["note"] = (f"permission {level or 'unknown'!r} on the configured site cannot submit sitemaps "
                           "(siteOwner or siteFullUser needed)")
    return out


# ------------------------------------------------------------------ week-over-week moves (gsc_moves)
# The tool itself is registered in server.py (it reuses that module's token, scope and allow-list for
# searchAnalytics/query); the pure logic lives here so it can be tested without a network.
MOVES_FETCH_ROWS = 5000  # rows asked per period; Search Analytics allows up to 25,000 per request
MOVES_DIMENSIONS = ("query", "page")
MOVES_NOTE = ("Search Console data for the last 2 to 3 days is incomplete and can still change; the current period "
              "ends 2 days ago, so its last days may be lower than their final values")


def moves_periods(today: datetime.date, days: int) -> tuple[tuple[str, str], tuple[str, str]]:
    """((current_start, current_end), (previous_start, previous_end)) in ISO dates: the `days` days ending 2 days
    before `today` (the Search Console lag, as in gsc_performance), and the `days` days right before them."""
    cur_end = today - datetime.timedelta(days=2)
    cur_start = cur_end - datetime.timedelta(days=days - 1)
    prev_end = cur_start - datetime.timedelta(days=1)
    prev_start = prev_end - datetime.timedelta(days=days - 1)
    return (cur_start.isoformat(), cur_end.isoformat()), (prev_start.isoformat(), prev_end.isoformat())


def _num(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _by_key(rows: list, dimension: str) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for row in rows:
        keys = row.get("keys") if isinstance(row, dict) else None
        if not isinstance(keys, list) or not keys:
            continue
        found[str(keys[0])] = {"clicks": int(_num(row.get("clicks"))), "impressions": int(_num(row.get("impressions"))),
                               "ctr": round(_num(row.get("ctr")), 4), "position": round(_num(row.get("position")), 1)}
    return found


def _totals(period: dict[str, dict]) -> dict:
    clicks = sum(r["clicks"] for r in period.values())
    impressions = sum(r["impressions"] for r in period.values())
    weighted = sum(r["position"] * r["impressions"] for r in period.values())
    return {"clicks": clicks, "impressions": impressions, "ctr": round(clicks / impressions, 4) if impressions else 0,
            "position": round(weighted / impressions, 1) if impressions else 0}


def compute_moves(current_rows: list, previous_rows: list, dimension: str, limit: int) -> dict:
    """Join the rows of the two periods on the dimension and rank the changes in clicks. A key missing from the
    current period is a full loss (clicks_current 0, status "lost"); a key missing from the previous one is a full
    gain (status "new"). Metrics of an absent period are None, not 0 (a position of 0 would read as the best)."""
    cur, prev = _by_key(current_rows, dimension), _by_key(previous_rows, dimension)
    moves = []
    for key in cur.keys() | prev.keys():
        now, before = cur.get(key), prev.get(key)
        delta = (now["clicks"] if now else 0) - (before["clicks"] if before else 0)
        moves.append({dimension: key, "status": "new" if before is None else "lost" if now is None else "changed",
                      "clicks_delta": delta,
                      "current": now or {"clicks": 0, "impressions": 0, "ctr": None, "position": None},
                      "previous": before or {"clicks": 0, "impressions": 0, "ctr": None, "position": None}})
    losers = sorted((m for m in moves if m["clicks_delta"] < 0), key=lambda m: (m["clicks_delta"], str(m[dimension])))
    gainers = sorted((m for m in moves if m["clicks_delta"] > 0), key=lambda m: (-m["clicks_delta"], str(m[dimension])))
    return {"losers": losers[:limit], "gainers": gainers[:limit], "losers_count": len(losers),
            "gainers_count": len(gainers),
            "totals": {"current": _totals(cur), "previous": _totals(prev)},
            "rows_seen": {"current": len(cur), "previous": len(prev)}}


def register(mcp: Any) -> list[str]:
    """Register the Search Console read tool (read-only)."""
    return gw.register_tools(mcp, [("Search Console sites", gsc_sites)], gw.READ_ONLY)

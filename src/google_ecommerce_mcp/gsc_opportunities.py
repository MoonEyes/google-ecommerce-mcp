"""gsc_opportunities: Search Console striking-distance queries (or pages) and the ones with no clicks.

The tool itself is registered in server.py (it reuses that module's token, scope and allow-list for
searchAnalytics/query); the pure filtering and ranking lives here so it can be tested without a network.
"""

from __future__ import annotations

from typing import Any

FETCH_ROWS = 5000  # rows asked in the single request; Search Analytics allows up to 25,000 per request
DIMENSIONS = ("query", "page")
ANONYMIZED_NOTE = ("Google leaves anonymized queries out of query rows, so some searches are not counted here; "
                   "the lists cover only the rows Search Console returned")
TRUNCATED_NOTE = (f"the request returned the {FETCH_ROWS} row ceiling, so rows beyond it (the lowest by clicks) "
                  "are not in these lists")


def _num(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _clean(rows: list, dimension: str) -> list[dict]:
    cleaned = []
    for row in rows:
        keys = row.get("keys") if isinstance(row, dict) else None
        if not isinstance(keys, list) or not keys:
            continue
        cleaned.append({dimension: str(keys[0]), "clicks": int(_num(row.get("clicks"))),
                        "impressions": int(_num(row.get("impressions"))),
                        "ctr": round(_num(row.get("ctr")), 4), "position": round(_num(row.get("position")), 1)})
    return cleaned


def find_opportunities(rows: list, dimension: str, min_impressions: int, min_position: float,
                       max_position: float, limit: int) -> dict:
    """Split Search Analytics rows into two lists, both sorted by impressions (highest first, then key).

    striking_distance: min_position <= position <= max_position and impressions >= min_impressions.
    no_clicks: clicks == 0 and impressions >= min_impressions, at any position.
    The counts are taken before `limit` cuts the lists."""
    kept = [r for r in _clean(rows, dimension) if r["impressions"] >= min_impressions]
    order = lambda r: (-r["impressions"], r[dimension])  # noqa: E731
    striking = sorted((r for r in kept if min_position <= r["position"] <= max_position), key=order)
    no_clicks = sorted((r for r in kept if r["clicks"] == 0), key=order)
    return {"striking_distance": striking[:limit], "no_clicks": no_clicks[:limit],
            "striking_count": len(striking), "no_clicks_count": len(no_clicks), "rows_seen": len(rows)}


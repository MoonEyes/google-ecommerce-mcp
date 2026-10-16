"""Section 10 sub-block: the event log of the month (promotions, stock-outs, tracking changes, launches).

Source: ``events`` of the contract (``{"date": "YYYY-MM-DD", "label": "...", "end": optional, "item":
optional}``), supplied by the client or the team: the tools cannot know about a promotion. Sorted by date,
undated entries last. Rendered as an unnumbered ``<h3>`` sub-block of data_quality (SUBSECTION).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..i18n import tr
from . import _common as h

if TYPE_CHECKING:  # pragma: no cover
    from ..render import ReportContext

ORDER = 110
SUBSECTION = True


def render(data: dict, ctx: "ReportContext") -> str:
    evts = h.events(data)
    if not evts:
        return ctx.nodata(tr(ctx.lang, "events.none"))
    rows = []
    for e in evts:
        cell = ctx.esc(e.label)
        if e.item:
            cell += " " + f'<span class="meta">({ctx.t("events.item", item=e.item[:120])})</span>'
        rows.append([ctx.esc(e.date_raw(ctx.lang)), cell])
    return (ctx.table(tr(ctx.lang, "section.events"), [tr(ctx.lang, "events.col.date"),
                                                       tr(ctx.lang, "events.col.event")], rows)
            + h.notes([ctx.t("events.note")]))

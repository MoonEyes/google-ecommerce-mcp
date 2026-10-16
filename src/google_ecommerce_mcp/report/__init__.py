"""Static client report: one self-contained HTML file (printable to PDF) built from the MCP tools' JSON.

    from google_ecommerce_mcp.report import render_report
    html = render_report(json.load(open("report.json", encoding="utf-8")))

No network, no script, no external URL, the client's brand only. The input contract is in ``contract.py``;
the section contract in ``sections/__init__.py``. Parts are ported from OpenGSC (MIT): see
THIRD_PARTY_NOTICES.md at the repository root.
"""

from __future__ import annotations

from .contract import ContractError, sanitize
from .render import ReportContext, markdown_to_html, render_report

__all__ = ["ContractError", "ReportContext", "markdown_to_html", "render_report", "sanitize"]

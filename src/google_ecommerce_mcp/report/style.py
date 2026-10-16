# Derived from OpenGSC (MIT), src/lib/reports/render.ts (css: KPI cards, pills, .nodata, @page A4 print rules); ported to Python
"""The report's stylesheet, inlined into the single HTML file.

No remote font, no ``@import``, no ``url()``: system font stacks only. Palette (contrast AA on white):
ink #14201B, muted text #3F5249, borders #D5DDD7, page #F4F6F3. Change colours never use red or green:
favourable #0B6E99 on #E4F1F7, unfavourable #B33A00 on #FBEAE0, neutral #3F5249 on #ECEFED, not significant
#5F6B66. Colour is never the only carrier of meaning: arrows, signs and words stay in print (the
"non significatif" word of a pastille is visually hidden on screen and printed).
"""

from __future__ import annotations

from .brand import Brand

_BASE = """
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;background:#F4F6F3;color:#14201B;font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
.wrap{max-width:1200px;margin:0 auto;padding:32px 24px 72px;display:flex;flex-direction:column;gap:32px}
.mono,.fig-value,.kpi .v{font-family:ui-monospace,"Cascadia Mono","SF Mono",Menlo,Consolas,monospace}
header.rpt{display:flex;flex-wrap:wrap;gap:16px;align-items:center;justify-content:space-between;padding-bottom:20px;border-bottom:3px solid var(--accent)}
.brand{display:flex;gap:16px;align-items:center}
.brand img{max-height:56px;max-width:220px}
.initials{width:56px;height:56px;background:var(--accent);color:var(--on-accent);display:flex;align-items:center;justify-content:center;font-size:24px;font-weight:600}
.client{font-size:22px;font-weight:600;color:var(--accent-text)}
.sample{border:1px solid #B45309;background:#FFF4E0;color:#7A3A05;padding:10px 14px;font-size:14px;font-weight:600;max-width:420px}
.kicker{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:#3F5249;font-weight:600}
h1{margin:0;font-size:40px;line-height:1.1;font-weight:600}
h2{margin:0 0 4px;font-size:24px;font-weight:600;color:#14201B;border-left:4px solid var(--accent);padding-left:10px}
h3{margin:0;font-size:18px;font-weight:600}
.lead{font-size:16px;color:#3F5249;margin:0}
.meta,.note{font-size:13px;color:#3F5249;margin:0}
.section{background:#FFFFFF;border:1px solid #D5DDD7;padding:28px;display:flex;flex-direction:column;gap:16px}
.section.plain{background:transparent;border:0;padding:0}
.subsection{display:flex;flex-direction:column;gap:10px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}
.grid.wide{grid-template-columns:repeat(auto-fit,minmax(340px,1fr))}
.card,.kpi{background:#FFFFFF;border:1px solid #D5DDD7;padding:20px;display:flex;flex-direction:column;gap:8px;break-inside:avoid}
.card.dark{background:#14201B;color:#F4F6F3;border-color:#14201B}
.card.dark .meta,.card.dark .l{color:#C9D6CE}
.fig-value.mer{font-size:40px;font-weight:500;line-height:1.1}
.kpi .l{font-size:14px;color:#3F5249}
.kpi .v{font-size:30px;font-weight:500;line-height:1.2}
.kpi .cmp{font-size:13px;color:#3F5249;line-height:1.7;margin:0;padding:0;list-style:none}
.kpi.todo{border-style:dashed;border-color:#8A9A91}
.kpi.todo .v{color:#3F5249}
.msg .figure{font-family:ui-monospace,"Cascadia Mono",Menlo,Consolas,monospace;font-size:28px;font-weight:500}
.msg .head{display:flex;justify-content:space-between;align-items:center;gap:8px}
.msg .title{font-size:15px;font-weight:600;margin:0}
.msg .detail{font-size:14px;color:#3F5249;margin:0}
.badge{font-size:13px;font-weight:600;border:1px solid #14201B;padding:2px 8px;white-space:nowrap}
.delta{display:inline-block;border-radius:3px;font-weight:600;padding:0 6px;white-space:nowrap}
.delta.fav{color:#0B6E99;background:#E4F1F7}
.delta.unfav{color:#B33A00;background:#FBEAE0}
.delta.neutral{color:#3F5249;background:#ECEFED}
.delta.ns{color:#5F6B66;background:transparent;font-weight:500}
.na{color:#5F6B66;font-style:italic}
.pill{display:inline-block;font-size:13px;font-weight:600;padding:2px 8px;border:1px solid #8A9A91}
.pill.ok{background:#E4F1F7;color:#0B4F6E;border-color:#0B6E99}
.pill.warn{background:#FFF4E0;color:#7A3A05;border-color:#B45309}
.nodata{display:inline-block;border:1px dashed #8A9A91;color:#3F5249;font-size:14px;padding:6px 12px;background:#FFFFFF}
.scroll{overflow-x:auto;max-width:100%}
table{width:100%;border-collapse:collapse;font-size:15px}
thead tr{border-bottom:2px solid #14201B}
th{text-align:left;font-weight:600;padding:10px 12px}
td{padding:10px 12px;border-bottom:1px solid #D5DDD7}
tbody th{border-bottom:1px solid #D5DDD7}
td.num,th.num{text-align:right;font-family:ui-monospace,"Cascadia Mono",Menlo,Consolas,monospace}
dl.metrics{margin:0;display:grid;grid-template-columns:1fr auto;gap:8px 12px;font-size:15px}
dl.metrics dt{color:#3F5249}
dl.metrics dd{margin:0;text-align:right;font-family:ui-monospace,"Cascadia Mono",Menlo,Consolas,monospace}
.platform-head{display:flex;justify-content:space-between;align-items:baseline;gap:8px;flex-wrap:wrap}
.takeaway{font-size:14px;line-height:1.5;border-top:1px solid #D5DDD7;padding-top:12px;margin:0}
.warning{border:1px solid #B45309;background:#FFF4E0;color:#5E2C04;padding:14px 16px;font-size:15px;line-height:1.5}
figure{margin:0;display:flex;flex-direction:column;gap:6px}
figcaption{font-size:14px;font-weight:600}
svg.chart{display:block;max-width:100%;height:auto}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}
.decisions{background:#14201B;color:#F4F6F3;border-color:#14201B}
.decisions h2{color:#F4F6F3}
.decisions .meta{color:#C9D6CE}
.decisions ol{margin:0;padding-left:22px;display:flex;flex-direction:column;gap:10px;font-size:16px;line-height:1.5}
.notes h3{font-size:16px}
footer.rpt{font-size:13px;color:#3F5249;line-height:1.6;display:flex;flex-direction:column;gap:4px}
footer.rpt p{margin:0}
.section-error{border-color:#B45309}
@page{size:A4;margin:14mm 12mm}
@media (max-width:640px){.wrap{padding:20px 14px 48px}h1{font-size:30px}.section{padding:18px}.kpi .v{font-size:24px}}
@media print{
body{background:#FFFFFF;font-size:11pt}
.wrap{max-width:none;padding:0;gap:18px}
.section{break-before:page;page-break-before:always;border:0;padding:0}
.section.first{break-before:auto;page-break-before:auto}
.card,.kpi,figure,tr,.warning{break-inside:avoid;page-break-inside:avoid}
.scroll{overflow:visible}
.card.dark,.decisions,.initials,.delta{-webkit-print-color-adjust:exact;print-color-adjust:exact}
.ns-word{position:static;width:auto;height:auto;margin:0;overflow:visible;clip:auto;white-space:nowrap}
}
"""


def css(brand: Brand) -> str:
    """The full stylesheet for ``brand`` (only validated #RRGGBB colours are interpolated).

    --accent is the graphic accent (at least 3:1 on white: borders, initials block, charts); --accent-text
    the text accent (at least 4.5:1 on white: the client name); --on-accent the text on the initials block."""
    return (f":root{{--accent:{brand.accent_graphic};--accent-text:{brand.accent_text};"
            f"--on-accent:{brand.on_accent}}}"
            + _BASE.strip())

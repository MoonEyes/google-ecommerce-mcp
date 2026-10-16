# Third-party notices

## OpenGSC

Parts of the client report generator (`src/google_ecommerce_mcp/report/`) are derived from OpenGSC
(https://github.com/fenjo26/OpenGSC) and ported from TypeScript to Python. Each derived file says so in its
first line.

| Our file | OpenGSC file |
|---|---|
| `report/compare.py` | `src/lib/gsc/periodCompare.ts`, `src/lib/reports/render.ts` (fmtDeltaPct) |
| `report/period.py` | `src/lib/reports/window.ts` |
| `report/summary.py` | `src/lib/reports/render.ts` (buildSummary, buildNextSteps) |
| `report/svg.py` | `src/lib/reports/render.ts` (clicksSvg) |
| `report/style.py` | `src/lib/reports/render.ts` (css) |
| `report/brand.py` | `src/lib/reports/branding.ts` |
| `report/render.py` | `src/lib/reports/render.ts` (renderReportHTML, markdownToHtml, escapeHtml) |

OpenGSC is distributed under the following license:

```
MIT License

Copyright (c) 2026 OpenGSC

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## seo-monster

`report/confidence.py` is inspired by the idea of banded, evidence-tagged scores in seo-monster
(avansaber/seo-monster). No code was copied.

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

## Ideas reused for multi-client and writing (0.5.0): inspiration, no code copied

The write path (plan, signed confirmation token, policy, journal) and the multi-client registry were written from
scratch for this project. The projects below were read for their design; **no code from any of them is included**.
Licences were checked on 2026-10-17.

| Project | Licence | What we took from it (the idea only) |
|---|---|---|
| googleads/google-ads-mcp (https://github.com/googleads/google-ads-mcp) | Apache-2.0 | The `login_customer_id` given per call, the idea of a frozen golden list of tools (our `tests/golden_tools.json`) |
| Luxand/google-ads-write-mcp (https://github.com/Luxand/google-ads-write-mcp) | MIT | Write guardrails for Google Ads: account allow-list, budget cap, entities removed only if labelled by us (our `gem-managed` label), audit log |
| kiwoongeom/gmc-mcp (https://github.com/kiwoongeom/gmc-mcp) | MIT | Journal with a snapshot of the state before the change, then rollback by an explicit, confirmed step; switchable dry run |
| stape-io/google-tag-manager-mcp-server (https://github.com/stape-io/google-tag-manager-mcp-server) | Apache-2.0 | Which Tag Manager API resources to cover (workspaces, tags, triggers, variables). We deliberately do not offer publishing |
| AminForou/mcp-gsc (https://github.com/AminForou/mcp-gsc) | MIT | The idea of an explicit opt-in before any destructive Search Console action |
| weyersk/google-ads-mcp (https://github.com/weyersk/google-ads-mcp) | MIT | A confirmation token tied to the content of the preview |
| byadsco/meta-ads-mcp (https://github.com/byadsco/meta-ads-mcp) | MIT | Read and destructive annotations on every tool; per-client rate and error handling |

Not used, on purpose: pipeboard-co/meta-ads-mcp (BUSL-1.1, not open source) and brijr/meta-mcp (no licence). Their
code was neither copied nor ported.

The MIT and Apache-2.0 licences of these projects ask for attribution when code is copied. Because no code is copied,
no licence text is reproduced here; the table is given as a courtesy and so that the origin of each idea is traceable.

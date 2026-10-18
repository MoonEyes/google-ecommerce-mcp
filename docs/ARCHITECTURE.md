# Architecture

google-ecommerce-mcp is a small, local bridge between an MCP client (Claude Desktop, Claude Code, or any other MCP host) and the APIs of an online shop: six Google APIs, plus, optionally, Google Ads, Meta (ads, pixel, catalog, Instagram, Page) and ChatGPT Ads. It **reads** by default. For a client whose writing you turned on, it can also **prepare, and with your approval apply, controlled changes**, through one single path (plan, approval, apply, journal). It works for one shop (legacy mode) or for several clients (registry mode).

![Architecture overview](diagrams/architecture-overview.png)

Interactive version: [`diagrams/architecture-overview.html`](diagrams/architecture-overview.html) (open it locally in a browser; it is self-contained). That diagram shows the reading side; the writing side is described below.

## Overview, in text

```
 MCP client (Claude Desktop, Claude Code)
        |  stdio (JSON-RPC)
        v
 server.py + registry.py : 78 tools, most take a client_id
        |
        +-- read tools ------------> clients.py (client_id -> ClientContext: ids, secrets, policy)
        |                                  |
        |                                  v
        |                            ads_common.AdsClient
        |                            read allow-list, no redirect, secrets masked
        |
        +-- *_plan_* tools --------> write_policy (locks) + platform handler (snapshot, dry-run)
        |                            -> plan stored, journal "plan", confirmation_token
        |
        +-- *_apply tools (4) -----> write_core.apply_plan
                                     re-check, approval, journal intent -> WriteGrant ->
                                     AdsClient + WriteAllowList -> journal result
                                           |
                                           v
                              Google / Meta / OpenAI APIs (pinned hosts)
```

## Components

| Component | File | Role |
|---|---|---|
| Entry point | `src/google_ecommerce_mcp/__main__.py` | `setup`, `setup-ads`, `check`, `doctor`, `install`, `client ...`, `approve`, `report`, or (default) run the MCP server over stdio |
| Configuration | `config.py` | Reads account ids from environment variables, defines the OAuth scopes |
| Credentials | `auth.py` | Stores the refresh token in the OS keyring (or a file), refreshes access tokens in memory |
| Installer | `installer.py` | `install` command: prompts, OAuth setup, Claude Desktop config merge with backup |
| Tools | `server.py`, `registry.py` | The first FastMCP tools for the Google services, one HTTP session, every call wrapped so errors come back as data. `registry.py` registers every other tool (78 in total) in a fixed order and freezes the field allow-lists at the end |
| Clients | `clients.py`, `paths.py`, `secrets_store.py`, `client_cli.py` | The client registry, the immutable `ClientContext`, per-client secrets, the `client` and `approve` commands. See [MULTI-CLIENT.md](MULTI-CLIENT.md) |
| Write path | `write_core.py`, `write_policy.py`, `write_tokens.py`, `write_journal.py` | The single path for every change. See [The write path](#the-write-path) |
| Doctor | `doctor.py` | `doctor` command: per service, set up / secret present / token / status and the exact command to fix it; `--live` makes one minimal read call through the existing tools; never prints a secret, ids masked. Exit code 0, 1 or 2 |
| Setup guide | `setup_guide.py` | The ordered setup steps per service (one bounded action, its command, how to see it worked), shared by `install`, `doctor` and the docs. The Paid media step of `install` lives in `installer.py` |
| Paid media | `ads_*.py`, `google_ads.py`, `meta_ads.py`, `openai_ads.py` | The read tools of the ad platforms, each platform with its own allow-list and client; see [Paid media modules](#paid-media-modules) |
| Platform write modules | `gads_write.py`, `meta_write.py`, `openai_write.py`, `meta_eco.py`, `meta_eco_write.py`, `google_write.py`, `ga4_tools.py`, `gtm_tools.py`, `gsc_tools.py`, `gsc_write.py`, `merchant_tools.py`, `merchant_write.py` | The `*_plan_*` tools and the handler of each platform; see [Platform handlers](#platform-handlers) |

There is no database, no background job and no network listener. The only files written are the client registry, the secrets (in the keyring), and, for writing, the state folder (plans, approvals, journal).

## Paid media modules

The paid media connectors are separate modules that `server.py` only imports and registers. They do not share the Google token, the Google allow-list or the Google HTTP session.

| Module | Role |
|---|---|
| `ads_common.py` | The only ads module that sends HTTP requests. Holds `AllowList`, `AdsClient`, secret masking (`redact`), error mapping (`map_error`), a 5-minute cache for discovery calls, the `guard` decorator and the shared calculations (`roas`, `safe_ratio`, `date_range`) |
| `ads_config.py` | `AdsSettings`, read from the environment at each use (never frozen at import), secrets excluded from `repr`; `status()` returns booleans only. Defines the `adwords` scope and its warning, and the keyring entry names |
| `ads_auth.py` | `AdsTokenProvider` (Google Ads OAuth token, separate keyring entry `oauth-token-ads` or `GADS_TOKEN_FILE`), `run_setup_ads`, `set_secret`. Never touches the `oauth-token` entry |
| `google_ads.py` | `gads_accounts`, `gads_performance`, `gads_query`; the GAQL validator and its resource lists |
| `meta_ads.py` | `meta_ad_accounts`, `meta_insights`; Graph API paging by cursor |
| `openai_ads.py` | `openai_ads_account`, `openai_ads_insights`, `openai_ads_conversions`; fixed host |
| `ads_overview.py` | `ads_overview`: calls the `summary()` of each configured connector and puts the results side by side. Sends no request itself |

Each connector module exposes the same small surface: `TOOLS` (title and function pairs), `CLIENT` (its `AdsClient`), `ALLOWED_ENDPOINTS`, `register(mcp)` and `summary(start_date, end_date)` for the overview.

**`AllowList` per platform.** A rule is a method, a URL pattern anchored at both ends with a pinned host, and optionally a body validator. A request is allowed only if a rule matches the scheme, host and path (query strings are not part of the match) and its validator, if any, accepts the body. URLs with whitespace, non-printable characters, `;`, backslashes, userinfo, `..` or encoded slashes are refused outright. Google Ads uses a validator on its `searchStream` body (the GAQL guard); ChatGPT Ads uses one on `conversions/insights`.

**`AdsClient`.** One per platform. `send` checks the allow-list and refuses secrets or `method` parameters in the URL, then calls the session with redirects disabled and the body streamed under a byte ceiling. `call` wraps it and always returns a dict: the parsed JSON, or an error object (`blocked`, `network`, `response_too_large`, or a platform error mapped to `auth_failed`, `missing_scope`, `rate_limited`, `not_available`, `too_many_rows`, `api_error`) with secrets masked. Settings and tokens are read at each call, so a change of environment is honoured without a restart. Tests replace the client's `session` with a fake one, and the connector test suites check that their modules neither import nor call `requests`.

**Tool shape.** Each tool is a module-level function wrapped in `guard` (turns a missing setting into `not_configured` and adds `fetched_at` and `freshness`) and an input-containment wrapper (invalid input and unexpected exceptions become `invalid_*` and `internal_error` results). The annotations declare `readOnlyHint`.

## Clients

`clients.py` turns a `client_id` into a `ClientContext`: an **immutable** object holding the client's account ids, the names of its keyring entries and its write policy per platform. It is passed explicitly to every tool; no global is changed.

- `resolve(client_id)` reads `clients.json` at each call. No registry means one implicit client `default` built from the old environment variables and keyring entries (legacy mode, no migration). A registry with two clients or more makes `client_id` mandatory. `GEM_CLIENT=<id>` pins the whole process to one client.
- `ctx.owns(platform, id)` and `ctx.require_owned(...)` answer "does this id belong to this client?". Every tool and every write checks it.
- `ctx.secret(kind, write=False)` reads a secret from the environment, then from the keyring (`secrets_store.py`). A secret value is never stored in the context, the registry, a plan or a log.
- `ctx.policy(platform)` returns a frozen `WritePolicy` (everything off by default).

Details and the file format: [MULTI-CLIENT.md](MULTI-CLIENT.md).

## The write path

Every change goes through `write_core.py`, and only through it.

```
 plan_* tool                 approve (terminal)               *_apply tool
 -----------                 ------------------               ------------
 build WriteOps              only if money, personal data,    re-check EVERYTHING:
 ask write_policy            irreversible, removal or         GEM_WRITE, token, confirm text,
 snapshot "before"           high risk                        digest, policy, single use,
 dry-run (native or local)   shows each op in full,           approval, fresh "before"
 store the plan (0600)       you retype the plan id,                |
 journal "plan"              signed record (0600)                   v
 return diff + token  --->   ---------------------------->   for each op: journal intent,
                                                              send with a WriteGrant,
                                                              journal result; stop at the
                                                              first error
```

| Module | Role |
|---|---|
| `write_core.py` | `WriteOp` (one request: method, URL, body, risk, financial flag, `before` snapshot, inverse), `PlatformHandler` (`dry_run`, `snapshot`, `execute`), `create_plan`, `apply_plan`, `rollback_plan`, and the tools `plan_get`, `plan_list`, `audit_log`, `rollback_plan` and the four `*_apply`. Also the approval record |
| `write_policy.py` | The locks: switches, size, URL shape, status rules (creations paused, no activation), delete and irreversible switches, PII, budget caps and the +20 % rule, bidding refusal, and the **field allow-list** (registered per platform and action, then frozen) |
| `write_tokens.py` | `confirmation_token` = `base64url(payload).base64url(hmac)`; the key is derived per client from a master key (`GEM_WRITE_HMAC_KEY`, keyring `write-hmac-master`, or a file in the state folder) |
| `write_journal.py` | JSONL journal per client and month, append and `fsync`, private file, secrets and personal data masked, **fail-closed** (no journal, no write) |
| `ads_common.py` | `WriteAllowList` and `WriteGrant`: a write request leaves the process only if a rule matches and a grant issued by `apply_plan` for that exact method, URL and body digest is given. Reads keep using `AllowList`, which has no write rule |

The four apply tools group the platforms so that you can set your MCP client's permissions per family: `gads_apply` (Google Ads), `meta_apply` (Meta ads and Meta ecosystem), `openai_ads_apply` (ChatGPT Ads) and `google_apply` (GA4, Search Console, Merchant Center, Tag Manager). They are the only tools annotated as non-read-only.

**Rollback** is a new plan, not an undo button. Where the previous state is known (a `before` snapshot), `rollback_plan` builds the inverse operations in reverse order and returns a new plan that goes through the same path, with the same approvals. Where it is not known, the plan says `rollback: none` before you apply it.

## Platform handlers

Each platform module registers a `PlatformHandler` and its field allow-list when the server starts, and exposes its `*_plan_*` tools.

| Platform | Module | Dry run | Notes |
|---|---|---|---|
| Google Ads | `gads_write.py` | native (`validateOnly`) | Pause, daily budget, negative keywords, one paused Search campaign; reads with GAQL through the read client |
| Meta ads | `meta_write.py` | native (`validate_only`) for creations, local for updates | Pause, daily budget, paused campaign, ad set and ad |
| ChatGPT Ads | `openai_write.py` | local (the unit endpoints have no dry run) | Pause, daily budget, paused campaign; the API key cannot be limited to reading |
| Meta ecosystem | `meta_eco.py`, `meta_eco_write.py` | local | Reads: pixel, dataset, catalog, Instagram, Page. Write: one catalog item (price, stock) |
| GA4 | `ga4_tools.py` (+ `google_write.py`) | local | Key event and custom dimension creation; no archive or delete |
| Tag Manager | `gtm_tools.py` (+ `google_write.py`) | quick preview | Workspace, tag, trigger, variable; **nothing goes live** (no version, no publish) |
| Search Console | `gsc_tools.py`, `gsc_write.py` | local | Sitemap submission |
| Merchant Center | `merchant_tools.py`, `merchant_write.py` | local | Product attribute patch with an explicit update mask |

The Google property modules share `google_write.py`: a write token provider kept apart from the read token, pinned HTTP clients, and the checks that run first in every snapshot (the account named in the URL must be the op's account and belong to the client).

## Client report package

`google-ecommerce-mcp report` runs `report/` (a package, not part of the MCP server). It reads one JSON file and writes one static HTML file. It makes no network call and imports no platform client.

| Module | Role |
|---|---|
| `contract.py` | The input contract, `sanitize` (personal-data keys removed, e-mails masked, wrong blocks reported in `errors`) and the accessors sections use (`view`, `periodic`, `totals`, `rows`) |
| `period.py` | Reads `periods`, labels, Search Console lag |
| `compare.py` | Changes between periods: polarity per metric, noise rule (about 2 standard deviations), small-volume rule, Core Web Vitals zones |
| `confidence.py` | High / medium / low with reasons |
| `summary.py` | The three key messages and the decisions, built only from supplied figures |
| `svg.py` | Inline charts: sparkline, columns, lines, bars, thresholds (bars from zero, gaps for missing values) |
| `style.py` | The inline stylesheet and print CSS |
| `brand.py` | The client's name, accent and logo, validated |
| `i18n.py` | French and English texts and number/date formats |
| `render.py` | `ReportContext`, escaping, the notes mini-markdown, assembly, `write_report` |
| `sections/<id>.py` | One module per section: `kpi`, `funnel`, `channels`, `products`, `seo`, `merchant`, `ads`, `cwv`, `data_quality`, `events` |

Parts are ported from OpenGSC (MIT); see `THIRD_PARTY_NOTICES.md`. Guide and contract: [REPORTING.md](REPORTING.md).

## Transport

The MCP client launches the server as a child process and talks to it over **stdio** (JSON-RPC on stdin/stdout). Nothing listens on a port, so nothing on your network can reach the server. The process lives as long as the client keeps it open.

## A tool call, step by step

![Sequence of one tool call](diagrams/sequence-tool-call.png)

1. You ask a question in the chat. The model picks a tool, for example `ga4_report`, and the client sends `tools/call`.
2. On the first call only, the server reads the refresh token from the OS keyring.
3. If the in-memory access token is missing or expired, `google-auth` refreshes it against Google's token endpoint. Access tokens live about one hour and are never written to disk.
4. The server calls the Google API with `Authorization: Bearer ...`.
5. The raw response is flattened into compact JSON (for example GA4 rows become `{"dimension": value, "metric": number}` objects). Report tools add what the model needs to read the numbers correctly: totals over every row, the unit and definition of each metric, and the date range resolved in the data's timezone with `data_complete`. The result goes back to the client.

## One-time setup

![Sequence of the setup command](diagrams/sequence-setup.png)

`google-ecommerce-mcp setup --client-secret client_secret.json` runs Google's installed-app OAuth flow: a temporary localhost redirect catches the authorization code, the code is exchanged for a refresh token, and the token is stored. The setup command is the only code path that writes the token.

## Design choices

**Reading is read-only by construction.** Every read request goes through `_send` in `server.py`, which checks it against `ALLOWED_ENDPOINTS` (method and URL pattern) and refuses anything else before it leaves the process. The list holds only `GET`s and the `POST`s Google uses for queries (`:runReport`, `:runRealtimeReport`, `searchAnalytics/query`, `index:inspect`, `reports:search`). `tests/test_server.py::test_every_request_is_on_the_read_only_allow_list` calls every tool and fails CI on any refused attempt; `test_allow_list_has_no_write_endpoint` guards the list itself.

**Read allow-list per platform.** The paid media connectors repeat the same idea with one allow-list per platform instead of one global list, because the three APIs share nothing: different hosts, auth schemes and error formats. A read-only token cannot be obtained for Google Ads (the `adwords` scope can write), so the allow-list and the GAQL validator are the guarantee there, and the token is stored apart. Details: [SECURITY.md](SECURITY.md#paid-media-connectors-read-side).

**One path for writing.** There is no tool that writes by itself. A change is a plan (inspectable, signed, dry-run), then an apply that re-checks everything. Fewer paths means fewer places to get wrong, and one place to test. What this protects and what it does not: [SECURITY.md](SECURITY.md).

**Fail closed.** No journal, no approval record, no signing key, no policy, no field in the allow-list: the answer is a refusal, never a default that lets the request through.

**Errors are results, not exceptions.** A missing variable returns `{"error": "not_configured", ...}`, an HTTP error returns `{"error": <status>, "detail": ...}`, a network failure returns `{"error": "network", ...}`. The model can read and explain the problem instead of seeing a crashed tool.

**Configuration per service.** Every environment variable is optional. Configure only what you use; the other tools answer `not_configured`.

**Plain HTTP instead of Google client libraries.** The REST endpoints are called with `requests`. That keeps the install small (no `google-api-python-client` discovery documents) and makes each call visible in the source.

**Result caps.** Row-returning tools cap `limit` at 1,000 to keep answers within a model's context window. Merchant product scanning is paginated (250 per page, `max_pages` bound).

## Diagram sources

The diagrams are generated with Archify (MIT-licensed diagram renderer) from the JSON specifications next to them in [`docs/diagrams/`](diagrams/). The architecture overview passes Archify's `validate` and `deliver` gates at showcase quality (9 of 9 checks, 0 errors, 0 warnings). To change a diagram, edit the JSON, then run these commands from the Archify skill directory (Archify v2.17, which has `validate`, `render` and `deliver` but no `finalize`):

```bash
node bin/archify.mjs validate architecture docs/diagrams/architecture-overview.json --quality showcase --json
node bin/archify.mjs deliver architecture docs/diagrams/architecture-overview.json docs/diagrams/architecture-overview.html --quality showcase --json
node bin/archify.mjs deliver sequence docs/diagrams/sequence-tool-call.json docs/diagrams/sequence-tool-call.html --quality showcase --json
node bin/archify.mjs deliver sequence docs/diagrams/sequence-setup.json docs/diagrams/sequence-setup.html --quality showcase --json
```

Archify has no command-line PNG export. The PNG is a headless Edge screenshot of the delivered HTML at device scale factor 2, taken from a temporary copy of the HTML where a CSS rule hides the viewer's navigation bar, toolbar and header (`.diagram-nav`, `.toolbar`, `.header`), then cropped to the diagram, legend and cards:

```bash
msedge --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=2 --screenshot=shot.png --window-size=1500,1500 file:///.../docs/diagrams/architecture-overview.html
```

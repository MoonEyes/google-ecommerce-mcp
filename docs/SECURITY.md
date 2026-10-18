# Security model

This page says what the server can access, what can go wrong, which protections are real, and which are not. To report a vulnerability, see [SECURITY.md](../SECURITY.md) at the repository root.

## The short version (0.5.0)

- **Reading is the default and stays read-only.** With no extra setting, no tool changes anything. Every read request is checked against a per-platform allow-list that contains no write endpoint.
- **Writing exists, is off by default, and is opt-in per client and per platform.** For a client whose writing you turned on, the old promise "this server can never write" **no longer holds**. What holds instead is a set of locks, listed below. They reduce the risk; they do not remove it.
- **No write has been tried on a real account.** Everything is tested offline against fake answers written from the platforms' documentation. Treat the first real writes as experiments, on a test account.
- **Prompt injection can now start a change.** A web page, a product name or a comment read by the assistant can try to make it prepare a plan. The locks below are designed so that such a plan stays small, paused, visible and, when it costs money, impossible to apply without you typing in a terminal. They do not make it impossible.

If you only read data, set nothing: `GEM_WRITE` unset means every apply is refused, whatever the registry says.

## What is protected

The assets are your **credentials** and, now, **your ad accounts, feeds and tags**.

- Credentials: the Google OAuth tokens (one read token and, if you use writing, one separate write token, per client), the Google Ads token, the Meta access token, the ChatGPT Ads API key. Whoever holds a token can call the matching API as you until you revoke it.
- Accounts: with writing on, a wrong or hostile plan could pause good campaigns, change a budget, edit a product price, or add a tag to your site.

## Threat model for writing

| Who or what | What it could try | What stops it |
|---|---|---|
| A prompt injection (text in a page, a review, a product title, a sheet) | Make the assistant plan a harmful change | Writing is off by default; the plan must pass the policy of the client (allowed accounts, caps, paused creations, field allow-list); money, personal data, removal and high-risk plans need your approval in a terminal; every plan is journaled |
| The assistant by mistake (wrong client, wrong account, wrong unit) | Change the wrong account or a 100 times bigger budget | Each tool works on one client; an account must be owned by the client and listed in `account_allow`; budget caps and +20 % per 24 h limit; amounts are checked in the account currency |
| A plan edited after you read it | Swap the content between "show" and "apply" | The confirmation token is an HMAC bound to the exact content of the plan (accounts, operations in order, API version, policy). Any change invalidates it |
| A plan replayed | Apply twice | A token works once; the journal records it and a second apply is refused |
| A stolen token (Google, Meta, OpenAI) | Call the platform directly | **Not stopped by this server**, see the limits below |
| Another process on your machine, as you | Read the keyring, edit the policy | Out of scope for a local tool; see the limits below |

## The real guardrails

Each line is checked in code **when the plan is made and again when it is applied**.

| Guardrail | What it does |
|---|---|
| Writing off by default | `GEM_WRITE=1` must be set in the server environment (exactly `1`), **and** `write.<platform>.enabled` must be true for the client, **and** the account of the plan must be in that client's `account_allow` **and** belong to that client |
| One client at a time | Every tool gets a client. An id that does not belong to the client is refused (`id_not_owned`). `GEM_CLIENT=<id>` pins a whole server process to one client |
| Spending caps | `max_daily_budget_micros` and `cap_currency` (the account currency must match), `max_increase_pct` (default 20 % over 24 h, measured against the freshest value read and the values journaled in the last 24 h), `max_ops_per_plan` (default 25, hard limit 100) |
| Creations are paused | Every new campaign, ad set or ad is created `PAUSED`; the builder forces it and the policy refuses anything else |
| No activation, no delete, no publish in v1 | There is no tool that sets `ENABLED` or `ACTIVE`, deletes or removes, publishes a GTM container, or archives. The only way back to active is `rollback_plan` of a pause, and it is refused unless `allow_activate` is true. Deletion needs `allow_delete`, irreversible needs `allow_irreversible`, personal data needs `allow_pii` (all false) |
| Field allow-list | Each action may only send the exact fields registered for it. Anything else is refused (`field_not_allowed`). A field name that means money (budget, bid, price, cap...) must be on a short, explicit list. Bidding and bid-strategy fields are refused in v1 |
| Plan, then approval | Nothing is written by a `*_plan_*` tool. A plan returns a diff, the risks, a signed `confirmation_token` and the exact `confirm` text. Only the four `*_apply` tools write |
| Human approval outside the chat | A plan that **changes spending**, is **irreversible**, **removes or archives**, touches **personal data**, or has **risk high** (for example a GTM custom HTML tag) is refused by apply until you run `google-ecommerce-mcp approve <plan_id> --client <id>` **in a terminal**. The command shows every operation in full and asks you to retype the plan id. It refuses to run without a real terminal. There is no environment switch to skip it |
| Signed tokens | `confirmation_token` is an HMAC-SHA256 over the plan id, client, platform, content digest and expiry, with a key derived per client from a master key. It expires (15 minutes by default, 60 at most), works once, and is checked in constant time |
| Fail-closed journal | Every plan, approval, intent and result is appended to a JSONL journal (one file per client and per month, mode `0600`). If the journal cannot be written, the plan or the apply is refused |
| Dry run | Google Ads uses `validateOnly`; Meta creations use `validate_only`; Tag Manager uses a quick preview; every other check is local and the plan says `mode: local`. A plan whose dry run fails gets no token |
| A signed grant per request | A write request leaves the process only with a grant issued by the apply step for that exact method, URL and body. Read tools hold no write allow-list at all |
| Untrusted text is labelled | Names and titles that come from a platform are cleaned of control and invisible characters, capped in length, and listed under `untrusted_text` so the assistant does not obey them |
| Sandbox rule for live tests | With `GEM_LIVE_TEST=1`, apply works only for a client marked `"sandbox": true` |

### What each kind of plan needs, in practice

| Kind of plan | Examples | Token and `confirm` | Your approval in a terminal |
|---|---|---|---|
| Low risk, no money | Pause a Google Ads campaign or Meta ad, submit a sitemap, create a GA4 key event, a normal GTM tag in a workspace | yes | no |
| Money or high risk | Any budget change, a new campaign, a price change in Merchant Center or a Meta catalog, a GTM custom HTML tag | yes | **yes** |
| Irreversible, removal, personal data | Not offered in v1 | yes | yes, and the matching `allow_*` switch |

For the first row, the only human step is the approval prompt of your MCP client on the `*_apply` tool. **If you set that tool to "always allow", the assistant can apply low-risk plans without asking you.** Leave the four apply tools on "ask each time".

## Known limits (read this before turning writing on)

These are real, and they are written here on purpose.

1. **The terminal check does not protect you from a model that has a shell.** `approve` refuses to run without an interactive terminal, which stops an assistant that only has MCP tools, and a plain script or pipe. It does **not** stop a model that can open a terminal session (a shell tool with a pseudo-terminal, an emulator driven by keystrokes). It also does not stop anything that can read the signing key and forge the approval record. The key lives in one of three places: the variable `GEM_WRITE_HMAC_KEY`, the keyring entry `write-hmac-master`, or the file `<state>/write-hmac-master.key`. If your assistant has a shell or file access on this machine, treat the human approval as weakened and keep it away from those places.
2. **Two credentials cannot be split into read and write.** The Google Ads token (scope `adwords`) and the ChatGPT Ads API key do the same thing for reading and writing. The same token is used for both. Only this server's policy stands between the assistant and a change, and a stolen token ignores that policy. Google offers separate scopes for Analytics, Tag Manager, Search Console and Merchant Center, so those get a separate write token (`setup --client <id> --write`); Google Ads and ChatGPT Ads do not.
3. **The policy is a file you can edit.** `clients.json` (and the `GEM_WRITE` setting in your MCP client config) can be changed by you, and by anything running as your user, including an assistant with file access. Changing it invalidates plans already made, but a new plan would pass. The registry holds ids and limits only, never a secret.
4. **No write was verified on a real account.** Everything is tested offline. Specifically unconfirmed: that Meta's Graph API accepts the `Authorization: Bearer` header on these write calls, that Meta's `validate_only` option works as documented, that Merchant Center accepts the update mask we build, that ChatGPT Ads, GA4, GTM and Search Console writes behave as documented. The first writes on a real account are experiments.
5. **Bids are refused in v1.** The policy cannot yet bound a bidding strategy, so any bid or bidding field is rejected. This blocks some creations (for example a Meta ad set whose goal needs a bid cap). Google Ads campaigns are created without a bidding field and take Google's default.
6. **`max_plan_budget_micros` is not usable.** The code can read it, but the registry does not accept it. The only plan-level ceiling is `max_daily_budget_micros` multiplied by `max_ops_per_plan`.
7. **The journal is a log, not evidence.** It is private and fail-closed, but it is a local file. It is not signed or chained, so someone with access to your folder could edit it.
8. **A plan can be read by the assistant, including its token.** The token proves the plan was not altered. It does not prove you agreed. Only the approval prompt of your MCP client and, for money, `approve` do.
9. **One machine, one user.** Plans, approvals and the journal live in the state folder of your profile. There is no multi-user protection, no remote mode and no central log.
10. **Data still leaves your machine through the model.** Everything a tool returns goes to your MCP client and its model provider.
11. **Tokens stay cached until the server restarts.** After `client remove`, or after you revoke the app at https://myaccount.google.com/permissions (or rotate a Meta / ChatGPT Ads secret), a server process that is already running may keep using the credential it has in memory until the access token expires or the process ends. Restart your MCP client (quit Claude Desktop completely) after removing a client or revoking access.
12. **The read token of a client with Merchant Center or Indexing carries a Google write scope.** Google offers no read-only scope for Merchant Center (`content`) nor for the Indexing API (`indexing`). When a client has a Merchant Center id, or you ask for indexing, its read token (`<client>/google`) can modify products and feeds or submit URL notifications. This server's read allow-lists never call those write endpoints, but a stolen token would not be limited by them. `setup` says so before the browser opens.
13. **The Claude entry is pinned to a version when writing is consented.** `install --client X --write` writes `"args": ["google-ecommerce-mcp==<version>"]` so that an update never brings new write code in silently. The other side: that entry no longer receives fixes, security ones included, until you rerun the same command after updating (it asks your consent again, group by group).

## Where secrets live

| What | Storage | Written by |
|---|---|---|
| Google read token | OS keyring: `<client>/google` (legacy: `oauth-token`), or the file in `GOOGLE_TOKEN_FILE` | `setup` |
| Google write token (separate) | OS keyring: `<client>/google-w` | `setup --client <id> --write` |
| Google Ads token (`adwords`) | OS keyring: `<client>/gads` (legacy: `oauth-token-ads`), or `GADS_TOKEN_FILE` | `setup-ads` |
| Meta access token | OS keyring: `<client>/meta` (legacy: `meta-access-token`), or `META_ACCESS_TOKEN` | `client set-secret <id> meta` |
| ChatGPT Ads API key | OS keyring: `<client>/openai` (legacy: `openai-ads-key`), or `OPENAI_ADS_API_KEY` | `client set-secret <id> openai` |
| Signing key for plans and approvals | `GEM_WRITE_HMAC_KEY`, else keyring `write-hmac-master`, else file `<state>/write-hmac-master.key` (mode `0600`) | created on first use |

The keyring is the default: Windows Credential Manager, macOS Keychain or Secret Service, encrypted at rest and scoped to your OS user. A token file is written with mode `0600` on POSIX systems; on Windows the server restricts it to your account with `icacls` and warns if it cannot (best effort).

Access tokens (valid about one hour) stay in memory. A secret is read from a hidden prompt, never from the command line. **The client registry never holds a secret**, only keyring entry names, and it refuses a value that looks like a key. In multi-client mode the legacy variables `META_ACCESS_TOKEN` and `OPENAI_ADS_API_KEY` are not read for registered clients, so one variable cannot feed two clients. Per-client environment variables use the form `GEM_SECRET_<CLIENT>_<KIND>`.

`setup-ads` never touches the Google read token, and refuses a `--token-file` equal to `GOOGLE_TOKEN_FILE`.

## Scopes and what a stolen token could do

| Scope | Token | Used for | What the token could do |
|---|---|---|---|
| `analytics.readonly` | read | GA4 reports | Read analytics data |
| `webmasters.readonly` | read | Search Console | Read search data and inspect URLs |
| `tagmanager.readonly` | read | GTM inventory | Read container configuration |
| `content` | read, if you ask for Merchant | Merchant API | Read **and modify** products, feeds, settings. Google has no read-only scope |
| `indexing` | read, if you ask for it | Indexing status | Read notification metadata **and submit** URL notifications |
| `analytics.edit` | **write** | GA4 key events and custom dimensions | Edit the GA4 property configuration |
| `tagmanager.edit.containers` (and `tagmanager.edit.containerversions` for the preview) | **write** | GTM workspaces, tags, triggers, variables | Edit containers. The publish scope is never requested in v1 |
| `webmasters` | **write** | Sitemap submission | Add or remove sites and sitemaps |
| `content` | **write** | Merchant product updates | As above |
| `adwords` | read **and** write, cannot be limited | Google Ads | Read and modify the account: campaigns, budgets, bids, conversion actions |

**Least privilege by default.** `setup` asks for the three read-only scopes. `content` and `indexing` are added only with `--with-merchant` and `--with-indexing`. The write token is created only by `setup --client <id> --write`, and a client without it has no way to write on Google properties (`write token missing`). `server_status` and `client_check` list the scopes held and flag the write-capable ones.

## Guarantees enforced by code: reading

- **Read allow-lists.** `ALLOWED_ENDPOINTS` in `server.py` and one allow-list per paid media platform list every read request. Any other request is refused before it leaves the process, with `{"error": "blocked"}`. These lists contain no write endpoint, and a test fails if one is added.
- **One separate write allow-list per platform.** Writes use a different object (`WriteAllowList`), without wildcards, with pinned hosts and API versions, and each rule is either a "validate" rule (the body must carry the dry-run flag) or a "write" rule (a signed grant is required). A test checks that every write request carries a grant.
- **Only the four `*_apply` tools** have `readOnlyHint` set to false. Reading tools and `*_plan_*` tools are annotated read-only, and the list of tools with their annotations is frozen by a test.
- No listener: stdio only, no open port. No telemetry, no third-party endpoint: the only hosts contacted are `*.googleapis.com`, Google's OAuth endpoint and, if you configure them, `graph.facebook.com` and `api.ads.openai.com`. Hosts are pinned in each allow-list.
- No account id is hard-coded; ids come from your environment or from the client registry.

## Paid media connectors: read side

What an attacker or a mistaken prompt could try, and what stops it. The connectors were tested offline only (see [ADS.md](ADS.md#what-has-not-been-tested-against-real-accounts)).

- Google Ads reads: `GET customers:listAccessibleCustomers` and `POST customers/<digits>/googleAds:searchStream`. The `POST` is accepted only when its body is a bounded read-only GAQL query: starts with `SELECT`, no `;`, `--`, `/*`, `*/` or backslash (even inside a string), one `SELECT` and one `FROM`, no `JOIN`, a `LIMIT` of at most 10,001, a `FROM` and field prefixes on the reporting allow-list, and no personal-data or billing name unless `GADS_ALLOW_SENSITIVE_QUERIES=1`. The same validator runs inside the allow-list.
- Meta reads: `GET` only, host `graph.facebook.com`. A `method` or `access_token` query parameter is refused (the Graph API honours `?method=delete` on a GET). Pagination follows `paging.cursors.after`; the `paging.next` URL is never requested.
- ChatGPT Ads reads: `GET` on account, campaign, ad group, ad and insights endpoints, and `POST /v1/conversions/insights`, a read query whose body is checked by a validator.

**Guards in `AdsClient` (reads and writes):**

- Redirects are never followed. A `3xx` answer is returned as `blocked`, so `Authorization` and `login-customer-id` headers cannot be replayed to another host.
- Responses are streamed with a 20 MB ceiling (`response_too_large`).
- Secrets are masked in every error and log line: exact values, plus the shapes `Bearer ...`, `access_token=...`, Meta `EAA...`, Google `ya29....` and `1//...`, and OpenAI `sk-...`. A secret found in a URL blocks the request instead of sending it.
- Unexpected failures become structured `internal_error` results instead of a stack trace.
- The environment is ignored by default: no `HTTPS_PROXY`, `REQUESTS_CA_BUNDLE` or `.netrc`. `ADS_TRUST_ENV=1` is an explicit opt-in.
- `openai_ads_account` keeps five fields and lists the rest by name. Attributed event details from `openai_ads_conversions` are reduced to documented aggregate fields, because they can contain order or user identifiers.

**Risks that remain, reading and writing:**

| Risk | Why it remains | What you can do |
|---|---|---|
| The Google Ads token and the ChatGPT Ads key can write | No read-only variant exists | Dedicated credential per client, keyring storage, revoke when unused. See limit 2 |
| The Meta token is long-lived | System user tokens can be created without a short expiry | `ads_read` only (add `ads_management` only for a client where you turn writing on), a role limited to one ad account, revoke in Business Settings when unused |
| A token file on Windows | POSIX mode `0600` has no effect there; the `icacls` restriction is best effort | Prefer the keyring |
| A relaxed GAQL check | `GADS_ALLOW_SENSITIVE_QUERIES=1` lifts the resource lists, so personal data can reach the conversation | Leave it unset unless you need it |
| Platform policy | A platform may treat automated changes differently from reading (for example a developer or app review for write access, or limits on non-official connectors) | Start on test accounts; read the platform's rules |
| Offline tests only | The allow-lists and validators are tested, the platforms' real behaviour is not | Compare the first answers with each platform's interface |

## Client report: threat model

`google-ecommerce-mcp report` reads a JSON file and writes an HTML file. It makes no network call and reads no credential. What it guards against:

| Threat | Mitigation |
|---|---|
| Markup or script injected through a value in the JSON (a campaign name, a note) | Every value is HTML-escaped; the notes are escaped first, then a tiny markdown subset becomes tags; links keep their text only; the page contains no `<script>` |
| A booby-trapped logo (an SVG with a script, an event handler, `javascript:`, `foreignObject`, an entity, a character reference `&#`, `<animate>` or `<set>`, text before the first tag, or an external reference) | The logo must be a PNG, JPEG or SVG data URI of at most 256 KB; PNG and JPEG need their magic bytes; an SVG with any of the above is refused and left out with a warning |
| A crafted accent colour breaking out of the stylesheet | The accent must match `#RRGGBB` exactly; anything else falls back to the default |
| The file phoning home when opened | No external URL, no web font, no script, no `@import`; a Content-Security-Policy meta tag backs this up; page URLs are shown as paths without query string, and identifier parameters (`gclid`, `order_id`...) are masked and reported; the logo is embedded |
| Personal data in a tool result reaching the file | Keys such as `email`, `transaction_id`, `order_id`, `user_id`, `client_id`, `phone` and names are dropped wherever they appear, e-mails in text are masked, and every removal is listed in section 10 |
| Overwriting a file by mistake, or a half-written report | An existing output file is refused unless `--force`; the write is atomic; `--output` equal to `--input` is refused |
| A very large or hostile input | Input over 50 MB is refused; JSON nested over 100 levels, a huge integer or an invalid character gives exit code 1 without a traceback |

The report shows what the tools returned, so it can still hold business figures: share it only with the client.

## Other threats

| Threat | Mitigation |
|---|---|
| Malware running as your OS user reads the keyring | Out of scope for any local tool; revoke the tokens if your machine is compromised |
| Token file committed to git | Use the keyring (default). `.gitignore` excludes `token*.json` and `client_secret*.json` |
| A planted or edited OAuth client file sends the consent or the token exchange to another server | Every `setup`, `setup-ads` and `install` (with or without `--client`) checks the file before any flow or browser: root key `installed` only (Desktop app), `auth_uri` on `https://accounts.google.com/`, `token_uri` exactly `https://oauth2.googleapis.com/token`, `client_id` and `client_secret` present, local loopback redirects only. Otherwise it is refused with where, why and the fix, and nothing is stored. A write token never uses a file found automatically: `--client-secret` must be given |
| Writing turned on without a human | `--yes` never answers a write question. `install --client X --write` offers writing group by group (Google properties, Google Ads, Meta, ChatGPT Ads), each with its own question and the typed client name, in a real terminal only; `GEM_WRITE=1` is added only after a consent typed in that run |
| Prompt injection makes the model call tools with odd arguments | Reading: the worst case is reading your own data into the conversation. Writing (turned on): see the threat model and the limits above. Review what you paste into the chat, and keep the apply tools on "ask each time" |
| Data leaves your machine through the model | Tool results go to your MCP client and therefore to your model provider. Do not connect accounts whose data you may not share with that provider |
| Supply-chain change in a dependency | Dependencies are few and pinned by range (`mcp<2`); install from a tagged release and review updates |

## Revoke access

- One click: [Google account > Security > Third-party connections](https://myaccount.google.com/permissions), remove your app. This kills the read, write and Google Ads tokens.
- Or delete the OAuth client in Google Cloud Console: every token issued to it stops working.
- Then delete the local copies. Legacy mode: `uvx keyring del google-ecommerce-mcp oauth-token`. Multi-client mode: `google-ecommerce-mcp client remove <id>` deletes the client's keyring entries.
- Turn writing off at any time: `google-ecommerce-mcp client disable-write <id> <family>`, or remove `GEM_WRITE` from your MCP client config.

Paid media credentials:

- Google Ads: remove the app in your [Google account permissions](https://myaccount.google.com/permissions), then `uvx keyring del google-ecommerce-mcp oauth-token-ads` (or `<client>/gads`), or delete the `GADS_TOKEN_FILE` file.
- Meta: revoke or regenerate the system user token in Business Settings, then delete the keyring entry (`meta-access-token` or `<client>/meta`).
- ChatGPT Ads: revoke the API key where you created it in Ads Manager, then delete the keyring entry (`openai-ads-key` or `<client>/openai`).

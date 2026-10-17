# Security model

This page explains what the server can access, what it does with it, and how to limit the damage if something goes wrong. To report a vulnerability, see [SECURITY.md](../SECURITY.md) at the repository root.

## What is protected

The assets are your **credentials**: the Google OAuth refresh token(s), and, if you use the paid media connectors, the Meta access token and the ChatGPT Ads API key. Whoever holds the Google token, together with your OAuth client, can call the granted Google APIs as you until you revoke it. A Meta token or a ChatGPT Ads key is used on its own.

## Where the token lives

| Storage | When | Protection |
|---|---|---|
| OS keyring (default) | Always, unless `GOOGLE_TOKEN_FILE` is set | Windows Credential Manager, macOS Keychain, Secret Service: encrypted at rest, scoped to your OS user |
| File | `GOOGLE_TOKEN_FILE` set | Written with permissions `0600` on POSIX systems; on Windows rely on your profile ACLs |

Access tokens (valid about one hour) stay in process memory and are never written. The refresh token is written only by the `setup` command.

The paid media credentials are stored apart:

| Credential | Storage | Written by |
|---|---|---|
| Google Ads OAuth token (scope `adwords`) | OS keyring entry `oauth-token-ads`, or the file named by `GADS_TOKEN_FILE` | `setup-ads` only |
| Meta access token | `META_ACCESS_TOKEN`, else OS keyring entry `meta-access-token` | `set-secret meta-access-token` (hidden prompt) |
| ChatGPT Ads API key | `OPENAI_ADS_API_KEY`, else OS keyring entry `openai-ads-key` | `set-secret openai-ads-key` (hidden prompt) |

`setup-ads` never touches the `oauth-token` entry or `GOOGLE_TOKEN_FILE`. It refuses a `--token-file` equal to `GOOGLE_TOKEN_FILE`, and refuses to overwrite an existing file that is not a Google Ads token. A secret is read from a hidden prompt, never from the command line, so it stays out of shell history. A token file is created with mode `0600` on POSIX systems; on Windows the server tries to restrict the file to your account with `icacls` and warns when it cannot (best effort, see the risks below).

## Scopes and what they could do

| Scope | Read-only? | Used for | What the token could do if stolen |
|---|---|---|---|
| `analytics.readonly` | yes | GA4 reports | Read analytics data |
| `webmasters.readonly` | yes | Search Console | Read search data and inspect URLs |
| `tagmanager.readonly` | yes | GTM inventory | Read container configuration |
| `content` | **no** | Merchant API | Read **and modify** Merchant Center products, feeds and settings |
| `indexing` | **no** | Indexing status | Read notification metadata **and submit** URL notifications |
| `adwords` | **no, and it cannot be limited** | Google Ads tools, through the separate `setup-ads` token | Read **and modify** the Google Ads account: campaigns, budgets, bids, conversion actions |

Google offers no read-only scope for the Merchant API or the Indexing API. The server never calls their write endpoints, but the token itself carries the permission. Treat it like a password.

The `adwords` scope is the broadest of the list and Google has no read-only variant. That is why it never travels with the other scopes: `scopes_for()` never contains it, `setup-ads` requests it alone, and the token lives in its own keyring entry. `server_status` and `check` report which Ads settings are present (booleans only) and repeat the warning. If you do not use Google Ads, never run `setup-ads`.

**Least privilege by default.** `setup` requests only the three read-only scopes. `content` is requested only with `--with-merchant` (the installer adds it when you give a Merchant Center id) and `indexing` only with `--with-indexing`. `server_status` lists the scopes your token holds and flags the write-capable ones under `write_capable_scopes`. A server that is read-only today does nothing for the next version if the token can write, so keep the token as narrow as your usage.

## Guarantees enforced by code

- **Runtime allow-list.** `ALLOWED_ENDPOINTS` in `server.py` lists every request the server may send (method and URL pattern). Any other request is refused before it leaves the process, with `{"error": "blocked"}`. `readOnlyHint` is declared too, but a client may ignore it: the allow-list is the guarantee.
- **Build gate.** `test_every_request_is_on_the_read_only_allow_list` runs every tool and fails CI on any blocked attempt. `test_allow_list_has_no_write_endpoint` fails if the list gains a `PUT`, `PATCH`, `DELETE` or a non-query `POST`. Tests also check that publish, delete and product-insert calls are refused without a network call.
- No listener: stdio transport only, no open port.
- No telemetry, no third-party endpoint: the only hosts contacted are `*.googleapis.com` and Google's OAuth endpoint, plus, only if you configure them, `graph.facebook.com` (Meta) and `api.ads.openai.com` (ChatGPT Ads). Hosts are pinned in each platform's allow-list; the ChatGPT Ads host is a constant and cannot be set through the environment or a tool parameter.
- No account id is hard-coded; everything comes from your environment.

## Paid media connectors: threat model

What an attacker or a mistaken prompt could try, and what stops it. The connectors have been tested offline only (see [ADS.md](ADS.md#what-has-not-been-tested-against-real-accounts)).

**Read-only is enforced per platform, in code.** Each platform has its own `AllowList` (method, anchored URL pattern, optional body validator), checked by `AdsClient` before anything is sent:

- Google Ads: `GET customers:listAccessibleCustomers` and `POST customers/<digits>/googleAds:searchStream`. No `:mutate` endpoint is listed. The `POST` is accepted only when its body is a bounded read-only GAQL query: starts with `SELECT`, no `;`, `--`, `/*`, `*/` or backslash (even inside a string), one `SELECT` and one `FROM`, no `JOIN`, a `LIMIT` of at most 10,001, a `FROM` and field prefixes on the reporting allow-list, and no personal-data or billing name unless `GADS_ALLOW_SENSITIVE_QUERIES=1`. The same validator runs inside the allow-list, so a query that skips the tool-level check is still refused.
- Meta: `GET` only, host `graph.facebook.com`, three paths (`/me/adaccounts`, `/act_<id>`, `/act_<id>/insights`). A `method` or `access_token` query parameter is refused (the Graph API honours `?method=delete` on a GET). Pagination follows `paging.cursors.after`; the `paging.next` URL is never requested.
- ChatGPT Ads: `GET` on the ad account and insights endpoints, and `POST /v1/conversions/insights`, a read query whose body is checked by a validator. Nothing on campaigns, ad groups or ads except their insights.

**Other guards, in `AdsClient`:**

- Redirects are never followed. A `3xx` answer is returned as `blocked`, so the `Authorization`, developer token and `login-customer-id` headers cannot be replayed to another host.
- Responses are streamed with a 20 MB ceiling and refused beyond it (`response_too_large`), so a huge answer does not exhaust memory.
- Secrets are masked in every error and log line: exact values, plus the shapes `Bearer ...`, `access_token=...`, Meta `EAA...`, Google `ya29....` and `1//...`, and OpenAI `sk-...`. A secret found in a URL blocks the request instead of sending it. Meta and ChatGPT Ads secrets travel only in the `Authorization: Bearer` header.
- Unexpected failures become structured `internal_error` results, with secrets masked, instead of reaching the client as a stack trace.
- The environment is ignored by default: no `HTTPS_PROXY`, `REQUESTS_CA_BUNDLE` or `.netrc`. `ADS_TRUST_ENV=1` is an explicit opt-in for the proxy and CA bundle only. The OAuth refresh of the Google Ads token uses the same hardened session and is refused if the stored token names an endpoint other than Google's.
- `openai_ads_account` keeps five fields and lists the rest by name. Attributed event details from `openai_ads_conversions` are reduced to documented aggregate fields, with a warning, because they can contain order or user identifiers.

**Risks that remain:**

| Risk | Why it remains | What you can do |
|---|---|---|
| The Google Ads token can write | The `adwords` scope has no read-only variant. The server never sends a write, but whoever steals the token can | Keyring storage (default), a separate Google Cloud project if you want to isolate it, revoke it when unused |
| The ChatGPT Ads key may not be limited to reading | OpenAI's documentation does not confirm a read-only key. The key may be able to change campaigns and budgets | Dedicated key, never in a shared config, revoke when unused |
| The Meta token is long-lived | System user tokens can be created without a short expiry | Only `ads_read`, a read or analyst role on one ad account, revoke in Business Settings when unused |
| A token file on Windows | POSIX mode `0600` has no effect there; the `icacls` restriction is best effort and a warning is printed when it fails | Prefer the keyring; keep any token file inside your user profile |
| A relaxed GAQL check | `GADS_ALLOW_SENSITIVE_QUERIES=1` lifts the resource lists, so personal data (search terms, click ids, user lists) can reach the conversation and your AI provider | Leave it unset unless you need it |
| Data leaves your machine through the model | Same as the Google tools: results go to your MCP client and its model provider | Do not connect accounts whose data you may not share with that provider |
| Offline tests only | The allow-lists and validators are tested, but the platforms' real behaviour is not | Compare the first answers with each platform's interface |

## Client report: threat model

`google-ecommerce-mcp report` reads a JSON file and writes an HTML file. It makes no network call and reads no credential. What it guards against:

| Threat | Mitigation |
|---|---|
| Markup or script injected through a value in the JSON (a campaign name, a note) | Every value is HTML-escaped; the notes are escaped first, then a tiny markdown subset becomes tags; links keep their text only; the page contains no `<script>` |
| A booby-trapped logo (an SVG with a script, an event handler, `javascript:`, `foreignObject`, an entity, a character reference `&#`, `<animate>` or `<set>`, text before the first tag, or an external reference) | The logo must be a PNG, JPEG or SVG data URI of at most 256 KB; PNG and JPEG need their magic bytes; an SVG with any of the above is refused and left out with a warning |
| A crafted accent colour breaking out of the stylesheet | The accent must match `#RRGGBB` exactly; anything else falls back to the default |
| The file phoning home when opened | No external URL, no web font, no script, no `@import`; a Content-Security-Policy meta tag backs this up; page URLs are shown as paths without query string, and identifier parameters (`gclid`, `order_id`...) are masked and reported; the logo is embedded |
| Personal data in a tool result reaching the file | Keys such as `email`, `transaction_id`, `order_id`, `user_id`, `client_id`, `phone` and names are dropped wherever they appear, e-mails in text are masked, and every removal is listed in section 10 |
| Overwriting a file by mistake, or a half-written report | An existing output file is refused unless `--force`; the write is atomic (temporary file, then rename), so an existing report stays intact after a failure; `--output` equal to `--input` is refused |
| A very large or hostile input | Input over 50 MB is refused; JSON nested over 100 levels, a huge integer or an invalid character gives exit code 1 without a traceback; numbers beyond 1e15, NaN and booleans are treated as "not provided" |

The report itself shows what the tools returned, so it can still hold business figures: share it only with the client.

## Threats and mitigations

| Threat | Mitigation |
|---|---|
| Malware running as your OS user reads the keyring | Out of scope for any local tool; revoke the token if your machine is compromised |
| Token file committed to git | Use the keyring (default). `.gitignore` excludes `token*.json` and `client_secret*.json` |
| Prompt injection makes the model call tools with odd arguments | Tools cannot write, so the worst case is reading your own data into the conversation. Review what you paste into the chat |
| Data leaves your machine through the model | Tool results go to your MCP client and therefore to the model provider you use. Do not connect accounts whose data you may not share with that provider |
| Supply-chain change in a dependency | Dependencies are few and pinned by range (`mcp<2`); install from a tagged release and review updates |

## Revoke access

- One click: [Google account > Security > Third-party connections](https://myaccount.google.com/permissions), remove your app.
- Or delete the OAuth client in Google Cloud Console: every token issued to it stops working.
- Then delete the local copy: `uvx keyring del google-ecommerce-mcp oauth-token`, or delete the token file.

Paid media credentials:

- Google Ads: remove the app in your [Google account permissions](https://myaccount.google.com/permissions), then `uvx keyring del google-ecommerce-mcp oauth-token-ads` (or delete the `GADS_TOKEN_FILE` file).
- Meta: revoke or regenerate the system user token in Business Settings, then `uvx keyring del google-ecommerce-mcp meta-access-token`.
- ChatGPT Ads: revoke the API key where you created it in Ads Manager, then `uvx keyring del google-ecommerce-mcp openai-ads-key`.

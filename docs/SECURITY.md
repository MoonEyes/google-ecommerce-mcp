# Security model

This page explains what the server can access, what it does with it, and how to limit the damage if something goes wrong. To report a vulnerability, see [SECURITY.md](../SECURITY.md) at the repository root.

## What is protected

The asset is the **OAuth refresh token**. Whoever holds it, together with your OAuth client, can call the granted Google APIs as you until you revoke it.

## Where the token lives

| Storage | When | Protection |
|---|---|---|
| OS keyring (default) | Always, unless `GOOGLE_TOKEN_FILE` is set | Windows Credential Manager, macOS Keychain, Secret Service: encrypted at rest, scoped to your OS user |
| File | `GOOGLE_TOKEN_FILE` set | Written with permissions `0600` on POSIX systems; on Windows rely on your profile ACLs |

Access tokens (valid about one hour) stay in process memory and are never written. The refresh token is written only by the `setup` command.

## Scopes and what they could do

| Scope | Read-only? | Used for | What the token could do if stolen |
|---|---|---|---|
| `analytics.readonly` | yes | GA4 reports | Read analytics data |
| `webmasters.readonly` | yes | Search Console | Read search data and inspect URLs |
| `tagmanager.readonly` | yes | GTM inventory | Read container configuration |
| `content` | **no** | Merchant API | Read **and modify** Merchant Center products, feeds and settings |
| `indexing` | **no** | Indexing status | Read notification metadata **and submit** URL notifications |

Google offers no read-only scope for the Merchant API or the Indexing API. The server never calls their write endpoints, but the token itself carries the permission. Treat it like a password.

**Least privilege by default.** `setup` requests only the three read-only scopes. `content` is requested only with `--with-merchant` (the installer adds it when you give a Merchant Center id) and `indexing` only with `--with-indexing`. `server_status` lists the scopes your token holds and flags the write-capable ones under `write_capable_scopes`. A server that is read-only today does nothing for the next version if the token can write, so keep the token as narrow as your usage.

## Guarantees enforced by code

- **Runtime allow-list.** `ALLOWED_ENDPOINTS` in `server.py` lists every request the server may send (method and URL pattern). Any other request is refused before it leaves the process, with `{"error": "blocked"}`. `readOnlyHint` is declared too, but a client may ignore it: the allow-list is the guarantee.
- **Build gate.** `test_every_request_is_on_the_read_only_allow_list` runs every tool and fails CI on any blocked attempt. `test_allow_list_has_no_write_endpoint` fails if the list gains a `PUT`, `PATCH`, `DELETE` or a non-query `POST`. Tests also check that publish, delete and product-insert calls are refused without a network call.
- No listener: stdio transport only, no open port.
- No telemetry, no third-party endpoint: the only hosts contacted are `*.googleapis.com` and Google's OAuth endpoint.
- No account id is hard-coded; everything comes from your environment.

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
- Then delete the local copy: `keyring del google-ecommerce-mcp oauth-token`, or delete the token file.

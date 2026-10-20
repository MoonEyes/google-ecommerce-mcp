# Reading pilot for an agency

A pilot where the assistant reads one client's data and prepares no change. Writing stays off. This page only assembles steps documented elsewhere in this repository; the links give the detail.

> **Not tested on real accounts.** The Google services and the paid media connectors were checked against fake answers. Compare the first figures with each platform's own interface ([ADS.md](ADS.md#what-has-not-been-tested-against-real-accounts)).

## What the pilot is

- One client in the registry (`clients.json`), with its own ids and its own secrets ([MULTI-CLIENT.md](MULTI-CLIENT.md)).
- Reading tools only. `GEM_WRITE` is never set, `client enable-write` is never run, and `--write` is never passed to `install` or `setup`. Without `GEM_WRITE=1` every `*_apply` call is refused ([SECURITY.md](SECURITY.md)).
- The requests sent to the platforms are reading requests, checked against an allow-list before they leave the process.

## Access checklist

Ask the client for the lowest role that reads. The full table and the e-mail template for the client are in [SETUP.md](SETUP.md#access-checklist).

| Service | Ask the client for | Keep in mind |
|---|---|---|
| GA4 | Viewer on the property, for the Google account you will authorize | The numeric Property ID helps |
| Search Console | Access to the property (a restricted user is enough to read) | Domain property (`sc-domain:...`) or URL-prefix |
| Tag Manager | Read access to the container | `GTM-XXXXXXX` id |
| Merchant Center | Leave it out of a first pilot unless you need it | A client with a Merchant Center id gets a Google token that carries the `content` scope, which can modify products and feeds ([SECURITY.md](SECURITY.md#known-limits-read-this-before-turning-writing-on), limit 12) |
| Google Ads | Access to the account, or to its manager account, at a level that only reads | The `adwords` scope can write and cannot be limited: the client's role narrows what you can see, not what the token is allowed to do ([SETUP-ADS.md](SETUP-ADS.md)) |
| Meta Ads | A system user with the analyst role on the ad account, and a token with `ads_read` only | Never ask for `ads_management` in a reading pilot |
| ChatGPT Ads | A dedicated Advertiser API key | OpenAI does not confirm a key limited to reading |

Do not request the Indexing API scope (`--with-indexing`): it can submit URL notifications.

## Steps

Example client name: `pilot-acme`. Run these yourself, in a terminal.

1. Register the client. Give only the ids of the platforms in the pilot, and leave the Merchant Center id empty. Do not use `--sandbox` for a real client.

   ```bash
   google-ecommerce-mcp client add pilot-acme
   ```

2. Authorize Google for this client, without `--write` and without `--with-indexing`.

   ```bash
   google-ecommerce-mcp setup --client pilot-acme --client-secret client_secret.json
   ```

3. Only for the ad platforms of the pilot. The secrets are typed at hidden prompts, never in the chat.

   ```bash
   google-ecommerce-mcp setup-ads --client pilot-acme --client-secret client_secret.json
   google-ecommerce-mcp client set-secret pilot-acme meta
   google-ecommerce-mcp client set-secret pilot-acme openai
   ```

4. Check. Exit code 0 means every configured service works.

   ```bash
   google-ecommerce-mcp doctor --client pilot-acme --live
   ```

5. Add the server to Claude Desktop, pinned to this client so the process cannot be asked about another one. Answer no at the writing question, and never pass `--write`. Then quit Claude Desktop completely and reopen it. For Claude Code, use the command in [QUICKSTART.md](QUICKSTART.md#4-add-it-to-claude).

   ```bash
   google-ecommerce-mcp install --client pilot-acme --skip-auth --pin --name google-pilot-acme
   ```

## Check that writing is off

- `google-ecommerce-mcp client list` shows no writing family turned on for the client.
- Ask the assistant for `clients_list` and `client_profile`: no writing family is enabled.
- `audit_log` and `plan_list` show no applied plan for a new client (any entry only means someone tried).
- The server entry in your Claude config has no `GEM_WRITE`.
- If the assistant still asks for a change, the plan is refused (see [WRITE.md](WRITE.md#common-refusals-and-their-fix)). Keep the four apply tools on "ask each time" anyway.

## During the pilot

- Compare the first figures with each platform's own interface. Each platform counts conversions its own way and the sum is not deduplicated ([ADS.md](ADS.md#read-this-first-attribution)).
- If your OAuth consent screen is in *Testing*, Google tokens expire after 7 days: rerun step 2 ([TROUBLESHOOTING.md](TROUBLESHOOTING.md#authentication)).
- Everything a tool returns goes to your MCP client and its model provider ([SECURITY.md](SECURITY.md#known-limits-read-this-before-turning-writing-on), limit 10). Tell the client before you connect their accounts.

## How to revoke

1. Remove the client and its keyring entries. It needs a real terminal and asks you to type the name.

   ```bash
   google-ecommerce-mcp client remove pilot-acme
   ```

2. Remove the `google-pilot-acme` server entry from your MCP client config, then quit the client completely and reopen it. A running server keeps the credentials it has in memory until it restarts ([SECURITY.md](SECURITY.md#known-limits-read-this-before-turning-writing-on), limit 11).
3. Revoke at the source:
   - Google (token and Google Ads token): [Google account permissions](https://myaccount.google.com/permissions), remove the app.
   - Meta: revoke or regenerate the system user token in Business Settings.
   - ChatGPT Ads: revoke the key in Ads Manager.
4. Ask the client to remove your user from each product ([SETUP.md](SETUP.md#revoking-each-access)).
5. Check: `google-ecommerce-mcp client list` no longer shows the client.

Writing was never on in this pilot, so there is nothing to disable. If you want writing later, read [WRITE.md](WRITE.md) and [SECURITY.md](SECURITY.md) first, and try it on a test client.

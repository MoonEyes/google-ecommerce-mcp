# First client in 10 minutes

You will connect one client (a shop or a customer) so that Claude can read its data. Writing is optional and comes at the end. Four commands.

Want the list without reading this page? Run:

```bash
google-ecommerce-mcp quickstart            # or: quickstart --client acme
```

It prints 8 lines (nothing is written, no network): the four commands below for your client, then the two optional writing commands.

```text
First client in 4 commands (acme = your client's name; nothing is written here):
  1. google-ecommerce-mcp client add acme
  2. google-ecommerce-mcp setup --client acme --client-secret <client_secret.json>
  3. google-ecommerce-mcp doctor --client acme
  4. google-ecommerce-mcp install --client acme --skip-auth   (Claude Code: claude mcp add google-ecommerce --scope user -- uvx google-ecommerce-mcp)
Writing (optional, off by default, every plan still needs approval):
  5. google-ecommerce-mcp install --client acme --skip-auth --write
  6. google-ecommerce-mcp setup --client acme --write --client-secret <client_secret.json>
```

## Before you start

- Python 3.10+ with [uv](https://docs.astral.sh/uv/) (it provides `uvx`). The [installer in the README](../README.md#quick-install) installs it for you.
- A Google OAuth client file (`client_secret.json`, type **Desktop app**). Creating it takes about 10 minutes the first time: [SETUP-GOOGLE-CLOUD.md](SETUP-GOOGLE-CLOUD.md). You reuse it for every client.
- The ids of what you want to read: a GA4 property id, a Search Console property, a Merchant Center id... **All optional.** Skip what the client does not use.

The OAuth client file is checked before any browser opens. It is refused (nothing opened, nothing stored) unless its only root key is `installed` (Desktop app, never `web`), its `auth_uri` is on `https://accounts.google.com/`, its `token_uri` is exactly `https://oauth2.googleapis.com/token` and its redirects are local (`http://localhost`). A file downloaded from Google Cloud Console passes as is. If yours is refused, download it again (type Desktop app).

## 1. Create the client

```bash
google-ecommerce-mcp client add acme
```

It asks for a display name and the ids, one by one (Enter skips). It checks each format. **It never asks for a secret**, and writes only ids to `clients.json`. Use `--sandbox` if these are test accounts.

## 2. Authorize Google for this client

```bash
google-ecommerce-mcp setup --client acme --client-secret client_secret.json
```

Your browser opens the Google consent screen. The token is stored in your OS keyring, under this client only. Without `--client-secret`, a single `client_secret*.json` in the current folder or in Downloads is offered, and used only if you answer yes in a terminal.

Using Google Ads, Meta or ChatGPT Ads for this client? One more command each, only for those you need:

```bash
google-ecommerce-mcp setup-ads --client acme --client-secret client_secret.json   # Google Ads
google-ecommerce-mcp client set-secret acme meta                                  # Meta token, hidden prompt
google-ecommerce-mcp client set-secret acme openai                                # ChatGPT Ads key, hidden prompt
```

## 3. Check it works

```bash
google-ecommerce-mcp doctor --client acme
```

You get one line per service and, for anything wrong, the exact command to fix it. Add `--live` to make one small real request per service. Exit code 0 means every configured service works.

## 4. Add it to Claude

**Claude Desktop**:

```bash
google-ecommerce-mcp install --client acme --skip-auth
```

`--skip-auth` keeps the token of step 2. It backs up `claude_desktop_config.json`, then adds the server entry with a minimal `env` (no id: the ids come from the client). Add `--pin` to bind the entry to this client (`GEM_CLIENT=acme`). In a terminal it also offers writing for the Google properties; the answer defaults to no, so press Enter to stay with reading.

**Claude Code**:

```bash
claude mcp add google-ecommerce --scope user -- uvx google-ecommerce-mcp
```

Quit Claude Desktop completely and reopen it. Then ask: *"Which clients can you see?"* (the `clients_list` tool) and *"Give me last month's sessions by channel for acme."*

With one client you never need to say its name. With two or more, name the client in your question and the assistant passes it as `client_id`.

That is all for reading. Next: more clients ([MULTI-CLIENT.md](MULTI-CLIENT.md)), or the questions you can ask ([example prompts](../examples/PROMPTS.md)).

## Optional: let the assistant prepare changes

Reading is enough for most people. Writing is **off**, has **never been tested on a real account**, and every change still needs your approval. Start with a test client (`client add acme --sandbox`). Two commands, both in a real terminal:

```bash
google-ecommerce-mcp install --client acme --skip-auth --write
google-ecommerce-mcp setup --client acme --write --client-secret client_secret.json
```

**Line 5, `install --client acme --skip-auth --write`**, asks your consent again, one group at a time: Google properties (GA4, Tag Manager, Search Console, Merchant Center), then Google Ads, Meta (ads, pixel, catalog, Page) and ChatGPT Ads. Each group has its own question (default no) and you type the client name to confirm it. Only the groups you confirm are turned on. `install --client` is the only command that adds `GEM_WRITE=1` to the server entry of your Claude config, and only when you typed a consent in that same run (a policy turned on earlier is not a consent: it asks again); the entry is then pinned to the exact package version (`google-ecommerce-mcp==<version>`), so an update never turns on new write code silently. `--yes` never skips these questions: without a terminal, nothing is turned on and the command to run is printed.

**Line 6, `setup --client acme --write`**, creates a **separate** Google token that can edit GA4, Tag Manager, Search Console and Merchant Center. It needs an explicit `--client-secret <file>` (a file is never picked automatically for a write token) and a real terminal, and it asks you to confirm. Skip it if you only use Google Ads, Meta or ChatGPT Ads.

Restart Claude Desktop, and keep the four apply tools on "ask each time".

How a change goes from idea to the platform, with examples, the limits that protect you and what is not covered: [WRITE.md](WRITE.md) and [SECURITY.md](SECURITY.md).

## If something fails

- `doctor` prints the fix. Start there.
- `client_required`: two clients are registered; say which one.
- `not_authenticated`: rerun step 2.
- `OAuth client file ...: token_uri must be exactly ...` (or `auth_uri`, `root key must be "installed"`): the file is not a Google Desktop app client. Download it again from Google Cloud Console.
- Everything else: [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

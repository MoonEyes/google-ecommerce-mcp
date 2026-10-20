# Several clients, one server

Version 0.5.0 can serve several shops or clients (an agency, for example) from one install. Each client has its own account ids, its own secrets and its own writing rules. Nothing is shared between clients.

If you have one shop, you do not need any of this: the old environment variables still work (see [Legacy mode](#legacy-mode-no-registry)).

## The idea in four lines

- A small file, `clients.json`, lists your clients and their account ids. **It never holds a secret.**
- Secrets (tokens, keys) live in the OS keyring, one set per client.
- Every tool takes a `client_id`. The server then uses that client's ids and secrets, and only those.
- With two clients or more, `client_id` is mandatory. A wrong or foreign id is refused.

## Where the files are

| What | Windows | macOS / Linux | Override |
|---|---|---|---|
| Registry | `%APPDATA%\google-ecommerce-mcp\clients.json` | `~/.config/google-ecommerce-mcp/clients.json` (`$XDG_CONFIG_HOME` is honoured) | `GEM_CLIENTS_FILE` (full path) |
| State (plans, approvals, journal, fallback key) | `...\google-ecommerce-mcp\state\` | `.../google-ecommerce-mcp/state/` | `GEM_STATE_DIR` |

Files are created private: mode `600` on POSIX, and on Windows the server restricts the file to your account with `icacls` (and warns if it cannot). They are written atomically.

## The file

```json
{
  "version": 1,
  "clients": {
    "acme": {
      "label": "Acme SA",
      "sandbox": false,
      "accounts": {
        "ga4": {"property_id": "123456789"},
        "gsc": {"site_url": "sc-domain:acme.fr"},
        "merchant": {"account_id": "9876543210"},
        "gtm": {"account_id": "6000000001", "container_id": "200000001", "public_id": "GTM-ABC1234"},
        "google_ads": {"customer_id": "1234567890", "login_customer_id": "9990001112"},
        "meta": {"ad_account_id": "123456789012345", "pixel_id": "", "catalog_id": "",
                 "business_id": "", "page_id": "", "ig_user_id": ""},
        "openai_ads": {"account_id": "acct_123"},
        "pagespeed": {}
      },
      "secrets": {
        "google_read": "acme/google", "google_write": "acme/google-w", "google_ads": "acme/gads",
        "meta": "acme/meta", "openai_ads": "acme/openai"
      },
      "write": {
        "google_ads": {"enabled": false, "account_allow": ["1234567890"],
                       "max_daily_budget_micros": 50000000, "cap_currency": "EUR",
                       "max_increase_pct": 20, "max_ops_per_plan": 25,
                       "allow_activate": false, "allow_delete": false,
                       "allow_irreversible": false, "allow_pii": false, "token_ttl_s": 900}
      }
    }
  }
}
```

The schema is closed: an unknown field is refused, and a broken file, including one that repeats a key in the same JSON object, is **refused, not guessed** (`registry_invalid`; the server never falls back to the environment variables). Normally you do not edit it by hand: `client add` fills the ids with checks. You do edit it for the money limits (see [WRITE.md](WRITE.md#turn-writing-on-in-5-steps)).

| Part | Rules |
|---|---|
| Client name | 1 to 40 characters: `a-z`, `0-9`, `-`. Starts with a letter or a digit. Lower case |
| `label`, `sandbox` | A display name (80 characters, never a key), and `true` for test accounts only |
| `accounts` | GA4 `property_id` (digits); Search Console `site_url` (`sc-domain:example.com` or a URL ending in `/`); Merchant `account_id`; Tag Manager `account_id`, `container_id` (digits) and `public_id` (`GTM-XXXX`); Google Ads `customer_id` and `login_customer_id` (10 digits, dashes accepted and removed); Meta `ad_account_id` (`act_` accepted and removed), `pixel_id`, `catalog_id`, `business_id`, `page_id`, `ig_user_id`; ChatGPT Ads `account_id`; `pagespeed` (no id) |
| `secrets` | Names of keyring entries, in the form `<client>/<something>`. Never a value, never another client's entry. Optional: the defaults are `<client>/google`, `google-w`, `gads`, `meta`, `openai` |
| `write.<family>` | The writing rules of one family: `google_ads`, `meta`, `meta_eco`, `openai_ads`, `ga4`, `gsc`, `merchant`, `gtm`. Defaults: everything off. See [WRITE.md](WRITE.md) |

A Google Ads manager (`login_customer_id`) and a Meta `business_id` can be shared by several clients, so they are stored but **never count as proof that an account belongs to a client**.

## Managing clients

```bash
google-ecommerce-mcp client add acme          # guided questions; every id is optional; no secret is asked
google-ecommerce-mcp client list
google-ecommerce-mcp client show acme         # ids masked, which secrets are stored (never their values)
google-ecommerce-mcp client add acme --replace   # change ids (write settings are kept)
google-ecommerce-mcp client remove acme       # asks you to type the name; also deletes its keyring entries
google-ecommerce-mcp client set-secret acme meta      # hidden prompt: meta, openai, meta-capi, pagespeed
google-ecommerce-mcp client enable-write acme google_ads   # see WRITE.md
google-ecommerce-mcp client disable-write acme google_ads
```

`client add --yes` asks nothing and takes one flag per id (for scripts). `remove`, `enable-write`, `set-secret` and `approve` need a real terminal. The prompts are in French or English from your system language (`GEM_LANG=fr|en` forces one).

The assistant sees the registry through three tools: `clients_list` (ids, labels, which platforms and which writing families are on, no account id), `client_profile` (one client's account ids in clear and its write policy; `client show` and `doctor` mask the ids) and `client_check` (token state and which secrets are present).

## Secrets, per client

Secrets are in the OS keyring (service `google-ecommerce-mcp`), under the user name `<client>/<kind>`:

| Kind | What it is | How it is stored |
|---|---|---|
| `google` | Google read token (OAuth) | `google-ecommerce-mcp setup --client <client> --client-secret client_secret.json` |
| `google-w` | Google **write** token (GA4, Tag Manager, Search Console, Merchant) | `setup --client <client> --write --client-secret client_secret.json` (explicit file, real terminal) |
| `gads` | Google Ads token (scope `adwords`) | `setup-ads --client <client> --client-secret client_secret.json` |
| `meta` | Meta access token | `client set-secret <client> meta` (hidden prompt) |
| `openai` | ChatGPT Ads API key | `client set-secret <client> openai` (hidden prompt) |
| `meta-capi`, `pagespeed` | Reserved / PageSpeed key | `client set-secret <client> <kind>` |

Instead of the keyring you can set `GEM_SECRET_<CLIENT>_<KIND>` in the environment, for example `GEM_SECRET_ACME_META` (upper case, `-` becomes `_`). That is meant for CI.

The old variables (`META_ACCESS_TOKEN`, `OPENAI_ADS_API_KEY`, `GOOGLE_TOKEN_FILE`...) are **not read for registered clients**: one variable cannot feed two clients. One exception, for zero migration: a client named `default` may point at the old keyring entries (`oauth-token`, `oauth-token-ads`, `meta-access-token`, `openai-ads-key`).

## Choosing the client

| Situation | What happens |
|---|---|
| No registry file | One implicit client `default`, built from the environment variables (legacy mode) |
| Registry, one client | That client is the default; `client_id` can be left out |
| Registry, two clients or more | `client_id` is mandatory on each call; without it: `client_required` |
| `GEM_CLIENT=acme` in the server environment | The whole process is **pinned** to `acme`. Another `client_id` is refused (`pinned_client`); `clients_list` shows only `acme` |

Once a registry exists, the id variables (`GA4_PROPERTY_ID`, `GSC_SITE_URL`, `MERCHANT_ACCOUNT_ID`, `GTM_CONTAINER_ID`, `GADS_CUSTOMER_ID`, `GADS_LOGIN_CUSTOMER_ID`, `META_AD_ACCOUNT_ID`) are **ignored**. Never mixed.

### Pinning, for isolation by process

For a client you want fully walled off, start a separate server entry pinned to it. It then cannot be asked about any other client:

```json
{
  "mcpServers": {
    "google-acme":  {"command": "uvx", "args": ["google-ecommerce-mcp"], "env": {"GEM_CLIENT": "acme"}},
    "google-beta":  {"command": "uvx", "args": ["google-ecommerce-mcp"], "env": {"GEM_CLIENT": "beta"}}
  }
}
```

Writing needs `GEM_WRITE=1` in the `env` of the entries where you want it, and only there.

## What keeps clients apart

- Each tool call builds an immutable `ClientContext` for one client. No global is changed between calls.
- An account id given to a tool (`property_id`, `customer_id`, `account_id`...) must belong to that client. Otherwise: `id_not_owned`, with the id masked in the message. In legacy mode any well-formed id is accepted, as before.
- Tokens, caches, plans, approvals and journals are all keyed by client. A confirmation token is bound to its client and platform.
- A test sends requests for two clients with a fake session and checks that each request carries only the ids and the credential of one client.
- Every result carries its `client_id`.

What it does not do: isolate clients from **you** or from a process running as you. They share your machine, your keyring and your registry file. Pin a process (above) if you need more.

## Errors

| `error` | Meaning | Fix |
|---|---|---|
| `client_required` | Several clients and no `client_id` | Pass `client_id`, or pin the server with `GEM_CLIENT` |
| `unknown_client` | The name is not in the registry (or there is no registry) | `client list`, then `client add <name>` |
| `platform_not_configured` | The client has no id for this platform | `client add <name> --replace` and give the id |
| `id_not_owned` | The id given does not belong to this client | Use the client's own ids (`client show <name>`) |
| `pinned_client` | The process is pinned to another client | Omit `client_id`, or use the server entry pinned to that client |
| `registry_invalid` | `clients.json` cannot be read, is not valid JSON, repeats a key in one JSON object (the message names the key), or breaks the schema | Fix or remove the entry or the repeated key named in the message, or delete the file to go back to legacy mode |

## Legacy mode (no registry)

If `clients.json` does not exist, nothing changes from 0.4.x: the ids come from the environment variables of your MCP client config (`GA4_PROPERTY_ID`, `GSC_SITE_URL`, `MERCHANT_ACCOUNT_ID`, `GTM_CONTAINER_ID`, `GADS_CUSTOMER_ID`, `GADS_LOGIN_CUSTOMER_ID`, `META_AD_ACCOUNT_ID`...) and the secrets from the existing keyring entries. There is one implicit client, `default`. Writing is **never** available in legacy mode, because the write policy lives in the registry: create a client first.

Creating the first client makes the registry authoritative: the id variables are then ignored. `client add` says so when it creates the file. To go back, delete the file.

## Example: an agency with three clients

```bash
google-ecommerce-mcp client add acme  --label "Acme SA"          # online shop, Google + Meta
google-ecommerce-mcp client add beta  --label "Beta Studio"       # Merchant Center + Google Ads
google-ecommerce-mcp client add gamma --label "Gamma (test)" --sandbox   # test accounts, for trying writes
google-ecommerce-mcp setup --client acme  --client-secret client_secret.json
google-ecommerce-mcp setup --client beta  --client-secret client_secret.json
google-ecommerce-mcp client set-secret acme meta
google-ecommerce-mcp doctor
```

Then in Claude: *"For acme, give me last month's sessions and purchases from GA4 and the Meta spend."* The assistant calls `ga4_report(client_id="acme", ...)` and `meta_insights(client_id="acme", ...)`. It cannot reach `beta`'s accounts with `acme`'s credentials, and a `property_id` that belongs to `beta` is refused for `acme`.

Turn writing on for the test client only, first:

```bash
google-ecommerce-mcp client enable-write gamma google_ads
```

and set `GEM_WRITE=1` only in a server entry pinned to `gamma` (`GEM_CLIENT=gamma`) until you trust the setup. For Claude Desktop, `google-ecommerce-mcp install --client gamma --pin --name google-gamma --skip-auth --write` does both: it asks your consent group by group (Google properties, Google Ads, Meta, ChatGPT Ads, typed client name each time), then writes an entry with `GEM_CLIENT=gamma`, `GEM_WRITE=1` and the package pinned to its exact version. `--yes` never answers these questions. See [WRITE.md](WRITE.md#the-short-path-one-command-group-by-group).

To start with reading only on one real client, see [PILOT.md](PILOT.md).

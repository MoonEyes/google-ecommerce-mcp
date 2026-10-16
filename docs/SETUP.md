# Setup: install one service at a time

The single entry point for installing and checking the server. You do not need everything: install one service, check it, then add the next. Every service is optional, and a tool for a service you did not set up answers `not_configured`.

> The paid media connectors are tested offline only (see [ADS.md](ADS.md#what-has-not-been-tested-against-real-accounts)). The commands below match `install --help` and `doctor --help`.

## Contents

- [Which services do you need?](#which-services-do-you-need)
- [Access checklist](#access-checklist)
- [E-mail template for your client](#e-mail-template-for-your-client)
- [Recommended order, with a check after each step](#recommended-order-with-a-check-after-each-step)
- [When it blocks](#when-it-blocks)
- [Security rules](#security-rules)
- [Revoking each access](#revoking-each-access)

## Which services do you need?

| You want to know... | Install | Needs |
|---|---|---|
| Where visits and sales come from | GA4 | read access to the GA4 property |
| Which searches bring organic visits, what is indexed | Search Console | read access to the property |
| Which products are refused in Google Shopping | Merchant Center | access to the Merchant Center account |
| Whether tracking is double-counted | Tag Manager (+ GA4 realtime) | read access to the container |
| How fast the pages are | PageSpeed | nothing (an API key helps) |
| What Google Ads spent and returned | Google Ads | read access to the account, or to its manager account |
| What Meta (Facebook / Instagram) ads spent and returned | Meta Ads | analyst role on the ad account |
| What ChatGPT Ads spent and returned | ChatGPT Ads | an Advertiser API key for the account |

Start with GA4 and Search Console: they answer most questions and ask for read-only access only. Add Merchant Center and Tag Manager next. Add the ad platforms last, only for those the shop actually advertises on. Several ad platforms side by side (`ads_overview`) need each one set up first.

## Access checklist

What to ask the client for. Ask for the **lowest role that reads**.

| Service | Ask for | Notes |
|---|---|---|
| GA4 | Viewer (read) access on the property, for the Google account you will authorize | The numeric Property ID helps |
| Search Console | Access to the property (a restricted user is enough to read) | Domain property (`sc-domain:...`) or URL-prefix |
| Merchant Center | Access to the account. Google has no read-only OAuth scope for the Merchant API, so the token you create can write even though this server never does. See [SECURITY.md](SECURITY.md) | Account id; the Cloud project must be registered with Merchant Center |
| Tag Manager | Read access to the container | `GTM-XXXXXXX` id |
| Google Ads | Read-only access to the account, or to the manager (MCC) account that holds it | Customer id; manager id if applicable. The Google Ads scope **can write and cannot be limited**; the server only reads, but the token is stored apart |
| Meta Ads | Business Manager access: a **system user** with an analyst / read role on the ad account, and a token with `ads_read` only | Ad account id (`act_...`) |
| ChatGPT Ads | An **Advertiser API key**, one per ad account, created in Ads Manager | OpenAI does not confirm a read-only key: use a dedicated one |

## E-mail template for your client

Short, no jargon. Replace the brackets.

> Subject: Access needed to prepare your [month] report
>
> Hello [first name],
>
> To prepare your report I need read access to your accounts. I will not be able to change anything in them. Could you please do the following?
>
> 1. **Google Analytics**: add [your Google account e-mail] as a *Viewer* of your property.
> 2. **Google Search Console**: add [your Google account e-mail] as a user of your site.
> 3. **Google Merchant Center** (if you sell on Google Shopping): add the same address as a user.
> 4. **Google Ads** (if you advertise there): give [your e-mail] read-only access to the account.
> 5. **Meta ads** (if you advertise on Facebook or Instagram): in Business Settings, add me as a partner or create a user with the *Analyst* role on the ad account.
> 6. **ChatGPT Ads** (if you advertise there): create an API key in Ads Manager and send it to me **by your password manager's secure sharing, never by plain e-mail or chat**.
>
> If one of these is not possible or you are not sure, tell me which and I will guide you.
>
> Thank you, [your name]

Never ask a client to send a password. Never accept a token or key pasted in an e-mail or in the chat with the assistant (see [Security rules](#security-rules)).

## Recommended order, with a check after each step

After each step, run the check for that service. It shows one row per service (Service, Set up, Secret, Token, Status, Account, with ids masked as `123***890`), then a "To fix" list with the exact command for each problem, a "Not set up" list, a `Result:` line and a `Next command:`.

```bash
google-ecommerce-mcp doctor --only <service> --live
```

`<service>` is one of `ga4`, `gsc`, `merchant`, `gtm`, `pagespeed`, `gads`, `meta`, `openai` (comma-separated for several). `doctor` reads this shell's environment and the server entry of your Claude Desktop config (`--config <file>` and `--name <server name>` point it at another one). Without `--live` it checks offline (settings, secrets, stored tokens). `--live` adds one minimal read call per service that passed the offline check (the same tools as the assistant uses: `ga4_properties`, `gsc_sitemaps`, `merchant_data_sources`, `gtm_inventory`, `gads_accounts`, `meta_ad_accounts`, `openai_ads_account`). PageSpeed is not tested live (shown as skipped: it needs a page address and about 30 seconds); ask the assistant to run `pagespeed`. Exit code: `0` all fine, `1` a configured service fails, `2` nothing is configured. `--json` prints the same result without any secret.

An excerpt of the offline output on a machine with nothing configured:

```text
Service         Set up  Secret  Token          Status      Account
------------------------------------------------------------------
Google Ads      no      no      -              not set up  -
Meta Ads        no      no      -              not set up  -
ChatGPT Ads     no      no      -              not set up  -

Result: 0 OK, 0 to fix, 7 not set up.
Nothing is set up yet. Start with one service: google-ecommerce-mcp install --yes --client-secret <client_secret.json> --ga4 <property id>
```

1. **Google Cloud and the OAuth client.** Follow [SETUP-GOOGLE-CLOUD.md](SETUP-GOOGLE-CLOUD.md) (about 10 minutes, once). Download the client JSON.
2. **Install and authorize Google.** Run the installer (see the README, [Quick install](https://github.com/MoonEyes/google-ecommerce-mcp#quick-install): download, read, compare SHA-256, run), or `google-ecommerce-mcp install` if already installed. Give the ids you have; skip the others.
3. **GA4:** `google-ecommerce-mcp doctor --only ga4 --live`.
4. **Search Console:** `google-ecommerce-mcp doctor --only gsc --live`.
5. **Merchant Center, Tag Manager, PageSpeed:** one `doctor --only ... --live` each.
6. **Paid media (optional step of `install`).** Interactive `install` asks "Set up paid media now?" after the Google questions; any paid media flag also starts it; `--skip-ads` leaves it out. Non-secret options: `--gads-customer-id`, `--gads-login-customer-id`, `--meta-account-id`; `--corporate-proxy` sets `ADS_TRUST_ENV=1` for a company proxy. Secrets are never arguments:
   - **Google Ads:** `install --yes --client-secret client_secret.json --gads-customer-id <customer id>` opens the Google consent for the Google Ads scope alone (same OAuth client file, separate token), or run `google-ecommerce-mcp setup-ads --client-secret client_secret.json` yourself. Then `doctor --only gads --live`.
   - **Meta:** `google-ecommerce-mcp set-secret meta-access-token` (hidden prompt, OS keyring), then add the ad account id with `google-ecommerce-mcp install --yes --skip-auth --meta-account-id <ad account id>`. Then `doctor --only meta --live`.
   - **ChatGPT Ads:** there is no id to give, so `--yes` skips it. Either run `google-ecommerce-mcp install --skip-auth` (answer `y` to the ChatGPT Ads question, then paste the key at the hidden prompt) or `google-ecommerce-mcp set-secret openai-ads-key`. Then `doctor --only openai --live`.
7. **Restart Claude Desktop completely**, then run `google-ecommerce-mcp doctor --live` for the whole picture.

`install` writes only non-secret identifiers to the Claude Desktop config. Before anything is written it makes a time-stamped backup (to the microsecond). It can be run again safely: a rerun keeps the variables already in the entry and adds new ones. If an existing value would change, it stops with exit code `3` and changes nothing; `--force` replaces that value (it never deletes a variable).

All checks run **before** any OAuth consent or secret prompt: the config is readable, there are no conflicting values, and the Google and Google Ads token files are different. So a refusal never leaves you half-authorized. Exit codes: `0` done, `2` refused before any authorization (for example the same token file for Google and Google Ads), `3` conflict or config problem, `130` you pressed Ctrl-C (it says what was already done). A config that is malformed, larger than 5 MB, in UTF-16 or read-only gives a clear message, never a traceback. Ids are checked: a Google Ads customer id has exactly 10 digits (dashes allowed), a Meta ad account id 3 to 20 digits.

`--corporate-proxy` alone is enough to write `ADS_TRUST_ENV=1` into an existing entry (`install --yes --skip-auth --corporate-proxy`).

The PageSpeed key is read from `--pagespeed-key` (not recommended: it stays on the command line and in your shell history), otherwise from the `PAGESPEED_API_KEY` environment variable of the terminal, otherwise from a hidden prompt when interactive. The guide step is `google-ecommerce-mcp install --skip-auth --skip-ads`.

## When it blocks

Look up the status or error code, then follow the entry.

| You see | Go to |
|---|---|
| `doctor` says a service is not configured, secret missing, token invalid or expired | [TROUBLESHOOTING.md, "doctor says..."](TROUBLESHOOTING.md#doctor-says) |
| `not_authenticated`, `invalid_grant`, `missing_scope`, `api_disabled` | [TROUBLESHOOTING.md, Authentication](TROUBLESHOOTING.md#authentication) |
| `not_configured`, `auth_failed`, `rate_limited`, `not_available`, `blocked`, `network`, `invalid_*` on an ads tool | [TROUBLESHOOTING.md, Paid media errors](TROUBLESHOOTING.md#paid-media-errors) |
| Installer or Claude Desktop config problems | [TROUBLESHOOTING.md, Installer](TROUBLESHOOTING.md#installer) |
| Behind a company proxy | set `ADS_TRUST_ENV=1` for the ad platforms ([ADS.md](ADS.md#corporate-proxy)) |

If you are stuck, open an issue with the output of `doctor --json` (it contains no secret).

## Security rules

- **Never paste a password, token, API key or client secret into the chat with the assistant.** Secrets are typed locally, at the hidden prompt of `set-secret` or in the browser consent of `setup` / `setup-ads`. An assistant that asks you to paste one is wrong; refuse.
- Secrets are never command-line arguments (they would stay in your shell history).
- The Claude Desktop config receives identifiers only, never a secret.
- Never run `curl ... | sh` or `irm ... | iex` without downloading, reading and comparing the SHA-256 (README, "Quick install").
- Back up the Claude Desktop config before changing it (the installer does).
- Use the lowest role that reads. Details and remaining risks: [SECURITY.md](SECURITY.md).

## Revoking each access

| Access | How to revoke | Then delete the local copy |
|---|---|---|
| Google (GA4, Search Console, Merchant, Tag Manager, Indexing) | [Google account permissions](https://myaccount.google.com/permissions), or delete the OAuth client in Cloud Console | `keyring del google-ecommerce-mcp oauth-token` or the token file |
| Google Ads | Same page; remove the app | `keyring del google-ecommerce-mcp oauth-token-ads` or the `GADS_TOKEN_FILE` file |
| Meta | Revoke or regenerate the system user token in Business Settings; remove the system user if no longer needed | `keyring del google-ecommerce-mcp meta-access-token` |
| ChatGPT Ads | Revoke the API key in Ads Manager | `keyring del google-ecommerce-mcp openai-ads-key` |
| Client-side grants | The client removes you as a user in each product | n/a |

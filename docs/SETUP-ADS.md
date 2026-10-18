# Easy setup guide: Google Ads, Meta Ads, ChatGPT Ads

Connect your ad platforms one at a time, with nothing to look up elsewhere. Written for an agency or a shop manager who is not technical. Author: MoonEyes.

> **Honest status: tested offline only.** The three connectors were checked against fake responses written from each platform's documentation. They have **not been run on real Google Ads, Meta or ChatGPT Ads accounts yet**. Compare your first figures with each platform's own screen. Anything this guide could not confirm is marked **(not confirmed)**; a screen name that may differ is marked **(label may differ)**. By default the connectors only read reports. They can prepare changes (pause, budget, new paused campaign) only after you turn writing on for a client, see [WRITE.md](WRITE.md); no write has been tried on a real account.

Official pages read on 17/10/2026 (consultation date) are listed at the [end](#sources-read-on-17102026).

**Several clients?** This guide shows the single-account commands. With a client registry, add `--client <name>` to each command and use `google-ecommerce-mcp client set-secret <name> meta|openai` for the Meta token and the ChatGPT Ads key: see [MULTI-CLIENT.md](MULTI-CLIENT.md) and [QUICKSTART.md](QUICKSTART.md).

## 0. At a glance

| Platform | What you need | Time | Who can do it | Difficulty | What can block |
|---|---|---|---|---|---|
| **Google Ads** | Customer id (10 digits), a Google Cloud OAuth client file, one browser consent | about 15 min (10 more if the Cloud project is new) | Both: the client gives access, you authorize | Medium | Client has not granted access; Google Ads API not enabled; consent screen left in *Testing* (token dies after 7 days); Cloud project access level (not confirmed for your case) |
| **Meta Ads** | Ad account id, a system user with a read role, a token with `ads_read` only | about 20 min | Both: the client (or admin) creates the token, you store it | Medium to hard | Business Manager admin rights needed; Meta app creation or review requirements (not tested); token pasted in the wrong place |
| **ChatGPT Ads** | One Advertiser API key for the ad account | about 5 min once you hold the key | The client, or you if you own the ad account | Easy | Where to create the key in Ads Manager is not documented in what we read (not confirmed); the key may not be limited to reading |

## 1. Before you start

1. **One service at a time.** Recommended order: Google Ads, then Meta Ads, then ChatGPT Ads (only the ones the shop really advertises on). Finish and check one before starting the next.
2. **`doctor` is your compass.** It tells you what is set up and prints the exact next command:

   ```bash
   google-ecommerce-mcp doctor
   ```

3. **Golden rule: never paste a password, token or key into the chat with the assistant.** Secrets are typed only at the hidden prompt on your own computer, or in the browser consent screen. If you pasted one by mistake, revoke it (see each "How to revoke").
4. **Keep your `client_secret...json` file** outside any git repository, outside OneDrive (or any synced folder) and outside your notes vault.
5. **Install first.** The server must already be installed (README, "Quick install": download, read, compare the SHA-256, run). This guide starts after that.

Three words used below:

- **MCC (manager account)**: a Google Ads account that manages other Google Ads accounts. Agencies usually have one.
- **Business Manager** (also called Business Portfolio): Meta's console where a company's ad accounts, people and system users live.
- **GAQL**: Google's query language for Google Ads reports. The assistant writes it for you.

An example of real `doctor` output on a machine with nothing configured (run with an empty home folder, no real keyring read):

```text
google-ecommerce-mcp doctor (offline check; add --live to test each service)

Service         Set up  Secret  Token          Status      Account
------------------------------------------------------------------
Google Ads      no      no      -              not set up  -
Meta Ads        no      no      -              not set up  -
ChatGPT Ads     no      no      -              not set up  -

Not set up (optional, one service at a time):
  - Google Ads: google-ecommerce-mcp install --yes --client-secret <client_secret.json> --gads-customer-id <customer id>
  - Meta Ads: google-ecommerce-mcp set-secret meta-access-token
  - ChatGPT Ads: google-ecommerce-mcp install --skip-auth

Result: 0 OK, 0 to fix, 3 not set up.
```

Exit code: `0` all fine, `1` a configured service needs fixing, `2` nothing configured. Ids are masked (`123***890`) and no secret is ever printed.

---

## 2. Google Ads

### What you need

- A Google Ads customer id (10 digits) and read access to that account.
- A Google Cloud project with the **Google Ads API** turned on.
- An OAuth client of type **Desktop app**, downloaded as a JSON file. Steps in [SETUP-GOOGLE-CLOUD.md](SETUP-GOOGLE-CLOUD.md) (about 10 minutes, once).
- The manager id too, only if you reach the account through an MCC.
- No developer token: Google ended them on 9 September 2026 (see below). Check the access level of your Google Cloud project instead.

### Ask your client for access

> Subject: Read access to your Google Ads account
>
> Hello [first name],
> To prepare your report I need to read your Google Ads figures. I will not change anything. Could you please invite [your Google account e-mail] to your Google Ads account with the lowest access level that can view reports (read-only, *label may differ*)? If you manage the account through a manager account, send me the 10-digit id of the account and of the manager.
> Thank you, [your name]

### Steps

1. **Note the customer id.** Open Google Ads: the ten digits at the top right (`123-456-7890`). Expected: you have 10 digits.
2. **Turn on the Google Ads API** in your Cloud project: APIs & Services > Library > *Google Ads API*. Expected: it shows Enabled.
3. **Keep your `client_secret.json`** somewhere safe (see the golden rule). Expected: you know its full path.
4. **Authorize and register the account:**

   ```bash
   google-ecommerce-mcp install --yes --client-secret <client_secret.json> --gads-customer-id <customer id>
   ```

   Expected: your browser opens a Google consent screen asking for Google Ads only; the summary then lists Google Ads under Configured. (Alternative: `google-ecommerce-mcp setup-ads --client-secret <client_secret.json>` for the consent alone, `--token-file <file>` to store the token in a file.)
5. **Only if you reach the account through an MCC**, add the manager id:

   ```bash
   google-ecommerce-mcp install --yes --skip-auth --gads-login-customer-id <manager id>
   ```

   Expected: the summary lists Google Ads under Configured.
6. **Check it works** (next section).

### Where to find each id

| Id | Looks like (fake example) | Where |
|---|---|---|
| Customer id | `123-456-7890` (dashes accepted) | Top right of Google Ads |
| Manager id (`login-customer-id`) | `987-654-3210` | Top right when signed in to the manager account |
| Developer token | does not exist any more | Not needed since 9 September 2026. If you still set `GADS_DEVELOPER_TOKEN` it is sent and ignored by Google |

Google's page on call structure says the manager id must be sent without dashes when access goes through a manager account; this server removes dashes for you.

### Check it works

```bash
google-ecommerce-mcp doctor --only gads --live
```

The row must read: Google Ads, Set up `yes`, Secret `yes`, Token `ok`, Live `ok`, Status `OK`, Account `customer 123***890`. (The offline run above shows the empty layout; a configured run was not available here.)

### If it fails

| What you see | Cause | Fix |
|---|---|---|
| Token `missing` | No Google Ads token stored (it is separate from the Google token) | `google-ecommerce-mcp setup-ads --client-secret <client_secret.json>` |
| Token `missing scope` or `not separate` | The stored token lacks Google Ads, or mixes other permissions | Rerun `setup-ads`; do not reuse the file named in `GOOGLE_TOKEN_FILE` |
| "must be 10 digits" | Wrong id format | `google-ecommerce-mcp install --yes --skip-auth --force --gads-customer-id <customer id>` |
| Live `auth_failed` | Token expired, revoked, or no access to this customer | Rerun `setup-ads`. If it repeats every 7 days, set the OAuth consent screen to *In production* |
| Live `not_visible` | The account is reached through an MCC | Add the manager id (step 5) |
| Live `not_available` | Google Ads API not enabled, wrong id or API version | Enable it in Cloud Console, wait a few minutes, retest |
| Live `rate_limited` | Too many requests | Wait the seconds shown, retest |
| Live `network` | No connection, or company proxy | `google-ecommerce-mcp install --yes --skip-auth --corporate-proxy` |

More: [TROUBLESHOOTING.md](TROUBLESHOOTING.md#doctor-says).

### What this access can and cannot do

- The consent asks for the `adwords` scope. **That scope can write, and Google offers no read-only version.** This server only sends read requests unless you turn writing on for the client (the reads are checked by an allow-list in the code), and the same token is used for both, so whoever steals the token could change the account. That is why it is stored in its own keyring entry (`oauth-token-ads`), apart from your other Google token. Read-only access given by your client in Google Ads narrows what the signed-in user can see (the client's choice), but the scope itself stays broad.
- No developer token: Google's page states developer tokens were sunset on 9 September 2026; sending one is ignored, and the access level now belongs to the Google Cloud project, with Test access as the default for a project without a transferred level. A call refused for that reason comes back as `project_not_approved`: ask Google for a higher level for the project, or use a test account. What your own project's level allows for real accounts: **(not confirmed)**.
- Writing: if you turn it on for this client ([WRITE.md](WRITE.md)), the same token can prepare pauses, daily budget changes, negative keywords and one new paused Search campaign. Nothing is activated or deleted, every change is a plan you approve, and it has not been tried on a real account.

### How to revoke

1. Remove the app at [Google account permissions](https://myaccount.google.com/permissions).
2. Delete the local copy: `uvx keyring del google-ecommerce-mcp oauth-token-ads` (or delete the file you set in `GADS_TOKEN_FILE`).
3. Your client can also remove you from their Google Ads account.

---

## 3. Meta Ads (Facebook and Instagram)

### What you need

- The ad account id (digits, `act_` accepted).
- A Meta Business Manager that has the ad account.
- A **system user** (a robot login owned by the business, so the token does not depend on one person) with a read or analyst role on that ad account.
- A token with the **`ads_read`** permission only.
- A Meta app of type Business with the Marketing API product (Meta may add review steps; **not tested**).

### Ask your client for access

> Subject: Read access to your Meta ad account
>
> Hello [first name],
> To prepare your report I need to read your Facebook and Instagram ad figures. I will not change anything. In Business Settings, could you please (1) add a system user, or add me as a partner, with only the *Analyst* (read) role on your ad account (*label may differ*), and (2) send me the ad account id? Please do not send any token by e-mail or chat; we will agree on a secure way.
> Thank you, [your name]

### Steps

1. **Get a Business-type Meta app** with the Marketing API product at developers.facebook.com. Expected: an app exists in your developer account (screens and extra review: **not confirmed**).
2. **Create a system user** in Business Settings (Users > System users, *label may differ*). Expected: it is listed.
3. **Give it a read role** on the ad account, nothing more. Expected: the ad account shows the system user with an analyst or read task (*label may differ*).
4. **Generate a token for that system user and your app, tick `ads_read` only** (never `ads_management`). Expected: you hold a long token starting with `EAA`. Do not paste it anywhere.
5. **Store it in the keyring:**

   ```bash
   google-ecommerce-mcp set-secret meta-access-token
   ```

   Expected: a hidden prompt; then the message "Stored in operating system keyring".
6. **Register the ad account id:**

   ```bash
   google-ecommerce-mcp install --yes --skip-auth --meta-account-id <ad account id>
   ```

   Expected: the summary lists Meta Ads under Configured.
7. **Check it works** (next section).

### Where to find each id

| Id | Looks like (fake example) | Where |
|---|---|---|
| Ad account id | `1234567890123` or `act_1234567890123` | Ads Manager: `act=` in the address bar; or Business Settings > Accounts > Ad accounts (*label may differ*) |
| Token | `EAA...` (long) | Shown once when generated; type it only at the hidden prompt |

### Check it works

```bash
google-ecommerce-mcp doctor --only meta --live
```

The row must read: Meta Ads, Set up `yes`, Secret `yes`, Token `ok`, Live `ok`, Status `OK`, Account `ad account 123***123`. (Not run on a real account: **not confirmed**.)

### If it fails

| What you see | Cause | Fix |
|---|---|---|
| Secret `no` / "no Meta token is stored" | Token not stored | `google-ecommerce-mcp set-secret meta-access-token` |
| "no ad account id is set" | Token stored, id missing | Step 6 command |
| "must be digits" | Wrong id format | Step 6 command with `--force` added |
| Live `auth_failed` | Token invalid, expired or revoked (Graph codes 102, 190, 463, 467) | Create a new token, run `set-secret meta-access-token`. If `META_ACCESS_TOKEN` is set in the config, it wins over the keyring: remove it |
| Live `missing_scope` | Token lacks `ads_read` or the ad account | Fix the role in Business Settings, create a new token, store it |
| Live `not_visible` | The token cannot see this ad account | Give the system user the ad account, or fix the id |
| Live `not_available` | Wrong id or API version | Check the ad account id and `META_API_VERSION`, retest |
| Live `rate_limited` | Meta quota | Wait, retest |

### What this access can and cannot do

- `ads_read` lets an app read ad reports for accounts it owns or was granted. `ads_management` would also manage ads; do not ask for it unless you turn writing on for this client ([WRITE.md](WRITE.md)), where it is required. Meta documents that a Business app gets standard access automatically and needs App Review for advanced access when it serves other people's accounts. Which one you need for a client account: **(not confirmed)**.
- A system user token can be long-lived: treat it like a password.

### How to revoke

1. In Business Settings, revoke or regenerate the system user's token, or remove the system user (*label may differ*; see the [system users page](https://developers.facebook.com/docs/marketing-api/system-users)).
2. Delete the local copy: `uvx keyring del google-ecommerce-mcp meta-access-token`.
3. Your client can remove the system user's access to the ad account.

---

## 4. ChatGPT Ads

### What you need

- A ChatGPT Ads advertiser account (ad account).
- One **Advertiser API key** for that account. OpenAI documents one key per ad account: several accounts need several keys.
- Nothing else: no id is requested.

### Ask your client for access

> Subject: A key to read your ChatGPT Ads figures
>
> Hello [first name],
> To prepare your report I need an API key for your ChatGPT Ads account, used only to read reports. Could you please create a dedicated key for me in Ads Manager (*label may differ*) and send it through your password manager's secure sharing, never by plain e-mail or chat? Please tell me if the key can be limited to reading. You can delete it any time.
> Thank you, [your name]

### Steps

1. **Obtain the key** in Ads Manager (the menu that creates it is **not confirmed**: OpenAI's authentication and reporting pages say how to use the key but not where to create it). Expected: you hold a key. Do not paste it anywhere.
2. **Store it and register the server:**

   ```bash
   google-ecommerce-mcp install --skip-auth
   ```

   Press Enter at the Google questions, answer `y` to ChatGPT Ads, paste the key at the hidden prompt. Expected: the summary lists ChatGPT Ads under Configured. (Shortcut for the key alone: `google-ecommerce-mcp set-secret openai-ads-key`.)
3. **Check it works** (next section).

### Where to find each id

| Id | Looks like (fake example) | Where |
|---|---|---|
| Ad account id | shown by the tool, not requested | The assistant's `openai_ads_account` answer |
| API key | secret, never shown again | Created in Ads Manager (menu not confirmed) |

### Check it works

```bash
google-ecommerce-mcp doctor --only openai --live
```

The row must read: ChatGPT Ads, Set up `yes`, Secret `yes`, Token `ok`, Live `ok`, Status `OK`. Behind it, the check is a read of the ad account: OpenAI's documentation names `GET /ad_account` as the test call. (Not run on a real account: **not confirmed**.)

### If it fails

| What you see | Cause | Fix |
|---|---|---|
| Secret `no` | Key not stored | `google-ecommerce-mcp set-secret openai-ads-key` |
| Live `auth_failed` | Wrong or revoked key | Create a new key, store it again. If `OPENAI_ADS_API_KEY` is set in the config, it wins over the keyring |
| Live `missing_scope` | The key cannot read this account | Create a key with read access, store it |
| Live `not_available` | The ChatGPT Ads API is not available to this account yet | Retest later; ask OpenAI support |
| Live `rate_limited` / `network` | Quota or connection | Wait; behind a proxy run `install --yes --skip-auth --corporate-proxy` |

### What this access can and cannot do

- **Whether a key can be limited to reading is not confirmed.** Assume it can change campaigns and budgets. This server only sends the read requests on its allow-list unless you turn writing on for the client, and the same key is used for both. A leaked key is not limited by that. Use a dedicated key and keep it out of shared configs.
- The host is fixed in the code (`api.ads.openai.com`): the key cannot be sent anywhere else.

### How to revoke

1. Revoke the key where you created it in Ads Manager (menu not confirmed).
2. Delete the local copy: `uvx keyring del google-ecommerce-mcp openai-ads-key`.

---

## 5. Quick reference

| Item | Google Ads | Meta Ads | ChatGPT Ads |
|---|---|---|---|
| Doctor name | `gads` | `meta` | `openai` |
| Ids (environment variables) | `GADS_CUSTOMER_ID`, `GADS_LOGIN_CUSTOMER_ID` (manager only) | `META_AD_ACCOUNT_ID` | none |
| Secret | OAuth token (scope `adwords`), no developer token | `META_ACCESS_TOKEN` | `OPENAI_ADS_API_KEY` |
| Where the secret lives | Keyring entry `oauth-token-ads`, or file `GADS_TOKEN_FILE` | Keyring entry `meta-access-token` | Keyring entry `openai-ads-key` |
| Store the secret | `setup-ads --client-secret <client_secret.json>` | `set-secret meta-access-token` | `set-secret openai-ads-key` |
| Same, per client | `setup-ads --client <name> --client-secret <file>` | `client set-secret <name> meta` | `client set-secret <name> openai` |
| Register the id | `install --yes --skip-auth --gads-customer-id <customer id>` | `install --yes --skip-auth --meta-account-id <ad account id>` | not needed |
| Test | `doctor --only gads --live` | `doctor --only meta --live` | `doctor --only openai --live` |
| Files | `client_secret...json` (yours), optional token file | none | none |
| Assistant tools | `gads_accounts`, `gads_performance`, `gads_query` | `meta_ad_accounts`, `meta_insights` | `openai_ads_account`, `openai_ads_insights`, `openai_ads_conversions` |

Prefix every command with `google-ecommerce-mcp`. Other variables: `GADS_API_VERSION`, `META_API_VERSION`, `ADS_TRUST_ENV=1` (company proxy only). Full list: [ADS.md](ADS.md#environment-variables-and-commands). Flags checked with `install --help`, `doctor --help`, `setup-ads --help` and `set-secret --help` on 17/10/2026.

Platforms count conversions independently, so the sum of the three is not your number of orders: [ADS.md, attribution](ADS.md#read-this-first-attribution).

## 6. Checklist

- [ ] The server is installed and `google-ecommerce-mcp doctor` runs.
- [ ] I chose which platform to connect first.
- [ ] The client gave the lowest access that reads (or I own the account).
- [ ] No password, token or key was pasted in the chat.
- [ ] `client_secret...json` is outside git, OneDrive and my vault.
- [ ] Google Ads: API enabled, `install ... --gads-customer-id` done, manager id added if needed.
- [ ] Meta: system user with read role, `ads_read` token stored with `set-secret`.
- [ ] ChatGPT Ads: dedicated key stored with `set-secret`.
- [ ] `doctor --only <service> --live` shows OK for each connected platform.
- [ ] First figures compared with each platform's own screen; Claude Desktop restarted.

## Sources read on 17/10/2026

- Google Ads developer token (sunset 9 September 2026, access levels per Cloud project): <https://developers.google.com/google-ads/api/docs/api-policy/developer-token>
- Google Ads call structure (`login-customer-id`, no hyphens, required via a manager): <https://developers.google.com/google-ads/api/docs/concepts/call-structure>
- Meta Marketing API authorization (`ads_read`, `ads_management`, standard and advanced access, system user limits): <https://developers.facebook.com/docs/marketing-api/overview/authorization>
- Meta system users (created in Business Manager; menu names not given): <https://developers.facebook.com/docs/marketing-api/system-users>
- OpenAI Ads authentication (bearer key, one key per ad account, `GET /ad_account`): <https://developers.openai.com/ads/api-reference/authentication>
- OpenAI Ads reporting (ad-account-scoped Advertiser key, insights endpoints): <https://developers.openai.com/ads/reporting>

Not confirmed from these pages: the 10-digit customer id format and the name of the Google Ads scope (taken from the project's own documentation), the exact Meta and Ads Manager menu names, where the ChatGPT Ads key is created, whether it can be read-only, and anything on real accounts.

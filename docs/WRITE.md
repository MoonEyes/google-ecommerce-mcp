# Writing: plan, approve, apply

> **Read this first.** Writing is **off by default** and has **never been tested on a real account**. Everything was checked offline against fake answers. Try it first on a test account (`client add <name> --sandbox`), and read the [known limits](SECURITY.md#known-limits-read-this-before-turning-writing-on). A change you apply is a real change on a real platform.

With writing on, the assistant can **prepare** changes. It cannot make one by itself. Every change follows the same three steps:

1. **Plan.** The assistant calls a `*_plan_*` tool. Nothing is written. You get a list of what would change, the risks, whether the platform accepted a dry run, and a signed token.
2. **Approve.** You read the plan. If it touches money, personal data, deletion or is high risk, you must approve it yourself in a terminal (`google-ecommerce-mcp approve`). The assistant cannot do that step.
3. **Apply.** The assistant calls the `*_apply` tool with the token. The server re-checks every lock, writes one operation at a time, and records everything in a journal.

What is **not** possible in this version: activating a paused campaign, deleting or removing anything, publishing a Tag Manager container, archiving, setting a bid amount or choosing a bidding strategy (a new Google Ads campaign is created with a fixed manual CPC that has no bid amount), uploading customer lists or conversions.

## The short path: one command, group by group

```bash
google-ecommerce-mcp install --client acme --skip-auth --write
```

Line 5 of `google-ecommerce-mcp quickstart`. In a real terminal it offers writing **one group at a time**: Google properties (GA4, Tag Manager, Search Console, Merchant Center), Google Ads, Meta (ads, pixel, catalog, Page), ChatGPT Ads. Each group has its own question (default no) and you type the client name to confirm that group; only the confirmed groups are turned on, for the client's own accounts. Without `--write`, `install --client` offers the Google properties only.

- `--yes` never skips a write question. Without a terminal (stdin and stdout), nothing is turned on and the command is printed.
- `GEM_WRITE=1` is added to the server entry of your Claude Desktop config **only by this command, and only when you typed a consent in that same run**. A policy turned on earlier is not a consent: the command asks again. An existing `GEM_WRITE` with another value is kept unless you pass `--force`.
- When it adds `GEM_WRITE=1`, the entry is pinned to the exact version (`"args": ["google-ecommerce-mcp==<version>"]`), so an update never brings new write code into Claude without you rerunning the command.
- With `--skip-auth`, the separate Google write token is left as a to-do: run line 6, `setup --client acme --write --client-secret client_secret.json` (step 3 below).

Then do step 4 (budget limits) if you change budgets, restart Claude Desktop and keep the apply tools on "ask each time". The 5 steps below are the same thing done by hand, one platform at a time.

## Turn writing on in 5 steps

Example for a client `acme` with a Google Ads account.

**1. Have a client in the registry.** If you do not have one yet, see [QUICKSTART.md](QUICKSTART.md). For a first try, make it a test client: `google-ecommerce-mcp client add acme --sandbox`.

**2. Turn writing on for one platform of this client.**

```bash
google-ecommerce-mcp client enable-write acme google_ads
```

It must be run by you, in a terminal. It shows what you are allowing and asks you to type the client name. It sets `write.google_ads.enabled` to `true` and puts the client's own account in `account_allow`. Families: `google_ads`, `meta`, `meta_eco` (pixel, catalog, Instagram, Page), `openai_ads`, `ga4`, `gsc`, `merchant`, `gtm`. Turn it off with `client disable-write acme google_ads`.

**3. Give the client a credential that can write.**

| Platform | What to do |
|---|---|
| GA4, Tag Manager, Search Console, Merchant Center | A separate write token: `google-ecommerce-mcp setup --client acme --write --client-secret client_secret.json`. The file must be given explicitly (never picked automatically), the command needs a real terminal and asks you to confirm |
| Google Ads | The same token as for reading: `google-ecommerce-mcp setup-ads --client acme --client-secret client_secret.json` |
| Meta | A system user token that also has `ads_management`: `google-ecommerce-mcp client set-secret acme meta` |
| ChatGPT Ads | The same key as for reading: `google-ecommerce-mcp client set-secret acme openai` |

**4. Set the limits for money.** A plan that changes a budget is refused until the client has a cap and a currency. Edit the client's entry in `clients.json` (see [MULTI-CLIENT.md](MULTI-CLIENT.md#the-file)):

```json
"write": {"google_ads": {"enabled": true, "account_allow": ["1234567890"],
  "max_daily_budget_micros": 50000000, "cap_currency": "EUR", "max_increase_pct": 20, "max_ops_per_plan": 25}}
```

`50000000` micros is 50.00 in the account currency. The cap currency must match the account currency. Leave `allow_activate`, `allow_delete`, `allow_irreversible` and `allow_pii` at `false`.

**5. Switch the server on, then check.** `GEM_WRITE=1` must be in the `env` of the server in your MCP client config. For Claude Desktop, `install --client acme --skip-auth --write` adds it after your typed consent (and pins the version, see above); for another MCP client, add it yourself. Restart the client. Without it every apply is refused. Then run:

```bash
google-ecommerce-mcp doctor --client acme
```

and keep the four apply tools (`gads_apply`, `meta_apply`, `openai_ads_apply`, `google_apply`) on **"ask each time"** in your MCP client. If one is set to "always allow", low-risk plans can be applied without any human step.

The OAuth client file of every `setup` and `install` command is checked before the browser opens: its only root key must be `installed` (Desktop app), `auth_uri` must be on `https://accounts.google.com/`, `token_uri` must be exactly `https://oauth2.googleapis.com/token`, and the redirects must be local (`http://localhost`). Anything else is refused, and nothing is opened nor stored. A file downloaded from Google Cloud Console (type Desktop app) passes as is.

## The locks in one table

All of these are checked when the plan is made and again at apply.

| Lock | Where you set it | Default |
|---|---|---|
| Global switch | `GEM_WRITE=1` in the server environment | off |
| Platform enabled for the client | `client enable-write` (`write.<platform>.enabled`) | off |
| Allowed accounts | `account_allow` in the client's write policy, and the account must belong to the client | the client's own account |
| Daily budget cap | `max_daily_budget_micros` + `cap_currency` | none: budget changes refused |
| Increase limit | `max_increase_pct` (over 24 hours) | 20 |
| Operations per plan | `max_ops_per_plan` | 25 (hard limit 100) |
| Creations paused | built in | always |
| Activation | `allow_activate` (no tool activates in this version) | false |
| Deletion / removal | `allow_delete` | false |
| Irreversible | `allow_irreversible` | false |
| Personal data | `allow_pii` | false |
| Token lifetime | `token_ttl_s` (60 to 3600) | 900 (15 minutes) |
| Test accounts only | `"sandbox": true` on the client, used with `GEM_LIVE_TEST=1` | not sandbox |

## Full example 1: pause a Google Ads campaign (low risk)

You say: *"Pause the campaign 'Brand - old' for acme."* The assistant:

**Plan.** It calls `gads_plan_pause_entities` with `client_id="acme"`, `level="campaign"`, `ids=["22334455"]`. The answer, shortened:

```json
{"plan_id": "pl_0123456789abcdef", "client_id": "acme", "platform": "google_ads",
 "summary": "Plan pl_0123456789abcdef (acme, google_ads): Pause campaign 22334455\n- op1 [low] Pause campaign 22334455 (account 1234567890; rollback: auto)\nRisk: low. Expires 2026-10-18T10:15:00+00:00.\nTo apply: gads_apply(...). Ask the user first.",
 "human_approval_required": false,
 "dry_run": [{"op_id": "op1", "ok": true, "mode": "native"}],
 "confirm": "acme:pl_0123456789abcdef:9f2c41ab",
 "confirmation_token": "eyJwbG...(long)...", "expires_at": "2026-10-18T10:15:00+00:00"}
```

**You read it.** One operation, status to `PAUSED`, nothing else. Google accepted it in its own dry run (`validateOnly`).

**Apply.** You tell the assistant to go ahead. It calls `gads_apply` with the `plan_id`, the `confirmation_token`, and `confirm="acme:pl_0123456789abcdef:9f2c41ab"`. Your MCP client asks you to allow the call. The server checks the switches, the token, the digest, the policy and that the plan was not used before, then sends the request and journals it.

**Undo.** `rollback_plan(client_id="acme", plan_id="pl_0123456789abcdef")` returns a new plan that restores the previous status. Re-enabling is an activation: the plan is refused unless `allow_activate` is true. Otherwise re-enable the campaign in the Google Ads interface.

## Full example 2: change a Meta daily budget (money, needs your approval)

You say: *"Set the daily budget of Meta campaign 6012345 to 40 euros for acme."* Meta budgets are in minor units (cents), so 40.00 EUR is `4000`.

**Plan.** `meta_plan_budget_change(client_id="acme", level="campaign", entity_id="6012345", daily_budget_minor=4000)`. The server reads the current budget and the account currency first. If the new budget is over the cap, or more than 20 % above the current one (or above the lowest value seen in the last 24 hours), the plan is refused with `budget_over_cap` or `increase_too_large` and no token. Otherwise:

```json
{"plan_id": "pl_aaaabbbbccccdddd", "risks": {"max": "high", "financial": true},
 "human_approval_required": true,
 "approval_command": "google-ecommerce-mcp approve pl_aaaabbbbccccdddd --client acme",
 "dry_run": [{"op_id": "op1", "ok": true, "mode": "local"}],
 "confirm": "acme:pl_aaaabbbbccccdddd:5d10e8c2", "confirmation_token": "eyJ..."}
```

**Approve, in a terminal you open yourself.**

```bash
google-ecommerce-mcp approve pl_aaaabbbbccccdddd --client acme
```

The command shows every operation in full (method, URL, each value, the total of the daily budgets with its currency), then asks you to retype the plan id. It records a signed, single-use approval and tells the plan it can go. It will not run without a real terminal.

**Apply.** The assistant calls `meta_apply` with the same `plan_id`, token and `confirm`. Without your approval record it gets `approval_required` and the command to run.

## Full example 3: add a Tag Manager tag (nothing goes live)

You say: *"Prepare a GA4 event tag for the 'add_to_cart' event on acme, in a new workspace."*

1. `gtm_plan_workspace_create(client_id="acme", name="mcp-add-to-cart")`, then `google_apply` with the token. No approval needed. (Tag Manager writes only in a non-default workspace; the Default Workspace is never touched.)
2. The assistant reads what exists with `gtm_inventory` and `gtm_get_tag`, then calls `gtm_plan_tag_upsert(client_id="acme", workspace_id="<new id>", tag={...})`. A normal tag is low or medium risk and needs only the token. A **custom HTML** tag is high risk: the plan says `human_approval_required: true` and you run `approve`.
3. `google_apply`.
4. Check the result with `gtm_workspace_status`, and optionally `gtm_plan_quick_preview` (a preview build that catches errors).
5. **You** create the version and publish in the Tag Manager interface after testing in Preview mode. There is no tool for that in this version, on purpose: a publish reaches every visitor.

To undo before publishing, use `rollback_plan`, or delete the workspace in the interface.

## What gets written down

- **Plans** are stored in the state folder (`plans/<client>/`, private files). `plan_list` and `plan_get` show them.
- **The journal** (`journal/<client>/YYYY-MM.jsonl`) records each plan, approval, intent (before the request) and result. `audit_log` reads it. Secrets and personal data are masked. If the journal cannot be written, nothing is applied.
- The state folder is `GEM_STATE_DIR` if set, otherwise `state` inside the configuration folder (see [MULTI-CLIENT.md](MULTI-CLIENT.md#where-the-files-are)).

## Common refusals and their fix

Every refusal is an object with `error`, `detail` (where and why) and `fix`. Nothing was written when you see one.

| `error` | What it means | Fix |
|---|---|---|
| `write_disabled` | `GEM_WRITE` is not `1` in the server environment | Add `GEM_WRITE=1` to the server `env`, restart the client |
| `write_not_enabled` | Writing is off for this client and platform | `google-ecommerce-mcp client enable-write <client> <family>` |
| `account_not_allowed` | The account is not in `account_allow` | `client enable-write <client> <family> --account <id>` (the id must be the client's own) |
| `id_not_owned`, `account_not_owned`, `account_not_bound` | The id does not belong to this client, or the platform answered for another account | Use the client's own ids (`client show <client>`) and the right `client_id` |
| `client_required` | Several clients and no `client_id` | Pass `client_id` (see `clients_list`) |
| `field_not_allowed`, `no_field_allowlist` | The request contains a field this action may not send | Remove the field. The list is fixed in the code and cannot be widened from the registry |
| `status_missing`, `status_not_paused` | A creation must say `PAUSED` | Nothing to do from the assistant; creations are paused by the builders. Activate in the platform interface |
| `activation_not_allowed` | Something tries to set `ENABLED` or `ACTIVE` | Not offered. Activate in the platform interface |
| `delete_not_allowed`, `irreversible_not_allowed`, `pii_not_allowed` | Removal, an irreversible change or personal data, and the matching switch is false | Not offered in this version |
| `bidding_not_supported` | The request contains a bid or bidding strategy field | Not supported in this version. Set the bid or strategy in the platform interface. The only bidding field ever sent is the fixed empty `manualCpc` of a new Google Ads campaign |
| `budget_cap_missing`, `cap_currency_missing` | The client has no `max_daily_budget_micros` or `cap_currency` | Add both to the client's write policy in `clients.json` |
| `currency_mismatch`, `currency_unknown`, `currency_unit_unknown` | The account currency differs from `cap_currency`, or could not be read | Align `cap_currency` with the account currency; check read access |
| `budget_over_cap` | New daily budget above `max_daily_budget_micros` | Choose a smaller budget, or raise the cap yourself in `clients.json` |
| `increase_too_large` | More than `max_increase_pct` above the freshest or 24-hour lowest value | Raise by less, in steps, or wait 24 hours |
| `plan_budget_over_cap` | The budgets of the plan add up above the ceiling | Split the plan |
| `too_many_ops`, `empty_plan` | More than `max_ops_per_plan` operations, or none | Split, or raise `max_ops_per_plan` (up to 100) |
| `before_unknown` | The current value could not be read, so a change cannot be bounded | Check read access, then plan again |
| `dry_run_failed` | The platform (or the local check) rejected an operation | Read `detail` (for Google Ads it ends with the field path in square brackets when Google gives one), fix the values, plan again. Nothing was written |
| `approval_required`, `approval_invalid` | The plan needs your approval, or the record does not match the plan | Run the `approval_command` shown, in a terminal, then apply again |
| `preview_incomplete` | The plan is too big to show in full, so it cannot be approved | Split it into smaller plans |
| `confirm_mismatch` | `confirm` is not the exact text of the plan | Pass the `confirm` value from the plan |
| `plan_expired` | The token lifetime ended (15 minutes by default) | Plan again |
| `plan_modified`, `policy_changed` | The plan or the client's policy changed since the plan | Plan again |
| `plan_already_applied` | A token works once | Look at `audit_log`; plan again if you need a new change |
| `wrong_apply_tool` | The plan belongs to another family | Use the apply tool named in `fix` |
| `sandbox_required` | `GEM_LIVE_TEST=1` is set and the client is not a sandbox client | Use a test client, or unset `GEM_LIVE_TEST` |
| `journal_unwritable`, `state_unwritable` | The state folder cannot be written | Make the folder writable (`GEM_STATE_DIR`); no write is allowed without a journal |
| `snapshot_failed` | The current state of the entity could not be read | Check the credential and the id, then plan again |
| `missing_scope` / write token missing | The Google write token is absent or lacks the scope | `google-ecommerce-mcp setup --client <client> --write` |
| `project_not_approved` | Google Ads: your Google Cloud project does not have the access level this call needs (Explorer, Basic or Standard), or the account is not a test account while the project is at the test level | See [ADS.md](ADS.md#quotas-access-levels-and-limits); use a test account or request a higher access level |
| `execute_failed` | A platform error during apply; earlier operations of the plan may have gone through | Read `audit_log`, then check the platform. Use `rollback_plan` if it applies |

## What is not covered

- A plan with several operations is applied one by one. If one fails, the following ones are not sent, but the earlier ones are not undone automatically. The result says which operations ran (`status: partial`).
- Merchant Center applies a change with a delay of up to 30 minutes. The product shows the old values meanwhile.
- The first real writes have not been verified on any platform. See [SECURITY.md](SECURITY.md#known-limits-read-this-before-turning-writing-on).

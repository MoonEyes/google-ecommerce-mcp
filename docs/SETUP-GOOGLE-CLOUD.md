# Google Cloud setup, step by step

You need your **own** OAuth client. It takes about 10 minutes, once. Nothing here costs money: all six APIs are free within their quotas.

## 1. Create or pick a project

[Google Cloud Console](https://console.cloud.google.com/) > project picker > **New project** (for example `shop-mcp`).

## 2. Enable the APIs

**APIs & Services > Library**, then enable the ones you will use:

| API to enable | Needed for |
|---|---|
| Google Analytics Data API | `ga4_report`, `ga4_realtime` |
| Google Search Console API | `gsc_performance`, `gsc_inspect_url`, `gsc_sitemaps` |
| Merchant API | `merchant_*` tools |
| Tag Manager API | `gtm_inventory` |
| Web Search Indexing API | `indexing_status` |
| PageSpeed Insights API | `pagespeed` (only if you create an API key) |

## 3. Configure the OAuth consent screen

**Google Auth Platform** (formerly "OAuth consent screen"):

1. **Branding**: app name (anything, for example `Shop MCP`), your support email.
2. **Audience**:
   - Google Workspace account: choose **Internal**. Tokens do not expire and no warning screen appears.
   - Personal Gmail account: choose **External**, add your own address as a **test user**.
3. **Important for External apps:** while the app is in *Testing*, Google expires refresh tokens after **7 days**, so you would have to rerun `setup` every week. To avoid that, click **Publish app** (status *In production*). You do not need Google's verification for your own use; the consent screen will show "Google hasn't verified this app", click **Advanced > Go to ... (unsafe)**. It is your own app talking to your own data.

You do not need to declare scopes on the consent screen; `setup` requests them.

## 4. Create the OAuth client

**Clients > Create client > Application type: Desktop app**. Download the JSON file. Keep it private (it is listed in `.gitignore` as `client_secret*.json`).

## 5. Merchant API only: register the project

The Merchant API refuses calls from a Cloud project that is not registered with your Merchant Center account. Follow Google's [Merchant API quickstart](https://developers.google.com/merchant/api/guides/quickstart) ("register as a developer"). Symptom if you skip it: `merchant_*` tools return `403` with a message about the GCP project not being registered.

## 6. Optional: PageSpeed API key

The anonymous PageSpeed quota is shared by everyone and often exhausted. **APIs & Services > Credentials > Create credentials > API key**, then **restrict** it to *PageSpeed Insights API*. Put it in `PAGESPEED_API_KEY`.

## 7. Authorize

```bash
uvx --from git+https://github.com/MoonEyes/google-ecommerce-mcp google-ecommerce-mcp setup --client-secret ~/Downloads/client_secret_XXXX.json
```

Sign in with the Google account that has access to your GA4 property, Search Console property, Merchant Center account and GTM container, and tick every scope. The token goes to your OS keyring.

## 8. Find your ids

| Variable | Where to find it |
|---|---|
| `GA4_PROPERTY_ID` | GA4 > Admin > Property settings > **Property ID** (digits only, not the `G-` measurement id) |
| `GSC_SITE_URL` | Search Console property selector. Domain property: `sc-domain:example.com`. URL-prefix property: the exact URL with trailing slash, `https://www.example.com/` |
| `MERCHANT_ACCOUNT_ID` | Merchant Center, top right under your store name, or in the URL (`a=1234567890`) |
| `GTM_CONTAINER_ID` | Tag Manager workspace header: `GTM-XXXXXXX` |

## 9. Check

```bash
google-ecommerce-mcp check
```

Every configured service should be `true` and `token` should be `ok`. Then add the server to your MCP client (see the [README](../README.md#3-add-it-to-your-mcp-client) and [`examples/`](../examples/)).

## Permissions the Google account needs

The token can only see what the signed-in account can see:

- GA4: at least **Viewer** on the property.
- Search Console: **Restricted** user or more on the property.
- Merchant Center: any user role on the account.
- Tag Manager: **Read** on the container.
- Indexing API: **Owner** of the Search Console property.

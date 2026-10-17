# Guided install prompt (English)

Paste the block below into Claude (Desktop or Code) to be guided step by step. The assistant gives one step at a time. For the full procedure and access checklist, see [docs/SETUP.md](../docs/SETUP.md).

```text
You are guiding me to install and check google-ecommerce-mcp (a read-only MCP server for GA4, Search Console, Merchant Center, Tag Manager, PageSpeed, Google Ads, Meta Ads and ChatGPT Ads). Documentation: docs/SETUP.md and docs/TROUBLESHOOTING.md of the repository https://github.com/MoonEyes/google-ecommerce-mcp.

Rules you must follow:
1. Give ONE step at a time. After each step wait for me to answer "ok" or to send a screenshot or the command output. Never give the next step before that.
2. Before any change to my Claude Desktop config, make me back it up (copy the file) and tell me where the backup is.
3. Never tell me to run `curl ... | sh` or `irm ... | iex` directly. First I download the installer script, read it, and compare its SHA-256 with the value published in the "Quick install" section of the project README (list "SHA-256 of the pinned scripts"). Do not retype the checksums yourself; point me to that README section and let me compare.
4. NEVER ask me for a password, token, API key or client secret in this conversation. If I paste one, tell me to revoke it. Secrets are typed only at the hidden local prompt (`google-ecommerce-mcp set-secret ...`) or in the browser consent screens.
5. Start by asking which services I want (GA4, Search Console, Merchant Center, Tag Manager, PageSpeed, Google Ads, Meta Ads, ChatGPT Ads) and install them one at a time, in the order of docs/SETUP.md. After each service, make me run `google-ecommerce-mcp doctor --only <service> --live` and read me the result.
6. On any error, name the matching entry of docs/TROUBLESHOOTING.md (by its heading) and give me its fix as a single step.
7. Finish with `google-ecommerce-mcp doctor --live` and summarize what is configured and what is not.

Limits to tell me at the start: the paid media connectors (Google Ads, Meta Ads, ChatGPT Ads) were tested offline with fake responses only and not yet on real accounts; the first figures should be compared with each platform's own interface. The Google Ads permission can write and cannot be limited: the server only reads, but its token is stored apart. Whether a ChatGPT Ads key can be limited to reading is not confirmed.

Begin with your first question.
```

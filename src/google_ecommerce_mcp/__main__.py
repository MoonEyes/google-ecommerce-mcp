"""Command line entry point.

    google-ecommerce-mcp                      run the MCP server over stdio (what MCP clients launch)
    google-ecommerce-mcp setup --client-secret client_secret.json
                                              open the Google consent screen and store the token
    google-ecommerce-mcp setup-ads --client-secret client_secret.json
                                              authorize Google Ads (separate token, never mixed with the one above)
    google-ecommerce-mcp set-secret meta-access-token | openai-ads-key
                                              store a Meta / ChatGPT Ads secret in the keyring (hidden prompt)
    google-ecommerce-mcp check                print which services are configured and test the token
    google-ecommerce-mcp install              guided install: ids, Google authorization, Claude Desktop config
                                              (+ optional paid media: --gads-customer-id, --meta-account-id...)
    google-ecommerce-mcp doctor [--live] [--json] [--only ga4,gsc,merchant,gtm,pagespeed,gads,meta,openai]
                                              one line per service, and the exact command to fix what fails
    google-ecommerce-mcp report --input report.json --output report.html [--lang fr|en]
                                [--brand-name NAME] [--accent #RRGGBB] [--logo logo.png] [--force]
                                              build the static client report (one HTML file, no network)
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def main(argv: list[str] | None = None) -> int | None:
    """Run one command; returns the process exit code (None means 0), used by sys.exit(main())."""
    parser = argparse.ArgumentParser(prog="google-ecommerce-mcp")
    sub = parser.add_subparsers(dest="command")
    setup = sub.add_parser("setup", help="authorize Google access and store the token")
    setup.add_argument("--client-secret", required=True, help="OAuth client JSON (Desktop app) from Google Cloud Console")
    setup.add_argument("--token-file", default=os.getenv("GOOGLE_TOKEN_FILE"),
                       help="store the token in this file instead of the OS keyring")
    setup.add_argument("--with-merchant", action="store_true",
                       help="also request the Merchant Center scope (content). Google has no read-only one")
    setup.add_argument("--with-indexing", action="store_true",
                       help="also request the Indexing API scope. Google has no read-only one")
    setup_ads = sub.add_parser("setup-ads", help="authorize Google Ads access (separate token, write-capable scope)")
    setup_ads.add_argument("--client-secret", required=True,
                           help="OAuth client JSON (Desktop app) from Google Cloud Console")
    setup_ads.add_argument("--token-file", default=os.getenv("GADS_TOKEN_FILE"),
                           help="store the Google Ads token in this file instead of the OS keyring")
    secret = sub.add_parser("set-secret", help="store the Meta Ads token or the ChatGPT Ads key in the OS keyring")
    secret.add_argument("name", choices=["meta-access-token", "openai-ads-key"],
                        help="which secret to store; the value is read from a hidden prompt, never from the command line")
    sub.add_parser("check", help="show configuration and test the stored token")
    inst = sub.add_parser("install", help="guided install: ask ids, authorize Google, register in Claude Desktop")
    inst.add_argument("--client-secret", help="OAuth client JSON (Desktop app)")
    inst.add_argument("--ga4", help="GA4 property id (digits)")
    inst.add_argument("--gsc", help="Search Console property, sc-domain:example.com or https://www.example.com/")
    inst.add_argument("--merchant", help="Merchant Center account id")
    inst.add_argument("--gtm", help="Tag Manager container id, GTM-XXXXXXX")
    inst.add_argument("--pagespeed-key", dest="pagespeed_key", help="PageSpeed Insights API key")
    inst.add_argument("--token-file", default=os.getenv("GOOGLE_TOKEN_FILE"),
                      help="store the token in this file instead of the OS keyring")
    inst.add_argument("--name", default="google-ecommerce", help="server name in the client config")
    inst.add_argument("--config", help="Claude Desktop config file (default: the standard location for this OS)")
    inst.add_argument("--skip-auth", action="store_true", help="keep the token already stored")
    inst.add_argument("--no-desktop", action="store_true", help="do not modify the Claude Desktop config")
    inst.add_argument("--force", action="store_true", help="replace an existing server entry with the same name")
    inst.add_argument("--with-indexing", action="store_true",
                      help="also request the Indexing API scope (write-capable; only needed by indexing_status)")
    inst.add_argument("--yes", action="store_true", help="never prompt; unanswered ids are skipped")
    inst.add_argument("--gads-customer-id", dest="gads_customer_id",
                      help="Google Ads customer id (digits, dashes allowed); authorizes Google Ads with a separate token")
    inst.add_argument("--gads-login-customer-id", dest="gads_login_customer_id",
                      help="Google Ads manager (MCC) id, only if you reach the account through a manager")
    inst.add_argument("--meta-account-id", dest="meta_account_id",
                      help="Meta ad account id (digits, act_ allowed); the token is asked in a hidden prompt")
    inst.add_argument("--skip-ads", action="store_true", help="leave out the paid media step")
    inst.add_argument("--corporate-proxy", action="store_true",
                      help="behind a company proxy: set ADS_TRUST_ENV=1 so the Ads connectors use its settings")
    doc = sub.add_parser("doctor", help="check each service and print the exact command to fix what fails")
    doc.add_argument("--live", action="store_true", help="also make one small read request per configured service")
    doc.add_argument("--json", action="store_true", help="machine-readable output (never contains a secret)")
    doc.add_argument("--only", help="comma-separated services: ga4,gsc,merchant,gtm,pagespeed,gads,meta,openai")
    doc.add_argument("--config", help="Claude Desktop config file (default: the standard location for this OS)")
    doc.add_argument("--name", default="google-ecommerce", help="server name in the Claude Desktop config")
    rep = sub.add_parser("report", help="build the static HTML client report from a JSON file (no network)")
    rep.add_argument("--input", required=True, help="report JSON built from the tools' results (see docs/REPORTING.md)")
    rep.add_argument("--output", required=True, help="HTML file to write; an existing file is kept unless --force")
    rep.add_argument("--lang", choices=["fr", "en"], help="report language (default: meta.lang, else fr)")
    rep.add_argument("--brand-name", dest="brand_name", help="client name shown on the report")
    rep.add_argument("--accent", help="client accent colour, #RRGGBB")
    rep.add_argument("--logo", help="client logo file (PNG, JPEG or SVG, 256 KB at most)")
    rep.add_argument("--force", action="store_true", help="replace the output file if it exists")
    args = parser.parse_args(argv)

    if args.command == "report":
        from .report.render import BRAND_ISSUES, ReportFileError, write_report

        try:
            where, issues = write_report(args.input, args.output, force=args.force, lang=args.lang,
                                         brand_name=args.brand_name, accent=args.accent, logo_path=args.logo)
        except ReportFileError as exc:
            print(f"Report not written: {exc}", file=sys.stderr)
            return 1
        for issue in issues:
            print(f"Warning: {issue}: {BRAND_ISSUES.get(issue, issue)}.", file=sys.stderr)
        print(f"Report written to {where}.")
        return

    if args.command == "setup":
        from .auth import run_setup
        from .config import scopes_for

        where = run_setup(args.client_secret, args.token_file,
                          scopes_for(merchant=args.with_merchant, indexing=args.with_indexing))
        print(f"Token stored in {where}.")
        return
    if args.command == "setup-ads":
        from .ads_auth import run_setup_ads
        from .ads_config import ADS_SCOPE_NOTE

        print(ADS_SCOPE_NOTE)
        where = run_setup_ads(args.client_secret, args.token_file)
        print(f"Google Ads token stored in {where}. Your other Google token is untouched.")
        from .setup_guide import doctor_command

        print(f"Next: check that it works:\n  {doctor_command('gads')}")
        return
    if args.command == "set-secret":
        import getpass

        from .ads_auth import set_secret

        value = (getpass.getpass(f"Paste the value for {args.name} (hidden): ") or "").strip()
        if not value:
            print("Nothing entered, nothing stored.", file=sys.stderr)
            return 1
        print(f"Stored in {set_secret(args.name, value)}.")
        from .setup_guide import doctor_command

        print(f"Next: check that it works:\n  {doctor_command('meta' if args.name == 'meta-access-token' else 'openai')}")
        return
    if args.command == "install":
        from .installer import run

        return run(args)
    if args.command == "doctor":
        from .doctor import run as run_doctor

        return run_doctor(args)
    if args.command == "check":
        from .server import check

        print(json.dumps(check(), indent=2))
        return

    from .server import mcp

    mcp.run()


if __name__ == "__main__":
    sys.exit(main())

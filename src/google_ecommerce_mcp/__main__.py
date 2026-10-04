"""Command line entry point.

    google-ecommerce-mcp                      run the MCP server over stdio (what MCP clients launch)
    google-ecommerce-mcp setup --client-secret client_secret.json
                                              open the Google consent screen and store the token
    google-ecommerce-mcp check                print which services are configured and test the token
    google-ecommerce-mcp install              guided install: ids, Google authorization, Claude Desktop config
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def main(argv: list[str] | None = None) -> None:
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
    args = parser.parse_args(argv)

    if args.command == "setup":
        from .auth import run_setup
        from .config import scopes_for

        where = run_setup(args.client_secret, args.token_file,
                          scopes_for(merchant=args.with_merchant, indexing=args.with_indexing))
        print(f"Token stored in {where}.")
        return
    if args.command == "install":
        from .installer import run

        return run(args)
    if args.command == "check":
        from .server import check

        print(json.dumps(check(), indent=2))
        return

    from .server import mcp

    mcp.run()


if __name__ == "__main__":
    sys.exit(main())

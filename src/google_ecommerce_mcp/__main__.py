"""Command line entry point.

    google-ecommerce-mcp                      run the MCP server over stdio (what MCP clients launch)
    google-ecommerce-mcp setup --client-secret client_secret.json
                                              open the Google consent screen and store the token
    google-ecommerce-mcp check                print which services are configured and test the token
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
    sub.add_parser("check", help="show configuration and test the stored token")
    args = parser.parse_args(argv)

    if args.command == "setup":
        from .auth import run_setup

        where = run_setup(args.client_secret, args.token_file)
        print(f"Token stored in {where}.")
        return
    if args.command == "check":
        from .server import check

        print(json.dumps(check(), indent=2))
        return

    from .server import mcp

    mcp.run()


if __name__ == "__main__":
    sys.exit(main())

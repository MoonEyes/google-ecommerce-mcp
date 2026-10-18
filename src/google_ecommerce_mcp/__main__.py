"""Command line entry point.

    google-ecommerce-mcp                      run the MCP server over stdio (what MCP clients launch)
    google-ecommerce-mcp quickstart [--client NAME]
                                              print the 4 commands for a first client (+2 for writing); writes nothing
    google-ecommerce-mcp setup --client-secret client_secret.json
                                              open the Google consent screen and store the token
    google-ecommerce-mcp setup --client NAME [--client-secret f.json] [--write]
                                              same, for one client of the registry: <client>/google (read-only), or
                                              with --write a SEPARATE token <client>/google-w (edit scopes, no publish)
    google-ecommerce-mcp setup-ads [--client NAME] --client-secret client_secret.json
                                              authorize Google Ads (separate token, never mixed with the one above)
    google-ecommerce-mcp set-secret meta-access-token | openai-ads-key
    google-ecommerce-mcp set-secret --client NAME meta | openai
                                              store a Meta / ChatGPT Ads secret in the keyring (hidden prompt)
    google-ecommerce-mcp check                print which services are configured and test the token
    google-ecommerce-mcp install              guided install: ids, Google authorization, Claude Desktop config
                                              (+ optional paid media: --gads-customer-id, --meta-account-id...)
    google-ecommerce-mcp install --client NAME [--pin] [--write]
                                              shortest multi-client path: client add, setup --client, optional
                                              writing (default no), Claude Desktop entry with a minimal env
    google-ecommerce-mcp doctor [--client NAME] [--live] [--json] [--only ga4,gsc,merchant,gtm,pagespeed,gads,meta,openai]
                                              one line per service (per client and platform with a registry), and
                                              the exact command to fix what fails
    google-ecommerce-mcp report --input report.json --output report.html [--lang fr|en]
                                [--brand-name NAME] [--accent #RRGGBB] [--logo logo.png] [--force]
                                              build the static client report (one HTML file, no network)
    google-ecommerce-mcp client add|list|show|remove|enable-write|disable-write|set-secret <name> ...
                                              several clients: guided registry of ids (never a secret)
    google-ecommerce-mcp approve <plan_id> [--client NAME]
                                              approve a financial / irreversible write plan out of band
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
    setup.add_argument("--client-secret", help="OAuth client JSON (Desktop app) from Google Cloud Console "
                                               "(required without --client; with --client, a single "
                                               "client_secret*.json in this folder or Downloads is found)")
    setup.add_argument("--client", help="client id of the registry: the token goes to its keyring entry "
                                        "<client>/google (or <client>/google-w with --write)")
    setup.add_argument("--write", action="store_true",
                       help="with --client: a SEPARATE write token (analytics.edit, tagmanager.edit.containers, "
                            "webmasters, content; never publish). The read token is kept")
    setup.add_argument("--yes", action="store_true",
                       help="kept for compatibility; never skips the --write confirmation (typed in a terminal)")
    setup.add_argument("--token-file", default=None,
                       help="store the token in this file instead of the OS keyring (default: GOOGLE_TOKEN_FILE; "
                            "not with --client)")
    setup.add_argument("--with-merchant", action="store_true",
                       help="also request the Merchant Center scope (content). Google has no read-only one")
    setup.add_argument("--with-indexing", action="store_true",
                       help="also request the Indexing API scope. Google has no read-only one")
    setup_ads = sub.add_parser("setup-ads", help="authorize Google Ads access (separate token, write-capable scope)")
    setup_ads.add_argument("--client-secret",
                           help="OAuth client JSON (Desktop app) from Google Cloud Console (required without "
                                "--client)")
    setup_ads.add_argument("--client", help="client id of the registry: the token goes to <client>/gads")
    setup_ads.add_argument("--token-file", default=None,
                           help="store the Google Ads token in this file instead of the OS keyring (default: "
                                "GADS_TOKEN_FILE; not with --client)")
    secret = sub.add_parser("set-secret", help="store the Meta Ads token or the ChatGPT Ads key in the OS keyring")
    secret.add_argument("name", choices=["meta-access-token", "openai-ads-key", "meta", "openai", "meta-capi",
                                         "pagespeed"],
                        help="which secret to store; the value is read from a hidden prompt, never from the command "
                             "line. Without --client: meta-access-token or openai-ads-key. With --client: meta, "
                             "openai, meta-capi or pagespeed")
    secret.add_argument("--client", help="client id of the registry (same as `client set-secret <client> <kind>`)")
    sub.add_parser("quickstart", help="print the 4 commands for a first client (+2 for writing); writes nothing") \
        .add_argument("--client", default=None, help="client name shown in the commands (default: acme)")
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
    inst.add_argument("--client", help="multi-client install: create this client (ids only), authorize it, offer "
                                       "writing (default no), register the server with a minimal env")
    inst.add_argument("--pin", action="store_true", help="with --client: pin the server entry to it (GEM_CLIENT)")
    inst.add_argument("--write", action="store_true",
                      help="with --client: go straight to the writing step (still asks to type the client name)")
    doc = sub.add_parser("doctor", help="check each service and print the exact command to fix what fails")
    doc.add_argument("--client", help="only this client of the registry (default: every client)")
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
    from .client_cli import add_approve_subcommand, add_subcommands

    add_subcommands(sub)
    add_approve_subcommand(sub)
    args = parser.parse_args(argv)

    if args.command in ("client", "approve"):
        from .client_cli import run as run_client
        from .client_cli import run_approve

        return run_client(args) if args.command == "client" else run_approve(args)

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

    if args.command == "quickstart":
        return _quickstart(args)
    if args.command == "setup":
        if _per_client(args):
            return _setup_client(args)
        if args.write:
            return _usage_error("setup --write: writing needs a client of the registry.",
                                "google-ecommerce-mcp client add <name>, then "
                                "google-ecommerce-mcp setup --client <name> --write")
        if not args.client_secret:
            setup.error("the following arguments are required: --client-secret")
        from .auth import run_setup
        from .config import scopes_for

        from .auth import read_token_warning
        from .client_cli import lang

        token_file = args.token_file if args.token_file is not None else os.getenv("GOOGLE_TOKEN_FILE")
        scopes = scopes_for(merchant=args.with_merchant, indexing=args.with_indexing)
        warning = read_token_warning("default", scopes, lang())
        if warning:
            print(warning)
        from .auth import SetupRefused

        try:
            where = run_setup(args.client_secret, token_file, scopes)
        except SetupRefused as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(2)
        print(f"Token stored in {where}.")
        return
    if args.command == "setup-ads":
        if _per_client(args):
            return _setup_ads_client(args)
        if not args.client_secret:
            setup_ads.error("the following arguments are required: --client-secret")
        from .ads_auth import run_setup_ads
        from .ads_config import ADS_SCOPE_NOTE

        print(ADS_SCOPE_NOTE)
        token_file = args.token_file if args.token_file is not None else os.getenv("GADS_TOKEN_FILE")
        from .auth import SetupRefused

        try:
            where = run_setup_ads(args.client_secret, token_file)
        except SetupRefused as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(2)
        print(f"Google Ads token stored in {where}. Your other Google token is untouched.")
        from .setup_guide import doctor_command

        print(f"Next: check that it works:\n  {doctor_command('gads')}")
        return
    if args.command == "set-secret" and (args.client or args.name not in ("meta-access-token", "openai-ads-key")):
        return _set_secret_client(args)
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


# ------------------------------------------------------------------ per-client setup (registry)
_MESSAGES = {
    "write_confirm": ("Continuer et ouvrir le navigateur ? [o/N] ", "Continue and open the browser? [y/N] "),
    "aborted": ("Annulé, rien n'a changé.", "Cancelled, nothing changed."),
    "no_secret_file": ("Fichier client OAuth introuvable : {why}.", "OAuth client file not found: {why}."),
    "using_file": ("Fichier client OAuth : {path}", "OAuth client file: {path}"),
    "use_found": ("Fichier client OAuth trouvé : {path}. L'utiliser ? [o/N] ",
                  "OAuth client file found: {path}. Use it? [y/N] "),
    "read_capable": ("lecture, avec {names} : PAS en lecture seule", "read, with {names}: NOT read-only"),
    "browser": ("Votre navigateur va s'ouvrir sur l'écran de consentement Google ({what}). Cochez toutes les cases.",
                "Your browser will open the Google consent screen ({what}). Tick every box."),
    "read": ("lecture seule", "read-only"),
    "write": ("écriture, jeton séparé", "write, separate token"),
    "stored": ("Jeton Google ({what}) du client {cid!r} enregistré dans {where}.",
               "Google token ({what}) of client {cid!r} stored in {where}."),
    "kept": ("Les autres jetons du client sont inchangés.", "The client's other tokens are untouched."),
    "missing_scopes": ("Attention : Google n'a pas accordé {scopes}. Relancez et cochez toutes les cases : {command}",
                       "Warning: Google did not grant {scopes}. Rerun and tick every box: {command}"),
    "failed": ("Autorisation non terminée ({kind}). Rien n'a été enregistré. Relancez : {command}",
               "Authorization not completed ({kind}). Nothing was stored. Rerun: {command}"),
    "next": ("Ensuite, vérifiez :", "Next, check:"),
    "ads_stored": ("Jeton Google Ads du client {cid!r} enregistré dans {where}. Ses jetons Google (lecture, "
                   "écriture) sont inchangés.",
                   "Google Ads token of client {cid!r} stored in {where}. Its Google tokens (read, write) are "
                   "untouched."),
    "no_google": ("Le client {cid!r} n'a aucun identifiant Google (GA4, Search Console, Merchant Center, Tag "
                  "Manager) : il n'y a rien à autoriser en écriture.",
                  "Client {cid!r} has no Google id (GA4, Search Console, Merchant Center, Tag Manager): there is "
                  "nothing to authorize for writing."),
}


def _t(key: str, **values: object) -> str:
    from .client_cli import lang

    fr, en = _MESSAGES[key]
    return (fr if lang() == "fr" else en).format(**values)


def _usage_error(detail: str, fix: str) -> int:
    from .client_cli import _usage

    return _usage(detail, fix)


def _per_client(args: argparse.Namespace) -> bool:
    """--client given, or a registry exists and no --token-file was passed (then the implicit client is used)."""
    if getattr(args, "client", None):
        return True
    if getattr(args, "token_file", None) is not None:
        return False
    from . import clients

    try:
        return clients.load_registry() is not None
    except clients.ClientError:
        return True  # broken registry: the per-client path reports it (never falls back to the legacy entry)


def _client_context(name: str | None):
    """The ClientContext for the CLI: --client NAME (GEM_CLIENT ignored: the CLI administers every client), else
    the implicit client (the only one of the registry, or legacy mode). Raises clients.ClientError."""
    from . import client_cli, clients

    return client_cli._context(name) if name else clients.resolve(None)


def _client_secret_file(given: str | None, write: bool = False) -> str | None:
    """The OAuth client file: --client-secret, else (never with write) a single client_secret*.json found in this
    folder or Downloads, used only after a [y/N] confirmation in a terminal (refused when not interactive)."""
    from . import client_cli
    from .auth import find_client_secret

    confirm = None
    if client_cli._is_tty():
        def confirm(found):
            return client_cli._yes(client_cli._ask(_t("use_found", path=found)))
    path, why = find_client_secret(given, write=write, confirm=confirm)
    if path is None:
        print(_t("no_secret_file", why=why), file=sys.stderr)
        return None
    if not given:
        print(_t("using_file", path=path))
    return str(path)


def _setup_client(args: argparse.Namespace) -> int:
    """`setup --client X [--write]`: consent in the browser, token stored in X's own keyring entry only."""
    from . import auth, client_cli, setup_guide as guide
    from .clients import ClientError

    try:
        ctx = _client_context(args.client)
    except ClientError as exc:
        return client_cli._err(exc)
    cid = ctx.client_id
    if args.token_file is not None:
        return _usage_error(f"setup --client {cid}: --token-file is not used with --client (each client's token "
                            f"lives in its keyring entry {cid}/google).", "leave --token-file out")
    command = guide.client_setup_command(cid, write=args.write)
    if args.write and ctx.legacy:
        return _usage_error("setup --write: writing needs a client of the registry (legacy mode has none).",
                            f"{guide.client_add_command(cid)} (it keeps your current ids and tokens), then {command}")
    if args.write:
        platforms = [p for p in auth.GOOGLE_PLATFORMS if ctx.accounts(p)]
        scopes = auth.write_scopes_for(platforms)
        if not scopes:
            print(_t("no_google", cid=cid), file=sys.stderr)
            print(f"  {guide.client_add_command(cid, replace=True)}", file=sys.stderr)
            return 2
        if not args.client_secret:  # never an auto-discovered client file for a write token
            return _usage_error(f"setup --client {cid} --write: {auth.WRITE_NEEDS_EXPLICIT_FILE}.", command)
        # Widening the rights needs a human at a real terminal (stdin AND stdout), whatever --yes says.
        if not client_cli._require_tty(f"setup --client {cid} --write"):
            return 1
        print(auth.write_token_warning(ctx, client_cli.lang()))
        if not client_cli._yes(client_cli._ask(_t("write_confirm"))):
            print(_t("aborted"))
            return 1
    else:
        scopes = auth.read_scopes_for(ctx, merchant=args.with_merchant, indexing=args.with_indexing)
    secret_file = _client_secret_file(args.client_secret, write=args.write)
    if secret_file is None:
        print(f"  {command}", file=sys.stderr)
        return 2
    capable = [] if args.write else auth.write_capable(scopes)
    if capable:  # content / indexing: Google has no read-only scope, so say it plainly, never "read-only"
        print(auth.read_token_warning(cid, scopes, client_cli.lang()))
        what = _t("read_capable", names=", ".join(s.rsplit("/", 1)[-1] for s in capable))
    else:
        what = _t("write" if args.write else "read")
    print(_t("browser", what=what))
    if ctx.legacy:  # no registry: exactly today's single-account setup (oauth-token / GOOGLE_TOKEN_FILE)
        where = auth.run_setup(secret_file, os.getenv("GOOGLE_TOKEN_FILE"), scopes)
        missing: list[str] = []
    else:
        try:
            where, missing = auth.run_client_setup(ctx, secret_file, write=args.write, scopes=scopes)
        except auth.SetupRefused as exc:
            return _usage_error(f"setup --client {cid}: {exc}.", command)
        except Exception as exc:  # consent refused, not an OAuth client file, port busy, no keyring backend
            print(_t("failed", kind=type(exc).__name__, command=command), file=sys.stderr)
            return 1
    print(_t("stored", what=what, cid=cid, where=where))
    print(_t("kept"))
    if missing:
        print(_t("missing_scopes", scopes=", ".join(s.rsplit("/", 1)[-1] for s in missing), command=command))
    print(f"{_t('next')}\n  {guide.client_doctor_command(cid)}")
    return 0


def _setup_ads_client(args: argparse.Namespace) -> int:
    """`setup-ads --client X`: adwords-only consent, stored in X's own entry <client>/gads."""
    from . import ads_auth, client_cli, setup_guide as guide
    from .ads_config import ADS_SCOPE_NOTE
    from .clients import ClientError

    try:
        ctx = _client_context(args.client)
    except ClientError as exc:
        return client_cli._err(exc)
    cid = ctx.client_id
    if args.token_file is not None:
        return _usage_error(f"setup-ads --client {cid}: --token-file is not used with --client (the token lives "
                            f"in the keyring entry {cid}/gads).", "leave --token-file out")
    command = guide.client_setup_ads_command(cid)
    secret_file = _client_secret_file(args.client_secret)
    if secret_file is None:
        print(f"  {command}", file=sys.stderr)
        return 2
    print(ADS_SCOPE_NOTE)
    if ctx.legacy:  # no registry: exactly today's setup-ads (oauth-token-ads / GADS_TOKEN_FILE)
        where = ads_auth.run_setup_ads(secret_file, os.getenv("GADS_TOKEN_FILE"))
    else:
        from .auth import SetupRefused

        try:
            where = ads_auth.run_client_setup_ads(ctx, secret_file)
        except SetupRefused as exc:  # hostile client file, unexpected scope: the message names no secret
            return _usage_error(f"setup-ads --client {cid}: {exc}.", command)
        except Exception as exc:
            print(_t("failed", kind=type(exc).__name__, command=command), file=sys.stderr)
            return 1
    print(_t("ads_stored", cid=cid, where=where))
    print(f"{_t('next')}\n  {guide.client_doctor_command(cid)} --only gads")
    return 0


def _set_secret_client(args: argparse.Namespace) -> int:
    """`set-secret --client X meta|openai|meta-capi|pagespeed`: alias of `client set-secret X <kind>`."""
    from . import client_cli, setup_guide as guide
    from .clients import ClientError

    try:
        cid = _client_context(args.client).client_id
    except ClientError as exc:
        return client_cli._err(exc)
    code = client_cli._cmd_set_secret(argparse.Namespace(name=cid, kind=args.name))
    if code == 0:
        service = "meta" if client_cli.SECRET_ALIASES.get(args.name, args.name) in ("meta", "meta-capi") else (
            "openai" if client_cli.SECRET_ALIASES.get(args.name, args.name) == "openai" else "pagespeed")
        print(f"{_t('next')}\n  {guide.client_doctor_command(cid)} --only {service}")
    return code


def _quickstart(args: argparse.Namespace) -> int:
    """Eight lines, nothing written, no network, no keyring read."""
    from . import client_cli, clients, setup_guide as guide

    name = args.client
    if name:
        try:
            name = clients.validate_client_id(name)
        except ValueError as exc:
            return _usage_error(f"quickstart --client {args.client!r}: {exc}.", "choose a name such as acme")
    else:
        try:
            data = clients.load_registry()
        except clients.ClientError:
            data = None
        known = sorted((data or {}).get("clients", {}))
        name = known[0] if len(known) == 1 else "acme"
    print("\n".join(guide.quickstart_lines(name, client_cli.lang())))
    return 0


if __name__ == "__main__":
    sys.exit(main())

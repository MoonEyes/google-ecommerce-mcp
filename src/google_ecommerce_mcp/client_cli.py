"""`google-ecommerce-mcp client ...` and `approve`: guided management of the client registry.

    client add <name>                     guided questions (French or English from the locale; GEM_LANG=fr|en),
                                          every id optional, format checked; --yes for no question
    client list                           one line per client
    client show <name> [--json]           ids masked, which secrets are present (never their value)
    client remove <name>                  asks to type the name; also deletes the client's keyring entries
    client enable-write <name> <family>   explicit confirmation; write.<family>.enabled=true + account_allow
    client disable-write <name> <family>
    client set-secret <name> <kind>       hidden prompt (meta, openai, meta-capi, pagespeed)
    approve <plan_id> [--client X]        out-of-band human approval of a financial / irreversible / removal /
                                          PII plan; shows every op in full before asking to retype the plan id

No secret is ever asked in clear text nor written to the registry. remove, enable-write, set-secret and approve
refuse to run unless stdin AND stdout are interactive terminals (_is_tty). --yes of remove only skips retyping the
name, never the terminal check; enable-write and approve have no non-interactive mode and no environment switch.

What the terminal check does and does not protect: it stops a model that only reaches this machine through the
MCP tools (no shell), and a plain pipe or script. It does NOT stop a model (or anything else) that has a real
terminal: a shell tool that allocates a pty (script, expect, winpty, a terminal emulator driven by keystrokes)
passes isatty(), and a process that can read the HMAC master key can forge an approval record in the state
folder. The per-client keys derive from that master key, read from (any one is enough to forge):
    - the environment variable GEM_WRITE_HMAC_KEY;
    - the OS keyring entry write-hmac-master (service google-ecommerce-mcp);
    - the fallback file <state>/write-hmac-master.key (mode 600), used when no keyring backend exists.
The human approval is only as strong as keeping such tools away from the model and these three places out of
the model's reach."""

from __future__ import annotations

import argparse
import getpass
import importlib
import json
import locale
import os
import sys
from typing import Any

from . import clients, secrets_store
from .clients import ACCOUNT_FIELDS, WRITE_PLATFORMS, ClientError

__all__ = ["add_subcommands", "add_approve_subcommand", "run", "run_approve", "lang", "PLATFORM_ALIASES"]

PLATFORM_ALIASES = {"gads": "google_ads", "google-ads": "google_ads", "googleads": "google_ads",
                    "openai": "openai_ads", "openai-ads": "openai_ads", "chatgpt": "openai_ads",
                    "meta-eco": "meta_eco", "search-console": "gsc", "searchconsole": "gsc",
                    "merchant-center": "merchant", "tag-manager": "gtm", "analytics": "ga4"}
SECRET_ALIASES = {"meta-access-token": "meta", "openai-ads-key": "openai", "openai-ads": "openai"}

# Wizard questions: (platform, key, FR prompt, EN prompt, example). Secondary Meta ids are behind one question.
QUESTIONS = [
    ("ga4", "property_id", "ID de propriété GA4", "GA4 property id", "123456789"),
    ("gsc", "site_url", "Propriété Search Console", "Search Console property", "sc-domain:example.com"),
    ("merchant", "account_id", "ID de compte Merchant Center", "Merchant Center account id", "123456789"),
    ("gtm", "public_id", "Conteneur Tag Manager", "Tag Manager container", "GTM-ABC1234"),
    ("gtm", "account_id", "ID numérique du compte Tag Manager (écriture)", "Tag Manager numeric account id (writes)",
     "6000000000"),
    ("gtm", "container_id", "ID numérique du conteneur Tag Manager (écriture)",
     "Tag Manager numeric container id (writes)", "200000000"),
    ("google_ads", "customer_id", "ID client Google Ads", "Google Ads customer id", "123-456-7890"),
    ("google_ads", "login_customer_id", "ID du compte administrateur (MCC), si vous passez par un MCC",
     "Manager (MCC) id, only if you go through a manager", "123-456-7890"),
    ("meta", "ad_account_id", "ID de compte publicitaire Meta", "Meta ad account id", "act_123456789"),
    ("openai_ads", "account_id", "ID de compte ChatGPT Ads (OpenAI)", "ChatGPT (OpenAI) Ads account id", "acct_123"),
]
META_EXTRA = [
    ("meta", "pixel_id", "ID du pixel / dataset", "Pixel / dataset id", "123456789"),
    ("meta", "catalog_id", "ID du catalogue", "Catalog id", "123456789"),
    ("meta", "business_id", "ID du Business Manager", "Business Manager id", "123456789"),
    ("meta", "page_id", "ID de la Page Facebook", "Facebook Page id", "123456789"),
    ("meta", "ig_user_id", "ID du compte Instagram professionnel", "Instagram professional account id", "1784..."),
]
# CLI flag for each field (also usable with --yes).
FLAGS = {("ga4", "property_id"): "ga4", ("gsc", "site_url"): "gsc", ("merchant", "account_id"): "merchant",
         ("gtm", "public_id"): "gtm", ("gtm", "account_id"): "gtm_account", ("gtm", "container_id"): "gtm_container",
         ("google_ads", "customer_id"): "gads_customer_id",
         ("google_ads", "login_customer_id"): "gads_login_customer_id",
         ("meta", "ad_account_id"): "meta_account_id", ("meta", "pixel_id"): "meta_pixel_id",
         ("meta", "catalog_id"): "meta_catalog_id", ("meta", "business_id"): "meta_business_id",
         ("meta", "page_id"): "meta_page_id", ("meta", "ig_user_id"): "meta_ig_user_id",
         ("openai_ads", "account_id"): "openai_account_id"}

_MESSAGES = {
    "error": ("Erreur", "Error"),
    "fix": ("Correctif", "Fix"),
    "intro": ("Nouveau client {name!r}. Tous les identifiants sont facultatifs : Entrée = valeur proposée "
              "entre crochets (ou rien), « - » = effacer. Aucun secret n'est demandé ici.",
              "New client {name!r}. Every id is optional: Enter = the value in brackets (or nothing), "
              "\"-\" = clear. No secret is asked here."),
    "label": ("Nom affiché (ex. Acme SA)", "Display name (e.g. Acme SA)"),
    "sandbox": ("Comptes de test uniquement (bac à sable) ? [o/N]", "Test accounts only (sandbox)? [y/N]"),
    "meta_extra": ("Renseigner aussi pixel, catalogue, Page, Instagram ? [o/N]",
                   "Also give pixel, catalog, Page, Instagram ids? [y/N]"),
    "invalid": ("Format invalide : {why}. Réessayez (ou Entrée pour passer).",
                "Invalid format: {why}. Try again (or Enter to skip)."),
    "skipped": ("{field} ignoré (format invalide).", "{field} skipped (invalid format)."),
    "saved": ("Client {name!r} enregistré dans {path}.", "Client {name!r} saved in {path}."),
    "acl": ("Attention : droits du fichier non restreints à votre compte Windows ; gardez-le dans votre profil.",
            "Warning: the file permissions could not be restricted to your Windows account; keep it in your "
            "profile."),
    "first_registry": ("Registre créé : les variables d'environnement d'IDs (GA4_PROPERTY_ID...) sont désormais "
                       "ignorées, seul ce registre compte.",
                       "Registry created: the id environment variables (GA4_PROPERTY_ID...) are now ignored, only "
                       "this registry counts."),
    "legacy_secrets": ("Client « default » : il garde vos jetons actuels du trousseau (aucune migration).",
                       "Client \"default\": it keeps your current keyring tokens (no migration)."),
    "two_clients": ("{n} clients enregistrés : chaque appel d'outil doit maintenant préciser client_id "
                    "(ou épinglez un serveur avec GEM_CLIENT=<nom>).",
                    "{n} clients registered: every tool call must now give client_id (or pin a server with "
                    "GEM_CLIENT=<name>)."),
    "next": ("Étapes suivantes (secrets, jamais en clair dans une commande) :",
             "Next steps (secrets, never in clear text on a command line):"),
    "next_google": ("  Google (lecture) : google-ecommerce-mcp setup --client {name} --client-secret <fichier>",
                    "  Google (read):    google-ecommerce-mcp setup --client {name} --client-secret <file>"),
    "next_gads": ("  Google Ads :       google-ecommerce-mcp setup-ads --client {name} --client-secret <fichier>",
                  "  Google Ads:       google-ecommerce-mcp setup-ads --client {name} --client-secret <file>"),
    "next_secret": ("  {what} :{pad}google-ecommerce-mcp client set-secret {name} {kind}",
                    "  {what}:{pad}google-ecommerce-mcp client set-secret {name} {kind}"),
    "next_check": ("  Vérifier :         google-ecommerce-mcp client show {name}",
                   "  Check:            google-ecommerce-mcp client show {name}"),
    "next_doctor": ("  Tester :           google-ecommerce-mcp doctor --client {name}",
                    "  Test:             google-ecommerce-mcp doctor --client {name}"),
    "next_all": ("  Ou tout en une fois (autorisation + Claude Desktop) : google-ecommerce-mcp install --client {name}",
                 "  Or all at once (authorization + Claude Desktop): google-ecommerce-mcp install --client {name}"),
    "no_registry": ("Mode historique (pas de registre {path}) : un seul client implicite « default », bâti sur "
                    "les variables d'environnement.",
                    "Legacy mode (no registry {path}): one implicit client \"default\", built from the environment "
                    "variables."),
    "registry": ("Registre : {path}", "Registry: {path}"),
    "header": ("CLIENT               NOM                  PLATEFORMES                         ÉCRITURE",
               "CLIENT               NAME                 PLATFORMS                           WRITE"),
    "none": ("aucune", "none"),
    "remove_confirm": ("Supprimer le client {name!r} et ses entrées du trousseau ? Tapez son nom pour confirmer : ",
                       "Remove client {name!r} and its keyring entries? Type its name to confirm: "),
    "remove_keep": ("Supprimer le client {name!r} (entrées du trousseau conservées) ? Tapez son nom : ",
                    "Remove client {name!r} (keyring entries kept)? Type its name: "),
    "aborted": ("Annulé, rien n'a changé.", "Cancelled, nothing changed."),
    "removed": ("Client {name!r} supprimé. Entrées du trousseau supprimées : {n}.",
                "Client {name!r} removed. Keyring entries deleted: {n}."),
    "write_warn": ("ATTENTION : vous autorisez l'assistant à PRÉPARER des modifications réelles sur {family} pour "
                   "{name!r} (comptes : {accounts}). Chaque modification reste un plan à approuver ; rien ne part "
                   "sans GEM_WRITE=1 sur le serveur. Création toujours en pause ; activation, suppression, "
                   "opérations irréversibles et données personnelles restent interdites.",
                   "WARNING: you allow the assistant to PREPARE real changes on {family} for {name!r} "
                   "(accounts: {accounts}). Each change stays a plan to approve; nothing is sent without "
                   "GEM_WRITE=1 on the server. Creations are always paused; activation, deletion, irreversible "
                   "operations and personal data stay forbidden."),
    "sandbox_note": ("Ce client n'est pas marqué bac à sable : testez d'abord sur un compte de test.",
                     "This client is not marked sandbox: test on a test account first."),
    "write_confirm": ("Tapez le nom du client ({name}) pour confirmer : ", "Type the client name ({name}) to confirm: "),
    "write_on": ("Écriture activée : {name}.{family} (enabled=true, account_allow={accounts}). Côté serveur, "
                 "GEM_WRITE=1 reste nécessaire.",
                 "Writing enabled: {name}.{family} (enabled=true, account_allow={accounts}). On the server, "
                 "GEM_WRITE=1 is still required."),
    "write_off": ("Écriture désactivée : {name}.{family}.", "Writing disabled: {name}.{family}."),
    "secret_prompt": ("Collez la valeur de {kind} pour {name} (masquée) : ",
                      "Paste the value of {kind} for {name} (hidden): "),
    "secret_empty": ("Rien saisi, rien enregistré.", "Nothing entered, nothing stored."),
    "secret_stored": ("Enregistré dans {where}.", "Stored in {where}."),
    "no_tty": ("{what} : terminal interactif requis (stdin et stdout). Lancez la commande vous-même dans un "
               "terminal ; elle ne peut pas être pilotée par un assistant ou un script. Rien n'a changé.",
               "{what}: an interactive terminal is required (stdin and stdout). Run the command yourself in a "
               "terminal; it cannot be driven by an assistant or a script. Nothing changed."),
    "approve_title": ("APPROBATION HUMAINE D'UN PLAN D'ÉCRITURE", "HUMAN APPROVAL OF A WRITE PLAN"),
    "approve_not_needed": ("Ce plan ne change ni les dépenses, ni rien d'irréversible, ne supprime rien et ne touche "
                           "pas de données personnelles : l'approbation est facultative.",
                           "This plan changes no spending, nothing irreversible, removes nothing and touches no "
                           "personal data: approval is optional."),
    "approve_prompt": ("Pour approuver, retapez l'identifiant du plan ({plan_id}) : ",
                       "To approve, type the plan id again ({plan_id}): "),
    "approve_done": ("Plan {plan_id} approuvé pour {client} (usage unique, expire {expires}). L'assistant peut "
                     "maintenant l'appliquer.",
                     "Plan {plan_id} approved for {client} (single use, expires {expires}). The assistant can now "
                     "apply it."),
    "approve_missing": ("La commande approve n'est pas encore disponible : le module d'écriture (write_core) n'est "
                        "pas branché dans cette version. Rien n'a été approuvé.",
                        "The approve command is not available yet: the write module (write_core) is not wired in "
                        "this version. Nothing was approved."),
}


def lang() -> str:
    """"fr" or "en": GEM_LANG, else LC_ALL / LC_MESSAGES / LANG, else the OS locale (French_France on Windows)."""
    for name in ("GEM_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = (os.environ.get(name) or "").strip().lower()
        if value and value not in ("c", "posix", "c.utf-8"):
            return "fr" if value.startswith("fr") else "en"
    try:
        current = (locale.getlocale()[0] or "").lower()
    except Exception:
        current = ""
    return "fr" if current.startswith("fr") else "en"


def _t(key: str, **values: Any) -> str:
    fr, en = _MESSAGES[key]
    return (fr if lang() == "fr" else en).format(**values)


def _err(exc: ClientError) -> int:
    print(f"{_t('error')} [{exc.code}] : {exc.detail}" if lang() == "fr" else f"{_t('error')} [{exc.code}]: {exc.detail}",
          file=sys.stderr)
    print(f"  {_t('fix')} : {exc.fix}" if lang() == "fr" else f"  {_t('fix')}: {exc.fix}", file=sys.stderr)
    return 1


def _usage(detail: str, fix: str) -> int:
    _err(ClientError("usage", detail, fix))
    return 2


def _is_tty() -> bool:
    """True only when stdin and stdout are both interactive terminals. Tests monkeypatch this function."""
    try:
        return bool(sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _require_tty(what: str) -> bool:
    if _is_tty():
        return True
    print(_t("no_tty", what=what), file=sys.stderr)
    return False


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:  # piped / closed stdin: take the default
        return ""


def _yes(answer: str) -> bool:
    return answer.strip().lower() in {"o", "oui", "y", "yes"}


def _platform(value: str) -> str | None:
    cleaned = (value or "").strip().lower()
    cleaned = PLATFORM_ALIASES.get(cleaned, cleaned.replace("-", "_"))
    return cleaned if cleaned in WRITE_PLATFORMS else None


# ------------------------------------------------------------------ argparse wiring
def add_subcommands(sub: Any) -> None:
    """Register `client ...` on the main parser's subparsers."""
    client = sub.add_parser("client", help="manage clients (multi-account registry): add, list, show, remove...")
    csub = client.add_subparsers(dest="client_command")
    add = csub.add_parser("add", help="guided creation of a client (ids only, never a secret)")
    add.add_argument("name", help="client id: a-z, 0-9 and -, e.g. acme")
    add.add_argument("--label", help="display name")
    add.add_argument("--sandbox", action="store_true", help="test accounts only")
    add.add_argument("--replace", action="store_true", help="update an existing client (write settings kept)")
    add.add_argument("--yes", action="store_true", help="ask nothing: use the flags (and current values)")
    for (platform, key), flag in FLAGS.items():
        add.add_argument("--" + flag.replace("_", "-"), dest=flag, help=f"{platform}.{key}")
    csub.add_parser("list", help="list the clients")
    show = csub.add_parser("show", help="show a client (ids masked)")
    show.add_argument("name")
    show.add_argument("--json", action="store_true", help="machine-readable output (ids masked, no secret)")
    rem = csub.add_parser("remove", help="remove a client and its keyring entries")
    rem.add_argument("name")
    rem.add_argument("--yes", action="store_true", help="do not ask to type the name")
    rem.add_argument("--keep-secrets", action="store_true", help="keep the client's keyring entries")
    families = ", ".join(WRITE_PLATFORMS)
    ew = csub.add_parser("enable-write", help="allow write plans for one platform of a client (asks confirmation)")
    ew.add_argument("name")
    ew.add_argument("platform", help=f"one of: {families} (aliases gads, openai)")
    ew.add_argument("--account", action="append", help="account id to allow (default: the client's own id)")
    dw = csub.add_parser("disable-write", help="forbid write plans for one platform of a client")
    dw.add_argument("name")
    dw.add_argument("platform", help=f"one of: {families}")
    ss = csub.add_parser("set-secret", help="store a client's secret in the OS keyring (hidden prompt)")
    ss.add_argument("name")
    ss.add_argument("kind", help=f"one of: {', '.join(secrets_store.PASTEABLE_KINDS)}")


def add_approve_subcommand(sub: Any) -> None:
    ap = sub.add_parser("approve", help="approve a financial or irreversible write plan out of band")
    ap.add_argument("plan_id")
    ap.add_argument("--client", help="client id of the plan")


# ------------------------------------------------------------------ commands
def _ask_field(platform: str, key: str, fr: str, en: str, example: str, default: str | None) -> str | None:
    label = fr if lang() == "fr" else en
    eg = "ex." if lang() == "fr" else "e.g."
    suffix = f" [{default}]" if default else f" ({eg} {example})"  # ids are not secrets: shown in full here
    for _ in range(3):
        answer = _ask(f"{label}{suffix} : " if lang() == "fr" else f"{label}{suffix}: ").strip()
        if answer == "-":
            return None
        if not answer:
            return default or None
        try:
            return clients.validate_account(platform, key, answer)
        except ValueError as exc:
            print(_t("invalid", why=exc))
    print(_t("skipped", field=f"{platform}.{key}"))
    return default or None


def _cmd_add(args: argparse.Namespace) -> int:
    try:
        name = clients.validate_client_id(args.name)
    except ValueError as exc:
        return _usage(f"client add {args.name!r}: {exc}.", "choose a name such as acme or shop-fr")
    data = clients.load_registry()
    first = data is None
    existing = (data or {}).get("clients", {}).get(name)
    if existing and not args.replace:
        return _err(ClientError("registry_invalid", f"client {name!r} already exists in {clients.registry_path()}.",
                                f"google-ecommerce-mcp client add {name} --replace, or choose another name"))
    # Defaults: flags > the existing entry > (first registry only) today's environment variables.
    defaults: dict[str, dict[str, str]] = {}
    if first:
        defaults = clients.legacy_accounts_from_env()
    if existing:
        defaults = {p: dict(v) for p, v in existing["accounts"].items()}
    for (platform, key), flag in FLAGS.items():
        value = getattr(args, flag, None)
        if value:
            try:
                defaults.setdefault(platform, {})[key] = clients.validate_account(platform, key, value)
            except ValueError as exc:
                return _usage(f"--{flag.replace('_', '-')} {value!r}: {exc}.", "fix the value or leave the flag out")
    accounts: dict[str, dict[str, str]] = {}
    label = args.label if args.label is not None else (existing or {}).get("label", "")
    sandbox = bool(args.sandbox or (existing or {}).get("sandbox", False))
    if args.yes:
        for platform, values in defaults.items():
            for key, value in values.items():
                try:
                    accounts.setdefault(platform, {})[key] = clients.validate_account(platform, key, value)
                except ValueError as exc:
                    return _usage(f"{platform}.{key} {value!r}: {exc}.", "pass a valid value with its flag")
    else:
        print(_t("intro", name=name))
        answer = _ask(f"{_t('label')} [{label}] : " if lang() == "fr" else f"{_t('label')} [{label}]: ").strip()
        label = label if not answer else ("" if answer == "-" else answer)
        for platform, key, fr, en, example in QUESTIONS:
            value = _ask_field(platform, key, fr, en, example, defaults.get(platform, {}).get(key))
            if value:
                accounts.setdefault(platform, {})[key] = value
        meta_defaults = defaults.get("meta", {})
        has_extra = any(meta_defaults.get(k) for _, k, _, _, _ in META_EXTRA)
        if accounts.get("meta") and (has_extra or _yes(_ask(_t("meta_extra") + " "))):
            for platform, key, fr, en, example in META_EXTRA:
                value = _ask_field(platform, key, fr, en, example, meta_defaults.get(key))
                if value:
                    accounts.setdefault(platform, {})[key] = value
        if not sandbox:
            sandbox = _yes(_ask(_t("sandbox") + " "))
    secrets = None
    if name == clients.LEGACY_CLIENT_ID and first:  # zero migration: keep today's keyring entries
        secrets = {"google_read": "oauth-token", "google_ads": "oauth-token-ads", "meta": "meta-access-token",
                   "openai_ads": "openai-ads-key"}
    try:
        private = clients.add_client(name, label=label, accounts=accounts, sandbox=sandbox, secrets=secrets,
                                     replace=bool(existing))
    except ClientError as exc:
        return _err(exc)
    except ValueError as exc:
        return _usage(f"client add {name}: {exc}.", "check the label (80 characters, no secret)")
    except OSError as exc:
        return _err(ClientError("registry_invalid", f"{clients.registry_path()}: not writable "
                                f"({type(exc).__name__}).", "check the folder permissions, or set GEM_CLIENTS_FILE"))
    print(_t("saved", name=name, path=clients.registry_path()))
    if not private:
        print(_t("acl"))
    if first:
        print(_t("first_registry"))
        if secrets:
            print(_t("legacy_secrets"))
    total = len((clients.load_registry() or {}).get("clients", {}))
    if total >= 2:
        print(_t("two_clients", n=total))
    _print_next_steps(name, accounts)
    return 0


def _print_next_steps(name: str, accounts: dict) -> None:
    print(_t("next"))
    if any(accounts.get(p) for p in ("ga4", "gsc", "merchant", "gtm")):
        print(_t("next_google", name=name))
    if accounts.get("google_ads"):
        print(_t("next_gads", name=name))
    if accounts.get("meta"):
        print(_t("next_secret", what="Meta", pad=" " * (17 - len("Meta")), name=name, kind="meta"))
    if accounts.get("openai_ads"):
        print(_t("next_secret", what="ChatGPT Ads", pad=" " * (17 - len("ChatGPT Ads")), name=name, kind="openai"))
    print(_t("next_check", name=name))
    print(_t("next_doctor", name=name))
    print(_t("next_all", name=name))


def _cmd_list(_: argparse.Namespace) -> int:
    rows = clients.list_clients(include_all=True)  # the CLI administers every client, pinned or not
    path = clients.registry_path()
    if rows and rows[0]["legacy"]:
        print(_t("no_registry", path=path))
    else:
        print(_t("registry", path=path))
    print(_t("header"))
    for row in rows:
        mark = "*" if row["pinned"] else " "
        sandbox = " (sandbox)" if row["sandbox"] else ""
        print(f"{mark}{row['client_id']:<20} {(row['label'] or '-')[:20]:<20} "
              f"{(', '.join(row['platforms']) or _t('none'))[:35]:<35} "
              f"{', '.join(row['write_enabled']) or _t('none')}{sandbox}")
    return 0


def _context(name: str) -> clients.ClientContext:
    """Context of name, ignoring GEM_CLIENT (the CLI administers every client)."""
    data = clients.load_registry()
    cid = (name or "").strip().lower()
    if data is None:
        if cid != clients.LEGACY_CLIENT_ID:
            raise ClientError("unknown_client", f"no client registry ({clients.registry_path()}); only "
                              f"{clients.LEGACY_CLIENT_ID!r} exists (environment variables).",
                              f"google-ecommerce-mcp client add {cid or '<name>'}")
        return clients._legacy_context()
    if cid not in data["clients"]:
        raise clients._unknown(cid, sorted(data["clients"]))
    return clients._context_from_entry(cid, data["clients"][cid])


def _cmd_show(args: argparse.Namespace) -> int:
    try:
        ctx = _context(args.name)
    except ClientError as exc:
        return _err(exc)
    info = ctx.summary(masked=True)
    info["secrets_present"] = {kind: ctx.secret(kind) is not None for kind in secrets_store.KINDS}
    if args.json:
        print(json.dumps(info, indent=2, ensure_ascii=False))
        return 0
    print(f"{info['client_id']}  {info['label']}" + ("  (legacy)" if info["legacy"] else "")
          + ("  (sandbox)" if info["sandbox"] else ""))
    for platform, values in info["accounts"].items():
        print(f"  {platform:<11} " + ", ".join(f"{k}={v}" for k, v in values.items()))
    for platform, policy in info["write"].items():
        state = "ON" if policy["enabled"] else "off"
        print(f"  write.{platform:<9} {state}  account_allow={policy['account_allow']}")
    present = [k for k, ok in info["secrets_present"].items() if ok]
    missing = [k for k, ok in info["secrets_present"].items() if not ok]
    print(f"  secrets ok: {', '.join(present) or '-'}   missing: {', '.join(missing) or '-'}")
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    if not _require_tty("client remove"):
        return 1
    try:
        ctx = _context(args.name)
        if ctx.legacy:
            raise ClientError("unknown_client", "legacy mode: the implicit client 'default' cannot be removed.",
                              "remove the ids from the MCP server env of your client config instead")
    except ClientError as exc:
        return _err(exc)
    if not args.yes:
        key = "remove_keep" if args.keep_secrets else "remove_confirm"
        if _ask(_t(key, name=ctx.client_id)).strip().lower() != ctx.client_id:
            print(_t("aborted"))
            return 1
    try:
        entry = clients.remove_client(ctx.client_id)
    except ClientError as exc:
        return _err(exc)
    removed: list[str] = []
    if not args.keep_secrets:
        removed = secrets_store.delete_client_secrets(ctx.client_id, entry.get("secrets", {}))
    print(_t("removed", name=ctx.client_id, n=len(removed)))
    return 0


def _cmd_write(args: argparse.Namespace, enabled: bool) -> int:
    if enabled and not _require_tty("client enable-write"):
        return 1
    family = _platform(args.platform)
    if family is None:
        return _usage(f"client {'enable' if enabled else 'disable'}-write: unknown platform {args.platform!r}.",
                      f"use one of: {', '.join(WRITE_PLATFORMS)} (aliases gads, openai)")
    try:
        ctx = _context(args.name)
        if ctx.legacy:
            raise ClientError("unknown_client", "legacy mode (no registry): writing can only be enabled for a "
                              "registered client.",
                              "google-ecommerce-mcp client add default (it imports your current ids and tokens)")
        if not enabled:
            clients.set_write(ctx.client_id, family, False)
            print(_t("write_off", name=ctx.client_id, family=family))
            return 0
        allow = args.account or clients.default_account_allow(ctx.client_id, family)
        if not allow:
            account_platform = WRITE_PLATFORMS[family]
            raise ClientError("platform_not_configured", f"client {ctx.client_id!r}: no {account_platform} id "
                              "configured, so no account to allow.",
                              f"google-ecommerce-mcp client add {ctx.client_id} --replace (give the "
                              f"{account_platform} id), or pass --account <id>")
        normalized = [ctx.require_owned(family, a) for a in allow]
    except ClientError as exc:
        return _err(exc)
    print(_t("write_warn", family=family, name=ctx.client_id, accounts=", ".join(normalized)))
    if not ctx.sandbox:
        print(_t("sandbox_note"))
    if _ask(_t("write_confirm", name=ctx.client_id)).strip().lower() != ctx.client_id:
        print(_t("aborted"))
        return 1
    try:
        policy = clients.set_write(ctx.client_id, family, True, account_allow=normalized)
    except ClientError as exc:
        return _err(exc)
    print(_t("write_on", name=ctx.client_id, family=family, accounts=", ".join(sorted(policy.account_allow))))
    return 0


def _cmd_set_secret(args: argparse.Namespace) -> int:
    if not _require_tty("client set-secret"):
        return 1
    kind = SECRET_ALIASES.get(args.kind, args.kind)
    if kind not in secrets_store.PASTEABLE_KINDS:
        hint = (f"Google tokens come from the browser consent: google-ecommerce-mcp setup --client {args.name}"
                if kind in ("google", "google-w", "gads") else f"use one of: {', '.join(secrets_store.PASTEABLE_KINDS)}")
        return _usage(f"client set-secret: {args.kind!r} cannot be pasted here.", hint)
    try:
        ctx = _context(args.name)
    except ClientError as exc:
        return _err(exc)
    value = (getpass.getpass(_t("secret_prompt", kind=kind, name=ctx.client_id)) or "").strip()
    if not value:
        print(_t("secret_empty"), file=sys.stderr)
        return 1
    try:
        where = secrets_store.set_secret(ctx.client_id, kind, value, legacy=ctx.legacy,
                                         name=None if ctx.legacy else ctx._secret_names.get(kind))
    except Exception as exc:  # no keyring backend: never echo the value
        return _err(ClientError("registry_invalid", f"keyring: could not store {kind} ({type(exc).__name__}).",
                                "make the OS keyring available and rerun `google-ecommerce-mcp client set-secret "
                                f"{ctx.client_id} {kind}` (recommended). LAST RESORT only: "
                                f"{secrets_store.env_name(ctx.client_id, kind)} in the MCP server env holds the "
                                "value IN CLEAR TEXT in the Claude config file"))
    print(_t("secret_stored", where=where))
    return 0


def run(args: argparse.Namespace) -> int:
    """Dispatch `client <subcommand>`; returns the exit code (0 ok, 1 refused/failed, 2 usage)."""
    command = getattr(args, "client_command", None)
    try:
        if command == "add":
            return _cmd_add(args)
        if command == "list":
            return _cmd_list(args)
        if command == "show":
            return _cmd_show(args)
        if command == "remove":
            return _cmd_remove(args)
        if command == "enable-write":
            return _cmd_write(args, True)
        if command == "disable-write":
            return _cmd_write(args, False)
        if command == "set-secret":
            return _cmd_set_secret(args)
    except ClientError as exc:  # broken registry, etc.
        return _err(exc)
    return _usage("client: no subcommand given.",
                  "google-ecommerce-mcp client add|list|show|remove|enable-write|disable-write|set-secret")


def run_approve(args: argparse.Namespace) -> int:
    """`approve <plan_id> [--client X]`: interactive terminal only; shows the plan in plain words, asks to retype
    the plan id, then writes the signed single-use approval (write_core._write_approval)."""
    if not _require_tty("approve"):
        return 1
    try:  # import_module honours sys.modules (None = module disabled), whatever was imported before
        write_core = importlib.import_module(f"{__package__}.write_core")
    except ImportError:
        write_core = None
    preview = getattr(write_core, "approval_preview", None) if write_core else None
    record = getattr(write_core, "_write_approval", None) if write_core else None
    if preview is None or record is None:
        print(_t("approve_missing"), file=sys.stderr)
        return 2
    try:
        cid = _context(args.client).client_id if args.client else clients.resolve(None).client_id
    except ClientError as exc:
        return _err(exc)
    plan_id = (args.plan_id or "").strip()
    shown = preview(cid, plan_id)
    if shown.get("error"):
        if isinstance(shown.get("text"), str):  # incomplete preview: show what it could, then refuse
            print(_t("approve_title"))
            print(shown["text"])
        return _err(ClientError(str(shown["error"]), str(shown.get("detail", "")), str(shown.get("fix", ""))))
    print(_t("approve_title"))
    print(shown["text"])
    if not shown.get("required"):
        print(_t("approve_not_needed"))
    if _ask(_t("approve_prompt", plan_id=plan_id)).strip() != plan_id:
        print(_t("aborted"))
        return 1
    try:
        approver = getpass.getuser()
    except Exception:
        approver = ""
    result = record(cid, plan_id, approver=approver)
    if result.get("error"):
        return _err(ClientError(str(result["error"]), str(result.get("detail", "")), str(result.get("fix", ""))))
    print(_t("approve_done", plan_id=plan_id, client=cid, expires=result.get("expires_at", "")))
    return 0

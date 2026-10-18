"""Ordered setup steps per service, shared by `install`, `doctor` and the docs so they all say the same thing.

One service at a time: each service lists its own steps, each step is one bounded action with the exact command
to copy (empty when the action happens in a web console) and how to see that it worked. Placeholders in angle
brackets (<client_secret.json>, <customer id>...) are the only parts the user replaces. No secret ever appears in
a command: secrets are typed into a hidden prompt."""

from __future__ import annotations

from dataclasses import dataclass

CLI = "google-ecommerce-mcp"
CLIENT_SECRET = "<client_secret.json>"
CLOUD_DOC = "docs/SETUP-GOOGLE-CLOUD.md"
ADS_DOC = "docs/ADS.md"


@dataclass(frozen=True)
class Step:
    """One bounded action. command is '' when the step happens in a web console (see prerequisite)."""

    id: str
    title: str
    prerequisite: str
    command: str
    verify: str


@dataclass(frozen=True)
class Service:
    id: str
    name: str
    steps: tuple[Step, ...]
    optional: bool = False

    def step(self, step_id: str) -> Step:
        return next(s for s in self.steps if s.id == step_id)

    @property
    def check_command(self) -> str:
        return doctor_command(self.id)


def doctor_command(service: str, live: bool = True) -> str:
    """The exact command that tests one service."""
    return f"{CLI} doctor --only {service}" + (" --live" if live else "")


def _oauth_client(prefix: str) -> Step:
    return Step(f"{prefix}-oauth-client", "Create your Google OAuth client (Desktop app)",
                f"A Google account that can open console.cloud.google.com; follow {CLOUD_DOC}", "",
                "You have a file named client_secret_....json on your computer")


def _enable(prefix: str, apis: str) -> Step:
    return Step(f"{prefix}-enable-api", f"Turn on the {apis}",
                "The Google Cloud project of your OAuth client: APIs & Services > Library", "",
                f"The {apis} show as Enabled")


def _google(service: str, name: str, apis: str, find_title: str, find_where: str, find_verify: str,
            flag: str, placeholder: str) -> Service:
    return Service(service, name, (
        _oauth_client(service),
        _enable(service, apis),
        Step(f"{service}-find-id", find_title, find_where, "", find_verify),
        Step(f"{service}-install", "Authorize Google and add the id to Claude Desktop",
             "The client_secret file and the id from the previous steps; your browser will open",
             f"{CLI} install --yes --client-secret {CLIENT_SECRET} --{flag} {placeholder}",
             f"The summary lists {name} under Configured"),
        Step(f"{service}-check", f"Test {name}", "Claude Desktop does not need to be open",
             doctor_command(service), f"The {name} line says OK"),
    ))


SERVICES: tuple[Service, ...] = (
    _google("ga4", "GA4", "Google Analytics Data API and Google Analytics Admin API",
            "Find your GA4 property id", "Viewer access to the property: GA4 > Admin > Property settings",
            "A number such as 123456789 (not G-XXXX)", "ga4", "<property id>"),
    _google("gsc", "Search Console", "Google Search Console API",
            "Find your Search Console property", "Access to the property in search.google.com/search-console",
            "sc-domain:example.com for a domain property, or the full https://www.example.com/ address",
            "gsc", "<property>"),
    _google("merchant", "Merchant Center", "Merchant API",
            "Find your Merchant Center account id", "Access to the account in merchants.google.com",
            "The number shown under your store name", "merchant", "<account id>"),
    _google("gtm", "Tag Manager", "Tag Manager API",
            "Find your Tag Manager container id", "Read access to the container in tagmanager.google.com",
            "An id such as GTM-XXXXXXX", "gtm", "<container id>"),
    Service("pagespeed", "PageSpeed", (
        Step("pagespeed-key", "Optional: create a PageSpeed API key",
             "Only if the shared quota runs out; Google Cloud > APIs & Services > Credentials > API key", "",
             "You have a key and the PageSpeed Insights API is Enabled"),
        Step("pagespeed-install", "Add the key to Claude Desktop",
             "The key from the previous step: press Enter at the Google questions and paste it at the hidden "
             "PageSpeed prompt (or set PAGESPEED_API_KEY in this terminal first); never type it on the command line",
             f"{CLI} install --skip-auth --skip-ads", "The summary lists PageSpeed under Configured"),
    ), optional=True),
    Service("gads", "Google Ads", (
        Step("gads-customer-id", "Note your Google Ads customer id",
             "Access to the account in ads.google.com", "",
             "Ten digits shown at the top right, such as 123-456-7890"),
        _enable("gads", "Google Ads API"),
        Step("gads-install", "Authorize Google Ads (separate token) and add the id to Claude Desktop",
             "The client_secret file of your OAuth client; your browser will open and ask for Google Ads only",
             f"{CLI} install --yes --client-secret {CLIENT_SECRET} --gads-customer-id <customer id>",
             "The summary lists Google Ads under Configured"),
        Step("gads-manager", "Only if you reach the account through a manager (MCC) account: add the manager id",
             "The ten-digit id of the manager account",
             f"{CLI} install --yes --skip-auth --gads-login-customer-id <manager id>",
             "The summary lists Google Ads under Configured"),
        Step("gads-check", "Test Google Ads", "Claude Desktop does not need to be open",
             doctor_command("gads"), "The Google Ads line says OK"),
    )),
    Service("meta", "Meta Ads", (
        Step("meta-token", "Create a Meta system user token with ads_read only",
             f"Admin of the Meta Business Manager that owns the ad account; follow {ADS_DOC}", "",
             "You have copied a long token starting with EAA"),
        Step("meta-store-token", "Store the token in this computer's keyring",
             "The token from the previous step; it is typed into a hidden prompt",
             f"{CLI} set-secret meta-access-token", "The command prints Stored in operating system keyring"),
        Step("meta-account-id", "Add your ad account id to Claude Desktop",
             "The ad account id: digits shown in Ads Manager (act= in the address)",
             f"{CLI} install --yes --skip-auth --meta-account-id <ad account id>",
             "The summary lists Meta Ads under Configured"),
        Step("meta-check", "Test Meta Ads", "Claude Desktop does not need to be open",
             doctor_command("meta"), "The Meta Ads line says OK"),
    )),
    Service("openai", "ChatGPT Ads", (
        Step("openai-key", "Create a dedicated ChatGPT Ads advertiser API key",
             f"Access to your ChatGPT Ads account in Ads Manager; follow {ADS_DOC}", "",
             "You have copied the key"),
        Step("openai-install", "Store the key and register the server in Claude Desktop",
             "The key; press Enter for the Google questions, answer y to ChatGPT Ads, paste the key (hidden)",
             f"{CLI} install --skip-auth", "The summary lists ChatGPT Ads under Configured"),
        Step("openai-check", "Test ChatGPT Ads", "Claude Desktop does not need to be open",
             doctor_command("openai"), "The ChatGPT Ads line says OK"),
    )),
)

SERVICE_IDS: tuple[str, ...] = tuple(s.id for s in SERVICES)
GOOGLE_SERVICES = ("ga4", "gsc", "merchant", "gtm")
ADS_SERVICES = ("gads", "meta", "openai")
_BY_ID = {s.id: s for s in SERVICES}

# Commands the doctor and the installer point to when something is missing (placeholders kept on purpose).
SETUP_ADS = f"{CLI} setup-ads --client-secret {CLIENT_SECRET}"
SET_META = f"{CLI} set-secret meta-access-token"
SET_OPENAI = f"{CLI} set-secret openai-ads-key"
CORPORATE_PROXY = f"{CLI} install --yes --skip-auth --corporate-proxy"


def service(service_id: str) -> Service:
    return _BY_ID[service_id]


def first_command(service_id: str) -> str:
    """The first step with a command: what to run to set the service up."""
    return next(s.command for s in service(service_id).steps if s.command)


def google_setup_command(token_file: str | None = None, merchant: bool = False, indexing: bool = False) -> str:
    """Re-authorize the read-only Google token, keeping the optional scopes the user already enabled."""
    command = f"{CLI} setup --client-secret {CLIENT_SECRET}"
    if token_file:
        command += f' --token-file "{token_file}"'
    if merchant:
        command += " --with-merchant"
    if indexing:
        command += " --with-indexing"
    return command


def setup_ads_command(token_file: str | None = None) -> str:
    return SETUP_ADS + (f' --token-file "{token_file}"' if token_file else "")


def install_id_command(flag: str, placeholder: str, force: bool = False) -> str:
    """Add (or with force, change) one id in the Claude Desktop entry, without re-authorizing."""
    return f"{CLI} install --yes --skip-auth" + (" --force" if force else "") + f" --{flag} {placeholder}"


def as_dicts() -> list[dict]:
    """Every service and its steps as plain data (for the docs or a JSON dump)."""
    return [{"id": s.id, "name": s.name, "optional": s.optional,
             "steps": [{"id": st.id, "title": st.title, "prerequisite": st.prerequisite, "command": st.command,
                        "verify": st.verify} for st in s.steps]} for s in SERVICES]


# ------------------------------------------------------------------ multi-client mode (registry) + writing
CLIENT = "<client>"
SERVER_NAME = "google-ecommerce"
# Write families in the order they are offered (Google first, paid media after).
WRITE_FAMILIES = ("ga4", "gtm", "gsc", "merchant", "google_ads", "meta", "meta_eco", "openai_ads")
GOOGLE_WRITE_FAMILIES = ("ga4", "gtm", "gsc", "merchant")
# Paid media platform -> pasted secret kind (set-secret --client X <kind>).
SECRET_KIND = {"meta": "meta", "openai_ads": "openai"}
# install --client X: writing is offered one group at a time, each with its own [y/N] question and its own typed
# client name. (group id, label, families, spending). Without --write only the Google properties are offered;
# the spending groups are offered only with --write, each confirmed on its own.
WRITE_GROUPS = (
    ("google", "Google properties (GA4, Tag Manager, Search Console, Merchant Center)", GOOGLE_WRITE_FAMILIES, False),
    ("google_ads", "Google Ads", ("google_ads",), True),
    ("meta", "Meta (ads, pixel, catalog, Page)", ("meta", "meta_eco"), True),
    ("openai_ads", "ChatGPT Ads", ("openai_ads",), True),
)


def client_add_command(client_id: str = CLIENT, replace: bool = False) -> str:
    return f"{CLI} client add {client_id}" + (" --replace" if replace else "")


def client_setup_command(client_id: str = CLIENT, write: bool = False, indexing: bool = False) -> str:
    """Browser consent for ONE client: read token "<client>/google", or with write the separate "<client>/google-w"."""
    return (f"{CLI} setup --client {client_id}" + (" --write" if write else "")
            + f" --client-secret {CLIENT_SECRET}" + (" --with-indexing" if indexing and not write else ""))


def client_setup_ads_command(client_id: str = CLIENT) -> str:
    return f"{CLI} setup-ads --client {client_id} --client-secret {CLIENT_SECRET}"


def client_secret_command(client_id: str = CLIENT, kind: str = "meta") -> str:
    """Hidden prompt for a pasted secret of one client (meta, openai, meta-capi, pagespeed)."""
    return f"{CLI} set-secret --client {client_id} {kind}"


def client_doctor_command(client_id: str = CLIENT, live: bool = False) -> str:
    return f"{CLI} doctor --client {client_id}" + (" --live" if live else "")


def enable_write_command(client_id: str = CLIENT, family: str = "ga4") -> str:
    return f"{CLI} client enable-write {client_id} {family}"


def client_install_command(client_id: str = CLIENT, *, skip_auth: bool = False, write: bool = False) -> str:
    return (f"{CLI} install --client {client_id}" + (" --skip-auth" if skip_auth else "")
            + (" --write" if write else ""))


def approve_command(client_id: str = CLIENT) -> str:
    return f"{CLI} approve <plan_id> --client {client_id}"


def claude_code_add_command(name: str = SERVER_NAME, env: dict[str, str] | None = None, package: str = CLI) -> str:
    """package: the uvx argument, e.g. "google-ecommerce-mcp==0.5.0" when writing is on (pinned version)."""
    flags = "".join(f" -e {k}={v}" for k, v in (env or {}).items() if v)
    return f"claude mcp add {name} --scope user{flags} -- uvx {package}"


def client_steps(client_id: str = CLIENT) -> tuple[Step, ...]:
    """The shortest read-only path for one client, in order (install --client chains steps 2 to 6)."""
    return (
        Step("client-oauth-client", "Create your Google OAuth client (Desktop app), once for all clients",
             f"A Google account that can open console.cloud.google.com; follow {CLOUD_DOC}", "",
             "You have a file named client_secret_....json on your computer"),
        Step("client-add", "Register the client: its ids only, never a secret",
             "The ids you have (GA4, Search Console, Merchant Center, Tag Manager, Google Ads, Meta); Enter skips",
             client_add_command(client_id), "The command prints Client ... saved"),
        Step("client-setup", "Authorize Google for this client (read-only token)",
             "The client_secret file; your browser opens, sign in with an account that can read this client",
             client_setup_command(client_id), f"The command prints the keyring entry {client_id}/google"),
        Step("client-ads", "Only if the client has paid media: authorize Google Ads, store the Meta token",
             "Google Ads: the same client_secret file. Meta / ChatGPT Ads: the token, typed in a hidden prompt",
             client_setup_ads_command(client_id), f"The command prints the keyring entry {client_id}/gads"),
        Step("client-check", "Test the client", "Claude does not need to be open",
             client_doctor_command(client_id), "Every configured platform of the client says OK"),
        Step("client-claude", "Register the server in Claude Desktop (Claude Code: claude mcp add)",
             "Claude Desktop installed; the config is backed up before any change. Read-only: this step never adds "
             "GEM_WRITE=1 (only a write consent typed in the same run does)",
             client_install_command(client_id, skip_auth=True), "The summary says registered; restart Claude"),
    )


def client_write_steps(client_id: str = CLIENT, family: str = "ga4") -> tuple[Step, ...]:
    """Optional writing, in order: policy on, separate write token, server switch, human approval, check."""
    return (
        Step("write-enable", "Allow write plans for one platform of the client (typed confirmation)",
             "An interactive terminal; the client must have an id for that platform",
             enable_write_command(client_id, family), "The command prints Writing enabled"),
        Step("write-token", "Authorize the separate Google write token (the read token is kept)",
             "An interactive terminal and the client_secret file passed explicitly (never found automatically); "
             "only the edit scopes of the client's Google platforms, never publish",
             client_setup_command(client_id, write=True), f"The command prints the keyring entry {client_id}/google-w"),
        Step("write-server", "Turn on the server write switch GEM_WRITE=1 in the Claude config",
             "An interactive terminal: answer y for each family group and type the client name each time. Only "
             "this consent, given in the same run, adds GEM_WRITE=1 (never --yes, never an earlier enable-write); "
             "the Claude config is backed up first and the package version is pinned",
             client_install_command(client_id, skip_auth=True, write=True), "The summary lists GEM_WRITE=1"),
        Step("write-approve", "Approve spending or irreversible plans yourself, outside the chat",
             "The plan id the assistant shows; an interactive terminal",
             approve_command(client_id), "The command prints Plan ... approved"),
        Step("write-check", "Test the client, writing included", "Claude does not need to be open",
             client_doctor_command(client_id), "The Write on column says on and the write token ok"),
    )


_QUICKSTART = {
    "fr": ("Premier client en 4 commandes ({cid} = le nom de votre client, rien n'est écrit ici) :",
           "Écriture (facultatif, désactivée par défaut, chaque plan reste à approuver) :",
           "  4. {install}   (Claude Code : {code})"),
    "en": ("First client in 4 commands ({cid} = your client's name; nothing is written here):",
           "Writing (optional, off by default, every plan still needs approval):",
           "  4. {install}   (Claude Code: {code})"),
}


def quickstart_lines(client_id: str = "acme", lang: str = "en") -> list[str]:
    """Exactly 8 lines: 4 commands for a first client (add, setup, doctor, register in Claude) + 2 for writing:
    install --skip-auth --write (consent family by family, typed name, adds GEM_WRITE=1 in the same run), then the
    separate write token (explicit --client-secret)."""
    head, write_head, claude = _QUICKSTART["fr" if lang == "fr" else "en"]
    return [
        head.format(cid=client_id),
        f"  1. {client_add_command(client_id)}",
        f"  2. {client_setup_command(client_id)}",
        f"  3. {client_doctor_command(client_id)}",
        claude.format(install=client_install_command(client_id, skip_auth=True), code=claude_code_add_command()),
        write_head,
        f"  5. {client_install_command(client_id, skip_auth=True, write=True)}",
        f"  6. {client_setup_command(client_id, write=True)}",
    ]

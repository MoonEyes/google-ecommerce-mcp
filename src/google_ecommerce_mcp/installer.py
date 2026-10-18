"""Guided installation: ask for the account ids, authorize Google, register the server in Claude Desktop.

Run with `google-ecommerce-mcp install`. Every prompt can be answered by a flag, so the same code
serves the interactive install scripts and unattended setups.

Paid media (optional, --skip-ads to leave it out): Google Ads gets its own OAuth token (setup-ads, same client
file), Meta and ChatGPT Ads secrets are typed into a hidden prompt and stored in the OS keyring. Only non-secret
ids ever reach the Claude Desktop config (GADS_CUSTOMER_ID, GADS_LOGIN_CUSTOMER_ID, META_AD_ACCOUNT_ID,
ADS_TRUST_ENV). Rerunning adds what is new to the existing entry and never changes a value without --force.

Order: the Claude Desktop config is read and validated, and every conflict detected, BEFORE any authorization or
secret prompt, so a refusal (exit code 3) really leaves everything untouched.

Multi-client (`install --client X [--pin] [--write]`): the shortest path for one client of the registry, in four
steps: (1) create the client through `client add` (a few ids, never a secret), (2) `setup --client X` (read-only
token in <client>/google; Google Ads, Meta, ChatGPT Ads of that client only), (3) writing offered one family
group at a time (Google properties; with --write also Google Ads, Meta, ChatGPT Ads), default NO, each group with
its own question and typed client name in a real terminal (enable-write + the separate write token
<client>/google-w, whose OAuth client file must be named explicitly), (4) the Claude Desktop entry with a minimal
env: nothing, or GEM_CLIENT (--pin), GEM_WRITE=1 ONLY when a write consent was typed in this very run (then the
package version is pinned too), ADS_TRUST_ENV (--corporate-proxy), GEM_CLIENTS_FILE / GEM_STATE_DIR when this
shell sets them to absolute paths. Same backup and conflict rules; backups are private (mode 600 / user ACL).
"""

from __future__ import annotations

import datetime
import getpass
import json
import os
import platform
import re
import secrets
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE = "google-ecommerce-mcp"  # published on PyPI
MAX_CONFIG_BYTES = 5 * 1024 * 1024  # a Claude Desktop config is a few KB; anything bigger is not one

FIELDS = [
    # (env variable, flag attribute, question, hint)
    ("GA4_PROPERTY_ID", "ga4", "GA4 property ID", "GA4 > Admin > Property settings, digits only (not G-XXXX)"),
    ("GSC_SITE_URL", "gsc", "Search Console property", "sc-domain:example.com or https://www.example.com/"),
    ("MERCHANT_ACCOUNT_ID", "merchant", "Merchant Center account ID", "digits under your store name"),
    ("GTM_CONTAINER_ID", "gtm", "Tag Manager container ID", "GTM-XXXXXXX"),
    ("PAGESPEED_API_KEY", "pagespeed_key", "PageSpeed API key", "optional, avoids the shared quota"),
]
GOOGLE_OAUTH_FIELDS = [f[0] for f in FIELDS[:4]]  # the services that need the Google consent screen

ADS_FIELDS = [
    # (env variable, flag attribute, question, hint)
    ("GADS_CUSTOMER_ID", "gads_customer_id", "Google Ads customer ID", "10 digits at the top right of Google Ads"),
    ("GADS_LOGIN_CUSTOMER_ID", "gads_login_customer_id", "Google Ads manager (MCC) ID",
     "only if you reach the account through a manager account"),
    ("META_AD_ACCOUNT_ID", "meta_account_id", "Meta ad account ID", "digits, act_ allowed, from Ads Manager"),
]
YES = ("y", "yes", "o", "oui")
# Variables that may be echoed in the Claude Code command: ids, paths and switches only, never a secret
# (PAGESPEED_API_KEY included). Any other name in the entry is left out and counted.
SHOWN_ENV = ("GA4_PROPERTY_ID", "GSC_SITE_URL", "MERCHANT_ACCOUNT_ID", "GTM_CONTAINER_ID", "GOOGLE_TOKEN_FILE",
             "GADS_CUSTOMER_ID", "GADS_LOGIN_CUSTOMER_ID", "GADS_TOKEN_FILE", "GADS_API_VERSION",
             "GADS_ALLOW_SENSITIVE_QUERIES", "META_AD_ACCOUNT_ID", "META_API_VERSION", "ADS_TRUST_ENV")


def claude_desktop_config_path() -> Path:
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Claude" / "claude_desktop_config.json"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "Claude" / "claude_desktop_config.json"


def server_entry(env: dict[str, str], uvx: str | None = None, args: list[str] | None = None) -> dict:
    """The mcpServers entry. An absolute uvx path is used because GUI apps often lack the shell PATH.
    args: [PACKAGE] by default; [pinned_package()] when writing is turned on (exact version)."""
    return {
        "command": uvx or shutil.which("uvx") or "uvx",
        "args": list(args) if args else [PACKAGE],
        "env": {key: value for key, value in env.items() if value},
    }


def pinned_package() -> str:
    """google-ecommerce-mcp==<this version>: the uvx argument of an entry with writing on, so a later release is
    never picked up silently by a server that can write."""
    from . import __version__

    return f"{PACKAGE}=={__version__}"


def _ours(args: object) -> bool:
    """True for the args of an entry of this package: [PACKAGE] or [PACKAGE==<version>]."""
    return (isinstance(args, list) and len(args) == 1 and isinstance(args[0], str)
            and (args[0] == PACKAGE or args[0].startswith(PACKAGE + "==")))


class ConfigError(ValueError):
    """The Claude Desktop config cannot be read or written safely; the message says what to do."""


def load_config(config_path: Path) -> dict:
    """The config as a dict ({} when missing or empty). Raises ConfigError, never anything else: too big, not
    UTF-8, not JSON, not an object, mcpServers not an object, or unreadable (permissions)."""
    try:
        if not config_path.exists():
            return {}
        if config_path.stat().st_size > MAX_CONFIG_BYTES:
            raise ConfigError(f"{config_path} is larger than {MAX_CONFIG_BYTES // (1024 * 1024)} MB, which is not a "
                              "Claude Desktop config. Check the --config path.")
        text = config_path.read_text(encoding="utf-8-sig").strip()  # -sig: Notepad / PowerShell add a BOM
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{config_path} is not UTF-8 text. Open it in a text editor, save it as UTF-8, "
                          "then rerun.") from exc
    except OSError as exc:
        raise ConfigError(f"{config_path} could not be read ({type(exc).__name__}). Check its permissions, "
                          "then rerun.") from exc
    try:
        config = json.loads(text) if text else {}
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{config_path} is not valid JSON ({exc}). Fix it or move it away, then rerun.") from exc
    if not isinstance(config, dict):
        raise ConfigError(f"{config_path} must contain a JSON object {{...}}, not {type(config).__name__}. "
                          "Fix it or move it away, then rerun.")
    if "mcpServers" in config and not isinstance(config["mcpServers"], dict):
        raise ConfigError(f'"mcpServers" in {config_path} must be a JSON object {{...}}. Fix it or move the file '
                          "away, then rerun.")
    return config


def _is_additive(old: dict, new: dict, allow_pin: bool = False) -> bool:
    """True when new only adds env variables to old: same command and args, every old env value kept as is.
    allow_pin: the args may also change from [PACKAGE] (or another pinned version) to a pinned version."""
    if not isinstance(old, dict):
        return False
    ignored = {"env", "args"} if allow_pin else {"env"}
    if allow_pin and not (_ours(old.get("args")) and _ours(new.get("args"))):
        return False
    if {k: v for k, v in old.items() if k not in ignored} != {k: v for k, v in new.items() if k not in ignored}:
        return False
    old_env, new_env = old.get("env") or {}, new.get("env") or {}
    return isinstance(old_env, dict) and all(new_env.get(k) == v for k, v in old_env.items())


def _backup_path(config_path: Path) -> Path:
    """config.json.bak-YYYYmmdd-HHMMSS-ffffff, never an existing file (two runs in the same second)."""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    candidate = config_path.with_name(f"{config_path.name}.bak-{stamp}")
    number = 1
    while candidate.exists():
        candidate = config_path.with_name(f"{config_path.name}.bak-{stamp}-{number}")
        number += 1
    return candidate


def _create_private(path: Path, data: bytes, restrict_acl: bool = True) -> None:
    """Create path (it must not exist: O_EXCL) with mode 600 and write data; on Windows the ACL is restricted to
    the current user (best effort, ads_auth._restrict_windows_acl). The Claude config can hold PAGESPEED_API_KEY
    or a GEM_SECRET_* value, so neither its backups nor the temporary file may be readable by others."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    if restrict_acl and os.name == "nt":
        from .ads_auth import _restrict_windows_acl

        _restrict_windows_acl(path)


def _tmp_path(config_path: Path) -> Path:
    """A random, hidden temporary name next to the config (never a predictable .tmp another process could plant)."""
    return config_path.with_name(f".{config_path.name}.{secrets.token_hex(8)}.tmp")


def register(config_path: Path, name: str, entry: dict, force: bool = False, allow_additions: bool = False,
             allow_pin: bool = False) -> str:
    """Add the server to a Claude Desktop config, keeping every other setting.

    Returns the backup path, or '' when no backup was needed (new file, or entry already identical).
    Nothing is written or backed up when the call is refused. allow_additions lets an existing entry gain new
    env variables without --force (no existing value may change); allow_pin also lets its uvx argument become the
    pinned version (writing turned on in this run). The backup is created mode 600 (ACL restricted on Windows) and
    the new content goes through a random temporary file. Raises FileExistsError (refused) or ConfigError
    (unreadable, wrong shape, or not writable: the file is then left as it was, no temporary file left behind).
    """
    config = load_config(config_path)
    servers = config.setdefault("mcpServers", {})
    if servers.get(name) == entry:
        return ""
    if name in servers and not force and not (allow_additions and _is_additive(servers[name], entry, allow_pin)):
        raise FileExistsError(f"A server named '{name}' already exists in {config_path}. "
                              "Choose another --name or pass --force to replace it.")
    backup = ""
    tmp = _tmp_path(config_path)
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        if config_path.exists():
            backup_path = _backup_path(config_path)
            _create_private(backup_path, config_path.read_bytes())
            backup = str(backup_path)
        servers[name] = entry
        _create_private(tmp, (json.dumps(config, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
                        restrict_acl=False)
        os.replace(tmp, config_path)
    except OSError as exc:
        if backup:
            Path(backup).unlink(missing_ok=True)  # nothing changed: no backup of an unchanged file
        raise ConfigError(f"{config_path} could not be written ({type(exc).__name__}). Check that the file is not "
                          "read-only and that Claude Desktop is closed, then rerun.") from exc
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return backup


def existing_entry(config_path: Path, name: str) -> dict | None:
    """The current entry of this server in the Claude Desktop config, or None (missing, unreadable, wrong shape)."""
    try:
        entry = load_config(config_path).get("mcpServers", {}).get(name)
    except ConfigError:
        return None
    return entry if isinstance(entry, dict) else None


def claude_code_command(name: str, env: dict[str, str]) -> str:
    flags = " ".join(f"-e {key}={value}" for key, value in env.items() if value)
    return f"claude mcp add {name} --scope user {flags} -- uvx {PACKAGE}".replace("  ", " ")


def shown_env(env: dict[str, str]) -> tuple[dict[str, str], int]:
    """(variables safe to print, number left out): a whitelist, so a secret under any name never shows."""
    shown = {k: v for k, v in env.items() if k in SHOWN_ENV and v}
    return shown, sum(1 for k, v in env.items() if v and k not in SHOWN_ENV)


def _ask(question: str, hint: str, current: str | None, interactive: bool) -> str:
    if current is not None or not interactive:
        return (current or "").strip()
    answer = input(f"{question} ({hint}), Enter to skip: ")
    return answer.strip()


def _ask_pagespeed(args, interactive: bool) -> str:
    """The PageSpeed key: --pagespeed-key, else PAGESPEED_API_KEY of this terminal, else a hidden prompt."""
    given = getattr(args, "pagespeed_key", None)
    if given is not None:
        return given.strip()
    from_env = (os.getenv("PAGESPEED_API_KEY") or "").strip()
    if from_env:
        print("PageSpeed API key: read from PAGESPEED_API_KEY.")
        return from_env
    if not interactive:
        return ""
    return (getpass.getpass("PageSpeed API key (optional, avoids the shared quota; hidden), Enter to skip: ")
            or "").strip()


def _confirm(question: str) -> bool:
    return input(f"{question} [y/N] ").strip().lower() in YES


# ------------------------------------------------------------------ paid media
class InvalidId(ValueError):
    """An ad account id with the wrong shape (message names the flag, never echoes a secret)."""


GADS_ID = re.compile(r"[0-9]{10}")
META_ID = re.compile(r"[0-9]{3,20}")


def normalize_gads_id(value: str, label: str = "Google Ads customer ID") -> str:
    """123-456-7890 or 1234567890 -> 1234567890 (exactly ten digits, dashes and spaces tolerated)."""
    cleaned = (value or "").strip().replace("-", "").replace(" ", "")
    if not GADS_ID.fullmatch(cleaned):
        raise InvalidId(f"{label} must be 10 digits, for example 123-456-7890")
    return cleaned


def normalize_meta_id(value: str) -> str:
    """act_123 or 123 -> 123."""
    cleaned = (value or "").strip()
    if cleaned.lower().startswith("act_"):
        cleaned = cleaned[4:]
    if not META_ID.fullmatch(cleaned):
        raise InvalidId("Meta ad account ID must be digits, optionally starting with act_")
    return cleaned


_NORMALIZE = {"GADS_CUSTOMER_ID": normalize_gads_id,
              "GADS_LOGIN_CUSTOMER_ID": lambda v: normalize_gads_id(v, "Google Ads manager ID"),
              "META_AD_ACCOUNT_ID": normalize_meta_id}


@dataclass
class AdsPlan:
    """What the user asked for in the paid media step (ids already normalized, no secret)."""

    env: dict[str, str] = field(default_factory=dict)
    openai: bool = False
    proxy: bool = False
    done: list[str] = field(default_factory=list)
    todo: list[tuple[str, str]] = field(default_factory=list)  # (what, exact command)
    effects: list[str] = field(default_factory=list)  # what was already stored on this machine

    @property
    def gads(self) -> bool:
        return bool(self.env.get("GADS_CUSTOMER_ID") or self.env.get("GADS_LOGIN_CUSTOMER_ID"))

    @property
    def requested(self) -> bool:
        return bool(self.env) or self.openai or self.proxy


def ask_ads(args, interactive: bool) -> AdsPlan:
    """Paid media questions (ids only). Raises InvalidId for a malformed id given by flag."""
    plan = AdsPlan(proxy=bool(getattr(args, "corporate_proxy", False)))
    if plan.proxy:
        plan.env["ADS_TRUST_ENV"] = "1"
    if getattr(args, "skip_ads", False):
        return plan
    given = {variable: getattr(args, attr, None) for variable, attr, _, _ in ADS_FIELDS}
    flagged = any(v is not None for v in given.values())
    wanted = flagged
    if interactive and not flagged:
        print("\nPaid media (optional): Google Ads, Meta Ads, ChatGPT Ads.")
        wanted = _confirm("Set up paid media now?")
    if not wanted:
        return plan
    for variable, attr, question, hint in ADS_FIELDS:
        value = given[variable]
        for attempt in range(3):
            answer = _ask(question, hint, value, interactive and not flagged)
            if not answer:
                break
            try:
                plan.env[variable] = _NORMALIZE[variable](answer)
                break
            except InvalidId as exc:
                if not interactive or flagged or attempt == 2:
                    raise
                print(f"  {exc}")
    if interactive and not flagged:
        plan.openai = _confirm("Connect ChatGPT Ads (advertiser API key)?")
    return plan


def token_files(args, merged: dict[str, str]) -> tuple[str | None, str | None]:
    """(Google token file, Google Ads token file) as the server will see them: this run, else the entry, else the
    terminal's environment."""
    google = args.token_file or merged.get("GOOGLE_TOKEN_FILE") or (os.getenv("GOOGLE_TOKEN_FILE") or "").strip()
    ads = (os.getenv("GADS_TOKEN_FILE") or "").strip() or merged.get("GADS_TOKEN_FILE")
    return google or None, ads or None


def _secret_stored(name: str) -> bool:
    from .ads_config import get_secret

    try:
        return bool(get_secret(name))
    except Exception:
        return False


def _store_secret(name: str, label: str, plan: AdsPlan, interactive: bool, command: str) -> None:
    """Hidden prompt then keyring. Never takes the value from the command line, never prints it."""
    from . import ads_auth

    if _secret_stored(name) and not (interactive and _confirm(f"A {label} is already stored. Replace it?")):
        plan.done.append(f"{label}: already stored")
        return
    if not interactive:
        plan.todo.append((f"store the {label} (hidden prompt)", command))
        return
    value = (getpass.getpass(f"Paste the {label} (hidden, Enter to skip): ") or "").strip()
    if not value:
        plan.todo.append((f"store the {label} (hidden prompt)", command))
        return
    try:
        ads_auth.set_secret(name, value)
    except Exception as exc:  # no keyring backend: the message never contains the value
        print(f"  Could not store the {label} ({type(exc).__name__}).")
        plan.todo.append((f"store the {label} (hidden prompt)", command))
        return
    plan.done.append(f"{label}: stored in the operating system keyring")
    plan.effects.append(f"{label} stored in the keyring")


def authorize_ads(args, plan: AdsPlan, interactive: bool, client_secret: str | None, ads_file: str | None) -> None:
    """The paid media effects: separate Google Ads OAuth, hidden prompts for Meta / ChatGPT Ads. Every check that
    can refuse the install (token separation, config conflicts) has already passed."""
    from . import ads_auth, setup_guide as guide

    if plan.gads:
        command = guide.setup_ads_command(ads_file)
        try:
            stored = bool(ads_auth._read_raw(ads_file))
        except Exception:  # unreadable file or no keyring backend: authorize again
            stored = False
        if getattr(args, "skip_auth", False) or (stored and not (interactive and _confirm(
                "A Google Ads token is already stored. Authorize again?"))):
            if stored:
                plan.done.append("Google Ads token: already stored (separate from the Google token)")
            else:
                plan.todo.append(("authorize Google Ads (separate token)", command))
        else:
            secret_file = client_secret
            if not secret_file and interactive:
                secret_file = input("Path to your OAuth client JSON for Google Ads (the same file as for Google): "
                                    ).strip().strip('"')
            if not secret_file or not Path(secret_file).expanduser().is_file():
                print("OAuth client file not found: Google Ads not authorized.")
                plan.todo.append(("authorize Google Ads (separate token)", command))
            else:
                from .ads_config import ADS_SCOPE_NOTE

                print(ADS_SCOPE_NOTE)
                print("Your browser will open the Google consent screen for Google Ads only.")
                try:
                    ads_auth.run_setup_ads(str(Path(secret_file).expanduser()), ads_file)
                    plan.done.append("Google Ads token: stored (separate from the Google token)")
                    plan.effects.append("Google Ads token stored")
                except Exception as exc:  # consent refused, file not an OAuth client, port busy...
                    print(f"Google Ads authorization failed ({type(exc).__name__}).")
                    plan.todo.append(("authorize Google Ads (separate token)", command))
    if plan.env.get("META_AD_ACCOUNT_ID"):
        _store_secret("meta-access-token", "Meta access token", plan, interactive, guide.SET_META)
    if plan.openai:
        _store_secret("openai-ads-key", "ChatGPT Ads key", plan, interactive, guide.SET_OPENAI)


def _summary(env: dict[str, str], plan: AdsPlan, notes: list[str]) -> list[str]:
    """One plain-text summary built from the same checks as `doctor` (offline, booleans and masked ids only)."""
    from . import doctor, setup_guide as guide

    relevant = []
    for service in guide.SERVICE_IDS:
        if service in guide.GOOGLE_SERVICES and env.get(doctor.GOOGLE_ID[service][0]):
            relevant.append(service)
        elif service == "pagespeed" and env.get("PAGESPEED_API_KEY"):
            relevant.append(service)
        elif service == "gads" and (env.get("GADS_CUSTOMER_ID") or env.get("GADS_LOGIN_CUSTOMER_ID")):
            relevant.append(service)
        elif service == "meta" and env.get("META_AD_ACCOUNT_ID"):
            relevant.append(service)
        elif service == "openai" and plan.openai:
            relevant.append(service)
    reports = doctor.offline(tuple(relevant), env)[0] if relevant else []
    configured = [f"{r.name}" + (f" ({r.account})" if r.account else "") for r in reports if r.status == doctor.OK]
    steps: list[tuple[str, str]] = []
    for what, command in plan.todo:
        if command not in {c for _, c in steps}:
            steps.append((what, command))
    known = {command for _, command in steps}
    for r in reports:
        if r.status == doctor.FIX and r.next_step not in known:
            steps.append((f"{r.name}: {r.problem}", r.next_step))
            known.add(r.next_step)
    lines = ["", "Summary", "  Configured: " + (", ".join(configured) if configured else "nothing yet")]
    lines += [f"  {note}" for note in plan.done + notes]
    if steps:
        lines.append("  Still to do:")
        for number, (what, command) in enumerate(steps, 1):
            lines += [f"    {number}. {what}", f"       {command}"]
        lines.append(f"  Next command: {steps[0][1]}")
    else:
        lines.append(f"  Next command: {guide.CLI} doctor --live")
    return lines


def _already_done(effects: list[str]) -> str:
    return f" Already done before this stop: {'; '.join(effects)}." if effects else " Nothing was changed."


def run(args) -> int:
    """Install; Ctrl-C or a closed input ends cleanly with code 130 and says what was already done."""
    effects: list[str] = []
    try:
        if getattr(args, "client", None):
            return _run_client(args, effects)
        return _run(args, effects)
    except (KeyboardInterrupt, EOFError):
        print("\nInstall interrupted." + _already_done(effects) + " Rerun the same command to continue.")
        return 130


def _run(args, effects: list[str]) -> int:
    interactive = sys.stdin.isatty() and not args.yes
    print("google-ecommerce-mcp installer\n")
    config_path = Path(args.config).expanduser() if args.config else claude_desktop_config_path()

    # 0. Read and validate the Claude Desktop config first: a broken file stops here, before anything happens.
    current = None
    if not args.no_desktop:
        try:
            config = load_config(config_path)
        except ConfigError as exc:
            print(f"{exc} Nothing was changed.")
            return 3
        found = config.get("mcpServers", {}).get(args.name)
        if found is not None and not isinstance(found, dict) and not args.force:
            print(f"The '{args.name}' entry in {config_path} is not a JSON object. Fix it, or rerun with --force "
                  "to replace it. Nothing was changed.")
            return 3
        current = found if isinstance(found, dict) else None
    current_env = dict(current.get("env") or {}) if current and isinstance(current.get("env"), dict) else {}

    # 1. Questions (ids only; secrets come later, in hidden prompts, once nothing can refuse the install)
    env = {variable: _ask(question, hint, getattr(args, attr), interactive)
           for variable, attr, question, hint in FIELDS[:4]}
    env["PAGESPEED_API_KEY"] = _ask_pagespeed(args, interactive)
    if args.token_file:
        env["GOOGLE_TOKEN_FILE"] = args.token_file
    google_requested = any(env.get(v) for v in GOOGLE_OAUTH_FIELDS)
    try:
        plan = ask_ads(args, interactive)
    except InvalidId as exc:
        print(f"{exc}. Nothing was changed.")
        return 2
    if not google_requested and not env.get("PAGESPEED_API_KEY") and not plan.requested:
        print("No service configured: give at least one id (GA4, Search Console, Merchant Center, Tag Manager, "
              "Google Ads or Meta Ads), a PageSpeed key, --corporate-proxy, or answer y to ChatGPT Ads.")
        return 2

    # 2. Token separation and config conflicts, still before any effect
    new_values = {**{k: v for k, v in env.items() if v}, **plan.env}
    google_file, ads_file = token_files(args, {**current_env, **new_values})
    if ads_file and google_file and _same_token_file(ads_file, google_file):
        print("GADS_TOKEN_FILE is the same file as the Google token file (GOOGLE_TOKEN_FILE / --token-file). The "
              "Google Ads token must be kept apart: choose another GADS_TOKEN_FILE (or unset it to use the "
              "keyring), then rerun. Nothing was changed.")
        return 2
    if plan.gads and ads_file:
        new_values["GADS_TOKEN_FILE"] = ads_file
    merged = {**current_env, **new_values}
    uvx = current.get("command") if current and current.get("args") == [PACKAGE] else None
    entry = server_entry(merged, uvx=uvx)
    if not args.no_desktop and not args.force:
        conflicts = sorted(k for k, v in new_values.items() if k in current_env and current_env[k] != v)
        if conflicts:
            print(f"'{args.name}' in {config_path} already has a different value for: {', '.join(conflicts)}. "
                  "Nothing was changed. Rerun with --force to replace these values.")
            return 3
        if current is not None and current != entry and not _is_additive(current, entry):
            print(f"A server named '{args.name}' already exists in {config_path} with another command. "
                  "Nothing was changed. Choose another --name or pass --force to replace it.")
            return 3

    # 3. Google authorization (only when a Google service is set in this run)
    client_secret = args.client_secret
    notes: list[str] = []
    if google_requested:
        skip_auth = args.skip_auth
        if not skip_auth and not args.client_secret and interactive:
            from .auth import _read_raw

            try:
                existing = _read_raw(args.token_file)
            except Exception:  # no keyring backend: treat as no token
                existing = None
            if existing and input("A Google token is already stored. Authorize again? [y/N] ").strip().lower() not in YES:
                skip_auth = True
        if skip_auth:
            print("Keeping the stored Google token.")
        else:
            if not client_secret and interactive:
                client_secret = input("Path to your OAuth client JSON (Desktop app, see docs/SETUP-GOOGLE-CLOUD.md): ").strip().strip('"')
            if not client_secret or not Path(client_secret).expanduser().is_file():
                print("OAuth client file not found. Create it first: docs/SETUP-GOOGLE-CLOUD.md. Nothing was changed.")
                return 2
            from . import auth
            from .config import scopes_for

            # Scopes for every Google service of the entry, not only the ones of this run: the new token replaces
            # the old one, which must not lose Merchant Center.
            scopes = scopes_for(merchant=bool(merged.get("MERCHANT_ACCOUNT_ID")),
                                indexing=getattr(args, "with_indexing", False))
            print("Your browser will open the Google consent screen. Tick every box.")
            where = auth.run_setup(str(Path(client_secret).expanduser()), args.token_file, scopes)
            effects.append("Google token stored")
            print(f"Token stored in {where}.")

    # 4. Paid media authorizations (separate Google Ads token, hidden prompts for Meta / ChatGPT Ads)
    if plan.requested:
        try:
            authorize_ads(args, plan, interactive, client_secret, ads_file)
        finally:
            effects.extend(plan.effects)

    # 5. Check the Google token with the values just entered
    if google_requested:
        from .config import Settings
        from . import server

        server.SETTINGS = Settings(*(merged.get(v) or None for v in ("GA4_PROPERTY_ID", "GSC_SITE_URL",
                                                                     "MERCHANT_ACCOUNT_ID", "GTM_CONTAINER_ID",
                                                                     "PAGESPEED_API_KEY", "GOOGLE_TOKEN_FILE")))
        server.TOKENS = server.TokenProvider(server.SETTINGS.token_file)
        status = server.check()
        notes.append("Google token: works" if status.get("token") == "ok"
                     else "Google token: does not work yet (see docs/TROUBLESHOOTING.md)")

    # 6. Register in Claude Desktop (backup first; new variables are added, existing values need --force)
    if args.no_desktop:
        notes.append("Claude Desktop: not modified (--no-desktop)")
    else:
        try:
            backup = register(config_path, args.name, entry, force=args.force, allow_additions=True)
        except (FileExistsError, ConfigError) as exc:
            kept = f" Already done and kept: {'; '.join(effects)}." if effects else ""
            print(f"\n{exc} The Claude Desktop config was not changed.{kept}")
            return 3
        notes.append(f"Claude Desktop: '{args.name}' registered in {config_path}" + (f" (backup: {backup})" if backup else ""))
        notes.append("Quit Claude Desktop completely (tray icon > Quit) and reopen it.")

    for line in _summary(merged, plan, notes):
        print(line)
    shown, hidden = shown_env(merged)
    print("\nClaude Code users can run instead:\n  " + claude_code_command(args.name, shown))
    if hidden:
        print(f"  ({hidden} variable(s) not shown: secrets and unknown names are never printed; add them by hand)")
    print("\nThen ask Claude: \"Run server_status from google-ecommerce\".")
    return 0


# ------------------------------------------------------------------ multi-client install (install --client X)
# The few ids asked by install --client (the full wizard stays `client add X --replace`): (platform, key).
MINIMAL_QUESTIONS = (("ga4", "property_id"), ("gsc", "site_url"), ("merchant", "account_id"), ("gtm", "public_id"),
                     ("google_ads", "customer_id"), ("meta", "ad_account_id"))
# install flag attribute for each registry field it can fill.
INSTALL_FLAGS = {("ga4", "property_id"): "ga4", ("gsc", "site_url"): "gsc", ("merchant", "account_id"): "merchant",
                 ("gtm", "public_id"): "gtm", ("google_ads", "customer_id"): "gads_customer_id",
                 ("google_ads", "login_customer_id"): "gads_login_customer_id",
                 ("meta", "ad_account_id"): "meta_account_id"}
# Variables of the multi-client entry (never a secret): pin, write switch, registry / state paths, proxy.
CLIENT_ENTRY_ENV = ("GEM_CLIENT", "GEM_WRITE", "GEM_CLIENTS_FILE", "GEM_STATE_DIR", "ADS_TRUST_ENV")

_CLIENT_MESSAGES = {
    "title": ("google-ecommerce-mcp : installation du client {cid!r}\n",
              "google-ecommerce-mcp installer, client {cid!r}\n"),
    "step": ("\nÉtape {n}/4 : {what}", "\nStep {n}/4: {what}"),
    "s1": ("créer le client (identifiants seulement, aucun secret)", "create the client (ids only, never a secret)"),
    "s2": ("autoriser Google pour ce client", "authorize Google for this client"),
    "s3": ("écriture (facultatif, NON par défaut)", "writing (optional, default NO)"),
    "s4": ("enregistrer le serveur dans Claude Desktop", "register the server in Claude Desktop"),
    "intro_min": ("Entrée = passer. Le reste plus tard : {command}", "Enter = skip. The rest later: {command}"),
    "client_kept": ("Client {cid!r} déjà enregistré : conservé tel quel.", "Client {cid!r} already registered: kept "
                    "as is."),
    "flags_ignored": ("Identifiants passés en option ignorés pour un client existant ; pour les changer : {command}",
                      "Ids given as flags are ignored for an existing client; to change them: {command}"),
    "no_google": ("Aucun identifiant Google : rien à autoriser.", "No Google id: nothing to authorize."),
    "token_kept": ("Jeton Google (lecture) déjà présent : conservé.", "Google token (read) already stored: kept."),
    "again": ("Un jeton Google (lecture) est déjà enregistré pour {cid}. Autoriser à nouveau ?",
              "A Google token (read) is already stored for {cid}. Authorize again?"),
    "ads_again": ("Un jeton Google Ads est déjà enregistré pour {cid}. Autoriser à nouveau ?",
                  "A Google Ads token is already stored for {cid}. Authorize again?"),
    "ask_secret": ("Chemin du fichier client OAuth (Desktop app, voir docs/SETUP-GOOGLE-CLOUD.md) : ",
                   "Path to your OAuth client JSON (Desktop app, see docs/SETUP-GOOGLE-CLOUD.md): "),
    "browser": ("Votre navigateur s'ouvre ({what}). Cochez toutes les cases.",
                "Your browser opens ({what}). Tick every box."),
    "stored": ("{what} : enregistré dans {where}.", "{what}: stored in {where}."),
    "failed": ("{what} : non terminé ({kind}).", "{what}: not completed ({kind})."),
    "paste": ("Collez le {label} (masqué, Entrée pour passer) : ", "Paste the {label} (hidden, Enter to skip): "),
    "write_intro": (
        "  Écrire permet à l'assistant de PRÉPARER des modifications ({families}) que vous confirmez une par une.\n"
        "  Garde-fous : plafonds par client, création en pause, jamais de suppression ni de publication Tag Manager ;\n"
        "  dépenses et irréversible : approbation hors du chat avec `google-ecommerce-mcp approve <plan_id>`.",
        "  Writing lets the assistant PREPARE changes ({families}) that you confirm one by one.\n"
        "  Guards: per-client caps, creations paused, never a deletion nor a Tag Manager publish;\n"
        "  spending and irreversible changes: approval outside the chat with `google-ecommerce-mcp approve "
        "<plan_id>`."),
    "write_question": ("Activer l'écriture pour {cid} : {group} ?", "Enable writing for {cid}: {group}?"),
    "write_spending_later": (
        "Dépenses ({groups}) : non proposées sans --write ; chacune demande sa propre confirmation : {command}",
        "Spending ({groups}): not offered without --write; each asks for its own confirmation: {command}"),
    "use_found": ("Fichier client OAuth trouvé : {path}. L'utiliser ?", "OAuth client file found: {path}. Use it?"),
    "ask_write_secret": ("Chemin du fichier client OAuth pour le jeton d'ÉCRITURE (jamais deviné) : ",
                         "Path to your OAuth client JSON for the WRITE token (never guessed): "),
    "gem_write_needs_consent": (
        "Écriture déjà activée dans le registre, mais GEM_WRITE=1 n'est PAS ajouté sans votre consentement dans "
        "cette exécution. Pour l'ajouter (la confirmation est redemandée) : {command}",
        "Writing is already enabled in the registry, but GEM_WRITE=1 is NOT added without your consent in this "
        "run. To add it (the confirmation is asked again): {command}"),
    "env_ignored": ("{name}={value!r} de ce terminal ignoré pour l'entrée Claude : {why}. Le serveur lira "
                    "l'emplacement par défaut ; corrigez la variable (chemin absolu) puis relancez.",
                    "{name}={value!r} from this shell ignored for the Claude entry: {why}. The server will read the "
                    "default location; fix the variable (absolute path) and rerun."),
    "pinned": ("Version épinglée dans l'entrée Claude (écriture activée) : {package}.",
               "Version pinned in the Claude entry (writing on): {package}."),
    "write_none": ("Aucune plateforme du client n'a d'identifiant : écriture non proposée.",
                   "No platform of the client has an id: writing not offered."),
    "write_tty": ("Écriture : terminal interactif requis ; plus tard, vous-même : {command}",
                  "Writing: an interactive terminal is required; later, yourself: {command}"),
    "write_later": ("Écriture laissée désactivée. Plus tard : {enable} puis {token}",
                    "Writing left off. Later: {enable} then {token}"),
    "write_on": ("Écriture activée pour {cid} : {families} (consentement donné maintenant : GEM_WRITE=1 ajouté à "
                 "l'entrée Claude).",
                 "Writing enabled for {cid}: {families} (consent given now: GEM_WRITE=1 added to the Claude "
                 "entry)."),
    "gem_write_kept": ("GEM_WRITE vaut {value!r} dans l'entrée Claude : inchangé (--force pour le remplacer).",
                       "GEM_WRITE is {value!r} in the Claude entry: unchanged (--force to replace it)."),
    "legacy_ids": ("{names} dans l'entrée Claude sont ignorés désormais (le registre fait foi).",
                   "{names} in the Claude entry are now ignored (the registry is what counts)."),
}


def _ct(key: str, **values) -> str:
    from .client_cli import lang

    fr, en = _CLIENT_MESSAGES[key]
    return (fr if lang() == "fr" else en).format(**values)


def _stdin_tty() -> bool:
    """True when stdin is a terminal (tests replace this single function)."""
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _add_client_minimal(args, cid: str, interactive: bool) -> int:
    """Create the client through `client add` (its validation, its first-registry rules), asking only the
    MINIMAL_QUESTIONS that no flag answered. `client add`'s own "next steps" are not printed: install runs them."""
    import argparse
    import contextlib
    import io

    from . import client_cli, clients

    namespace = argparse.Namespace(name=cid, label=None, sandbox=False, replace=False, yes=True,
                                   **{flag: None for flag in client_cli.FLAGS.values()})
    for field_key, attr in INSTALL_FLAGS.items():
        setattr(namespace, client_cli.FLAGS[field_key], getattr(args, attr, None))
    if interactive:
        try:
            first = clients.load_registry() is None
        except clients.ClientError:
            first = False
        defaults = clients.legacy_accounts_from_env() if first else {}
        print(_ct("intro_min", command=f"google-ecommerce-mcp client add {cid} --replace"))
        questions = {(q[0], q[1]): q for q in client_cli.QUESTIONS}
        for platform, key in MINIMAL_QUESTIONS:
            flag = client_cli.FLAGS[(platform, key)]
            if getattr(namespace, flag) is not None:
                continue
            _, _, fr, en, example = questions[(platform, key)]
            setattr(namespace, flag, client_cli._ask_field(platform, key, fr, en, example,
                                                           defaults.get(platform, {}).get(key)))
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = client_cli._cmd_add(namespace)
    stop = client_cli._t("next")
    for line in buffer.getvalue().splitlines():
        if line == stop:
            break
        print(line)
    return code


def _shell_path_problem(value: str, is_dir: bool) -> str:
    """'' when value is an ABSOLUTE path that exists (a file for GEM_CLIENTS_FILE, a folder for GEM_STATE_DIR) or
    can be created (its first existing parent is a writable folder); otherwise why it is ignored."""
    path = Path(value)
    if not path.is_absolute():
        return "not an absolute path"
    try:
        if path.exists():
            if is_dir and not path.is_dir():
                return "exists but is not a folder"
            if not is_dir and not path.is_file():
                return "exists but is not a file"
            return ""
        parent = path if is_dir else path.parent
        while not parent.exists():
            if parent.parent == parent:
                return "no existing parent folder"
            parent = parent.parent
        if not parent.is_dir() or not os.access(parent, os.W_OK):
            return f"{parent} is not a writable folder"
    except OSError as exc:
        return f"cannot be checked ({type(exc).__name__})"
    return ""


def _secret_present(ctx, kind: str) -> bool:
    try:
        return bool(ctx.secret(kind))
    except Exception:
        return False


def _run_client(args, effects: list[str]) -> int:
    """install --client X: (1) client add, (2) setup --client (+ Google Ads / Meta / ChatGPT Ads of the client),
    (3) writing offered, default no, (4) Claude Desktop entry with the minimal env. The Claude config is read and
    every conflict detected before any effect (exit 3, nothing changed)."""
    from . import ads_auth, auth, client_cli, clients, doctor, secrets_store, setup_guide as guide
    from .clients import ClientError

    interactive = _stdin_tty() and not args.yes
    try:
        cid = clients.validate_client_id(args.client)
    except ValueError as exc:
        print(f"install --client {args.client!r}: {exc}. Nothing was changed.")
        return 2
    print(_ct("title", cid=cid))
    config_path = Path(args.config).expanduser() if args.config else claude_desktop_config_path()

    # 0. Claude Desktop config first, and every conflict on the variables known now.
    current = None
    if not args.no_desktop:
        try:
            config = load_config(config_path)
        except ConfigError as exc:
            print(f"{exc} Nothing was changed.")
            return 3
        found = config.get("mcpServers", {}).get(args.name)
        if found is not None and not isinstance(found, dict) and not args.force:
            print(f"The '{args.name}' entry in {config_path} is not a JSON object. Fix it, or rerun with --force "
                  "to replace it. Nothing was changed.")
            return 3
        current = found if isinstance(found, dict) else None
    current_env = dict(current.get("env") or {}) if current and isinstance(current.get("env"), dict) else {}
    env_new: dict[str, str] = {}
    if args.pin:
        env_new["GEM_CLIENT"] = cid
    if args.corporate_proxy:
        env_new["ADS_TRUST_ENV"] = "1"
    # The server must read the same registry as this shell, but only an ABSOLUTE path that exists or can be
    # created is copied into the Claude entry (a relative one would resolve elsewhere for Claude Desktop).
    for name, is_dir in (("GEM_CLIENTS_FILE", False), ("GEM_STATE_DIR", True)):
        value = (os.environ.get(name) or "").strip()
        if not value:
            continue
        why = _shell_path_problem(value, is_dir)
        if why:
            print(_ct("env_ignored", name=name, value=value, why=why))
        else:
            env_new[name] = value
    ours = bool(current) and _ours(current.get("args"))
    uvx = current.get("command") if ours else None
    current_args = list(current["args"]) if ours else None  # an existing pin is kept, never removed silently
    if not args.no_desktop and not args.force:
        conflicts = sorted(k for k, v in env_new.items() if k in current_env and current_env[k] != v)
        if conflicts:
            print(f"'{args.name}' in {config_path} already has a different value for: {', '.join(conflicts)}. "
                  "Nothing was changed. Rerun with --force to replace these values.")
            return 3
        if current is not None and not _is_additive(current, server_entry({**current_env, **env_new}, uvx=uvx,
                                                                           args=current_args)):
            print(f"A server named '{args.name}' already exists in {config_path} with another command. "
                  "Nothing was changed. Choose another --name or pass --force to replace it.")
            return 3

    # 1. The client (ids only).
    print(_ct("step", n=1, what=_ct("s1")))
    try:
        data = clients.load_registry()
    except ClientError as exc:
        client_cli._err(exc)
        return 2
    if data and cid in data["clients"]:
        print(_ct("client_kept", cid=cid))
        if any(getattr(args, attr, None) for attr in INSTALL_FLAGS.values()):
            print(_ct("flags_ignored", command=guide.client_add_command(cid, replace=True)))
    else:
        code = _add_client_minimal(args, cid, interactive)
        if code != 0:
            return code
        effects.append(f"client {cid} saved in the registry")
    ctx = client_cli._context(cid)

    # 2. Authorizations of this client only (read token, Google Ads token, pasted secrets).
    print(_ct("step", n=2, what=_ct("s2")))
    done: list[str] = []
    todo: list[tuple[str, str]] = []
    # The OAuth client file: --client-secret, else a typed path, else (read tokens only) a single
    # client_secret*.json found automatically and confirmed [y/N] in a terminal. "explicit" remembers whether the
    # file was named by the user (flag or typed path): only such a file may serve the write token.
    secret_holder: dict[str, object] = {}

    def secret_file(write: bool = False) -> str | None:
        if "path" in secret_holder and (not write or secret_holder.get("explicit")):
            return secret_holder["path"]  # type: ignore[return-value]
        explicit = bool(args.client_secret)
        confirm = (lambda found: _confirm(_ct("use_found", path=found))) if interactive else None
        path, why = auth.find_client_secret(args.client_secret, write=write, confirm=confirm)
        if path is None and interactive:
            answer = input(_ct("ask_write_secret" if write else "ask_secret")).strip().strip('"')
            if answer:
                path, why = auth.find_client_secret(answer)
                explicit = path is not None
        if path is None:
            print(f"  {why}")
        secret_holder.update(path=str(path) if path else None, explicit=explicit)
        return secret_holder["path"]  # type: ignore[return-value]

    def refused_or_failed(what: str, exc: Exception) -> None:
        if isinstance(exc, auth.SetupRefused):  # hostile client file, unexpected scope: message names no secret
            print(f"  {exc}")
        print(_ct("failed", what=what, kind=type(exc).__name__))

    google_platforms = [p for p in auth.GOOGLE_PLATFORMS if ctx.accounts(p)]
    read_command = guide.client_setup_command(cid, indexing=getattr(args, "with_indexing", False))
    read_scopes = auth.read_scopes_for(ctx, indexing=getattr(args, "with_indexing", False))
    capable = auth.write_capable(read_scopes)
    read_label = ("Google, read + " + ", ".join(s.rsplit("/", 1)[-1] for s in capable) + " (NOT read-only)"
                  if capable else "Google, read-only")
    if not google_platforms:
        print(_ct("no_google"))
    elif args.skip_auth or (_secret_present(ctx, "google") and not (interactive and _confirm(_ct("again", cid=cid)))):
        if _secret_present(ctx, "google"):
            done.append(_ct("token_kept"))
        else:
            todo.append(("authorize Google (read token)", read_command))
    else:
        path = secret_file()
        if path is None:
            todo.append(("authorize Google (read token)", read_command))
        else:
            if capable:  # Merchant Center / Indexing: Google has no read-only scope, say it before the consent
                print(auth.read_token_warning(cid, read_scopes, client_cli.lang()))
            print(_ct("browser", what=read_label))
            try:
                where, missing = auth.run_client_setup(ctx, path, scopes=read_scopes)
                effects.append("Google read token stored")
                done.append(_ct("stored", what="Google token (read" + (", NOT read-only)" if capable else ")"),
                                where=where))
                if missing:
                    todo.append(("grant every Google permission (some boxes were not ticked)", read_command))
            except Exception as exc:  # consent refused, not an OAuth client file, port busy, no keyring backend
                refused_or_failed("Google token (read)", exc)
                todo.append(("authorize Google (read token)", read_command))
    if ctx.accounts("google_ads") and not args.skip_ads:
        ads_command = guide.client_setup_ads_command(cid)
        stored = _secret_present(ctx, "gads")
        if args.skip_auth or (stored and not (interactive and _confirm(_ct("ads_again", cid=cid)))):
            if stored:
                done.append("Google Ads token: already stored (separate)")
            else:
                todo.append(("authorize Google Ads (separate token)", ads_command))
        else:
            path = secret_file()
            if path is None:
                todo.append(("authorize Google Ads (separate token)", ads_command))
            else:
                from .ads_config import ADS_SCOPE_NOTE

                print(ADS_SCOPE_NOTE)
                print(_ct("browser", what="Google Ads only"))
                try:
                    where = ads_auth.run_client_setup_ads(ctx, path)
                    effects.append("Google Ads token stored")
                    done.append(_ct("stored", what="Google Ads token", where=where))
                except Exception as exc:
                    refused_or_failed("Google Ads token", exc)
                    todo.append(("authorize Google Ads (separate token)", ads_command))
    for platform, kind, label in (("meta", "meta", "Meta access token"), ("openai_ads", "openai", "ChatGPT Ads key")):
        if not ctx.accounts(platform) or args.skip_ads:
            continue
        command = guide.client_secret_command(cid, kind)
        if _secret_present(ctx, kind):
            done.append(f"{label}: already stored")
            continue
        value = (getpass.getpass(_ct("paste", label=label)) or "").strip() if interactive else ""
        if not value:
            todo.append((f"store the {label} (hidden prompt)", command))
            continue
        try:
            where = secrets_store.set_secret(cid, kind, value, name=ctx._secret_names.get(kind))
        except Exception as exc:  # no keyring backend: the message never contains the value
            print(_ct("failed", what=label, kind=type(exc).__name__))
            todo.append((f"store the {label} (hidden prompt)", command))
            continue
        effects.append(f"{label} stored")
        done.append(_ct("stored", what=label, where=where))

    # 3. Writing: offered one family group at a time, default NO; each group needs its own [y/N] AND the typed
    # client name, in a real terminal (stdin and stdout); never under --yes. Without --write only the Google
    # properties are offered. GEM_WRITE=1 is added ONLY when a consent was typed in THIS run (consented).
    print(_ct("step", n=3, what=_ct("s3")))
    groups = [(gid, label, [f for f in families if clients.default_account_allow(cid, f)], spending)
              for gid, label, families, spending in guide.WRITE_GROUPS]
    groups = [g for g in groups if g[2]]
    already_on = any(ctx.policy(f).enabled for f in clients.WRITE_PLATFORMS)
    consented: list[str] = []  # families enabled by a consent typed in this run
    install_write = guide.client_install_command(cid, skip_auth=True, write=True)
    if not groups:
        print(_ct("write_none"))
    else:
        offered = [g for g in groups if not g[3] or args.write]
        print(_ct("write_intro", families=", ".join(f for g in offered for f in g[2]) or "-"))
        if not (interactive and client_cli._is_tty()):
            print(_ct("write_tty", command=install_write))
        else:
            for gid, label, families, _spending in offered:
                if not _confirm(_ct("write_question", cid=cid, group=label)):
                    continue
                accounts = ", ".join(clients.mask_id(a) for f in families
                                     for a in clients.default_account_allow(cid, f))
                print(client_cli._t("write_warn", family=", ".join(families), name=cid, accounts=accounts))
                if not ctx.sandbox:
                    print(client_cli._t("sandbox_note"))
                if client_cli._ask(client_cli._t("write_confirm", name=cid)).strip().lower() != cid:
                    print(client_cli._t("aborted"))
                    continue
                for family in families:
                    clients.set_write(cid, family, True)
                consented.extend(families)
                effects.append(f"writing enabled for {cid}: {', '.join(families)}")
                ctx = client_cli._context(cid)
                if gid != "google":
                    continue
                write_command = guide.client_setup_command(cid, write=True)
                if args.skip_auth:
                    todo.append(("authorize the separate Google write token", write_command))
                    continue
                print(auth.write_token_warning(ctx, client_cli.lang()))
                path = secret_file(write=True)  # explicit file only, never one found automatically
                if path is None:
                    todo.append(("authorize the separate Google write token", write_command))
                    continue
                print(_ct("browser", what="Google, write, separate token"))
                try:
                    where, missing = auth.run_client_setup(ctx, path, write=True)
                    effects.append("Google write token stored")
                    done.append(_ct("stored", what="Google token (write, separate)", where=where))
                    if missing:
                        todo.append(("grant every Google edit permission", write_command))
                except Exception as exc:
                    refused_or_failed("Google token (write)", exc)
                    todo.append(("authorize the separate Google write token", write_command))
            if consented:
                done.append(_ct("write_on", cid=cid, families=", ".join(consented)))
            else:
                first = offered[0][2][0] if offered else groups[0][2][0]
                print(_ct("write_later", enable=guide.enable_write_command(cid, first),
                          token=guide.client_setup_command(cid, write=True)))
        later = [g[1] for g in groups if g[3] and not args.write]
        if later:
            print(_ct("write_spending_later", groups=", ".join(later), command=install_write))
    notes: list[str] = []
    pin = False
    if consented:
        if current_env.get("GEM_WRITE") not in (None, "1") and not args.force:
            notes.append(_ct("gem_write_kept", value=current_env.get("GEM_WRITE")))
        else:
            env_new["GEM_WRITE"] = "1"
            pin = True
    elif already_on and current_env.get("GEM_WRITE") != "1":
        # A policy enabled earlier is not a consent given now: never add GEM_WRITE silently (nor under --yes).
        print(_ct("gem_write_needs_consent", command=install_write))
        todo.append(("turn on GEM_WRITE=1 (asks your consent again, family by family)", install_write))

    # 4. Claude Desktop (backup first; additions only unless --force; the version is pinned when writing is on).
    print(_ct("step", n=4, what=_ct("s4")))
    merged = {**current_env, **env_new}
    entry_args = [pinned_package()] if pin else current_args
    entry = server_entry(merged, uvx=uvx, args=entry_args)
    if pin:
        notes.append(_ct("pinned", package=entry["args"][0]))
    legacy_ids = sorted(k for k in GOOGLE_OAUTH_FIELDS + ["GADS_CUSTOMER_ID", "META_AD_ACCOUNT_ID"]
                        if current_env.get(k))
    if legacy_ids:
        notes.append(_ct("legacy_ids", names=", ".join(legacy_ids)))
    if args.no_desktop:
        notes.append("Claude Desktop: not modified (--no-desktop)")
    else:
        try:
            backup = register(config_path, args.name, entry, force=args.force, allow_additions=True, allow_pin=pin)
        except (FileExistsError, ConfigError) as exc:
            kept = f" Already done and kept: {'; '.join(effects)}." if effects else ""
            print(f"\n{exc} The Claude Desktop config was not changed.{kept}")
            return 3
        notes.append(f"Claude Desktop: '{args.name}' registered in {config_path}"
                     + (f" (backup: {backup})" if backup else ""))
        notes.append("Quit Claude Desktop completely (tray icon > Quit) and reopen it.")

    # Summary: the doctor's own offline rows for this client.
    rows = doctor.client_rows(client_cli._context(cid))
    ok = [f"{r.name} ({r.account})" if r.account else r.name for r in rows if r.status == doctor.OK]
    steps = list(dict.fromkeys(todo))
    known = {command for _, command in steps}
    for r in rows:
        if r.status == doctor.FIX and r.next_step not in known:
            steps.append((r.problem, r.next_step))
            known.add(r.next_step)
    print("\nSummary")
    print(f"  Client: {cid}   Configured: " + (", ".join(ok) if ok else "nothing yet"))
    for line in done + notes:
        print(f"  {line}")
    if steps:
        print("  Still to do:")
        for number, (what, command) in enumerate(steps, 1):
            print(f"    {number}. {what}\n       {command}")
        print(f"  Next command: {steps[0][1]}")
    else:
        print(f"  Next command: {guide.client_doctor_command(cid, live=True)}")
    shown = {k: v for k, v in merged.items() if k in CLIENT_ENTRY_ENV and v}
    print("\nClaude Code users can run instead:\n  "
          + guide.claude_code_add_command(args.name, shown, package=entry["args"][0]))
    print(f"\nThen ask Claude: \"Run client_check for {cid}\".")
    return 0


def _same_token_file(a: str, b: str) -> bool:
    from .ads_auth import _same_file

    try:
        return _same_file(a, b)
    except (OSError, RuntimeError):
        return os.path.normcase(a) == os.path.normcase(b)

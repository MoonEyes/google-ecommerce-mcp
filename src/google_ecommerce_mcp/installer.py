"""Guided installation: ask for the account ids, authorize Google, register the server in Claude Desktop.

Run with `google-ecommerce-mcp install`. Every prompt can be answered by a flag, so the same code
serves the interactive install scripts and unattended setups.

Paid media (optional, --skip-ads to leave it out): Google Ads gets its own OAuth token (setup-ads, same client
file), Meta and ChatGPT Ads secrets are typed into a hidden prompt and stored in the OS keyring. Only non-secret
ids ever reach the Claude Desktop config (GADS_CUSTOMER_ID, GADS_LOGIN_CUSTOMER_ID, META_AD_ACCOUNT_ID,
ADS_TRUST_ENV). Rerunning adds what is new to the existing entry and never changes a value without --force.

Order: the Claude Desktop config is read and validated, and every conflict detected, BEFORE any authorization or
secret prompt, so a refusal (exit code 3) really leaves everything untouched.
"""

from __future__ import annotations

import datetime
import getpass
import json
import os
import platform
import re
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


def server_entry(env: dict[str, str], uvx: str | None = None) -> dict:
    """The mcpServers entry. An absolute uvx path is used because GUI apps often lack the shell PATH."""
    return {
        "command": uvx or shutil.which("uvx") or "uvx",
        "args": [PACKAGE],
        "env": {key: value for key, value in env.items() if value},
    }


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


def _is_additive(old: dict, new: dict) -> bool:
    """True when new only adds env variables to old: same command and args, every old env value kept as is."""
    if not isinstance(old, dict) or {k: v for k, v in old.items() if k != "env"} != \
            {k: v for k, v in new.items() if k != "env"}:
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


def register(config_path: Path, name: str, entry: dict, force: bool = False, allow_additions: bool = False) -> str:
    """Add the server to a Claude Desktop config, keeping every other setting.

    Returns the backup path, or '' when no backup was needed (new file, or entry already identical).
    Nothing is written or backed up when the call is refused. allow_additions lets an existing entry gain new
    env variables without --force (no existing value may change). Raises FileExistsError (refused) or
    ConfigError (unreadable, wrong shape, or not writable: the file is then left as it was, no .tmp left behind).
    """
    config = load_config(config_path)
    servers = config.setdefault("mcpServers", {})
    if servers.get(name) == entry:
        return ""
    if name in servers and not force and not (allow_additions and _is_additive(servers[name], entry)):
        raise FileExistsError(f"A server named '{name}' already exists in {config_path}. "
                              "Choose another --name or pass --force to replace it.")
    backup = ""
    tmp = config_path.with_suffix(".tmp")
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        if config_path.exists():
            backup_path = _backup_path(config_path)
            shutil.copy2(config_path, backup_path)
            backup = str(backup_path)
        servers[name] = entry
        tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
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


def _same_token_file(a: str, b: str) -> bool:
    from .ads_auth import _same_file

    try:
        return _same_file(a, b)
    except (OSError, RuntimeError):
        return os.path.normcase(a) == os.path.normcase(b)

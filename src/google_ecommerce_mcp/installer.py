"""Guided installation: ask for the account ids, authorize Google, register the server in Claude Desktop.

Run with `google-ecommerce-mcp install`. Every prompt can be answered by a flag, so the same code
serves the interactive install scripts and unattended setups.
"""

from __future__ import annotations

import datetime
import json
import os
import platform
import shutil
import sys
from pathlib import Path

REPO = "https://github.com/MoonEyes/google-ecommerce-mcp/archive/refs/heads/main.zip"  # no git needed

FIELDS = [
    # (env variable, flag attribute, question, hint)
    ("GA4_PROPERTY_ID", "ga4", "GA4 property ID", "GA4 > Admin > Property settings, digits only (not G-XXXX)"),
    ("GSC_SITE_URL", "gsc", "Search Console property", "sc-domain:example.com or https://www.example.com/"),
    ("MERCHANT_ACCOUNT_ID", "merchant", "Merchant Center account ID", "digits under your store name"),
    ("GTM_CONTAINER_ID", "gtm", "Tag Manager container ID", "GTM-XXXXXXX"),
    ("PAGESPEED_API_KEY", "pagespeed_key", "PageSpeed API key", "optional, avoids the shared quota"),
]


def claude_desktop_config_path() -> Path:
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Claude" / "claude_desktop_config.json"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "Claude" / "claude_desktop_config.json"


def server_entry(env: dict[str, str], uvx: str | None = None) -> dict:
    """The mcpServers entry. An absolute uvx path is used because GUI apps often lack the shell PATH."""
    return {
        "command": uvx or shutil.which("uvx") or "uvx",
        "args": ["--from", REPO, "google-ecommerce-mcp"],
        "env": {key: value for key, value in env.items() if value},
    }


def register(config_path: Path, name: str, entry: dict, force: bool = False) -> str:
    """Add the server to a Claude Desktop config, keeping every other setting.

    Returns the backup path, or '' when no backup was needed (new file, or entry already identical).
    Nothing is written or backed up when the call is refused.
    """
    config: dict = {}
    if config_path.exists():
        text = config_path.read_text(encoding="utf-8-sig").strip()  # -sig: Notepad / PowerShell add a BOM
        try:
            config = json.loads(text) if text else {}
        except json.JSONDecodeError as exc:
            raise ValueError(f"{config_path} is not valid JSON ({exc}). Fix it or move it away, then rerun.") from exc
    servers = config.setdefault("mcpServers", {})
    if servers.get(name) == entry:
        return ""
    if name in servers and not force:
        raise FileExistsError(f"A server named '{name}' already exists in {config_path}. "
                              "Choose another --name or pass --force to replace it.")
    backup = ""
    if config_path.exists():
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = config_path.with_name(f"{config_path.name}.bak-{stamp}")
        shutil.copy2(config_path, backup_path)
        backup = str(backup_path)
    servers[name] = entry
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, config_path)
    return backup


def claude_code_command(name: str, env: dict[str, str]) -> str:
    flags = " ".join(f"-e {key}={value}" for key, value in env.items() if value)
    return f"claude mcp add {name} --scope user {flags} -- uvx --from {REPO} google-ecommerce-mcp".replace("  ", " ")


def _ask(question: str, hint: str, current: str | None, interactive: bool) -> str:
    if current is not None or not interactive:
        return (current or "").strip()
    answer = input(f"{question} ({hint}), Enter to skip: ")
    return answer.strip()


def run(args) -> int:
    interactive = sys.stdin.isatty() and not args.yes
    print("google-ecommerce-mcp installer\n")

    env = {variable: _ask(question, hint, getattr(args, attr), interactive) for variable, attr, question, hint in FIELDS}
    if args.token_file:
        env["GOOGLE_TOKEN_FILE"] = args.token_file
    if not any(env.get(v) for v, *_ in FIELDS[:4]):
        print("No service configured: give at least one id (GA4, Search Console, Merchant Center or Tag Manager).")
        return 2

    # 1. Google authorization
    skip_auth = args.skip_auth
    if not skip_auth and not args.client_secret and interactive:
        from .auth import _read_raw

        try:
            existing = _read_raw(args.token_file)
        except Exception:  # no keyring backend: treat as no token
            existing = None
        if existing and input("A Google token is already stored. Authorize again? [y/N] ").strip().lower() not in ("y", "yes", "o", "oui"):
            skip_auth = True
    if skip_auth:
        print("Keeping the stored Google token.")
    else:
        client_secret = args.client_secret
        if not client_secret and interactive:
            client_secret = input("Path to your OAuth client JSON (Desktop app, see docs/SETUP-GOOGLE-CLOUD.md): ").strip().strip('"')
        if not client_secret or not Path(client_secret).expanduser().is_file():
            print(f"OAuth client file not found: {client_secret!r}. Create it first: docs/SETUP-GOOGLE-CLOUD.md")
            return 2
        from .auth import run_setup

        print("Your browser will open the Google consent screen. Tick every box.")
        where = run_setup(str(Path(client_secret).expanduser()), args.token_file)
        print(f"Token stored in {where}.")

    # 2. Check with the values just entered
    from .config import Settings
    from . import server

    server.SETTINGS = Settings(*(env.get(v) or None for v in ("GA4_PROPERTY_ID", "GSC_SITE_URL", "MERCHANT_ACCOUNT_ID",
                                                               "GTM_CONTAINER_ID", "PAGESPEED_API_KEY", "GOOGLE_TOKEN_FILE")))
    server.TOKENS = server.TokenProvider(server.SETTINGS.token_file)
    status = server.check()
    print("\nCheck:", json.dumps(status, indent=2))
    if status.get("token") != "ok":
        print("The token does not work yet; the server is registered anyway. See docs/TROUBLESHOOTING.md.")

    # 3. Register in Claude Desktop
    if args.no_desktop:
        print("\nClaude Desktop not modified (--no-desktop).")
    else:
        config_path = Path(args.config).expanduser() if args.config else claude_desktop_config_path()
        try:
            backup = register(config_path, args.name, server_entry(env), force=args.force)
        except (FileExistsError, ValueError) as exc:
            print(f"\n{exc}")
            return 3
        print(f"\nRegistered '{args.name}' in {config_path}" + (f" (backup: {backup})" if backup else ""))
        print("Quit Claude Desktop completely (tray icon > Quit) and reopen it.")

    print("\nClaude Code users can run instead:\n  " + claude_code_command(args.name, env))
    print("\nThen ask Claude: \"Run server_status from google-ecommerce\".")
    return 0

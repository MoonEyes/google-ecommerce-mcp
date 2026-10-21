"""`google-ecommerce-mcp client pilot-check <client>`: is this client ready for a read-only pilot?

Offline: no network call, no tool call. It reads the client registry, the names and presence of the keyring entries
(a stored value is only tested for presence and scopes, never printed, never kept in the result), this shell's
environment and the server entry of the Claude Desktop config. It prints one table and a last line:

    Pilot: ready for read-only use          exit code 0
    Pilot: not ready, N thing(s) to fix     exit code 1, each with its exact command

What blocks the pilot ("to fix"):
    - GA4 or Search Console has no id, or its read token is missing, unreadable or lacks the scope;
    - Merchant Center has an id but no usable read token (Merchant Center without an id is optional);
    - a write family has write.<family>.enabled = true in the registry;
    - GEM_WRITE is set (this shell, or the server entry of the Claude Desktop config).
What is only advised (shown, never blocks): the Claude Desktop entry is not pinned to an exact version, was not
found, or is pinned to another client. A pilot that runs from Claude Code has no such entry.

The token states and the scope checks are the doctor's (doctor._token_state), so both commands agree."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import doctor
from . import setup_guide as guide

__all__ = ["check", "render", "run"]

# (platform key in the registry, label, required for the pilot)
PILOT_PLATFORMS = (("ga4", "GA4", True), ("gsc", "Search Console", True), ("merchant", "Merchant Center", False))
SERVER_NAME = "google-ecommerce"
PACKAGE = "google-ecommerce-mcp"
READY = "Pilot: ready for read-only use"


def _write_families(ctx: Any) -> list[str]:
    """Families with write.<family>.enabled true. Every family the registry knows, not only the pilot's three."""
    from .clients import WRITE_PLATFORMS

    return [family for family in WRITE_PLATFORMS if ctx.policy(family).enabled]


def _read_token_state(ctx: Any, platform: str) -> str:
    """ok / missing / scope / unreadable. Only the state leaves this function, never the token."""
    try:
        raw = ctx.secret("google")
        error = None
    except Exception as exc:  # no keyring backend, locked keychain, unreadable token file
        raw, error = None, type(exc).__name__
    return doctor._token_state(raw, error, (doctor.GOOGLE_SCOPE[platform],))


def _is_ours(args: Any) -> bool:
    return (isinstance(args, list) and len(args) == 1 and isinstance(args[0], str)
            and (args[0] == PACKAGE or args[0].startswith(PACKAGE + "==")))


def read_claude_entry(config_path: Path | None, client: str, server_name: str | None) -> dict:
    """The server entry of Claude Desktop that serves this client, as plain data. Never raises.

    server_name given: only that entry. Otherwise the entry of this package pinned to the client (GEM_CLIENT), else
    the one called google-ecommerce, else the only one of this package."""
    out: dict[str, Any] = {"checked": False, "found": False, "name": None, "args": None, "pinned_version": None,
                           "env": {}, "client_pin": None, "problem": ""}
    if config_path is None:
        return out
    from .installer import ConfigError, load_config

    if not config_path.exists():
        out["problem"] = f"Claude Desktop config not found ({config_path})"
        return out
    try:
        servers = load_config(config_path).get("mcpServers", {})
    except ConfigError as exc:
        out["problem"] = f"Claude Desktop config could not be read ({type(exc.__cause__).__name__ if exc.__cause__ else 'wrong shape or too large'})"
        return out
    out["checked"] = True
    ours = {name: e for name, e in servers.items() if isinstance(e, dict) and _is_ours(e.get("args"))}
    if server_name:
        name = server_name if server_name in servers and isinstance(servers[server_name], dict) else None
    else:
        pinned = [n for n, e in ours.items()
                  if isinstance(e.get("env"), dict) and str(e["env"].get("GEM_CLIENT") or "").strip().lower() == client]
        name = (pinned[0] if pinned else SERVER_NAME if SERVER_NAME in ours
                else next(iter(ours)) if len(ours) == 1 else None)
    if name is None:
        return out
    entry = servers[name]
    env = entry.get("env") if isinstance(entry.get("env"), dict) else {}
    args = entry.get("args")
    out.update(found=True, name=name, args=args if isinstance(args, list) else None,
               env={str(k): str(v) for k, v in env.items() if v is not None},
               client_pin=str(env.get("GEM_CLIENT") or "").strip().lower() or None)
    if _is_ours(args) and "==" in args[0]:
        out["pinned_version"] = args[0].split("==", 1)[1]
    return out


def check(ctx: Any, entry: dict, environ: dict[str, str] | None = None, config_path: Path | None = None) -> dict:
    """The whole check as data. Booleans, token states, masked ids and commands: no secret, no full id."""
    environ = os.environ if environ is None else environ
    cid = ctx.client_id
    fixes: list[dict] = []
    advice: list[str] = []
    platforms: list[dict] = []
    for key, label, required in PILOT_PLATFORMS:
        values = ctx.accounts(key)
        id_set = bool(values)
        token = _read_token_state(ctx, key)
        row = {"platform": key, "name": label, "required": required, "id_set": id_set,
               "account": doctor._primary_account(ctx, key) if id_set else "", "read_token": token, "status": "ok"}
        if not id_set and required:
            row["status"] = "fix"
            fixes.append({"problem": f"{label}: no id set",
                          "command": guide.client_add_command(cid, replace=True)})
        elif not id_set:
            row["status"] = "skipped"
        elif token != "ok":
            row["status"] = "fix"
            why = {"missing": "no read token stored", "scope": "the stored token does not include this service",
                   "unreadable": "the stored token cannot be read"}[token]
            fixes.append({"problem": f"{label}: {why}", "command": guide.client_setup_command(cid)})
        platforms.append(row)
    # setup is one consent for every Google service: list the command once
    seen: set[str] = set()
    fixes = [f for f in fixes if not (f["command"] in seen or seen.add(f["command"]))]

    enabled = _write_families(ctx)
    for family in enabled:
        fixes.append({"problem": f"writing is enabled for {family}",
                      "command": f"{guide.CLI} client disable-write {cid} {family}"})
    gem_write = {"shell": (environ.get("GEM_WRITE") or "").strip() or None,
                 "claude_entry": (entry.get("env", {}).get("GEM_WRITE") or "").strip() or None}
    if gem_write["shell"]:
        fixes.append({"problem": "GEM_WRITE is set in this shell's environment",
                      "command": "unset GEM_WRITE   (PowerShell: Remove-Item Env:GEM_WRITE)"})
    if gem_write["claude_entry"]:
        where = f" in {config_path}" if config_path else ""
        fixes.append({"problem": f"GEM_WRITE is set in the Claude Desktop entry {entry.get('name')!r}",
                      "command": f"delete the GEM_WRITE line from the env of that entry{where}, then quit and "
                                 "reopen Claude Desktop"})

    if entry.get("problem"):
        advice.append(f"{entry['problem']}: the server entry was not checked (fine if you use Claude Code)")
    elif not entry["checked"]:
        advice.append("the Claude Desktop config was not checked")
    elif not entry["found"]:
        advice.append("no Claude Desktop entry found for this client: "
                      f"{guide.client_install_command(cid, skip_auth=True)} --pin")
    else:
        if entry["pinned_version"] is None:
            from . import __version__

            advice.append(f"entry {entry['name']!r} is not pinned to an exact version: set its args to "
                          f"[\"{PACKAGE}=={__version__}\"] so a later release is not picked up silently")
        if entry["client_pin"] and entry["client_pin"] != cid:
            advice.append(f"entry {entry['name']!r} is pinned (GEM_CLIENT) to {entry['client_pin']!r}, not to {cid!r}")
        elif not entry["client_pin"] and entry["name"] != SERVER_NAME:
            advice.append(f"entry {entry['name']!r} is not pinned to a client (GEM_CLIENT): "
                          f"{guide.client_install_command(cid, skip_auth=True)} --pin --force")
    return {"client_id": cid, "legacy": ctx.legacy,
            "platforms": platforms,
            "write": {"families_enabled": enabled, "gem_write": gem_write},
            "claude_desktop": {k: entry[k] for k in ("checked", "found", "name", "pinned_version", "client_pin")},
            "fixes": fixes, "advice": advice, "ready": not fixes}


def render(result: dict) -> str:
    def yes(flag: bool) -> str:
        return "yes" if flag else "NO"

    lines = [f"{guide.CLI} client pilot-check {result['client_id']} (offline: nothing is sent, no secret is shown)", ""]
    header = f"{'Service':<17}{'Id set':<8}{'Read token':<12}{'Account':<14}Status"
    lines += [header, "-" * len(header)]
    labels = {"ok": "OK", "fix": "FIX", "skipped": "skipped (optional)"}
    for p in result["platforms"]:
        token = p["read_token"] if p["id_set"] or p["read_token"] == "ok" else "-"
        lines.append(f"{p['name']:<17}{yes(p['id_set']):<8}{token:<12}{(p['account'] or '-'):<14}{labels[p['status']]}")
    write = result["write"]
    lines.append("")
    if write["families_enabled"]:
        lines.append(f"Writing: ON for {', '.join(write['families_enabled'])}")
    else:
        lines.append("Writing: OFF for every family (write.<family>.enabled all false)")
    gem = write["gem_write"]
    where = [w for w, v in (("this shell", gem["shell"]), ("the Claude Desktop entry", gem["claude_entry"])) if v]
    lines.append(f"GEM_WRITE: set in {' and '.join(where)}" if where else "GEM_WRITE: unset")
    cd = result["claude_desktop"]
    if not cd["checked"]:
        lines.append("Claude Desktop entry: not checked")
    elif not cd["found"]:
        lines.append("Claude Desktop entry: not found")
    elif cd["pinned_version"]:
        lines.append(f"Claude Desktop entry: {cd['name']!r} pinned to {PACKAGE}=={cd['pinned_version']}")
    else:
        lines.append(f"Claude Desktop entry: {cd['name']!r} NOT pinned to an exact version")
    if result["advice"]:
        lines += ["", "Advice (does not block the pilot):"] + [f"  - {a}" for a in result["advice"]]
    lines.append("")
    if result["ready"]:
        lines.append(READY)
    else:
        n = len(result["fixes"])
        lines.append(f"Pilot: not ready, {n} thing{'s' if n != 1 else ''} to fix (one at a time, then run it again):")
        for number, fix in enumerate(result["fixes"], 1):
            lines += [f"  {number}. {fix['problem']}", f"     {fix['command']}"]
    return "\n".join(lines)


def run(ctx: Any, config: str | None = None, server_name: str | None = None, as_json: bool = False) -> int:
    """Print the check for ctx and return 0 (ready) or 1 (not ready)."""
    from .installer import claude_desktop_config_path

    config_path = Path(config).expanduser() if config else claude_desktop_config_path()
    entry = read_claude_entry(config_path, ctx.client_id, server_name)
    result = check(ctx, entry, config_path=config_path)
    print(json.dumps(result, indent=2, ensure_ascii=False) if as_json else render(result))
    return 0 if result["ready"] else 1

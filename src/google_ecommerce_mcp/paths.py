"""Where google-ecommerce-mcp keeps its own files, and how it writes them privately.

    registry   %APPDATA%\\google-ecommerce-mcp\\clients.json (Windows) or ~/.config/google-ecommerce-mcp/clients.json
               ($XDG_CONFIG_HOME honoured); GEM_CLIENTS_FILE overrides the whole path.
    state      <config dir>/state (plans, journal); GEM_STATE_DIR overrides it.

Files are written atomically (temporary file in the same folder, fsync, os.replace) and created mode 600; on
Windows, where mode bits do nothing, the ACL is restricted to the current user with the same icacls logic as
ads_auth (absolute icacls.exe path, no shell, best effort)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

__all__ = ["APP_DIR_NAME", "CLIENTS_FILE_ENV", "STATE_DIR_ENV", "config_dir", "clients_file", "state_dir",
           "ensure_private_dir", "write_private_atomic", "restrict_windows_acl"]

APP_DIR_NAME = "google-ecommerce-mcp"
CLIENTS_FILE_ENV = "GEM_CLIENTS_FILE"
STATE_DIR_ENV = "GEM_STATE_DIR"
_IS_WINDOWS = os.name == "nt"


def _env_path(name: str) -> Path | None:
    value = (os.environ.get(name) or "").strip()
    return Path(value).expanduser() if value else None


def config_dir() -> Path:
    """Per-user configuration folder (read from the environment at each call, never frozen at import)."""
    if _IS_WINDOWS:
        base = (os.environ.get("APPDATA") or "").strip()
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    else:
        base = (os.environ.get("XDG_CONFIG_HOME") or "").strip()
        root = Path(base) if base else Path.home() / ".config"
    return root / APP_DIR_NAME


def clients_file() -> Path:
    """Path of the client registry: GEM_CLIENTS_FILE, else <config dir>/clients.json."""
    return _env_path(CLIENTS_FILE_ENV) or config_dir() / "clients.json"


def state_dir() -> Path:
    """Folder for plans and the write journal: GEM_STATE_DIR, else <config dir>/state."""
    return _env_path(STATE_DIR_ENV) or config_dir() / "state"


def restrict_windows_acl(path: Path) -> bool:
    """Grant only the current Windows user on path (inheritance removed). Never raises; True on success.
    Reuses ads_auth's implementation so there is a single icacls code path to review."""
    try:
        from .ads_auth import _restrict_windows_acl
    except Exception:  # optional dependency missing: report failure, the caller warns
        return False
    return _restrict_windows_acl(path)


def ensure_private_dir(path: Path) -> None:
    """Create path (and parents); on POSIX tighten it to 700 when we created it."""
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True)
    if not existed and not _IS_WINDOWS:
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass


def write_private_atomic(path: Path, text: str) -> bool:
    """Atomically replace path with text, the file being private (600 / user-only ACL) before any byte is written.

    Returns False only when the Windows ACL restriction failed (the content is written all the same; callers
    print a warning). Raises OSError when the folder is not writable."""
    path = Path(path)
    ensure_private_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    private = True
    try:
        try:
            os.chmod(tmp, 0o600)  # mkstemp already uses 600 on POSIX; explicit for clarity
        except OSError:
            pass
        if _IS_WINDOWS:
            private = restrict_windows_acl(tmp)
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return private

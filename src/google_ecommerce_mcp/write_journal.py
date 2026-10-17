"""Write journal: one JSONL file per client and per month, append + fsync, mode 600 (user-only ACL on Windows).

    <state>/journal/<client_id>/YYYY-MM.jsonl

Kinds: plan, approval (out-of-band human approval), apply_intent (written BEFORE the request), apply_result,
rollback. Every record is masked before it is written: secrets through ads_common.redact (or the masking function
injected in Journal), PII keys (PII_KEYS + write_policy.is_pii_key, any depth) replaced by "[pii]", bodies and query params truncated (body_digest / params_digest: the SHA-256 of
the clear value is kept). An append that cannot
be made durable raises JournalError: the caller (write_core.apply_plan) refuses to write (fail-closed).

Lines are written with every non-ASCII character escaped (journal_line) and read back split on "\n" only, so a
value containing U+2028, U+2029 or U+0085 can never split a record. Readers that take security decisions
(single use, 24 h budget baseline, rollback) read with strict=True: an unreadable line raises JournalError
instead of being skipped.

Also home of state_dir() and the private-file helpers shared by the write modules (paths.py is used when present).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qsl, urlencode

__all__ = ["JournalError", "Journal", "KINDS", "PII_KEYS", "PII_MASK", "state_dir", "safe_id", "mask_pii",
           "write_private_atomic", "canonical_json", "journal_line", "sha256_hex"]

STATE_ENV = "GEM_STATE_DIR"
APP_DIR = "google-ecommerce-mcp"
KINDS = ("plan", "approval", "apply_intent", "apply_result", "rollback")
BODY_LIMIT = 2000
PII_MASK = "[pii]"
# Keys whose value is personal data (Meta CAPI user_data short names included). Masked in every record.
PII_KEYS = frozenset({
    "email", "emails", "em", "phone", "phones", "phone_number", "ph", "first_name", "last_name", "fn", "ln",
    "full_name", "name_on_card", "address", "street", "street_address", "city", "ct", "zip", "zp", "postal_code",
    "postalcode", "dob", "db", "gender", "ge", "external_id", "client_ip_address", "client_user_agent", "ip",
    "ip_address", "user_agent", "fbc", "fbp", "gclid", "gbraid", "wbraid", "user_data", "useridentifiers",
    "user_identifiers", "hashedemail", "hashed_email", "hashedphonenumber", "hashed_phone_number", "madid",
})
_ID_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_IS_WINDOWS = os.name == "nt"


class JournalError(Exception):
    """The journal could not be written durably (writes must then be refused)."""


# ------------------------------------------------------------------ canonical form
def canonical_json(obj: Any) -> str:
    """Canonical JSON (keys sorted, no whitespace, UTF-8 kept, NaN refused); sets become sorted lists."""

    def default(value: Any) -> Any:
        if isinstance(value, (set, frozenset)):
            return sorted(value, key=str)
        raise TypeError(f"not JSON serialisable: {type(value).__name__}")

    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
                      default=default)


def journal_line(record: dict) -> str:
    """One journal line: canonical JSON with every non-ASCII character escaped (U+2028, U+2029 and U+0085
    included), so the line holds no line separator of any kind; ends with "\n"."""
    def default(value: Any) -> Any:
        if isinstance(value, (set, frozenset)):
            return sorted(value, key=str)
        raise TypeError(f"not JSON serialisable: {type(value).__name__}")

    line = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
                      default=default)
    if "\n" in line or "\r" in line:  # cannot happen with ensure_ascii; refuse rather than split a record
        raise ValueError("journal record would span several lines")
    return line + "\n"


def sha256_hex(obj: Any) -> str:
    """SHA-256 hex of canonical_json(obj) (a str is hashed as is)."""
    text = obj if isinstance(obj, str) else canonical_json(obj)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ paths and private files
def state_dir() -> Path:
    """Plans + journal folder: GEM_STATE_DIR, else paths.state_dir() (A1), else <config dir>/state."""
    override = (os.environ.get(STATE_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    try:
        from .paths import state_dir as paths_state_dir
        return Path(paths_state_dir())
    except Exception:  # paths.py absent or broken: same rule, computed here
        pass
    if _IS_WINDOWS:
        base = (os.environ.get("APPDATA") or "").strip()
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    else:
        base = (os.environ.get("XDG_CONFIG_HOME") or "").strip()
        root = Path(base) if base else Path.home() / ".config"
    return root / APP_DIR / "state"


def safe_id(value: Any, what: str = "id") -> str:
    """value when it is a safe file-name component (letters, digits, _ . -; 1..64), else ValueError."""
    if not isinstance(value, str) or not _ID_RX.match(value) or value in (".", ".."):
        raise ValueError(f"{what} {value!r} is not a valid identifier (letters, digits, _ . -, max 64)")
    return value


def _restrict(path: Path) -> bool:
    if not _IS_WINDOWS:
        return True
    try:
        from .paths import restrict_windows_acl
        return restrict_windows_acl(path)
    except Exception:
        try:
            from .ads_auth import _restrict_windows_acl
            return _restrict_windows_acl(path)
        except Exception:
            return False


def _ensure_dir(path: Path) -> None:
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True)
    if not existed and not _IS_WINDOWS:
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass


def write_private_atomic(path: Path, text: str) -> None:
    """Atomically replace path with text, private (600 / user-only ACL) before any byte is written.
    Uses paths.write_private_atomic when available. Raises OSError when the folder is not writable."""
    try:
        from .paths import write_private_atomic as paths_write
    except Exception:
        paths_write = None
    if paths_write is not None:
        paths_write(Path(path), text)
        return
    path = Path(path)
    _ensure_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        _restrict(tmp)
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


def _append_durable(path: Path, line: str) -> None:
    """Append one line and fsync it; the file is created mode 600 (ACL restricted on Windows when new)."""
    _ensure_dir(path.parent)
    new = not path.exists()
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        data = line.encode("utf-8")
        while data:
            written = os.write(fd, data)
            data = data[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    if new:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        _restrict(path)


# ------------------------------------------------------------------ masking
_POLICY_PII: list[Callable[[Any], bool] | None] = []  # [write_policy.is_pii_key or None], filled on first use


def _every_key_is_pii(key: Any) -> bool:
    """Fail-closed detector used when write_policy cannot be imported: every key counts as personal data."""
    return True


def _policy_pii_key() -> Callable[[Any], bool]:
    """write_policy.is_pii_key (the canonical detector: substring + exact short keys), imported lazily because
    write_policy imports this module. FAIL CLOSED: when write_policy cannot be imported (or is only partially
    initialised), every key is treated as personal data (_every_key_is_pii), never the bare local list."""
    if not _POLICY_PII:
        try:
            from .write_policy import is_pii_key
        except Exception:  # write_policy absent or broken
            return _every_key_is_pii  # not cached: a later import may succeed
        if not callable(is_pii_key):
            return _every_key_is_pii  # not cached: a partially initialised write_policy may gain it later
        _POLICY_PII.append(is_pii_key)
    return _POLICY_PII[0] or _every_key_is_pii


def _is_pii(key: Any) -> bool:
    """True when key names personal data: in PII_KEYS (case-insensitive) OR write_policy.is_pii_key says so
    (emailAddress, contact_email, hashedPhoneNumber, userData...). Fail closed: a detector error, or a missing
    detector, counts as PII."""
    if not isinstance(key, str):
        return False
    if key.lower() in PII_KEYS:
        return True
    detector = _policy_pii_key()
    if detector is None:
        return True
    try:
        return bool(detector(key))
    except Exception:
        return True


class _TooDeep(Exception):
    """A JSON string nests deeper than the parser (or the masking) can follow."""


def _json_container(value: str) -> Any:
    """A str holding a JSON object / array (Meta form params such as user_data='{"em": ...}') parsed, else None.
    Raises _TooDeep when the JSON is too deep to parse (the caller masks it whole)."""
    if value.strip()[:1] not in ("{", "["):
        return None
    try:
        parsed = json.loads(value)
    except RecursionError as exc:
        raise _TooDeep() from exc
    except ValueError:
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


_LOCAL_FORM_PAIR = r"[A-Za-z0-9_.\[\]%~-]+=[^&=\s]+"
_LOCAL_FORM_RE = re.compile(rf"{_LOCAL_FORM_PAIR}(?:&{_LOCAL_FORM_PAIR})*")


def _form_pairs(value: str) -> list[tuple[str, str]] | None:
    """The (key, value) pairs of a form string 'a=b&c=d' (same grammar as write_policy._decode: its _FORM_RE
    when importable, else a local copy), None when value is not one."""
    try:
        from .write_policy import _FORM_RE as form_re
    except Exception:  # write_policy absent or broken: same grammar, local copy
        form_re = _LOCAL_FORM_RE
    text = value.strip()
    if "=" not in text or not form_re.fullmatch(text):
        return None
    try:
        return parse_qsl(text, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None


def _mask_str(value: str) -> Any:
    """A JSON object / array string or a form string, re-encoded masked when it held personal data; a JSON
    string too deep to inspect is masked whole."""
    try:
        parsed = _json_container(value)
        if parsed is not None:
            masked = mask_pii(parsed)
            if masked != parsed:
                return json.dumps(masked, ensure_ascii=False, separators=(",", ":"))
            return value
    except (_TooDeep, RecursionError):
        return PII_MASK
    pairs = _form_pairs(value)
    if pairs is not None:
        masked_pairs = [(k, PII_MASK if _is_pii(k) and v != "" else mask_pii(v)) for k, v in pairs]
        if masked_pairs != pairs:
            return urlencode(masked_pairs, safe="[]")
    return value


def mask_pii(value: Any) -> Any:
    """New structure where the value of every personal data key is "[pii]", at every nesting level. Strings
    are inspected too: a JSON object / array, or a form string "a=b&c=d" (Meta form params), is re-encoded
    masked when it held personal data; a JSON string nested too deep to parse is replaced whole by "[pii]".
    A key is personal data when it is in PII_KEYS (case-insensitive) or write_policy.is_pii_key matches it
    (same rule as the policy).

    Fail closed: when the detector is unavailable (write_policy cannot be imported) or raises, EVERY key counts
    as personal data, budget keys included. The journaled `before` snapshots then lose their budget values, so
    the 24 h budget baseline (write_core) loses its history and falls back on the fresh `before` alone. This is
    accepted: write_policy is part of this package, and the policy that applies a plan imports it, so the case
    is all but unreachable while writes are possible."""
    if isinstance(value, dict):
        return {k: (PII_MASK if _is_pii(k) and v not in (None, "", [], {}) else mask_pii(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_pii(v) for v in value]
    if isinstance(value, str):
        return _mask_str(value)
    return value


def _default_redactor() -> Callable[[Any], Any]:
    try:
        from .ads_common import redact
        return redact
    except Exception:  # ads_common unavailable: minimal local masking of the obvious token shapes
        rx = re.compile(r"(?i)(Bearer\s+|access_token=|api_key=|key=)[^&\s\"']+")

        def local(value: Any) -> Any:
            if isinstance(value, str):
                return rx.sub(lambda m: m.group(1) + "[redacted]", value)
            if isinstance(value, dict):
                return {k: local(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [local(v) for v in value]
            return value

        return local


def _truncate(value: Any) -> Any:
    try:
        text = canonical_json(value)
    except (TypeError, ValueError):
        return repr(value)[:BODY_LIMIT]
    if len(text) <= BODY_LIMIT:
        return value
    return {"_truncated": True, "_length": len(text), "preview": text[:BODY_LIMIT]}


# ------------------------------------------------------------------ journal
class Journal:
    """Append-only, per-client, per-month JSONL journal.

    root: journal folder (default <state_dir()>/journal, read at each call); redactor: masking function applied
    to every record after PII masking (default ads_common.redact)."""

    def __init__(self, root: Path | None = None, redactor: Callable[[Any], Any] | None = None) -> None:
        self._root = Path(root) if root is not None else None
        self._redactor = redactor

    @property
    def root(self) -> Path:
        return self._root if self._root is not None else state_dir() / "journal"

    def masked(self, value: Any) -> Any:
        """value with PII keys and secrets masked (what the journal stores)."""
        redactor = self._redactor or _default_redactor()
        return redactor(mask_pii(value))

    def path(self, client_id: str, when: datetime.datetime | None = None) -> Path:
        when = when or datetime.datetime.now(datetime.timezone.utc)
        return self.root / safe_id(client_id, "client_id") / f"{when:%Y-%m}.jsonl"

    def append(self, client_id: str, kind: str, **fields: Any) -> dict:
        """Mask, then append one record durably; returns the stored record. Raises JournalError."""
        if kind not in KINDS:
            raise JournalError(f"unknown journal kind {kind!r}")
        now = datetime.datetime.now(datetime.timezone.utc)
        record: dict[str, Any] = {"ts": now.replace(microsecond=0).isoformat(), "kind": kind,
                                  "client_id": client_id}
        for name in ("body", "params"):  # payloads: SHA-256 of the clear value kept, value masked + truncated
            if name not in fields:
                continue
            payload = fields.pop(name)
            try:
                fields.setdefault(f"{name}_digest", sha256_hex(payload))
            except (TypeError, ValueError):
                fields.setdefault(f"{name}_digest", None)
            fields[name] = _truncate(self.masked(payload))
        for key, value in fields.items():
            record[key] = self.masked(value)
        record["client_id"] = client_id
        try:
            line = journal_line(record)
            _append_durable(self.path(client_id, now), line)
        except (OSError, ValueError, TypeError) as exc:
            raise JournalError(f"{type(exc).__name__}: {exc}") from exc
        return record

    def files(self, client_id: str) -> list[Path]:
        folder = self.root / safe_id(client_id, "client_id")
        return sorted(folder.glob("*.jsonl")) if folder.is_dir() else []

    def entries(self, client_id: str, *, plan_id: str | None = None, kinds: Iterable[str] | None = None,
                limit: int | None = None, strict: bool = False) -> list[dict]:
        """Records of a client, oldest first; filtered by plan_id / kinds; the last `limit` ones when given.

        Lines are split on "\n" only. strict=False skips unreadable lines and files (display); strict=True raises
        JournalError on the first unreadable file or line (security decisions must not ignore a record)."""
        wanted = set(kinds) if kinds else None
        out: list[dict] = []
        for path in self.files(client_id):
            try:
                text = path.read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                if strict:
                    raise JournalError(f"journal file {path} is unreadable ({type(exc).__name__})") from exc
                continue
            for number, line in enumerate(text.split("\n"), start=1):
                line = line.rstrip("\r")
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    record = None
                if not isinstance(record, dict):
                    if strict:
                        raise JournalError(f"journal file {path}, line {number}: unreadable record")
                    continue
                if plan_id is not None and record.get("plan_id") != plan_id:
                    continue
                if wanted is not None and record.get("kind") not in wanted:
                    continue
                out.append(record)
        return out[-limit:] if limit else out

    def plan_consumed(self, client_id: str, plan_id: str) -> bool:
        """True when an apply_intent was already journaled for this plan (its token is spent). Strict: raises
        JournalError when a journal line cannot be read (the caller must refuse)."""
        return bool(self.entries(client_id, plan_id=plan_id, kinds=("apply_intent",), strict=True))

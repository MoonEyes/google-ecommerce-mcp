"""The commands of the docs, run for real: docs/SETUP.md, docs/SETUP-GOOGLE-CLOUD.md, docs/REPORTING.md and
examples/INSTALL-PROMPT.md.

1. Drift check: every `google-ecommerce-mcp <command> --flag ...` written in those files names a real sub-command and
   real flags (compared with the `--help` of the installed CLI).
2. The safe ones are executed as the user would type them (`python -m google_ecommerce_mcp ...` in a subprocess):
   --help, doctor / doctor --live / doctor --json / check in an empty environment, install --yes with --config in a
   temporary folder, report on the sample, set-secret with a mocked keyring and hidden prompt. The exit code and the
   lines the docs announce are checked.

Never executed: setup and setup-ads (browser consent), `uvx keyring del ...`, anything touching the real keyring (the
subprocesses run with keyring's `fail` backend and HOME / APPDATA / USERPROFILE in tmp_path; the network is a local
proxy that refuses everything, see test_e2e_stdio.RefusingProxy).
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from test_e2e_stdio import RefusingProxy, isolated_env

ROOT = Path(__file__).resolve().parents[1]
DOCS = [ROOT / "docs" / "SETUP.md", ROOT / "docs" / "SETUP-GOOGLE-CLOUD.md", ROOT / "docs" / "REPORTING.md",
        ROOT / "examples" / "INSTALL-PROMPT.md"]
SAMPLE = ROOT / "tests" / "data" / "sample_report_full.json"
COMMANDS = ("setup-ads", "setup", "set-secret", "check", "install", "doctor", "report")
TIMEOUT = 60


@pytest.fixture
def cli(tmp_path):
    """Run `python -m google_ecommerce_mcp <args>` in an isolated environment; returns (code, stdout, stderr)."""
    from mcp.client.stdio import get_default_environment

    proxy = RefusingProxy().__enter__()
    # Same base as an MCP client launching the server (PATH, SYSTEMROOT...), then the isolated values on top.
    env = {**get_default_environment(), **isolated_env(tmp_path, proxy.port)}
    # A plain user shell: no token file variables (install would copy GOOGLE_TOKEN_FILE into the entry); tokens are
    # then looked up in the keyring, which is keyring's `fail` backend here.
    env.pop("GOOGLE_TOKEN_FILE")
    env.pop("GADS_TOKEN_FILE")
    work = tmp_path / "work"
    work.mkdir()

    def run(*args: str, extra: dict | None = None) -> tuple[int, str, str]:
        done = subprocess.run([sys.executable, "-m", "google_ecommerce_mcp", *args], env={**env, **(extra or {})},
                              cwd=work, capture_output=True, text=True, encoding="utf-8",
                              stdin=subprocess.DEVNULL, timeout=TIMEOUT)
        return done.returncode, done.stdout, done.stderr

    run.work = work  # type: ignore[attr-defined]
    run.proxy = proxy  # type: ignore[attr-defined]
    yield run
    proxy.__exit__(None, None, None)
    assert proxy.requests == [], f"a documented command tried the network: {proxy.requests}"


# ---------------------------------------------------------------------------------------- 1. drift
_PREFIXED = re.compile(r"google-ecommerce-mcp (" + "|".join(COMMANDS) + r")\b([^`\n|]*)")
_BARE = re.compile(r"`(install|doctor) (--[^`\n]*)`")


def documented_commands() -> list[tuple[str, int, str, list[str]]]:
    """[(file, line, command, flags)] for every CLI line of the four documents."""
    found = []
    for path in DOCS:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for match in list(_PREFIXED.finditer(line)) + list(_BARE.finditer(line)):
                found.append((path.relative_to(ROOT).as_posix(), number, match.group(1),
                              re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", match.group(2))))
    return found


def test_every_documented_command_and_flag_exists(cli):
    commands = documented_commands()
    assert len(commands) >= 20, commands  # the extraction itself still works
    helps = {}
    for name in COMMANDS:
        code, out, err = cli(name, "--help")
        assert code == 0, (name, err)
        helps[name] = out
    unknown = [(f"{file}:{line}", command, flag) for file, line, command, flags in commands
               for flag in flags if not re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", helps[command])]
    assert unknown == [], unknown


def test_top_level_help_lists_every_command(cli):
    code, out, _ = cli("--help")
    assert code == 0
    for name in COMMANDS:
        assert name in out


# ---------------------------------------------------------------------------------------- 2. doctor / check
def test_doctor_offline_on_an_empty_machine(cli, tmp_path):
    """SETUP.md:79-93: one row per service, 'Result: 0 OK, 0 to fix, 7 not set up.', the first install command,
    exit code 2 when nothing is configured."""
    code, out, err = cli("doctor", "--config", str(tmp_path / "no-claude.json"))
    assert code == 2, (out, err)
    assert re.search(r"Service\s+Set up\s+Secret\s+Token\s+Status\s+Account", out)
    for service in ("GA4", "Search Console", "Merchant Center", "Tag Manager", "PageSpeed", "Google Ads",
                    "Meta Ads", "ChatGPT Ads"):
        assert re.search(rf"^{re.escape(service)}\s+no\s+no\s+", out, re.M), service
    assert re.search(r"^Meta Ads\s+no\s+no\s+-\s+not set up\s+-$", out, re.M)
    assert re.search(r"^ChatGPT Ads\s+no\s+no\s+-\s+not set up\s+-$", out, re.M)
    assert "Result: 0 OK, 0 to fix, 7 not set up." in out
    assert ("Nothing is set up yet. Start with one service: google-ecommerce-mcp install --yes --client-secret "
            "<client_secret.json> --ga4 <property id>") in out
    assert "Traceback" not in out + err


def test_doctor_live_and_json_make_no_call_when_nothing_is_set_up(cli, tmp_path):
    """SETUP.md:79 / INSTALL-PROMPT.md:13,15: --live only calls services that passed the offline check; --json has
    the same result and no secret; PageSpeed is 'skipped'."""
    config = str(tmp_path / "no-claude.json")
    code, out, _ = cli("doctor", "--live", "--config", config)
    assert code == 2 and "Live" in out and re.search(r"^PageSpeed\s+no\s+no\s+-\s+skipped", out, re.M)
    code, out, _ = cli("doctor", "--only", "ga4", "--live", "--config", config)
    assert code == 2 and "Result: 0 OK, 0 to fix, 1 not set up." in out
    code, out, _ = cli("doctor", "--json", "--config", config)
    assert code == 2
    data = json.loads(out)
    assert data["exit_code"] == 2 and len(data["services"]) == 8
    assert {s["status"] for s in data["services"]} <= {"not_set_up", "optional"}


def test_check_on_an_empty_machine(cli, tmp_path):
    """SETUP-GOOGLE-CLOUD.md:71-74: `check` prints one boolean per service and the token state."""
    code, out, err = cli("check", extra={"GOOGLE_TOKEN_FILE": str(tmp_path / "absent.json")})
    assert code == 0, err
    status = json.loads(out)
    assert status["ga4"] is status["search_console"] is status["merchant_center"] is status["tag_manager"] is False
    assert status["token"].startswith("No Google token found") and status["token_storage"] == "file"
    from google_ecommerce_mcp import __version__

    assert status["version"] == __version__


# ---------------------------------------------------------------------------------------- 3. install
def test_install_yes_writes_identifiers_only_and_is_rerunnable(cli, tmp_path):
    """SETUP.md:92-112: install --yes (no browser: --skip-auth, or a missing client file), additive reruns with a
    time-stamped backup, exit 3 on a conflicting value, --force, --corporate-proxy, id checks (exit 2)."""
    config = tmp_path / "claude" / "claude_desktop_config.json"
    c = ["--config", str(config)]

    code, out, _ = cli("install", "--yes", *c)
    assert code == 2 and "No service configured" in out and not config.exists()

    # SETUP.md:92 as written, with a client file that does not exist: refused before any browser or write
    code, out, _ = cli("install", "--yes", "--client-secret", str(tmp_path / "missing.json"), "--ga4", "123456789", *c)
    assert code == 2 and "Nothing was changed" in out and not config.exists()

    code, out, err = cli("install", "--yes", "--skip-auth", "--ga4", "123456789", *c)
    assert code == 0, (out, err)
    entry = json.loads(config.read_text(encoding="utf-8"))["mcpServers"]["google-ecommerce"]
    assert entry["env"] == {"GA4_PROPERTY_ID": "123456789"}
    assert "registered in" in out and "claude mcp add google-ecommerce" in out

    # SETUP.md:99 additive rerun (Merchant Center id), with a backup
    code, out, _ = cli("install", "--yes", "--skip-auth", "--merchant", "5550001", *c)
    assert code == 0 and "(backup: " in out
    assert list(config.parent.glob("claude_desktop_config.json.bak-*"))
    # SETUP.md:106: a changed value stops with exit 3 and changes nothing; --force replaces it
    before = config.read_text(encoding="utf-8")
    code, out, _ = cli("install", "--yes", "--skip-auth", "--ga4", "987654321", *c)
    assert code == 3 and "Nothing was changed" in out and config.read_text(encoding="utf-8") == before
    code, out, _ = cli("install", "--yes", "--skip-auth", "--ga4", "987654321", "--force", *c)
    assert code == 0
    # SETUP.md:110 and :102
    assert cli("install", "--yes", "--skip-auth", "--corporate-proxy", *c)[0] == 0
    code, out, _ = cli("install", "--yes", "--skip-auth", "--meta-account-id", "123456789", *c)
    assert code == 0 and "google-ecommerce-mcp set-secret meta-access-token" in out
    env = json.loads(config.read_text(encoding="utf-8"))["mcpServers"]["google-ecommerce"]["env"]
    assert env == {"GA4_PROPERTY_ID": "987654321", "MERCHANT_ACCOUNT_ID": "5550001", "ADS_TRUST_ENV": "1",
                   "META_AD_ACCOUNT_ID": "123456789"}  # identifiers only, never a secret
    # SETUP.md:108: ids are checked before anything happens
    code, out, _ = cli("install", "--yes", "--skip-auth", "--gads-customer-id", "123", *c)
    assert code == 2 and "10 digits" in out
    # SETUP.md:108: same token file for Google and Google Ads: refused with 2 before any authorization
    same = str(tmp_path / "same-token.json")
    code, out, _ = cli("install", "--yes", "--skip-auth", "--token-file", same, "--gads-customer-id", "1234567890",
                       *c, extra={"GADS_TOKEN_FILE": same})
    assert code == 2 and "GADS_TOKEN_FILE is the same file" in out
    # and doctor now reads that entry: SETUP.md:79 exit 1 when a configured service fails
    code, out, _ = cli("doctor", *c)
    assert code == 1 and "To fix" in out and "property 987***321" in out


# ---------------------------------------------------------------------------------------- 4. report
def _png() -> bytes:
    """A valid 1x1 PNG."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (len(data).to_bytes(4, "big") + kind + data
                + zlib.crc32(kind + data).to_bytes(4, "big"))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", (1).to_bytes(4, "big") * 2 + b"\x08\x02\x00\x00\x00")
            + chunk(b"IDAT", zlib.compress(b"\x00\x1f\x6f\x5b")) + chunk(b"IEND", b""))


def test_report_command_as_documented(cli):
    """REPORTING.md:193-196: the documented line writes report.html; an existing output is kept unless --force
    ('Report not written: ...', exit 1); an invalid accent prints a warning (REPORTING.md:262)."""
    work = cli.work
    shutil.copy(SAMPLE, work / "report.json")
    (work / "logo.png").write_bytes(_png())
    line = ["report", "--input", "report.json", "--output", "report.html", "--brand-name", "Client name",
            "--accent", "#1F6F5B", "--logo", "logo.png", "--lang", "en"]
    code, out, err = cli(*line)
    assert code == 0, err
    assert out.strip() == "Report written to report.html." and err == ""
    page = (work / "report.html").read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>") and '<html lang="en">' in page and "Client name" in page
    assert "data:image/png;base64," + base64.b64encode(_png()).decode() in page
    assert "<script" not in page.lower()

    code, out, err = cli(*line)
    assert code == 1 and err.startswith("Report not written: ") and "already exists" in err
    code, out, err = cli(*line[:-4], "--accent", "blue", "--force")
    assert code == 0 and "Warning: accent_invalid" in err


# ---------------------------------------------------------------------------------------- 5. set-secret
def test_set_secret_refuses_an_empty_value_and_stores_in_the_keyring(monkeypatch, capsys):
    """SETUP.md:102-103: set-secret reads a hidden prompt; nothing typed -> nothing stored (exit 1); a value goes to
    the keyring entry and is never printed. Keyring and prompt are mocked: the real keyring is never touched."""
    import getpass

    import keyring

    from ads_fakes import FakeKeyring
    from google_ecommerce_mcp.__main__ import main

    store = FakeKeyring()
    monkeypatch.setattr(keyring, "get_password", store.get_password)
    monkeypatch.setattr(keyring, "set_password", store.set_password)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "   ")
    assert main(["set-secret", "meta-access-token"]) == 1
    assert "Nothing entered, nothing stored." in capsys.readouterr().err and store == {}

    secret = "EA" + "Afake-doc-walkthrough-0000"
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": secret)
    assert main(["set-secret", "openai-ads-key"]) is None
    out = capsys.readouterr()
    assert store == {("google-ecommerce-mcp", "openai-ads-key"): secret}
    assert secret not in out.out + out.err
    assert "Stored in operating system keyring (entry openai-ads-key)" in out.out
    assert "google-ecommerce-mcp doctor --only openai --live" in out.out

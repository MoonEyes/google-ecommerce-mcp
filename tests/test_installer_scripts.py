"""The one-line installers must not move under the user.

A line that pipes a branch to a shell runs whatever the branch holds today. These tests keep the scripts
pinned to the package version they were released with, and the README pinned to an exact commit with the
SHA-256 of each script published next to it. No network, no Google call.
"""

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ("install.ps1", "install.sh")
README = (ROOT / "README.md").read_text(encoding="utf-8")
RAW = r"raw\.githubusercontent\.com/MoonEyes/google-ecommerce-mcp"


def project_version() -> str:
    match = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    assert match, "no version in pyproject.toml"
    return match.group(1)


def sha256_lf(path: Path) -> str:
    """Hash with LF endings: what GitHub serves, even when a Windows checkout rewrote them to CRLF."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def test_installers_pin_the_package_version():
    for name in SCRIPTS:
        lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
        text = "\n".join(line for line in lines if not line.lstrip().startswith("#"))  # code, not comments
        pinned = set(re.findall(r"google-ecommerce-mcp==([0-9][0-9A-Za-z.\-+]*)", text))
        assert pinned == {project_version()}, f"{name} must install google-ecommerce-mcp=={project_version()}"
        assert not re.search(r"google-ecommerce-mcp\s+install", text), f"{name} still runs an unpinned install"


def test_no_branch_is_piped_to_a_shell():
    for name in SCRIPTS:
        text = (ROOT / name).read_text(encoding="utf-8")
        assert not re.search(RAW + r"/(main|master)/", text), f"{name} advertises a branch URL in its header"
    assert not re.search(RAW + r"/(main|master)/install\.(ps1|sh)", README), "README pipes a branch to a shell"


def test_readme_one_liners_name_an_exact_commit():
    for name in SCRIPTS:
        assert re.search(RAW + rf"/[0-9a-f]{{40}}/{re.escape(name)}", README), f"README lacks a pinned URL for {name}"


def test_readme_publishes_the_sha256_of_each_installer():
    for name in SCRIPTS:
        assert sha256_lf(ROOT / name) in README, f"README does not publish the current SHA-256 of {name}"

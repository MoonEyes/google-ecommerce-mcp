"""The documentation of the multi-client and write release stays consistent with the code.

No network, no credential. Checks:
- one version everywhere (pyproject, server.json twice, __init__);
- every tool of tests/golden_tools.json is documented in docs/TOOLS.md, and the tool count quoted in the docs is right;
- every plan / apply tool named in the docs exists;
- no stale "read-only" promise is left in the README and the docs (reading is read-only; writing is opt-in);
- the error codes quoted in docs/WRITE.md exist in the code;
- every relative link of the docs points to a file (and, for Markdown, to a heading that exists).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "google_ecommerce_mcp"
GITHUB = "https://github.com/MoonEyes/google-ecommerce-mcp/"

DOC_FILES = sorted(
    [ROOT / "README.md", ROOT / "CONTRIBUTING.md", ROOT / "SECURITY.md", ROOT / "CHANGELOG.md",
     ROOT / "THIRD_PARTY_NOTICES.md"]
    + list((ROOT / "docs").glob("*.md")) + list((ROOT / "examples").glob("*.md")))
# Files whose wording is checked for stale promises (the changelog and the release notes quote old versions).
PROMISE_FILES = [f for f in DOC_FILES if f.name not in ("CHANGELOG.md", "RELEASING.md", "THIRD_PARTY_NOTICES.md")]


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def golden_tools() -> list[dict]:
    return json.loads(text(ROOT / "tests" / "golden_tools.json"))["tools"]


# ------------------------------------------------------------------ versions
def test_version_is_identical_everywhere():
    pyproject = re.search(r'^version = "([^"]+)"', text(ROOT / "pyproject.toml"), re.M).group(1)
    server = json.loads(text(ROOT / "server.json"))
    init = re.search(r'^__version__ = "([^"]+)"', text(SRC / "__init__.py"), re.M).group(1)
    assert server["version"] == pyproject, "server.json version differs from pyproject.toml"
    assert server["packages"][0]["version"] == pyproject, "server.json packages[0].version differs"
    assert init == pyproject, "__init__.__version__ differs from pyproject.toml"
    assert pyproject == "0.5.0"


def test_package_description_does_not_claim_read_only():
    pyproject = text(ROOT / "pyproject.toml")
    description = re.search(r'^description = "([^"]+)"', pyproject, re.M).group(1)
    server = json.loads(text(ROOT / "server.json"))["description"]
    for label, value in (("pyproject.toml", description), ("server.json", server)):
        assert "read-only" not in value.lower(), f"{label} still calls the server read-only"
    assert "multi-client" in description.lower() and "multi-client" in server.lower()
    assert len(server) <= 100, "the MCP registry description is limited to 100 characters"


# ------------------------------------------------------------------ tools
def test_every_tool_is_documented_in_tools_md():
    tools_md = text(ROOT / "docs" / "TOOLS.md")
    missing = [t["name"] for t in golden_tools() if f"`{t['name']}`" not in tools_md]
    assert not missing, f"tools missing from docs/TOOLS.md: {missing}"


def test_tool_count_quoted_in_the_docs_is_the_golden_count():
    count = len(golden_tools())
    assert count == 80
    for name in ("README.md", "docs/TOOLS.md"):
        quoted = {int(n) for n in re.findall(r"\*\*(\d+) tools\.?\*\*", text(ROOT / name))}
        assert quoted == {count}, f"{name} quotes {quoted}, the code has {count} tools"
    assert f"({count})" in text(ROOT / "README.md")
    assert f"{count} tools" in text(ROOT / "docs" / "ARCHITECTURE.md")


def test_only_the_apply_tools_are_documented_as_writing():
    writers = {t["name"] for t in golden_tools() if not t["annotations"]["readOnlyHint"]}
    assert writers == {"gads_apply", "meta_apply", "openai_ads_apply", "google_apply"}
    tools_md = text(ROOT / "docs" / "TOOLS.md")
    for row in re.findall(r"^\| `([a-z0-9_]+)` \| (R|Plan|Apply) \|", tools_md, re.M):
        name, kind = row
        assert (kind == "Apply") == (name in writers), f"TOOLS.md types {name} as {kind}"
        if kind == "Plan":
            assert "_plan_" in name or name == "rollback_plan", name


def test_plan_and_apply_tools_named_in_the_docs_exist():
    known = {t["name"] for t in golden_tools()}
    families = r"(?:gads|meta|meta_eco|openai_ads|ga4|gsc|gtm|merchant)"
    pattern = re.compile(rf"`({families}_plan_[a-z0-9_]+|(?:gads|meta|openai_ads|google)_apply)`")
    unknown = set()
    for path in DOC_FILES:
        if path.name == "CHANGELOG.md":
            continue  # lists tools by family on purpose; checked below
        for name in pattern.findall(text(path)):
            if name not in known:
                unknown.add((path.name, name))
    assert not unknown, f"documented tools that do not exist: {sorted(unknown)}"
    changelog = text(ROOT / "CHANGELOG.md")
    section = changelog.split("## 0.5.0", 1)[1].split("\n## 0.4.1", 1)[0]
    unknown_changelog = {n for n in pattern.findall(section) if n not in known}
    assert not unknown_changelog, f"CHANGELOG names tools that do not exist: {sorted(unknown_changelog)}"


# ------------------------------------------------------------------ stale promises
FALSE_PROMISES = [
    r"No tool can modify",
    r"Tools? cannot write",
    r"\bcannot write\b",
    r"No writes, anywhere",
    r"\bread-only MCP server\b",
    r"\ball read-only\b",
    r"\bnine read-only tools\b",
    r"(?<!original )\b22 tools\b",
    r"never changes a campaign, a feed or a tag",
    r"server never calls their write endpoints",
]
# "read-only" is true for scopes, GAQL, allow-lists, roles and the reading path. A line that says it must also
# carry one of these qualifiers, otherwise it is probably a stale blanket promise.
SAFE_READ_ONLY = re.compile(
    r"(\bno read-only\b|\bread-only (scopes?|GAQL|allow-lists?|access|role|token|key|config|version|variant|OAuth|"
    r"aggregate|tool\b|query|analyst)|read-only (and|or) |"
    r"\b(reading|reads|read tools?|read requests?|read path|construction|enforced|Reading stays|by default|unless)\b|"
    r"readOnlyHint|non-read-only|read-only: it only reads|lowest|give \[your|Read-only,? aggregate|"
    r"can be read-only|read-only file)", re.I)


def test_no_stale_read_only_promise_is_left():
    problems = []
    for path in PROMISE_FILES:
        for number, line in enumerate(text(path).splitlines(), 1):
            for pattern in FALSE_PROMISES:
                if re.search(pattern, line, re.I):
                    problems.append(f"{path.name}:{number}: matches {pattern!r}: {line.strip()[:100]}")
            if re.search(r"read[- ]only", line, re.I) and not SAFE_READ_ONLY.search(line):
                problems.append(f"{path.name}:{number}: unqualified 'read-only': {line.strip()[:100]}")
    assert not problems, "\n".join(problems)


def test_old_versions_are_only_quoted_where_they_belong():
    allowed = {"CHANGELOG.md", "RELEASING.md", "README.md", "SETUP.md", "TROUBLESHOOTING.md", "TOOLS.md"}
    for path in DOC_FILES:
        for number, line in enumerate(text(path).splitlines(), 1):
            if re.search(r"\b0\.4\.[01]\b", line):
                assert path.name in allowed, f"{path.name}:{number} still names an old version: {line.strip()[:90]}"
                if path.name == "README.md":
                    assert "==0.4.1" in line, f"README.md:{number}: only the pinned installer may name 0.4.1"


def test_writing_is_described_as_off_by_default_and_untested():
    readme = text(ROOT / "README.md")
    top = readme[:readme.index("## Documentation")]
    assert "off by default" in top and "never been tested on a real account" in top
    assert "QUICKSTART.md" in top
    security = text(ROOT / "docs" / "SECURITY.md")
    for needle in ("GEM_WRITE=1", "known limits", "pseudo-terminal", "cannot be split", "No write was verified"):
        assert needle.lower() in security.lower(), f"SECURITY.md lacks: {needle}"
    write_md = text(ROOT / "docs" / "WRITE.md")
    assert "never been tested on a real account" in write_md
    assert "ask each time" in write_md


# ------------------------------------------------------------------ error codes
def test_error_codes_of_write_md_exist_in_the_code():
    source = "\n".join(p.read_text(encoding="utf-8") for p in SRC.glob("*.py"))
    table = text(ROOT / "docs" / "WRITE.md").split("## Common refusals and their fix", 1)[1].split("\n## ", 1)[0]
    codes = set()
    for line in table.splitlines():
        match = re.match(r"^\| ((?:`[a-z_]+`(?:, | / )?)+)", line)
        if match:
            codes.update(re.findall(r"`([a-z_]+)`", match.group(1)))
    assert len(codes) >= 25, f"only found {len(codes)} codes in the refusals table"
    missing = sorted(c for c in codes if f'"{c}"' not in source and f"'{c}'" not in source)
    assert not missing, f"error codes of docs/WRITE.md not found in the code: {missing}"


def test_error_codes_of_multi_client_md_match_clients_py():
    clients_py = text(SRC / "clients.py")
    declared = set(re.findall(r'"([a-z_]+)"', re.search(r"ERROR_CODES = \((.*?)\)", clients_py, re.S).group(1)))
    doc = text(ROOT / "docs" / "MULTI-CLIENT.md").split("## Errors", 1)[1].split("\n## ", 1)[0]
    documented = set(re.findall(r"^\| `([a-z_]+)` \|", doc, re.M)) - {"error"}
    assert documented == declared, f"documented {sorted(documented)} vs code {sorted(declared)}"


def test_registry_schema_of_multi_client_md_is_accepted_by_the_code():
    """The example file of docs/MULTI-CLIENT.md must load with the real validator."""
    from google_ecommerce_mcp import clients

    doc = text(ROOT / "docs" / "MULTI-CLIENT.md")
    block = re.search(r"## The file\n\n```json\n(.*?)\n```", doc, re.S).group(1)
    data = json.loads(block)
    clean = clients._validate_registry(data)  # raises ValueError if the documented example is wrong
    assert set(clean["clients"]) == {"acme"}
    policy = clean["clients"]["acme"]["write"]["google_ads"]
    assert policy["enabled"] is False and policy["max_daily_budget_micros"] == 50000000


# ------------------------------------------------------------------ links
_FENCE = re.compile(r"^(```|~~~)")
_LINK = re.compile(r"(?<!\\)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def _strip_code(markdown: str) -> str:
    out, fenced = [], False
    for line in markdown.splitlines():
        if _FENCE.match(line.strip()):
            fenced = not fenced
            continue
        if not fenced:
            out.append(re.sub(r"`[^`]*`", "", line))
    return "\n".join(out)


def _slug(heading: str) -> str:
    heading = re.sub(r"`", "", heading.strip().lower())
    heading = re.sub(r"[^\w\- ]", "", heading, flags=re.UNICODE)
    return heading.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    anchors, fenced = set(), False
    for line in text(path).splitlines():
        if _FENCE.match(line.strip()):
            fenced = not fenced
            continue
        match = None if fenced else re.match(r"^#{1,6}\s+(.*?)\s*#*$", line)
        if match:
            anchors.add(_slug(match.group(1)))
    return anchors


def test_relative_links_and_anchors_exist():
    broken = []
    for path in DOC_FILES:
        for target in _LINK.findall(_strip_code(text(path))):
            if target.startswith(("mailto:", "#")) or re.match(r"^[a-z]+://", target) and not target.startswith(GITHUB):
                if target.startswith("#") and path.suffix == ".md" and target[1:] not in _anchors(path):
                    broken.append(f"{path.name}: anchor {target} not found")
                continue
            if target.startswith(GITHUB) and not re.match(GITHUB + r"(blob|tree)/main/", target):
                continue  # issues, actions...: not files of the repository
            if target.startswith(GITHUB):
                local = re.sub(r"^(blob|tree)/main/", "", target[len(GITHUB):])
                base = ROOT
            else:
                local, base = target, path.parent
            file_part, _, anchor = local.partition("#")
            if not file_part:
                continue
            resolved = (base / file_part).resolve()
            if not resolved.exists():
                broken.append(f"{path.name}: {target} -> missing file")
            elif anchor and resolved.suffix == ".md" and anchor not in _anchors(resolved):
                broken.append(f"{path.name}: {target} -> no heading '{anchor}' in {resolved.name}")
    assert not broken, "\n".join(broken)


def test_new_pages_are_linked_from_the_readme_and_each_other():
    readme = text(ROOT / "README.md")
    for page in ("QUICKSTART.md", "WRITE.md", "MULTI-CLIENT.md", "SECURITY.md", "TOOLS.md", "ARCHITECTURE.md"):
        assert f"docs/{page}" in readme, f"README does not link docs/{page}"
    assert "MULTI-CLIENT.md" in text(ROOT / "docs" / "QUICKSTART.md")
    assert "WRITE.md" in text(ROOT / "docs" / "QUICKSTART.md")
    assert "SECURITY.md" in text(ROOT / "docs" / "WRITE.md")

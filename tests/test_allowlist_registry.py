"""Field allow-list registry: shape of golden_allowlists() once frozen, and the golden-file helper.

assert_golden(path) is shared with the platform waves: they import their platform modules (which register
their allow-lists), then call assert_golden(GOLDEN) so that any change of an allow-list or of a money
exemption shows up as a diff of tests/golden_allowlists.json.

tests/golden_allowlists.json is the registry built by registry.register_all (every module present, in an
empty registry, freeze=False). Regenerate it after a deliberate change of an allow-list, then review the diff:

    python -m pytest tests/test_allowlist_registry.py --regen-golden
    (or: python -c "import sys; sys.path[:0] = ['src', 'tests']; import test_allowlist_registry as t; t.regen()")
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from google_ecommerce_mcp import write_core
from google_ecommerce_mcp import write_policy as wp

GOLDEN = Path(__file__).with_name("golden_allowlists.json")
# Actions whose op never carries a body nor params: an EMPTY field list (nothing allowed) is the strictest one.
NO_PAYLOAD_ACTIONS = frozenset({("openai_ads", "pause")})


class _ToolSink:
    """Stands in for FastMCP: accepts every mcp.tool(...)(fn) and keeps nothing."""

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        return lambda fn: fn


def real_registry_snapshot() -> dict:
    """golden_allowlists() of a registry filled by registry.register_all(..., freeze=False) from scratch (empty
    registry and handlers, not permissive); the previous state is put back afterwards."""
    from google_ecommerce_mcp import registry, server  # noqa: F401  (server's own import runs register_all)

    saved =(wp._FIELD_ALLOWLIST, wp._FROZEN, wp._TEST_PERMISSIVE, write_core._HANDLERS)
    try:
        wp._FIELD_ALLOWLIST, wp._FROZEN, wp._TEST_PERMISSIVE, write_core._HANDLERS = {}, False, False, {}
        registry.register_all(_ToolSink(), freeze=False)
        assert not wp.allowlists_frozen()
        return wp.golden_allowlists()
    finally:
        wp._FIELD_ALLOWLIST, wp._FROZEN, wp._TEST_PERMISSIVE, write_core._HANDLERS = saved


def regen(path: Path | str = GOLDEN) -> dict:
    """Rewrite the golden file from the real registry (sorted, LF, trailing newline); returns the snapshot."""
    snap = real_registry_snapshot()
    _check_shape(snap)
    Path(path).write_bytes((json.dumps(snap, indent=1, sort_keys=True) + "\n").encode("utf-8"))
    return snap


def _check_shape(snap: Any, *, ordered: bool = True) -> None:
    """{platform: {action: {"fields": [sorted paths], "money_ok": [sorted exact paths of fields]}}}; with
    ordered, platforms and actions are in sorted order too (golden_allowlists() itself)."""
    assert isinstance(snap, dict) and (not ordered or list(snap) == sorted(snap))
    for platform, actions in snap.items():
        assert isinstance(platform, str) and platform
        assert isinstance(actions, dict) and actions and (not ordered or list(actions) == sorted(actions))
        for action, entry in actions.items():
            assert isinstance(action, str) and action == action.lower()
            assert isinstance(entry, dict) and set(entry) == {"fields", "money_ok"}, (platform, action)
            fields, money_ok = entry["fields"], entry["money_ok"]
            assert fields or (platform, action) in NO_PAYLOAD_ACTIONS, (platform, action)
            assert fields == sorted(set(fields)), (platform, action)
            assert money_ok == sorted(set(money_ok)) and set(money_ok) <= set(fields), (platform, action)
            assert not any(p.endswith(".*") for p in money_ok), (platform, action)


def assert_golden(path: Path | str = GOLDEN, snapshot: dict | None = None) -> None:
    """Compare golden_allowlists() (or `snapshot`) with the JSON file at path.

    A file holding exactly {} is a placeholder (before the platform modules register anything): only the
    shape of the registry is checked. Otherwise the registry must equal the file: a platform, an action, a
    field or a money_ok path present on one side only fails, with the differing platforms named."""
    snap = wp.golden_allowlists() if snapshot is None else snapshot
    _check_shape(snap)
    expected = json.loads(Path(path).read_text(encoding="utf-8"))
    assert isinstance(expected, dict), f"{path}: the golden file must hold a JSON object"
    if expected == {}:
        return
    _check_shape(expected, ordered=False)
    only_file = sorted(set(expected) - set(snap))
    only_registry = sorted(set(snap) - set(expected))
    assert not only_file and not only_registry, (
        f"{path}: platforms only in the golden file {only_file}, only in the registry {only_registry}")
    changed = sorted(p for p in snap if snap[p] != expected[p])
    assert not changed, (f"{path}: allow-lists differ for {changed}; registry: "
                         f"{json.dumps({p: snap[p] for p in changed}, indent=1)}")


@pytest.fixture
def registry(monkeypatch):
    """An empty, open registry for the test (the real one is restored afterwards)."""
    monkeypatch.setattr(wp, "_FIELD_ALLOWLIST", {})
    monkeypatch.setattr(wp, "_FROZEN", False)
    monkeypatch.setattr(wp, "_TEST_PERMISSIVE", False)
    yield
    wp._unfreeze_for_tests()


def test_golden_shape_after_freeze(registry):
    wp.register_field_allowlist("meta", "update", ["status", "name", "creative.object_story_spec.link_data.*"])
    wp.register_field_allowlist("meta", "capi", ["data.event_name", "data.custom_data.value"],
                                money_ok=("data.custom_data.value",))
    wp.register_field_allowlist("google_ads", "set_budget", ["operations.update.amountMicros"])
    wp.freeze_allowlists()
    with pytest.raises(ValueError, match="registry_frozen"):
        wp.register_field_allowlist("merchant", "update", ["title"])
    snap = wp.golden_allowlists()
    _check_shape(snap)
    assert list(snap) == ["google_ads", "meta"]
    assert snap["meta"]["capi"] == {"fields": ["data.customdata.value", "data.eventname"],
                                    "money_ok": ["data.customdata.value"]}
    assert json.loads(json.dumps(snap)) == snap
    assert wp.golden_allowlists() == snap  # stable


def test_real_registry_matches_the_golden_file(regen_golden):
    """The registry as filled by register_all (every module present) equals the golden file EXACTLY: same
    platforms, actions, fields and money_ok (--regen-golden rewrites the file instead)."""
    if regen_golden:
        regen()
    snap = real_registry_snapshot()
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert expected, f"{GOLDEN} is empty: run python -m pytest {Path(__file__).name} --regen-golden"
    assert_golden(GOLDEN, snapshot=snap)
    assert snap == expected  # exact, key for key


def test_golden_file_covers_every_write_platform_present():
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    for platform in ("google_ads", "meta", "meta_eco", "openai_ads", "ga4", "gtm"):
        assert expected.get(platform), platform
    # openai_ads pause: registered, with NO field (its POST .../pause has no body): an empty list, not a missing
    # one, so any body or params is field_not_allowed instead of no_field_allowlist
    assert expected["openai_ads"]["pause"] == {"fields": [], "money_ok": []}
    assert expected["openai_ads"]["budget_change"]["fields"] == ["budget.dailyspendlimitmicros"]
    assert set(expected["google_ads"]) >= {"add_negatives", "create_campaign"}


def test_openai_pause_with_any_payload_is_refused_by_its_empty_list(registry):
    from google_ecommerce_mcp.write_core import WriteOp

    wp.register_field_allowlist("openai_ads", "pause", [])
    base = dict(op_id="o1", platform="openai_ads", account_id="acct", action="pause", method="POST",
                url="https://api.ads.openai.com/v1/campaigns/c1/pause")
    assert wp._field_violations(WriteOp(**base)) == []
    for extra in ({"body": {"status": "paused"}}, {"params": {"name": "x"}}):
        assert wp._field_violations(WriteOp(**base, **extra))[0][0] == "field_not_allowed", extra


def test_register_all_freezes_once_unless_told_not_to(registry):
    from google_ecommerce_mcp import registry as reg

    reg.register_all(_ToolSink(), freeze=False)
    assert not wp.allowlists_frozen() and wp.golden_allowlists()
    reg.register_all(_ToolSink())  # the same lists again, then the freeze
    assert wp.allowlists_frozen()
    reg.register_all(_ToolSink())  # already frozen: identical lists are no-ops, no error
    assert wp.allowlists_frozen()
    with pytest.raises(ValueError, match="registry_frozen"):
        wp.register_field_allowlist("openai_ads", "archive", ["status"])


def test_assert_golden_placeholder_and_mismatch(registry, tmp_path):
    wp.register_field_allowlist("meta", "update", ["status"])
    placeholder = tmp_path / "empty.json"
    placeholder.write_text("{}", encoding="utf-8")
    assert_golden(placeholder)  # {} never blocks
    same = tmp_path / "same.json"
    same.write_text(json.dumps(wp.golden_allowlists()), encoding="utf-8")
    assert_golden(same)
    extra = tmp_path / "extra.json"
    extra.write_text(json.dumps({**wp.golden_allowlists(),
                                 "merchant": {"update": {"fields": ["title"], "money_ok": []}}}), encoding="utf-8")
    with pytest.raises(AssertionError, match="only in the golden file"):
        assert_golden(extra)
    wp.register_field_allowlist("google_ads", "pause", ["status"])
    with pytest.raises(AssertionError, match="only in the registry"):
        assert_golden(same)
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps({"google_ads": {"pause": {"fields": ["status"], "money_ok": []}},
                                   "meta": {"update": {"fields": ["name", "status"], "money_ok": []}}}),
                       encoding="utf-8")
    with pytest.raises(AssertionError, match="differ"):
        assert_golden(changed)

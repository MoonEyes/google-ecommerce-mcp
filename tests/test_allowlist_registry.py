"""Field allow-list registry: shape of golden_allowlists() once frozen, and the golden-file helper.

assert_golden(path) is shared with the platform waves: they import their platform modules (which register
their allow-lists), then call assert_golden(GOLDEN) so that any change of an allow-list or of a money
exemption shows up as a diff of tests/golden_allowlists.json.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from google_ecommerce_mcp import write_policy as wp

GOLDEN = Path(__file__).with_name("golden_allowlists.json")


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
            assert fields and fields == sorted(set(fields)), (platform, action)
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


def test_real_registry_matches_the_golden_file():
    """The registry as filled by the imported modules (empty until the platform modules register theirs)."""
    assert_golden(GOLDEN)


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

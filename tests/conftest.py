"""Shared test isolation for the write path, and the golden regeneration switch.

Isolation: every test starts from, and leaves behind, the same process-wide write state: the field allow-list
registry (write_policy._FIELD_ALLOWLIST), its freeze flag (_FROZEN), the test-only permissive switch
(_TEST_PERMISSIVE) and the platform handlers (write_core._HANDLERS). Each is deep-copied before the test and put
back (as a fresh deep copy) after it, so a test that registers allow-lists, freezes the registry or swaps
handlers (directly, or by reloading server / calling registry.register_all) cannot leak into the next one,
whatever the order. PlatformHandler objects are kept as they are (immutable; tests compare them with `is`).
A test that sets these values itself (monkeypatch or direct assignment) keeps working: the restore runs after
its own teardown.

Importing google_ecommerce_mcp.server (at collection) runs registry.register_all, which freezes the registry
like in production; the session fixture reopens it once, before the first test, so the tests start unfrozen.

Golden files: `python -m pytest tests/test_allowlist_registry.py tests/test_tools_golden.py --regen-golden`
rewrites tests/golden_allowlists.json and tests/golden_tools.json from the real registry instead of comparing
(then run the same tests again without the option: they must pass).
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from google_ecommerce_mcp import write_core, write_policy

_WRITE_STATE = ((write_policy, "_FIELD_ALLOWLIST"), (write_policy, "_FROZEN"), (write_policy, "_TEST_PERMISSIVE"),
                (write_core, "_HANDLERS"))


def pytest_addoption(parser: Any) -> None:
    parser.addoption("--regen-golden", action="store_true", default=False,
                     help="rewrite tests/golden_allowlists.json and tests/golden_tools.json from the real registry")


@pytest.fixture
def regen_golden(request: Any) -> bool:
    """True when pytest runs with --regen-golden."""
    return bool(request.config.getoption("--regen-golden"))


def _deep_copy(value: Any) -> Any:
    """Deep copy that keeps PlatformHandler objects (immutable, compared by identity)."""
    memo: dict[int, Any] = {}
    if isinstance(value, dict):
        for item in value.values():
            if isinstance(item, write_core.PlatformHandler):
                memo[id(item)] = item
    return copy.deepcopy(value, memo)


def snapshot_write_state() -> dict[tuple[str, str], Any]:
    return {(module.__name__, name): _deep_copy(getattr(module, name)) for module, name in _WRITE_STATE}


def restore_write_state(saved: dict[tuple[str, str], Any]) -> None:
    for module, name in _WRITE_STATE:
        setattr(module, name, _deep_copy(saved[(module.__name__, name)]))


@pytest.fixture(scope="session", autouse=True)
def _registry_open_for_tests() -> Any:
    write_policy._unfreeze_for_tests()
    yield


@pytest.fixture(autouse=True)
def _isolated_write_state() -> Any:
    saved = snapshot_write_state()
    try:
        yield
    finally:
        restore_write_state(saved)


@pytest.fixture(autouse=True)
def _isolated_read_sessions() -> Any:
    """The HTTP session of the shared read clients (google_ads / meta_ads / openai_ads .CLIENT) is put back after
    each test: a test that swaps it for a fake without monkeypatch (test_doctor.use) cannot leak it."""
    from google_ecommerce_mcp import google_ads, meta_ads, openai_ads

    clients = [m.CLIENT for m in (google_ads, meta_ads, openai_ads)]
    saved = [c.session for c in clients]
    try:
        yield
    finally:
        for client, session in zip(clients, saved):
            client.session = session

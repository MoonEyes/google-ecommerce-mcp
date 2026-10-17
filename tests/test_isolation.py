"""Strict isolation between clients (PLAN_FULLCONTROL §1): a client can never obtain another client's ids or
secrets, whatever the environment, the keyring or the registry says. Offline: fake keyring, tmp registry."""

from __future__ import annotations

import dataclasses
import itertools
import json

import pytest

from clients_fakes import ACME, BETA, setup_env, write_registry
from google_ecommerce_mcp import clients, secrets_store
from google_ecommerce_mcp.clients import ACCOUNT_FIELDS, WRITE_PLATFORMS, ClientError, resolve

SVC = "google-ecommerce-mcp"
CLIENTS = {"acme": ACME, "beta": BETA}
SHARED = {("google_ads", "login_customer_id"), ("meta", "business_id")}  # same MCC / business on purpose


def secret_value(cid: str, kind: str) -> str:
    return f"SECRET::{cid}::{kind}"


@pytest.fixture
def two(monkeypatch, tmp_path):
    fake, path = setup_env(monkeypatch, tmp_path)
    write_registry(path, CLIENTS)
    for cid, kind in itertools.product(CLIENTS, secrets_store.KINDS):
        fake.store[(SVC, f"{cid}/{kind}")] = secret_value(cid, kind)
    # Legacy entries and variables that must never reach a registry client.
    fake.store[(SVC, "meta-access-token")] = "LEGACY-META"
    fake.store[(SVC, "oauth-token")] = "LEGACY-GOOGLE"
    monkeypatch.setenv("META_ACCESS_TOKEN", "LEGACY-META-ENV")
    monkeypatch.setenv("OPENAI_ADS_API_KEY", "LEGACY-OPENAI-ENV")
    monkeypatch.setenv("PAGESPEED_API_KEY", "LEGACY-PS-ENV")
    monkeypatch.setenv("GA4_PROPERTY_ID", "999999999")
    return fake, path


def own_values(cid: str) -> dict[tuple[str, str], str]:
    ctx = resolve(cid)
    return {(p, k): v for p in ACCOUNT_FIELDS for k, v in ctx.accounts(p).items()}


def test_matrix_accounts_never_cross(two):
    for me, other in itertools.permutations(CLIENTS, 2):
        ctx = resolve(me)
        mine, theirs = own_values(me), own_values(other)
        for (platform, key), value in mine.items():
            assert ctx.account(platform, key) == value
            if (platform, key) not in SHARED:
                assert value != theirs.get((platform, key)), (platform, key)
        for (platform, key), value in theirs.items():
            if (platform, key) in SHARED:
                assert not ctx.owns(platform, value)  # a shared manager never proves ownership
                continue
            assert not ctx.owns(platform, value), (me, platform, key)
            with pytest.raises(ClientError) as info:
                ctx.require_owned(platform, value)
            assert info.value.code == "id_not_owned"
            assert value not in info.value.detail or len(value) <= 3


def test_matrix_own_ids_are_owned_with_normalization(two):
    acme = resolve("acme")
    assert acme.owns("google_ads", "111-111-1111")
    assert acme.owns("meta", "act_1111") and acme.owns("meta", "1111")
    assert acme.owns("ga4", "properties/111111111")
    assert acme.owns("merchant", "accounts/1111")
    assert acme.owns("gtm", "gtm-acme111")
    assert acme.owns("meta_eco", "1112") and acme.owns("meta_eco", "1113")
    assert acme.owns("gsc", "sc-domain:acme.fr")
    assert not acme.owns("gsc", "sc-domain:beta.com")
    assert not acme.owns("google_ads", "")
    assert not acme.owns("google_ads", None)  # type: ignore[arg-type]


def test_matrix_secrets_never_cross(two):
    fake, _ = two
    for me in CLIENTS:
        ctx = resolve(me)
        for kind in secrets_store.KINDS:
            fake.reads.clear()
            assert ctx.secret(kind) == secret_value(me, kind)
            assert all(user.startswith(f"{me}/") for user in fake.reads), fake.reads
        assert ctx.secret("google", write=True) == secret_value(me, "google-w")


def test_env_secret_of_one_client_is_invisible_to_the_other(two, monkeypatch):
    monkeypatch.setenv("GEM_SECRET_BETA_META", "BETA-ENV-META")
    assert resolve("beta").secret("meta") == "BETA-ENV-META"
    assert resolve("acme").secret("meta") == secret_value("acme", "meta")


def test_legacy_variables_never_reach_registry_clients(two, monkeypatch):
    fake, _ = two
    for cid in CLIENTS:
        fake.store.pop((SVC, f"{cid}/meta"))
        fake.store.pop((SVC, f"{cid}/openai"))
        fake.store.pop((SVC, f"{cid}/pagespeed"))
        fake.store.pop((SVC, f"{cid}/google"))
        ctx = resolve(cid)
        assert ctx.secret("meta") is None
        assert ctx.secret("openai") is None
        assert ctx.secret("pagespeed") is None
        assert ctx.secret("google") is None
        assert ctx.account("ga4", "property_id") != "999999999"


def test_registry_cannot_point_at_another_clients_secret(two):
    _, path = two
    for name in ("beta/meta", "meta-access-token", "beta/../acme/meta", "acme/meta/../../beta/meta"):
        write_registry(path, {"acme": dict(ACME, secrets={"meta": name}), "beta": BETA})
        with pytest.raises(ClientError) as info:
            resolve("acme")
        assert info.value.code == "registry_invalid"


def test_registry_own_explicit_secret_names(two):
    fake, path = two
    write_registry(path, {"acme": dict(ACME, secrets={"meta": "acme/meta-system-user"}), "beta": BETA})
    fake.store[(SVC, "acme/meta-system-user")] = "SYS"
    assert resolve("acme").secret("meta") == "SYS"
    assert resolve("acme").secret_entry("meta") == "acme/meta-system-user"
    assert resolve("beta").secret("meta") == secret_value("beta", "meta")


def test_context_is_immutable(two):
    ctx = resolve("acme")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.client_id = "beta"  # type: ignore[misc]
    with pytest.raises(TypeError):
        ctx._accounts["ga4"] = {"property_id": "222222222"}  # type: ignore[index]
    with pytest.raises(TypeError):
        ctx._accounts["ga4"]["property_id"] = "222222222"  # type: ignore[index]
    ctx.accounts("ga4")["property_id"] = "222222222"  # a copy
    assert ctx.account("ga4", "property_id") == "111111111"
    assert "SECRET" not in repr(ctx) and "111111111" not in repr(ctx)


def test_contexts_do_not_share_state(two):
    a1, b, a2 = resolve("acme"), resolve("beta"), resolve("acme")
    assert a1 == a2 and a1 != b
    assert a1.policy("google_ads") is not None and b.accounts("ga4") != a1.accounts("ga4")


def test_pin_isolates_the_process(two, monkeypatch):
    monkeypatch.setenv("GEM_CLIENT", "beta")
    assert resolve(None).client_id == "beta"
    with pytest.raises(ClientError) as info:
        resolve("acme")
    assert info.value.code == "pinned_client"


def test_write_policy_of_one_client_does_not_leak(two):
    clients.set_write("acme", "google_ads", True)
    assert resolve("acme").policy("google_ads").enabled
    assert not resolve("beta").policy("google_ads").enabled
    assert resolve("acme").policy("google_ads").account_allow == frozenset({"1111111111"})
    for family in WRITE_PLATFORMS:
        with pytest.raises(ClientError):
            clients.set_write("beta", family, True, account_allow=list(clients.default_account_allow("acme", family)))


def test_outputs_and_registry_hold_no_secret(two):
    _, path = two
    clients.set_write("acme", "meta", True)
    dumped = json.dumps(clients.list_clients()) + json.dumps(resolve("acme").summary(masked=False)) \
        + json.dumps(resolve("beta").summary()) + path.read_text(encoding="utf-8")
    assert "SECRET::" not in dumped and "LEGACY" not in dumped
    masked = json.dumps(resolve("acme").summary())
    assert "111111111" not in masked and "1111111111" not in masked


def test_fake_request_carries_one_client_only(two):
    """Simulated tool call: everything a request can embed comes from ONE context."""
    for me, other in itertools.permutations(CLIENTS, 2):
        ctx = resolve(me)
        request = {"url": f"https://googleads.googleapis.com/v26/customers/{ctx.account('google_ads', 'customer_id')}"
                          f"/googleAds:search", "headers": {"Authorization": f"Bearer {ctx.secret('gads')}"}}
        text = json.dumps(request)
        other_ctx = resolve(other)
        assert other_ctx.account("google_ads", "customer_id") not in text
        assert str(other_ctx.secret("gads")) not in text

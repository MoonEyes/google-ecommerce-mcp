"""The shared setup steps: complete, ordered, one bounded action each, no secret in any command."""

import re

import pytest

from google_ecommerce_mcp import setup_guide as guide
from google_ecommerce_mcp.__main__ import main


def test_every_doctor_service_has_steps_in_order():
    assert guide.SERVICE_IDS == ("ga4", "gsc", "merchant", "gtm", "pagespeed", "gads", "meta", "openai")
    for service in guide.SERVICES:
        assert service.steps, service.id
        ids = [s.id for s in service.steps]
        assert len(ids) == len(set(ids)) and all(i.startswith(service.id + "-") for i in ids)
        for step in service.steps:
            assert step.title and step.prerequisite and step.verify
            assert step.command == "" or step.command.startswith("google-ecommerce-mcp ")


def test_last_step_of_each_mandatory_service_is_the_doctor_check():
    for service in guide.SERVICES:
        if not service.optional:
            assert service.steps[-1].command == guide.doctor_command(service.id)


def test_no_secret_is_ever_passed_on_the_command_line():
    commands = [s.command for service in guide.SERVICES for s in service.steps]
    commands += [guide.SETUP_ADS, guide.SET_META, guide.SET_OPENAI, guide.CORPORATE_PROXY]
    for command in commands:
        assert not re.search(r"--(pagespeed-key|token|key|secret)\b(?!-file)", command.replace("--client-secret", ""))
        assert "EAA" not in command and "sk-" not in command


@pytest.mark.parametrize("command", sorted({s.command for service in guide.SERVICES for s in service.steps
                                            if s.command}))
def test_every_command_parses(command, monkeypatch):
    """The documented commands are valid for the real CLI (placeholders replaced, nothing executed)."""
    import argparse

    words = re.sub(r"<[^>]+>", "123", command).split()[1:]
    real = argparse.ArgumentParser.parse_args

    def parse(self, args=None, namespace=None):
        real(self, args, namespace)  # argparse exits with code 2 on an unknown flag
        raise SystemExit(0)

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", parse)
    with pytest.raises(SystemExit) as exc:
        main(words)
    assert exc.value.code == 0


def test_as_dicts_round_trip():
    data = guide.as_dicts()
    assert [d["id"] for d in data] == list(guide.SERVICE_IDS)
    assert data[5]["steps"][2]["command"].startswith("google-ecommerce-mcp install --yes --client-secret")


def test_helpers():
    assert guide.google_setup_command("t.json", merchant=True) == \
        'google-ecommerce-mcp setup --client-secret <client_secret.json> --token-file "t.json" --with-merchant'
    assert guide.setup_ads_command() == "google-ecommerce-mcp setup-ads --client-secret <client_secret.json>"
    assert guide.install_id_command("ga4", "<id>", force=True) == \
        "google-ecommerce-mcp install --yes --skip-auth --force --ga4 <id>"
    assert guide.first_command("meta") == guide.SET_META

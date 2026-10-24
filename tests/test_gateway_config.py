"""Toggling the gateway registration without breaking the gateway.

The committed config must not register the router: the gateway calls every
registered middleware at startup and exits if one is unreachable. These tests
pin the toggle and its validation, so the registration cannot silently become
active in the repository.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import gateway_config
import pytest

SANDBOX = Path(__file__).resolve().parent.parent / "sandbox"
BASE = (SANDBOX / "gateway.toml").read_text(encoding="utf-8")


def test_the_committed_config_does_not_register_the_middleware():
    assert gateway_config.middleware_names(BASE) == []
    # The block is present, but commented out.
    assert gateway_config.COMMENTED_MARKER in BASE


def test_enable_uncomments_only_the_registration_block():
    enabled = gateway_config.enable(BASE)
    parsed = tomllib.loads(enabled)
    entries = parsed["openshell"]["supervisor"]["middleware"]
    assert [entry["name"] for entry in entries] == ["ledger-router"]
    assert entries[0]["grpc_endpoint"] == "http://host.openshell.internal:50051"
    assert entries[0]["max_payload_bytes"] == 4_194_304
    assert entries[0]["timeout"] == "1500ms"
    # The prose after the block stays a comment; uncommenting it would be
    # invalid TOML.
    assert "# `allow_insecure_transport`" in enabled


def test_enable_is_idempotent():
    once = gateway_config.enable(BASE)
    twice = gateway_config.enable(once)
    assert twice == once


def test_disable_round_trips_to_the_base_registration_state():
    enabled = gateway_config.enable(BASE)
    disabled = gateway_config.disable(enabled)
    assert gateway_config.middleware_names(disabled) == []
    assert gateway_config.COMMENTED_MARKER in disabled
    # The rest of the config is not rewritten.
    assert tomllib.loads(disabled)["openshell"]["version"] == 2


def test_disable_then_enable_is_stable():
    enabled = gateway_config.enable(BASE)
    assert gateway_config.enable(gateway_config.disable(enabled)) == enabled


def test_enable_requires_the_commented_block():
    with pytest.raises(SystemExit):
        gateway_config.enable("[openshell]\nversion = 2\n")


def test_validate_rejects_missing_openshell_table():
    with pytest.raises(SystemExit):
        gateway_config.validate("version = 2\n")


def test_validate_rejects_a_middleware_without_a_name():
    text = BASE.replace('# name = "ledger-router"', '# enabled = true')
    with pytest.raises(SystemExit):
        gateway_config.validate(gateway_config.enable(text))


def test_middleware_names_reads_the_enabled_entry():
    enabled = gateway_config.enable(BASE)
    assert gateway_config.middleware_names(enabled) == ["ledger-router"]

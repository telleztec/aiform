"""Offline tests for the live interrupt suite's teardown (specs/system_test_interrupt.md)."""

import types

import pytest

from tests.system import test_cli_interrupt as live
from tests.system.provider_ledger import Ledger


@pytest.fixture
def provider_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(live, "list_droplets_tagged", lambda token, tag: [])
    monkeypatch.setattr(live, "list_firewalls", lambda token: [])
    monkeypatch.setattr(
        live, "destroy_droplet_or_shout", lambda token, did, name: calls.append(("droplet", did))
    )
    monkeypatch.setattr(
        live, "delete_firewall_directly", lambda token, fid: calls.append(("firewall", fid))
    )
    return calls


def _project_with_state(tmp_path):
    (tmp_path / ".aiform").mkdir()
    (tmp_path / ".aiform" / "state.json").write_text("{}")
    return tmp_path


def test_recorded_ids_are_deleted_even_when_the_aiform_destroy_is_interrupted(
    tmp_path, monkeypatch, provider_calls
):
    def interrupted(argv):
        raise KeyboardInterrupt

    monkeypatch.setattr(live.cli, "main", interrupted)
    monkeypatch.setattr(
        live.state, "load", lambda path, deployment: types.SimpleNamespace(resources={})
    )
    ledger = Ledger(droplet_ids={"11"}, firewall_ids={"ab"})
    with pytest.raises(KeyboardInterrupt):
        live.teardown_provider("token", ledger, _project_with_state(tmp_path))
    assert ("droplet", "11") in provider_calls
    assert ("firewall", "ab") in provider_calls


def test_recorded_ids_are_deleted_when_the_aiform_destroy_raises(
    tmp_path, monkeypatch, provider_calls
):
    def failing(argv):
        raise RuntimeError("401 invalid x-api-key")

    monkeypatch.setattr(live.cli, "main", failing)
    monkeypatch.setattr(
        live.state, "load", lambda path, deployment: types.SimpleNamespace(resources={})
    )
    ledger = Ledger(droplet_ids={"11"})
    with pytest.warns(UserWarning, match="raised"):
        live.teardown_provider("token", ledger, _project_with_state(tmp_path))
    assert provider_calls == [("droplet", "11")]

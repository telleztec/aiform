# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Run and tear down a live fault stage -- shared by the interrupt and the
timeout suites (specs/system_test_interrupt.md, "Teardown").

Not a test module: the fixtures that hand these out live in each suite.
"""

import warnings
from pathlib import Path

import pytest

from aiform import cli, state
from tests.system.conftest import (
    SYSTEM_TEST_TAG,
    assert_cli_ok,
    delete_firewall_directly,
    destroy_droplet_or_shout,
    list_droplets_tagged,
    list_firewalls,
    verbose_call_count,
)
from tests.system.fault_injection import Fault, InjectedInterrupt
from tests.system.provider_ledger import Ledger

APPLY = ["plan", "apply", "--yes"]


def teardown_provider(token, ledger: Ledger, project_dir: Path) -> None:
    """Two passes, because the first depends on the code under test and on a
    state entry existing, and an interrupted create may leave neither."""
    state_path = project_dir / ".aiform" / "state.json"
    try:
        if state_path.exists():
            try:
                ledger.note_state(state.load(state_path, deployment="default"))
                code = cli.main(
                    [
                        "plan",
                        "destroy",
                        "--all",
                        "--deployment",
                        "default",
                        "--yes",
                        "--state-file",
                        str(state_path),
                    ]
                )
                if code != 0:
                    warnings.warn(f"teardown 'plan destroy' exited {code}", stacklevel=2)
            except Exception as exc:
                warnings.warn(f"teardown 'plan destroy' raised {exc!r}", stacklevel=2)
    finally:
        failures: list[str] = []
        for droplet_id in sorted(
            ledger.droplet_ids_to_delete(lambda: list_droplets_tagged(token, SYSTEM_TEST_TAG))
        ):
            try:
                destroy_droplet_or_shout(token, droplet_id, f"droplet {droplet_id}")
            except RuntimeError as exc:
                failures.append(str(exc))
        for firewall_id in sorted(ledger.firewall_ids_to_delete(lambda: list_firewalls(token))):
            try:
                delete_firewall_directly(token, firewall_id)
            except Exception as exc:
                failures.append(f"could not delete firewall {firewall_id}: {exc!r}")
        if failures:
            raise RuntimeError("; ".join(failures))


class Runner:
    def __init__(self, ledger: Ledger, capsys):
        self.ledger = ledger
        self.capsys = capsys

    def tracked(self):
        return state.load(state.DEFAULT_STATE_PATH, deployment="default")

    def _note(self) -> None:
        self.ledger.note_state(self.tracked())

    def run(self, argv: list[str]):
        code = cli.main(argv)
        captured = self.capsys.readouterr()
        self._note()
        return code, captured

    def ok(self, argv: list[str], step: str):
        code, captured = self.run(argv)
        assert_cli_ok(code, captured, step)
        return captured

    def faulted(self, argv: list[str], injector, step: str) -> Fault:
        with injector as fault:
            try:
                code = cli.main(argv)
            except InjectedInterrupt:
                pass
            else:
                captured = self.capsys.readouterr()
                pytest.fail(
                    f"{step}: the injection point was never reached, so nothing was "
                    f"tested (the run exited {code})\n--- stderr ---\n{captured.err}"
                    f"\n--- calls seen ---\n{fault.seen}"
                )
        self.capsys.readouterr()
        self.ledger.note_response(fault.response)
        self._note()
        return fault

    def faulted_run(self, argv: list[str], injector, step: str):
        """Like `faulted`, for a fault that makes the run fail without an
        interrupt: the run returns, and the injection point must have fired."""
        with injector as fault:
            code = cli.main(argv)
        captured = self.capsys.readouterr()
        if not fault.fired:
            pytest.fail(
                f"{step}: the injection point was never reached, so nothing was "
                f"tested (the run exited {code})\n--- stderr ---\n{captured.err}"
                f"\n--- calls seen ---\n{fault.seen}"
            )
        self.ledger.note_response(fault.response)
        self._note()
        return code, captured, fault

    def assert_second_run_is_a_noop(self, keys: list[str]) -> None:
        for argv in (["plan", "apply", "--yes", "--verbose"], ["plan", "create", "--verbose"]):
            step = " ".join(argv)
            captured = self.ok(argv, step)
            assert verbose_call_count(captured) == 0, f"{step} made Anthropic calls"
            if keys:
                summary = f"Plan: 0 to create, 0 to update, 0 to destroy, {len(keys)} no-op."
                assert summary in captured.out, f"{step} was not a no-op:\n{captured.out}"
            else:
                summary = "Plan: 0 to create, 0 to update, 0 to destroy"
                assert "Plan:" not in captured.out or summary in captured.out, (
                    f"{step} planned work with nothing declared:\n{captured.out}"
                )
            for key in keys:
                assert f"= {key}: no-op" in captured.out, f"{step}: {key} is not a no-op"
            assert "Warning:" not in captured.out, f"{step} warned:\n{captured.out}"
        assert set(self.tracked().resources) == set(keys)

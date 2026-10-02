# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Live end-to-end system test for interrupted runs and for refusing to orphan
a dependent (UC-G and UC-F of specs/MULTI_RESOURCE_PRD.md), against the real
DigitalOcean and Anthropic APIs. Excluded from the default `pytest` run (see
pyproject.toml's `addopts`); run explicitly with:

    pytest -m system tests/system/test_cli_interrupt.py

One stage:

    pytest -m system tests/system/test_cli_interrupt.py -k C1

Requires ANTHROPIC_API_KEY and DIGITALOCEAN_TOKEN, with droplet and firewall
scope. See specs/system_test_interrupt.md for what each stage cuts off.

**Billable.** The create and delete stages and the UC-F tests each create a real
droplet (the smallest size, destroyed in teardown). The update stages use free
firewalls only.

What this settles that no unit test can: whether an interrupted run leaves the
provider and aiform's state in a condition a plain re-run repairs. A mock cannot
say, because it encodes the same assumption about call order the orchestrator
does.

**Stages expected to fail today.** C1 and C2 cut a droplet create off after the
provider accepted it and before aiform's state recorded it. `create()` carries
no idempotency key and looks nothing up by name, so the retry has nothing to
tell it the droplet exists and POSTs a second (#253, confirmed live). Both
assert the desired behavior -- exactly one droplet per declared name -- under
`xfail(strict=True)`. Every other stage converges.

Each stage's faulted run is expected to raise `InjectedInterrupt`, a
`KeyboardInterrupt`, from `cli.main()`. That stands for the non-zero exit
(130) a real Ctrl-C produces.
"""

import re
import warnings
from dataclasses import dataclass
from pathlib import Path

import pytest

from aiform import cli, state
from tests.system.conftest import (
    SYSTEM_TEST_TAG,
    assert_cli_ok,
    delete_firewall_directly,
    destroy_droplet_or_shout,
    ensure_system_test_tag,
    get_droplet_or_none,
    get_firewall_or_none,
    list_droplets_tagged,
    list_firewalls,
    live_token,
    token_has_firewall_scope,
    unique_droplet_name,
    unique_firewall_name,
    verbose_call_count,
    wait_until_droplet_gone,
    write_aiform_md,
    write_firewall_aiform_md,
)
from tests.system.fault_injection import (
    Fault,
    InjectedInterrupt,
    interrupt_after_request,
    interrupt_after_state_save,
    interrupt_before_request,
)
from tests.system.provider_ledger import Ledger

pytestmark = pytest.mark.system

SSH_RULE = {
    "protocol": "tcp",
    "ports": "22",
    "action": "allow",
    "sources": {"addresses": ["0.0.0.0/0"]},
}
HTTPS_RULE = {
    "protocol": "tcp",
    "ports": "443",
    "action": "allow",
    "sources": {"addresses": ["0.0.0.0/0"]},
}
MARKER_PREFIX = "AIFORM-DELETE-"
APPLY = ["plan", "apply", "--yes"]


def droplet_key(name: str) -> str:
    return f"digitalocean.compute.{name}"


def firewall_key(name: str) -> str:
    return f"digitalocean.firewall.{name}"


def droplet_ref(name: str) -> str:
    return f"${{{droplet_key(name)}:provider_id}}"


def droplets_named(token, name: str) -> list[dict]:
    # Tag-scoped on the provider side, then an exact-name match: a droplet
    # without the system-test tag is never in the response at all.
    return [d for d in list_droplets_tagged(token, SYSTEM_TEST_TAG) if d.get("name") == name]


_RETRY_DUPLICATES_DROPLET = pytest.mark.xfail(
    strict=True,
    reason="#253: a retry after an interrupted droplet create makes a second droplet",
)


def firewalls_named(token, name: str) -> list[dict]:
    # #249: a firewall cannot carry a tag, so aiform creates it as
    # `aiform-<deployment>-<name>`; every test here runs in "default".
    return [f for f in list_firewalls(token) if f.get("name") == f"aiform-default-{name}"]


def rule_ports(firewall: dict) -> set[str]:
    return {rule["ports"] for rule in firewall["inbound_rules"]}


def mark_for_deletion(path: Path) -> Path:
    marker = path.with_name(MARKER_PREFIX + path.name)
    path.rename(marker)
    return marker


def has_two_rules(entry) -> bool:
    return len(entry.attributes.get("inbound_rules") or []) == 2


def skip_without_firewall_scope(token) -> None:
    if not token_has_firewall_scope(token):
        pytest.skip("this DIGITALOCEAN_TOKEN cannot read /v2/firewalls")


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


@pytest.fixture
def ledger(project_dir):
    token = live_token()
    recorded = Ledger()
    try:
        yield recorded
    finally:
        teardown_provider(token, recorded, project_dir)


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


@pytest.fixture
def runner(ledger, capsys):
    return Runner(ledger, capsys)


class TestInterruptedCreate:
    @pytest.mark.parametrize(
        "stage",
        [
            pytest.param("C1", marks=_RETRY_DUPLICATES_DROPLET),
            pytest.param("C2", marks=_RETRY_DUPLICATES_DROPLET),
            "C3",
        ],
    )
    def test_a_rerun_converges_with_one_of_everything_declared(
        self, stage, project_dir, ledger, runner
    ):
        token = live_token()
        with_firewall = stage == "C3"
        if with_firewall:
            skip_without_firewall_scope(token)
            ensure_system_test_tag(token)

        droplet_name = unique_droplet_name(stage.lower())
        key = droplet_key(droplet_name)
        keys = [key]
        ledger.droplet_names.add(droplet_name)
        write_aiform_md(project_dir, name=droplet_name, filename="droplet.aiform.md")
        firewall_name = None
        if with_firewall:
            firewall_name = unique_firewall_name(stage.lower())
            ledger.firewall_names.add(firewall_name)
            keys.append(firewall_key(firewall_name))
            write_firewall_aiform_md(
                project_dir,
                name=firewall_name,
                inbound_rules=[SSH_RULE],
                droplet_ids=[droplet_ref(droplet_name)],
            )

        if stage == "C1":
            injector = interrupt_after_request("POST", r"/v2/droplets$")
        elif stage == "C2":
            injector = interrupt_after_request(
                "GET",
                r"/v2/droplets/\d+$",
                response_predicate=lambda body: (
                    bool(body) and body.get("droplet", {}).get("status") != "active"
                ),
            )
        else:
            injector = interrupt_after_state_save(lambda st: key in st.resources)
        runner.faulted(APPLY, injector, f"plan apply ({stage})")

        tracked = runner.tracked()
        if stage == "C3":
            assert key in tracked.resources
            live = get_droplet_or_none(token, tracked.resources[key].id)
            assert live is not None
            assert firewalls_named(token, firewall_name) == []
        else:
            assert key not in tracked.resources
            assert len(ledger.droplet_ids) == 1, "the faulted POST's response gave no droplet id"
            first_id = next(iter(ledger.droplet_ids))
            assert get_droplet_or_none(token, first_id) is not None

        runner.ok(APPLY, f"retry plan apply ({stage})")

        droplets = droplets_named(token, droplet_name)
        assert len(droplets) == 1, (
            f"expected exactly one droplet named {droplet_name}, found ids "
            f"{[d['id'] for d in droplets]}"
        )
        assert runner.tracked().resources[key].id == str(droplets[0]["id"])
        if with_firewall:
            firewalls = firewalls_named(token, firewall_name)
            assert len(firewalls) == 1, f"expected one firewall named {firewall_name}"
            assert firewalls[0]["droplet_ids"] == [droplets[0]["id"]]

        runner.assert_second_run_is_a_noop(keys)


class TestInterruptedUpdate:
    @pytest.mark.parametrize("stage", ["U1", "U2", "U3"])
    def test_a_rerun_converges_with_the_new_rules_everywhere(
        self, stage, project_dir, ledger, runner
    ):
        token = live_token()
        skip_without_firewall_scope(token)
        ensure_system_test_tag(token)

        labels = ["a", "b"] if stage == "U3" else ["a"]
        names = {label: unique_firewall_name(f"{stage.lower()}{label}") for label in labels}
        keys = {label: firewall_key(name) for label, name in names.items()}
        ledger.firewall_names.update(names.values())

        def write_all(rules: list[dict]) -> None:
            for label, name in names.items():
                write_firewall_aiform_md(
                    project_dir,
                    name=name,
                    inbound_rules=rules,
                    filename=f"firewall-{label}.aiform.md",
                )

        write_all([SSH_RULE])
        runner.ok(APPLY, "initial plan apply")
        tracked = runner.tracked()
        ids = {label: tracked.resources[keys[label]].id for label in names}
        for label in names:
            before = get_firewall_or_none(token, ids[label])
            assert before is not None
            assert rule_ports(before) == {"22"}

        write_all([SSH_RULE, HTTPS_RULE])
        first_id = re.escape(ids["a"])
        if stage == "U1":
            injector = interrupt_before_request("PUT", rf"/v2/firewalls/{first_id}$")
        elif stage == "U2":
            injector = interrupt_after_request("PUT", rf"/v2/firewalls/{first_id}$")
        else:
            injector = interrupt_after_state_save(
                lambda st: (
                    sum(has_two_rules(st.resources[k]) for k in keys.values() if k in st.resources)
                    == 1
                )
            )
        runner.faulted(APPLY, injector, f"plan apply ({stage})")

        tracked = runner.tracked()
        live = {label: get_firewall_or_none(token, ids[label]) for label in names}
        assert all(firewall is not None for firewall in live.values())
        if stage == "U1":
            assert rule_ports(live["a"]) == {"22"}
            assert not has_two_rules(tracked.resources[keys["a"]])
        elif stage == "U2":
            assert rule_ports(live["a"]) == {"22", "443"}
            assert not has_two_rules(tracked.resources[keys["a"]])
        else:
            saved = [label for label in names if has_two_rules(tracked.resources[keys[label]])]
            assert len(saved) == 1
            for label in names:
                expected = {"22", "443"} if label in saved else {"22"}
                assert rule_ports(live[label]) == expected

        runner.ok(APPLY, f"retry plan apply ({stage})")

        for label, name in names.items():
            firewalls = firewalls_named(token, name)
            assert len(firewalls) == 1, (
                f"expected one firewall named {name}, found {len(firewalls)}"
            )
            assert str(firewalls[0]["id"]) == ids[label], "the firewall was replaced, not updated"
            assert rule_ports(firewalls[0]) == {"22", "443"}

        runner.assert_second_run_is_a_noop(list(keys.values()))


class TestInterruptedDelete:
    @pytest.mark.parametrize("stage", ["D1", "D2", "D3"])
    def test_a_rerun_converges_with_everything_marked_gone(
        self, stage, project_dir, ledger, runner
    ):
        token = live_token()
        with_firewall = stage == "D3"
        if with_firewall:
            skip_without_firewall_scope(token)
            ensure_system_test_tag(token)

        droplet_name = unique_droplet_name(stage.lower())
        key = droplet_key(droplet_name)
        ledger.droplet_names.add(droplet_name)
        droplet_path = write_aiform_md(project_dir, name=droplet_name, filename="droplet.aiform.md")
        markers = []
        fw_name = fw_key = None
        firewall_path = None
        if with_firewall:
            fw_name = unique_firewall_name(stage.lower())
            fw_key = firewall_key(fw_name)
            ledger.firewall_names.add(fw_name)
            firewall_path = write_firewall_aiform_md(
                project_dir,
                name=fw_name,
                inbound_rules=[SSH_RULE],
                droplet_ids=[droplet_ref(droplet_name)],
            )

        runner.ok(APPLY, "initial plan apply")
        tracked = runner.tracked()
        droplet_id = tracked.resources[key].id
        assert get_droplet_or_none(token, droplet_id) is not None
        fw_id = tracked.resources[fw_key].id if with_firewall else None

        markers.append(mark_for_deletion(droplet_path))
        if with_firewall:
            markers.append(mark_for_deletion(firewall_path))

        if stage == "D1":
            injector = interrupt_after_request("DELETE", rf"/v2/droplets/{re.escape(droplet_id)}$")
        elif stage == "D2":
            injector = interrupt_after_state_save(lambda st: key not in st.resources)
        else:
            injector = interrupt_after_state_save(
                lambda st: fw_key not in st.resources and key in st.resources
            )
        runner.faulted(APPLY, injector, f"plan apply ({stage})")

        tracked = runner.tracked()
        assert all(marker.exists() for marker in markers), "a marker file moved before its state"
        if stage == "D1":
            assert key in tracked.resources
        elif stage == "D2":
            assert key not in tracked.resources
            gone = wait_until_droplet_gone(token, droplet_id)
            assert gone is None, f"droplet {droplet_id} survived a delete the provider accepted"
        else:
            assert fw_key not in tracked.resources
            assert key in tracked.resources
            assert get_firewall_or_none(token, fw_id) is None
            assert get_droplet_or_none(token, droplet_id) is not None

        runner.ok(APPLY, f"retry plan apply ({stage})")

        leftover = wait_until_droplet_gone(token, droplet_id)
        assert leftover is None, f"droplet {droplet_id} is still live after the retry"
        if with_firewall:
            assert get_firewall_or_none(token, fw_id) is None
        assert droplets_named(token, droplet_name) == []
        assert not list(project_dir.glob(f"{MARKER_PREFIX}*")), "a marker was not moved to trash"

        runner.assert_second_run_is_a_noop([])


@dataclass
class DropletWithFirewall:
    droplet_name: str
    droplet_key: str
    droplet_id: str
    droplet_path: Path
    firewall_name: str
    firewall_key: str
    firewall_id: str


def apply_droplet_and_dependent_firewall(
    token, label: str, project_dir: Path, ledger: Ledger, runner: Runner
) -> DropletWithFirewall:
    skip_without_firewall_scope(token)
    ensure_system_test_tag(token)
    droplet_name = unique_droplet_name(label)
    firewall_name = unique_firewall_name(label)
    ledger.droplet_names.add(droplet_name)
    ledger.firewall_names.add(firewall_name)
    droplet_path = write_aiform_md(project_dir, name=droplet_name, filename="droplet.aiform.md")
    write_firewall_aiform_md(
        project_dir,
        name=firewall_name,
        inbound_rules=[SSH_RULE],
        droplet_ids=[droplet_ref(droplet_name)],
    )
    runner.ok(APPLY, "initial plan apply")
    tracked = runner.tracked()
    pair = DropletWithFirewall(
        droplet_name=droplet_name,
        droplet_key=droplet_key(droplet_name),
        droplet_id=tracked.resources[droplet_key(droplet_name)].id,
        droplet_path=droplet_path,
        firewall_name=firewall_name,
        firewall_key=firewall_key(firewall_name),
        firewall_id=tracked.resources[firewall_key(firewall_name)].id,
    )
    assert tracked.resources[pair.firewall_key].depends_on == [pair.droplet_key]
    return pair


def assert_refused_naming_both(code: int, captured, pair: DropletWithFirewall, step: str) -> None:
    assert code == 2, f"{step} exited {code}, not a refusal\n{captured.err}\n{captured.out}"
    assert "Error:" in captured.err
    assert pair.firewall_key in captured.err
    assert pair.droplet_key in captured.err


class TestRefuseToOrphanADependent:
    def test_a_marker_and_its_dependent_in_one_run_is_refused_and_deletes_nothing(
        self, project_dir, ledger, runner
    ):
        token = live_token()
        pair = apply_droplet_and_dependent_firewall(token, "ucf", project_dir, ledger, runner)
        marker = mark_for_deletion(pair.droplet_path)

        for argv in (["plan", "create"], APPLY):
            step = " ".join(argv)
            code, captured = runner.run(argv)
            assert_refused_naming_both(code, captured, pair, step)
            assert "marked for deletion" in captured.err

            tracked = runner.tracked()
            assert pair.droplet_key in tracked.resources, f"{step} dropped the droplet from state"
            assert pair.firewall_key in tracked.resources
            assert marker.exists(), f"{step} moved the marker"
            assert get_droplet_or_none(token, pair.droplet_id) is not None, (
                f"{step} deleted the droplet"
            )
            assert get_firewall_or_none(token, pair.firewall_id) is not None

    @pytest.mark.xfail(
        strict=True,
        reason="#226: a marker path given alone skips the dependency check",
    )
    def test_a_marker_path_given_alone_is_refused_too(self, project_dir, ledger, runner):
        token = live_token()
        pair = apply_droplet_and_dependent_firewall(token, "ucf-alone", project_dir, ledger, runner)
        marker = mark_for_deletion(pair.droplet_path)

        code, captured = runner.run(["plan", "apply", str(marker), "--yes"])

        assert_refused_naming_both(code, captured, pair, "plan apply <marker>")
        assert get_droplet_or_none(token, pair.droplet_id) is not None

    def test_an_orphaned_dependent_is_reported_and_a_rerun_after_editing_it_converges(
        self, project_dir, ledger, runner
    ):
        # Pins what works TODAY while #226 is open, so the recovery is not lost
        # when the refusal above lands: the orphaning itself is not refused, the
        # next plan refuses with the dangling edge named, and dropping the dead
        # reference from the firewall's own file repairs it. When #226 is
        # fixed the first apply below exits 2 and this test needs rewriting.
        token = live_token()
        pair = apply_droplet_and_dependent_firewall(
            token, "ucf-recover", project_dir, ledger, runner
        )
        marker = mark_for_deletion(pair.droplet_path)

        runner.ok(["plan", "apply", str(marker), "--yes"], "plan apply <marker> (orphans)")
        leftover = wait_until_droplet_gone(token, pair.droplet_id)
        assert leftover is None, f"droplet {pair.droplet_id} is still live"

        code, captured = runner.run(["plan", "create"])
        assert_refused_naming_both(code, captured, pair, "plan create after the orphaning")
        assert get_firewall_or_none(token, pair.firewall_id) is not None

        write_firewall_aiform_md(
            project_dir,
            name=pair.firewall_name,
            inbound_rules=[SSH_RULE],
            filename="firewall.aiform.md",
        )
        runner.ok(APPLY, "plan apply after dropping the dead reference")

        firewall = get_firewall_or_none(token, pair.firewall_id)
        assert firewall is not None
        assert firewall["droplet_ids"] == []
        assert len(firewalls_named(token, pair.firewall_name)) == 1

        runner.assert_second_run_is_a_noop([pair.firewall_key])

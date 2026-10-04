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

**C1 and C2** cut a droplet create off after the provider accepted it and
before aiform's state recorded it. Before PR 4b (#253) the retry had nothing to
tell it the droplet exists and POSTed a second, confirmed live. `create()` now
tags the droplet with a per-resource marker and adopts a marked droplet on the
retry, so both assert the desired behavior -- exactly one droplet per declared
name -- with no `xfail`. They are written, not yet run live.

Each stage's faulted run is expected to raise `InjectedInterrupt`, a
`KeyboardInterrupt`, from `cli.main()`. That stands for the non-zero exit
(130) a real Ctrl-C produces.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.system.conftest import (
    SYSTEM_TEST_TAG,
    ensure_system_test_tag,
    get_droplet_or_none,
    get_firewall_or_none,
    list_droplets_tagged,
    list_firewalls,
    live_token,
    token_has_firewall_scope,
    unique_droplet_name,
    unique_firewall_name,
    wait_until_droplet_gone,
    wait_until_firewall_droplet_ids,
    write_aiform_md,
    write_firewall_aiform_md,
)
from tests.system.fault_injection import (
    interrupt_after_request,
    interrupt_after_state_save,
    interrupt_before_request,
)
from tests.system.live_support import APPLY, Runner, teardown_provider
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


@pytest.fixture
def ledger(project_dir):
    token = live_token()
    recorded = Ledger()
    try:
        yield recorded
    finally:
        teardown_provider(token, recorded, project_dir)


@pytest.fixture
def runner(ledger, capsys):
    return Runner(ledger, capsys)


class TestInterruptedCreate:
    @pytest.mark.parametrize(
        "stage",
        [
            "C1",
            "C2",
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

    def test_a_marker_path_given_alone_repairs_the_firewall_and_editing_its_file_converges(
        self, project_dir, ledger, runner
    ):
        # A marker path given alone leaves the firewall's file out of the run, so
        # the apply repairs the firewall (#226) instead of orphaning it. The
        # firewall's file still names the dead droplet, so the next plan refuses
        # with both keys named until that reference is dropped from the file.
        token = live_token()
        pair = apply_droplet_and_dependent_firewall(
            token, "ucf-recover", project_dir, ledger, runner
        )
        marker = mark_for_deletion(pair.droplet_path)

        runner.ok(["plan", "apply", str(marker), "--yes"], "plan apply <marker> (repairs)")
        leftover = wait_until_droplet_gone(token, pair.droplet_id)
        assert leftover is None, f"droplet {pair.droplet_id} is still live"
        repaired = wait_until_firewall_droplet_ids(token, pair.firewall_id, [])
        assert repaired is not None
        assert repaired["droplet_ids"] == []

        code, captured = runner.run(["plan", "create"])
        assert_refused_naming_both(code, captured, pair, "plan create after the repair")
        assert get_firewall_or_none(token, pair.firewall_id) is not None

        write_firewall_aiform_md(
            project_dir,
            name=pair.firewall_name,
            inbound_rules=[SSH_RULE],
            filename="firewall.aiform.md",
        )
        runner.ok(APPLY, "plan apply after dropping the dead reference")

        firewall = wait_until_firewall_droplet_ids(token, pair.firewall_id, [])
        assert firewall is not None
        assert firewall["droplet_ids"] == []
        assert len(firewalls_named(token, pair.firewall_name)) == 1

        runner.assert_second_run_is_a_noop([pair.firewall_key])

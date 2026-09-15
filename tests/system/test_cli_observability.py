# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Live end-to-end system test for `aiform resource check/metrics/status`
against a real DigitalOcean droplet, per specs/driver_observability.md.
Excluded from the default `pytest` run (see pyproject.toml's `addopts`);
run explicitly with:

    pytest -m system tests/system/

The compute driver now implements both methods, so all three verbs are
exercised end to end against a real droplet: a real health verdict from
DigitalOcean's droplet object, real samples from its monitoring
endpoints, and `status`' `live`/`config` lines from a real `read()`.

Two things this suite is the only place that can catch, because both are
properties of the live API rather than of the code:

  - a freshly created droplet's agent has not reported yet, so `metrics`
    legitimately returns nothing for a minute or two after `apply`. The
    suite asserts the empty case rather than waiting for data, because
    waiting would make a slow run look like a broken one.
  - `check` on a droplet that was powered off really does report
    FAILING, and its exit code really is 1 -- the assertion the whole
    verb exists for.
"""

import json

import pytest

from aiform import cli
from tests.system.conftest import (
    assert_cli_ok,
    get_droplet_or_none,
    live_token,
    unique_name,
    wait_until_droplet_gone,
    write_aiform_md,
)

pytestmark = pytest.mark.system


def _resource_key(name: str) -> str:
    return f"digitalocean.compute.{name}"


class TestResourceVerbsAgainstALiveDroplet:
    def test_the_three_verbs_against_a_real_droplet(
        self, project_dir, teardown_tracked_resources, capsys
    ):
        state_path = project_dir / ".aiform" / "state.json"
        name = unique_name("aiform-system-test-observ")
        key = _resource_key(name)
        token = live_token()
        md_path = write_aiform_md(project_dir, name=name)

        assert_cli_ok(
            cli.main(["plan", "apply", "--yes", "--state-file", str(state_path)]),
            capsys.readouterr(),
            "plan apply",
        )
        droplet_id = json.loads(state_path.read_text())["resources"][key]["id"]
        assert get_droplet_or_none(token, droplet_id) is not None

        # State must be byte-identical across every command below. These
        # are inspection commands: one that mutates the record makes the
        # next `plan` mean something different because you looked.
        before = state_path.read_bytes()
        backup_before = state_path.with_name(state_path.name + ".backup").read_bytes()

        # --- check: a real verdict from the live droplet object. It was
        # created moments ago and is active with a public v4, so OK, and
        # the exit code is the verdict.
        code = cli.main(["resource", "check", name, "--state-file", str(state_path)])
        out = capsys.readouterr().out
        assert code == 0, out
        assert out.startswith(f"ok  {key}  active, public v4 "), out
        # observations are hidden on an ok verdict -- one line, no block.
        assert len(out.splitlines()) == 1, out

        # --- metrics: real samples, or legitimately none. The droplet is
        # under a minute old, and DigitalOcean's agent has not pushed a
        # first point yet (probe 23) -- 200 with an empty series, not an
        # error. Asserting "either real rows or the no-samples line"
        # rather than sleeping for the agent: a wait here would turn a
        # slow provider into a failed build.
        code = cli.main(["resource", "metrics", name, "--state-file", str(state_path)])
        out = capsys.readouterr().out
        assert code == 0, out
        if out.strip() == "no samples":
            pass
        else:
            for line in out.splitlines():
                kind, metric_name, value = line.split()
                assert kind in ("gauge", "counter")
                assert float(value) == float(value)  # parses
                if kind == "counter":
                    assert metric_name.endswith("_total")

        # --- status: the verb that actually exercises new code live. Its
        # `live` line is a real read() against DigitalOcean and its
        # `config` line a real diff against the .aiform.md on disk.
        code = cli.main(["resource", "status", name, "--state-file", str(state_path)])
        out = capsys.readouterr().out
        assert code == 0, out
        lines = dict(line.split(None, 1) for line in out.splitlines())
        assert lines["live"] == "present"
        assert lines["config"] == f"in sync with {md_path.name}"
        assert droplet_id in lines["deployed"]
        assert lines["health"].startswith("ok — active, public v4 ")

        # --- the fleet form: no <name>, so every tracked resource, and
        # check's coverage line proves the leniency is visible rather
        # than inferred from an exit code that cannot express it.
        code = cli.main(["resource", "check", "--state-file", str(state_path)])
        out = capsys.readouterr().out
        assert code == 0, out
        assert out.splitlines()[-1] == "1 of 1 resources report health; 0 unsupported"

        # --- json stays a single parseable document on the live path.
        code = cli.main(
            ["resource", "status", name, "--format", "json", "--state-file", str(state_path)]
        )
        out = capsys.readouterr().out
        assert code == 0, out
        doc = json.loads(out)
        assert doc["resources"][0]["live"] == "present"
        assert doc["resources"][0]["health"]["status"] == "ok"
        assert doc["resources"][0]["health_unsupported"] is None
        assert doc["resources"][0]["health"]["observations"]["locked"] == "false"

        # --- the assertion the verb exists for: power the droplet off
        # behind aiform's back and confirm check says so, and that its
        # exit code says so. Done on this droplet rather than a third
        # one, and last, because it leaves the droplet unusable for the
        # assertions above.
        _power_off(token, droplet_id)
        code = cli.main(["resource", "check", name, "--state-file", str(state_path)])
        out = capsys.readouterr().out
        assert code == 1, out
        lines = out.splitlines()
        assert lines[0] == f'failing  {key}  status is "off"', out
        # observations appear exactly when the verdict is bad, which is
        # the whole conditional -- no flag, because the gate case and the
        # diagnosis case never overlap. Asserted by content, not by exact
        # padding: the column is padded to the widest key across the
        # block, so hardcoding it here just re-guesses what
        # tests/test_observability.py already pins character-for-character
        # -- and guessing it wrong is what this assertion first did.
        observations = dict(line.split() for line in lines[1:])
        assert observations["status"] == "off", out
        assert observations["locked"] == "false", out
        assert all(line.startswith("    ") for line in lines[1:]), out

        code = cli.main(["resource", "status", name, "--state-file", str(state_path)])
        out = capsys.readouterr().out
        assert code == 0, out
        # status exits 0 on a FAILING resource: a wrapper must not turn
        # one bad reading into a command failure.
        assert 'health    failing — status is "off"' in out, out
        # ...and it is still in sync, which is the distinction status
        # exists to draw. Powering a droplet off is not drift.
        assert f"config    in sync with {md_path.name}" in out, out

        assert state_path.read_bytes() == before, (
            "an `aiform resource` command wrote state; these commands must never do that"
        )
        # The backup too: state.save() writes one before every overwrite,
        # so an untouched backup is the second half of "nothing wrote
        # state". It exists already, from `plan apply` above.
        backup = state_path.with_name(state_path.name + ".backup")
        assert backup.read_bytes() == backup_before

    def test_status_on_a_destroyed_resource_still_says_when_it_was_deployed(
        self, project_dir, teardown_tracked_resources, capsys
    ):
        """The case that distinguishes a deletion from a crash, and the
        one `plan show` structurally cannot answer -- it prints stored
        state with zero API calls and cannot tell you the record is no
        longer true."""
        state_path = project_dir / ".aiform" / "state.json"
        name = unique_name("aiform-system-test-gone")
        key = _resource_key(name)
        token = live_token()
        write_aiform_md(project_dir, name=name)

        assert_cli_ok(
            cli.main(["plan", "apply", "--yes", "--state-file", str(state_path)]),
            capsys.readouterr(),
            "plan apply",
        )
        droplet_id = json.loads(state_path.read_text())["resources"][key]["id"]

        # Delete it out from under aiform, directly against the API --
        # the drift this command exists to report. Not `plan destroy`,
        # which would also drop the state entry and leave nothing to ask
        # about.
        driver = _load_compute_driver()
        driver.delete(droplet_id, {"DIGITALOCEAN_TOKEN": str(token)})
        wait_until_droplet_gone(token, droplet_id)

        before = state_path.read_bytes()
        code = cli.main(["resource", "status", name, "--state-file", str(state_path)])
        out = capsys.readouterr().out
        assert code == 0, out
        lines = dict(line.split(None, 1) for line in out.splitlines())
        assert lines["live"] == "missing on the provider"
        assert lines["config"] == "not applicable: resource is gone"
        assert droplet_id in lines["deployed"]

        # check reports the same vanished resource through health(),
        # which raises ResourceNotFoundError on the live 404 and reaches
        # the renderer as FAILING. Exit 1, not 2: a verdict was produced,
        # and it is a bad one.
        code = cli.main(["resource", "check", name, "--state-file", str(state_path)])
        out = capsys.readouterr().out
        assert code == 1, out
        assert out.startswith(f"failing  {key}  resource not found"), out

        assert state_path.read_bytes() == before, (
            "`aiform resource status` wrote state after observing drift; it must not -- "
            "`plan refresh` is the command that reconciles"
        )


def _power_off(token, droplet_id: str) -> None:
    """Power the droplet off directly, not through aiform: this is meant
    to be a change aiform did not make, the way a real operator's console
    click would be."""
    driver = _load_compute_driver()
    credentials = {"DIGITALOCEAN_TOKEN": str(token)}
    driver._do_action_and_wait(
        droplet_id,
        credentials,
        {"type": "power_off"},
        lambda d: d["status"] == "off",
        "system-test-power-off",
    )


def _load_compute_driver():
    # orchestrator.load_driver() rather than a direct import, so this
    # deletes through exactly the file aiform itself would load.
    from aiform import orchestrator

    return orchestrator.load_driver("digitalocean", "compute")

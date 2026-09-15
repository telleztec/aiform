# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Live end-to-end system test for `aiform resource check/metrics/status`
against a real DigitalOcean droplet, per specs/driver_observability.md.
Excluded from the default `pytest` run (see pyproject.toml's `addopts`);
run explicitly with:

    pytest -m system tests/system/

What this can and cannot prove today. No driver implements `health()` or
`metrics()` yet, so `check` and `metrics` are exercised only on their
decline paths -- which is still worth a live run, because it is the real
dynamic import of `drivers/digitalocean/compute.py` producing the real
base-class decline, not a stub. `status` is the verb that genuinely
exercises new code against the live API: its `live` and `config` lines
come from a real `read()` against DigitalOcean, and the gone-resource
case is asserted by destroying the droplet and asking again.

Extend the decline assertions into real verdict assertions when the
compute driver implements the two methods.
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

        # --- check: declines, because no driver implements health() yet.
        # Exit 2, not 0: aiform has no verdict to give, and a gate that
        # passes having assessed nothing is the failure mode to avoid.
        code = cli.main(["resource", "check", name, "--state-file", str(state_path)])
        out = capsys.readouterr().out
        assert code == 2, out
        assert out == f"unsupported  {key}  this driver does not implement health()\n"

        # --- metrics: also declines, but exit 0 -- it reports no verdict,
        # so there is nothing for its exit code to carry.
        code = cli.main(["resource", "metrics", name, "--state-file", str(state_path)])
        out = capsys.readouterr().out
        assert code == 0, out
        assert out == "unsupported: this driver does not implement metrics()\n"

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
        assert lines["health"] == "unsupported: this driver does not implement health()"

        # --- the fleet form: no <name>, so every tracked resource, and
        # check's coverage line proves the leniency is visible rather
        # than inferred from an exit code that cannot express it.
        code = cli.main(["resource", "check", "--state-file", str(state_path)])
        out = capsys.readouterr().out
        assert code == 2, out
        assert out.splitlines()[-1] == "0 of 1 resources report health; 1 unsupported"

        # --- json stays a single parseable document on the live path.
        code = cli.main(
            ["resource", "status", name, "--format", "json", "--state-file", str(state_path)]
        )
        out = capsys.readouterr().out
        assert code == 0, out
        doc = json.loads(out)
        assert doc["resources"][0]["live"] == "present"
        assert doc["resources"][0]["health"] is None
        assert doc["resources"][0]["health_unsupported"]

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

        # check reports the same resource as failing, via the same live
        # 404 -- but through health(), which declines here, so it is
        # still `unsupported` rather than `failing`. Asserted so the day
        # the driver implements health() this line fails loudly and gets
        # tightened rather than silently continuing to pass.
        code = cli.main(["resource", "check", name, "--state-file", str(state_path)])
        assert code == 2
        assert "unsupported" in capsys.readouterr().out

        assert state_path.read_bytes() == before, (
            "`aiform resource status` wrote state after observing drift; it must not -- "
            "`plan refresh` is the command that reconciles"
        )


def _load_compute_driver():
    # orchestrator.load_driver() rather than a direct import, so this
    # deletes through exactly the file aiform itself would load.
    from aiform import orchestrator

    return orchestrator.load_driver("digitalocean", "compute")

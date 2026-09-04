# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Live end-to-end system test against real DigitalOcean and Anthropic
APIs, per specs/system_test.md. Excluded from the default `pytest` run
(see pyproject.toml's `addopts`); run explicitly with:

    pytest -m system tests/system/

Requires ANTHROPIC_API_KEY and DIGITALOCEAN_TOKEN in the environment --
see tests/system/conftest.py's session-scoped skip fixture. Creates a
real, billable droplet (tagged "aiform-system-test") and makes real
Opus/Sonnet-priced calls; never run this on the default pull_request/push
CI trigger.
"""

import pytest

from aiform import cli, state
from drivers.digitalocean import compute as do_compute
from tests.system.conftest import (
    ALTERNATE_REGION,
    ALTERNATE_SIZE,
    SYSTEM_TEST_TAG,
    assert_cli_ok,
    count_driver_reads,
    get_droplet_or_none,
    list_account_ssh_key_fingerprints,
    live_token,
    unique_name,
    wait_until_droplet_gone,
    write_aiform_md,
)

pytestmark = pytest.mark.system

# Fixed rather than per-run: assigning a tag creates a tag object on the
# account, and a unique one per run would accumulate them forever.
IN_PLACE_TAG = "aiform-system-test-inplace"


def _resource_key(name: str) -> str:
    return f"digitalocean.compute.{name}"


class TestFullLifecycleSequence:
    """Cases 1-9 of specs/system_test.md's Behavior section: one ordered
    sequence sharing a single tmp_path and a single tracked droplet,
    because each step depends on state the previous one created."""

    def test_full_lifecycle(self, project_dir, teardown_tracked_resources, monkeypatch, capsys):
        state_path = project_dir / ".aiform" / "state.json"
        name = unique_name("aiform-system-test-lifecycle")
        key = _resource_key(name)
        token = live_token()

        # Case 1: `aiform init`
        code = cli.main(["init"])
        out = capsys.readouterr().out
        assert code == 0
        assert "[✓] ANTHROPIC_API_KEY" in out
        assert "[✓] DIGITALOCEAN_TOKEN" in out

        write_aiform_md(project_dir, name=name)

        # Case 2: first `plan create` on an untracked resource -- zero
        # Anthropic calls. #118 already skips categorization for an
        # untracked resource, and #119 removed gate #1 (the driver review)
        # from this path entirely, so nothing is left to call.
        code = cli.main(["plan", "create", "--state-file", str(state_path), "--verbose"])
        captured = capsys.readouterr()
        assert_cli_ok(code, captured, "case 2: first plan create")
        assert f"+ {key}: create" in captured.out
        assert "[verbose] 0 Anthropic API call(s) made" in captured.err

        # Case 3: `plan apply --yes` -- a CREATE action never triggers gate
        # #2 either (apply_plan()'s needs_review only covers DESTROY and a
        # likely-replace UPDATE), so this is zero calls too. This is the
        # strongest statement of the project's cost claim: a first `plan
        # create` and `apply` on a brand-new project cost nothing.
        code = cli.main(["plan", "apply", "--yes", "--state-file", str(state_path), "--verbose"])
        captured = capsys.readouterr()
        assert_cli_ok(code, captured, "case 3: plan apply --yes")
        assert "[verbose] 0 Anthropic API call(s) made" in captured.err

        st = state.load(state_path)
        assert key in st.resources
        entry = st.resources[key]
        assert entry.id
        assert entry.driver.sha256
        assert (project_dir / ".aiform" / "state.json.backup").exists()
        droplet_id = entry.id

        # "the printed result includes an id" -- checked via `plan show`'s
        # actual stdout surface (apply's own output never prints an id).
        cli.main(["plan", "show", "--state-file", str(state_path)])
        assert f"id: {droplet_id}" in capsys.readouterr().out

        # Case 4: second `plan create`, file unchanged -- the zero-Anthropic
        # -call no-op guarantee. ssh_keys is deliberately unset in this
        # fixture (see case 11 for the configuration where this is known
        # not to hold).
        read_calls = count_driver_reads(monkeypatch)
        code = cli.main(["plan", "create", "--state-file", str(state_path), "--verbose"])
        captured = capsys.readouterr()
        assert_cli_ok(code, captured, "case 4: unchanged plan create")
        assert "[verbose] 0 Anthropic API call(s) made" in captured.err
        assert f"= {key}: no-op" in captured.out
        assert len(read_calls) == 1

        # Case 5: `plan refresh` -- no LLM calls at all (structural, not
        # verifiable via --verbose; refresh/show never construct a
        # _CountingClient). Assert against a direct live read instead.
        code = cli.main(["plan", "refresh", "--state-file", str(state_path)])
        assert code == 0
        st = state.load(state_path)
        live = get_droplet_or_none(token, droplet_id)
        assert live is not None
        assert st.resources[key].attributes["region"] == live["region"]["slug"]
        assert st.resources[key].attributes["size"] == live["size_slug"]

        # Case 6a: in-place update (size only).
        write_aiform_md(project_dir, name=name, size=ALTERNATE_SIZE)
        code = cli.main(["plan", "create", "--state-file", str(state_path)])
        assert code == 0
        assert f"~ {key}: update" in capsys.readouterr().out

        code = cli.main(["plan", "apply", "--yes", "--state-file", str(state_path)])
        assert code == 0

        live = get_droplet_or_none(token, droplet_id)
        assert live is not None
        assert live["size_slug"] == ALTERNATE_SIZE
        assert str(live["id"]) == droplet_id  # in-place: id unchanged

        # Case 6b: in-place update (tags only) -- the regression guard for
        # issue #77, where any diff that wasn't exactly ["size"] destroyed
        # and recreated the droplet. Two things must hold: `plan` must not
        # predict a replace, and the droplet id must survive the apply.
        # --yes cannot mask a regression here: the mid-apply "Replace ...?"
        # confirmation is not skippable by it, so a driver that still forced
        # a replace would fail this apply outright on a non-TTY.
        write_aiform_md(project_dir, name=name, size=ALTERNATE_SIZE, extra_tags=[IN_PLACE_TAG])
        code = cli.main(["plan", "create", "--state-file", str(state_path)])
        assert code == 0
        tags_plan = capsys.readouterr().out
        assert f"~ {key}: update" in tags_plan
        # Model output, not driver output: likely_replace comes verbatim from
        # the intent-orchestration model (planner.py), which is only *hinted*
        # by LIKELY_REPLACE_FIELDS and can in principle flag an unhinted
        # field. Case 7 already depends on the mirror of this assertion. If
        # this line ever fails while the two id assertions below pass, the
        # driver is fine and the plan wording is the thing to look at -- a
        # spurious `true` only routes the update through the pre-apply
        # review, it does not cause a replace.
        assert "(likely replace)" not in tags_plan

        code = cli.main(["plan", "apply", "--yes", "--state-file", str(state_path)])
        captured = capsys.readouterr()
        assert_cli_ok(code, captured, "case 6b: tags-only in-place apply")

        live = get_droplet_or_none(token, droplet_id)
        assert live is not None
        assert str(live["id"]) == droplet_id  # in-place: the droplet survives
        assert sorted(live["tags"]) == sorted([SYSTEM_TEST_TAG, IN_PLACE_TAG])
        assert state.load(state_path).resources[key].id == droplet_id

        # And it must converge. `tags` is compared as a list, so if DO ever
        # returns the same tags in a different order than they were
        # submitted, this plan reports `update` again -- forever, one
        # intent-orchestration-model call per run. Mocks cannot answer
        # whether DO preserves order; this assertion makes the real API
        # answer it. Case 4 checks the same property for the initial create.
        code = cli.main(["plan", "create", "--state-file", str(state_path)])
        assert code == 0
        assert f"= {key}: no-op" in capsys.readouterr().out

        # Case 7: forced replace (region).
        write_aiform_md(project_dir, name=name, size=ALTERNATE_SIZE, region=ALTERNATE_REGION)
        code = cli.main(["plan", "create", "--state-file", str(state_path)])
        assert code == 0
        assert "(likely replace)" in capsys.readouterr().out

        code = cli.main(["plan", "apply", "--yes", "--state-file", str(state_path), "--verbose"])
        captured = capsys.readouterr()
        assert_cli_ok(code, captured, "case 7: forced-replace plan apply")
        replace_call_count = int(captured.err.split("[verbose] ")[1].split(" Anthropic")[0])
        assert replace_call_count >= 1

        # Old droplet gone. Polled, not checked once: DO's delete is async
        # (see conftest.wait_until_droplet_gone).
        leftover = wait_until_droplet_gone(token, droplet_id)
        assert leftover is None, f"replaced droplet {droplet_id} still live: {leftover}"

        st = state.load(state_path)
        replaced_id = st.resources[key].id
        assert replaced_id != droplet_id

        # Case 8: `plan destroy --yes` -- gate #2 fires unconditionally.
        code = cli.main(["plan", "destroy", "--yes", "--state-file", str(state_path), "--verbose"])
        captured = capsys.readouterr()
        assert_cli_ok(code, captured, "case 8: plan destroy --yes")
        destroy_call_count = int(captured.err.split("[verbose] ")[1].split(" Anthropic")[0])
        assert destroy_call_count >= 1

        destroyed = wait_until_droplet_gone(token, replaced_id)
        assert destroyed is None, f"destroyed droplet {replaced_id} still live: {destroyed}"
        assert list((project_dir / ".aiform" / "trash").glob("*compute*")) or list(
            (project_dir / ".aiform" / "trash").glob(f"*{name}*")
        )
        st = state.load(state_path)
        assert key not in st.resources

        # Case 9: idempotent delete -- drive the driver directly against
        # the now-untracked id; a 404 must be treated as success.
        driver = do_compute.Driver()
        result = driver.delete(replaced_id, {"DIGITALOCEAN_TOKEN": token})
        assert result is None


def test_bad_token_fails_cleanly_without_leaking_or_tracking(
    project_dir, teardown_tracked_resources, monkeypatch, capsys
):
    """Case 10: an obviously invalid DIGITALOCEAN_TOKEN must fail
    `plan apply` cleanly -- no crash, no state.json entry, no droplet,
    and the bad token's literal value must never appear in output."""
    bad_token = "dop_v1_intentionally_invalid_system_test_token"
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", bad_token)

    state_path = project_dir / ".aiform" / "state.json"
    name = unique_name("aiform-system-test-bad-token")
    write_aiform_md(project_dir, name=name)

    code = cli.main(["plan", "apply", "--yes", "--state-file", str(state_path)])
    captured = capsys.readouterr()

    assert code == 2
    assert "Error:" in captured.err
    assert bad_token not in captured.out
    assert bad_token not in captured.err

    st = state.load(state_path)
    assert _resource_key(name) not in st.resources


def test_ssh_keys_configured_no_op_guarantee_holds(project_dir, teardown_tracked_resources, capsys):
    """Case 11: this case originally documented the opposite of what it
    now asserts -- a real gap where ssh_keys (write-only on DO's side,
    read() can never recover it) produced a non-empty diff, and a real
    Anthropic call, on every plan after the first refresh. That gap is
    now closed (drivers/digitalocean/compute.py's NON_DIFFABLE_FIELDS,
    specs/digitalocean_compute.md) -- kept as its own case so a
    regression that reintroduces it starts failing here immediately."""
    token = live_token()
    fingerprints = list_account_ssh_key_fingerprints(token)
    if not fingerprints:
        pytest.skip(
            "no SSH keys registered in this DigitalOcean account -- case 11 needs at "
            "least one to exercise the real ssh_keys no-op guarantee"
        )

    state_path = project_dir / ".aiform" / "state.json"
    name = unique_name("aiform-system-test-ssh-keys")
    key = _resource_key(name)
    write_aiform_md(project_dir, name=name, ssh_keys=[fingerprints[0]])

    code = cli.main(["plan", "apply", "--yes", "--state-file", str(state_path)])
    assert_cli_ok(code, capsys.readouterr(), "case 11: ssh_keys plan apply")

    code = cli.main(["plan", "refresh", "--state-file", str(state_path)])
    assert_cli_ok(code, capsys.readouterr(), "case 11: plan refresh")

    code = cli.main(["plan", "create", "--state-file", str(state_path), "--verbose"])
    captured = capsys.readouterr()
    assert_cli_ok(code, captured, "case 11: unchanged plan create")
    assert "[verbose] 0 Anthropic API call(s) made" in captured.err
    assert f"= {key}: no-op" in captured.out

# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Live end-to-end system test for a provider call that times out mid-apply,
against the real DigitalOcean and Anthropic APIs. Excluded from the default
`pytest` run (see pyproject.toml's `addopts`); run explicitly with:

    pytest -m system tests/system/test_cli_timeout.py

One stage:

    pytest -m system tests/system/test_cli_timeout.py -k T2

Requires ANTHROPIC_API_KEY and DIGITALOCEAN_TOKEN. See
specs/system_test_interrupt.md, "Timeout stages", for what each stage does.

**Billable.** Each stage creates one real droplet (the smallest size, destroyed
in teardown).

What this settles that no unit test can: whether a client that gives up on a
slow request, while the provider goes on to do the work, leaves the provider
and aiform's state in a condition a plain re-run repairs. A real timeout cannot
be waited for, so the injector makes the caller lose a race instead
(`fail_request(..., "timeout")`): the provider is slow, the caller's deadline
passes first, and the provider still acts. T4 is the control: the same
deadline passes but the request never reaches the provider, so nothing exists
to duplicate and the retry must make exactly one resource.

A stage names no provider. It asks the `ProviderProfile` which request creates
the resource, which one polls it, where the id sits in a response and how to
list what the suite owns.
"""

import contextlib
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.system.conftest import live_token, unique_droplet_name, write_aiform_md
from tests.system.fault_injection import Fault, fail_request, rewrite_responses, skip_driver_sleeps
from tests.system.live_support import APPLY, Runner, teardown_provider
from tests.system.provider_ledger import Ledger
from tests.system.provider_profile import DIGITALOCEAN, ProviderProfile

pytestmark = pytest.mark.system

PROFILE = DIGITALOCEAN
CREATE_FAILED = "driver failed during create"


class RetryDuplicatesResource(AssertionError):
    """The retry left more than one resource under one declared name."""


class ErrorOmitsResourceId(AssertionError):
    """The provider returned a resource id before the failure; the error does not name it."""


# `raises=` keeps the marker honest: only the duplicate counts as the expected
# failure. Any other assertion that fails, an error that omits the resource id
# for one, is a different finding and must not be hidden behind #253.
_RETRY_DUPLICATES_RESOURCE = pytest.mark.xfail(
    strict=True,
    raises=RetryDuplicatesResource,
    reason="#253: a retry after a create the provider accepted makes a second resource",
)

# Once the error carries the id, T2 reaches the duplicate check and fails with
# RetryDuplicatesResource, which this marker does not accept: swap it for the
# #253 marker then.
_ERROR_OMITS_RESOURCE_ID = pytest.mark.xfail(
    strict=True,
    raises=ErrorOmitsResourceId,
    reason="a timed-out poll after an accepted create says only 'timed out', not which resource",
)


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


def owned_named(profile: ProviderProfile, token, name: str) -> list[dict]:
    return [r for r in profile.list_owned(token) if r.get("name") == name]


def stage_injector(
    stage: str, profile: ProviderProfile
) -> Callable[[], contextlib.AbstractContextManager[Fault]]:
    create_method, create_url = profile.create
    poll_method, poll_url = profile.poll
    if stage == "T1":
        return lambda: fail_request(create_method, create_url, "timeout")
    if stage == "T2":
        return lambda: fail_request(poll_method, poll_url, "timeout")
    if stage == "T4":
        return lambda: fail_request(create_method, create_url, "timeout", provider_acts=False)

    @contextlib.contextmanager
    def every_poll_reports_not_ready():
        with (
            skip_driver_sleeps({profile.poll_loop}),
            rewrite_responses(poll_method, poll_url, profile.not_ready) as fault,
        ):
            yield fault

    return every_poll_reports_not_ready


class TestTimedOutCreate:
    @pytest.mark.parametrize(
        "stage",
        [
            pytest.param("T1", marks=_RETRY_DUPLICATES_RESOURCE),
            pytest.param("T2", marks=_ERROR_OMITS_RESOURCE_ID),
            pytest.param("T3", marks=_RETRY_DUPLICATES_RESOURCE),
            pytest.param("T4"),
        ],
    )
    def test_a_rerun_converges_with_one_resource_per_declared_name(
        self, stage, project_dir: Path, ledger, runner
    ):
        token = live_token()
        name = unique_droplet_name(stage.lower())
        ledger.droplet_names.add(name)
        write_aiform_md(project_dir, name=name, filename="droplet.aiform.md")

        code, captured, fault = runner.faulted_run(
            APPLY, stage_injector(stage, PROFILE)(), f"plan apply ({stage})"
        )

        assert code != 0, (
            f"the faulted run exited 0\n--- stdout ---\n{captured.out}"
            f"\n--- stderr ---\n{captured.err}\n--- calls seen ---\n{fault.seen}"
            f"\n--- provider errors ---\n{fault.provider_errors!r}"
        )
        assert CREATE_FAILED in captured.err, (
            f"the error does not name the failed operation:\n{captured.err}"
        )
        held = owned_named(PROFILE, token, name)
        if stage == "T4":
            assert held == [], (
                f"the create never reached the provider, yet it holds "
                f"{[r['id'] for r in held]} for {name}"
            )
        else:
            assert len(held) == 1, (
                f"after the faulted run the provider holds {[r['id'] for r in held]} "
                f"for {name}, expected the one the provider accepted"
            )
            returned_id = PROFILE.resource_id(fault.response)
            assert returned_id == str(held[0]["id"]), (
                f"the provider's response carried id {returned_id}, "
                f"the live resource is {held[0]['id']}"
            )
            if stage != "T1" and returned_id not in captured.err:
                raise ErrorOmitsResourceId(
                    f"the provider returned resource {returned_id} before the failure, "
                    f"but the error does not name it:\n{captured.err}"
                )
        assert runner.tracked().resources == {}, "state recorded a create that never returned"

        runner.ok(APPLY, f"retry plan apply ({stage})")

        resources = owned_named(PROFILE, token, name)
        if len(resources) != 1:
            raise RetryDuplicatesResource(
                f"expected exactly one resource named {name}, found ids "
                f"{[r['id'] for r in resources]}"
            )
        tracked = runner.tracked()
        assert len(tracked.resources) == 1
        (entry,) = tracked.resources.values()
        assert entry.id == str(resources[0]["id"])

        runner.assert_second_run_is_a_noop(list(tracked.resources))

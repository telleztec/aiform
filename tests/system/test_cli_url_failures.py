# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Live end-to-end system test for a provider call that fails outright
mid-apply (a dropped connection, a 5xx, a rate limit), against the real
DigitalOcean and Anthropic APIs. Excluded from the default `pytest` run (see
pyproject.toml's `addopts`); run explicitly with:

    pytest -m system tests/system/test_cli_url_failures.py

One cell:

    pytest -m system tests/system/test_cli_url_failures.py -k create-reset

Requires ANTHROPIC_API_KEY and DIGITALOCEAN_TOKEN. See
specs/system_test_interrupt.md, "URL failure stages", for what each cell does.

**Billable.** Each cell creates one real droplet (the smallest size, destroyed
in teardown).

What this settles that no unit test can: whether a request that fails at the
network or the HTTP layer, after the provider may already have acted, leaves
the provider and aiform's state in a condition a plain re-run repairs. The
failure is injected (`fail_request(..., "reset" | "http_500" | "http_503" |
"http_429")`) and, for every kind but `http_429`, the real request reaches the
provider first: the caller sees a failure, the provider did the work.

No cell names a provider in its requests: it asks the `ProviderProfile` which
request creates, polls, resizes and destroys the resource, where the id sits in
a response and how to list what the suite owns. `resize-http503` still reads the
resized attribute (`size_slug`) straight from the listing.
"""

import contextlib
import time
from pathlib import Path

import pytest

from tests.system.conftest import (
    ALTERNATE_SIZE,
    live_token,
    unique_droplet_name,
    write_aiform_md,
)
from tests.system.fault_injection import Fault, fail_request
from tests.system.live_support import APPLY, Runner, teardown_provider
from tests.system.provider_ledger import Ledger
from tests.system.provider_profile import DIGITALOCEAN
from tests.system.test_cli_interrupt import MARKER_PREFIX, droplet_key, mark_for_deletion
from tests.system.test_cli_timeout import (
    CREATE_FAILED,
    ErrorOmitsResourceId,
    RetryDuplicatesResource,
    owned_named,
)

pytestmark = pytest.mark.system

PROFILE = DIGITALOCEAN
FAILURES = ("reset", "http_500", "http_503", "http_429")
GONE_TIMEOUT_SECONDS = 300
GONE_POLL_SECONDS = 5

# The same provisional markers the timeout stages use: they record what the
# first live run is expected to show, and `raises=` keeps any other failed
# assertion from hiding behind them.
_RETRY_DUPLICATES_RESOURCE = pytest.mark.xfail(
    strict=True,
    raises=RetryDuplicatesResource,
    reason="#253: a retry after a create the provider accepted makes a second resource",
)
_ERROR_OMITS_RESOURCE_ID = pytest.mark.xfail(
    strict=True,
    raises=ErrorOmitsResourceId,
    reason="a failed poll after an accepted create names no resource id",
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


def provider_acts(failure: str) -> bool:
    # A 429 is the provider refusing before it acts, so it is the one kind
    # whose request never takes effect.
    return failure != "http_429"


def failing(failure: str, method: str, url_pattern: str, **options):
    return lambda: fail_request(
        method, url_pattern, failure, provider_acts=provider_acts(failure), **options
    )


def create_cells():
    for where, marker in (
        ("create", _RETRY_DUPLICATES_RESOURCE),
        ("poll", _ERROR_OMITS_RESOURCE_ID),
    ):
        for failure in FAILURES:
            marks = [] if (where, failure) == ("create", "http_429") else [marker]
            yield pytest.param(
                where, failure, id=f"{where}-{failure.replace('_', '')}", marks=marks
            )


def wait_until_unowned(profile, token, name: str) -> list[dict]:
    # A provider's delete can be accepted before the resource stops being listed.
    deadline = time.monotonic() + GONE_TIMEOUT_SECONDS
    while True:
        held = owned_named(profile, token, name)
        if not held or time.monotonic() >= deadline:
            return held
        time.sleep(GONE_POLL_SECONDS)


def faulted_run(runner: Runner, injector: contextlib.AbstractContextManager[Fault], step: str):
    code, captured, fault = runner.faulted_run(APPLY, injector, step)
    assert code != 0, f"{step} exited 0\n{captured.out}"
    return captured, fault


class TestFailedCreate:
    @pytest.mark.parametrize("where, failure", list(create_cells()))
    def test_a_rerun_converges_with_one_resource_per_declared_name(
        self,
        where,
        failure,
        project_dir: Path,
        ledger,
        runner,
    ):
        token = live_token()
        name = unique_droplet_name(f"{where}-{failure.replace('_', '')}")
        ledger.droplet_names.add(name)
        write_aiform_md(project_dir, name=name, filename="droplet.aiform.md")
        method, url_pattern = getattr(PROFILE, where)

        step = f"plan apply ({where} {failure})"
        captured, fault = faulted_run(runner, failing(failure, method, url_pattern)(), step)

        assert CREATE_FAILED in captured.err, (
            f"the error does not name the failed operation:\n{captured.err}"
        )
        accepted = 1 if (where == "poll" or provider_acts(failure)) else 0
        held = owned_named(PROFILE, token, name)
        assert len(held) == accepted, (
            f"after the faulted run the provider holds {[r['id'] for r in held]} "
            f"for {name}, expected {accepted}"
        )
        if fault.response is not None:
            returned_id = PROFILE.resource_id(fault.response)
            assert returned_id == str(held[0]["id"]), (
                f"the provider's response carried id {returned_id}, "
                f"the live resource is {held[0]['id']}"
            )
        if where == "poll" and str(held[0]["id"]) not in captured.err:
            raise ErrorOmitsResourceId(
                f"the provider created resource {held[0]['id']} before the failure, "
                f"but the error does not name it:\n{captured.err}"
            )
        assert runner.tracked().resources == {}, "state recorded a create that never returned"

        runner.ok(APPLY, f"retry plan apply ({where} {failure})")

        resources = owned_named(PROFILE, token, name)
        if len(resources) != 1:
            raise RetryDuplicatesResource(
                f"expected exactly one resource named {name}, found ids "
                f"{[r['id'] for r in resources]}"
            )
        tracked = runner.tracked()
        (entry,) = tracked.resources.values()
        assert entry.id == str(resources[0]["id"])

        runner.assert_second_run_is_a_noop(list(tracked.resources))


class TestFailedUpdate:
    def test_a_rerun_converges_on_the_declared_size(
        self,
        project_dir: Path,
        ledger,
        runner,
    ):
        token = live_token()
        name = unique_droplet_name("resize-http503")
        key = droplet_key(name)
        ledger.droplet_names.add(name)
        write_aiform_md(project_dir, name=name, filename="droplet.aiform.md")
        runner.ok(APPLY, "initial plan apply")
        created_id = runner.tracked().resources[key].id

        write_aiform_md(project_dir, name=name, size=ALTERNATE_SIZE, filename="droplet.aiform.md")
        method, url_pattern, body_pattern = PROFILE.resize
        captured, _ = faulted_run(
            runner,
            failing("http_503", method, url_pattern, body_pattern=body_pattern)(),
            "plan apply (resize http503)",
        )

        assert "driver failed during update" in captured.err, (
            f"the error does not name the failed operation:\n{captured.err}"
        )
        held = owned_named(PROFILE, token, name)
        assert [str(r["id"]) for r in held] == [created_id], (
            f"after the faulted update the provider holds {[r['id'] for r in held]} "
            f"for {name}, expected only {created_id}"
        )
        assert runner.tracked().resources[key].id == created_id

        runner.ok(APPLY, "retry plan apply (resize http503)")

        resources = owned_named(PROFILE, token, name)
        assert [str(r["id"]) for r in resources] == [created_id], (
            "the retry replaced the resource instead of updating it"
        )
        assert resources[0]["size_slug"] == ALTERNATE_SIZE
        assert runner.tracked().resources[key].id == created_id

        runner.assert_second_run_is_a_noop([key])


class TestFailedDelete:
    def test_a_rerun_converges_with_everything_marked_gone(
        self,
        project_dir: Path,
        ledger,
        runner,
    ):
        token = live_token()
        name = unique_droplet_name("delete-http503")
        key = droplet_key(name)
        ledger.droplet_names.add(name)
        path = write_aiform_md(project_dir, name=name, filename="droplet.aiform.md")
        runner.ok(APPLY, "initial plan apply")
        created_id = runner.tracked().resources[key].id

        marker = mark_for_deletion(path)
        method, url_pattern = PROFILE.destroy
        captured, _ = faulted_run(
            runner, failing("http_503", method, url_pattern)(), "plan apply (delete http503)"
        )

        assert "driver failed during delete" in captured.err, (
            f"the error does not name the failed operation:\n{captured.err}"
        )
        assert marker.exists(), "the marker file moved before the delete was confirmed"
        assert key in runner.tracked().resources, "state dropped a delete that never returned"

        runner.ok(APPLY, "retry plan apply (delete http503)")

        leftover = wait_until_unowned(PROFILE, token, name)
        assert leftover == [], f"{created_id} is still live after the retry: {leftover}"
        assert not list(project_dir.glob(f"{MARKER_PREFIX}*")), "a marker was not moved to trash"

        runner.assert_second_run_is_a_noop([])

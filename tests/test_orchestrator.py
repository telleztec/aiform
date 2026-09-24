# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import contextlib
import hashlib
import json
import logging
import os
import pty
import subprocess
import sys
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aiform import llm, orchestrator, state
from aiform.driver import DriverUpdateNotSupported, ResourceDriver
from aiform.exceptions import DriverExecutionError, PlanBlockedError, ResourceNotFoundError
from aiform.models import DriverInfo, PlanAction, PlanEntry


class FakeTextBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class FakeResponse:
    def __init__(self, text: str):
        self.content = [FakeTextBlock(text)]
        self.stop_reason = "end_turn"
        self.usage = types.SimpleNamespace(input_tokens=0, output_tokens=0)


class FakeMessages:
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse(self._responses.pop(0))


class FakeClient:
    def __init__(self, responses: list[str]):
        self.messages = FakeMessages(responses)


class FakeDriver(ResourceDriver):
    """A directly-instantiated (not disk-loaded) ResourceDriver stub for
    apply_plan()/refresh_resource()-level tests, where PlannedResource.driver
    is just handed an instance directly -- no dynamic import involved."""

    PARAM_SCHEMA = {"type": "object", "properties": {}}
    LIKELY_REPLACE_FIELDS = ["image"]

    def __init__(
        self,
        *,
        create_result=None,
        read_result=None,
        read_exception=None,
        update_result=None,
        update_exception=None,
        delete_exception=None,
    ):
        self.create_result = create_result
        self.read_result = read_result
        self.read_exception = read_exception
        self.update_result = update_result
        self.update_exception = update_exception
        self.delete_exception = delete_exception
        self.calls: list[tuple] = []

    def create(self, name, params, credentials):
        self.calls.append(("create", name, params, credentials))
        return self.create_result

    def read(self, id, credentials):
        self.calls.append(("read", id, credentials))
        if self.read_exception:
            raise self.read_exception
        return self.read_result

    def update(self, id, current, desired, credentials):
        self.calls.append(("update", id, current, desired, credentials))
        if self.update_exception:
            raise self.update_exception
        return self.update_result

    def delete(self, id, credentials):
        self.calls.append(("delete", id, credentials))
        if self.delete_exception:
            raise self.delete_exception


class FakeDriverWithNonDiffableFields(FakeDriver):
    NON_DIFFABLE_FIELDS = ["ssh_keys"]


# Written to disk for tests that exercise load_driver()/driver_info_for()/
# build_create_plan()/build_destroy_plan(), which dynamically import a real file.
# Behavior is driven entirely by the `id`/`desired` values the orchestrator
# naturally passes in -- no shared mutable test state, since each dynamic
# import gets its own fresh module namespace.
FAKE_DRIVER_SOURCE = """\
from aiform.driver import DriverUpdateNotSupported, ResourceDriver
from aiform.exceptions import ResourceNotFoundError


class Driver(ResourceDriver):
    PARAM_SCHEMA = {"type": "object", "properties": {}}
    LIKELY_REPLACE_FIELDS = ["image"]

    def create(self, name, params, credentials):
        return {"id": "new-id-1", "name": name, **params}

    def read(self, id, credentials):
        if id == "MISSING":
            raise ResourceNotFoundError(f"resource {id} not found")
        if id == "BROKEN":
            raise RuntimeError("simulated CSP read failure")
        return {"id": id, "region": "sfo3", "size": "s-1vcpu-2gb"}

    def update(self, id, current, desired, credentials):
        if desired.get("size") == "unsupported-size":
            raise DriverUpdateNotSupported("cannot resize in place", unsupported_fields=["size"])
        return {"id": id, **{**current, **desired}}

    def delete(self, id, credentials):
        if id == "FAIL-DELETE":
            raise RuntimeError("simulated CSP delete failure")
"""


FAKE_DRIVER_SOURCE_WITH_NON_DIFFABLE_FIELDS = FAKE_DRIVER_SOURCE.replace(
    'LIKELY_REPLACE_FIELDS = ["image"]',
    'LIKELY_REPLACE_FIELDS = ["image"]\n    NON_DIFFABLE_FIELDS = ["ssh_keys"]',
)

# read() returns tags in the opposite order from what write_aiform_md()
# below declares in params -- the exact "CSP returns the same elements
# in a different order" scenario specs/unordered_fields.md fixes.
FAKE_DRIVER_SOURCE_WITH_REORDERED_TAGS = """\
from aiform.driver import DriverUpdateNotSupported, ResourceDriver
from aiform.exceptions import ResourceNotFoundError


class Driver(ResourceDriver):
    PARAM_SCHEMA = {"type": "object", "properties": {}}
    LIKELY_REPLACE_FIELDS = ["image"]
    UNORDERED_FIELDS = ["tags"]

    def create(self, name, params, credentials):
        return {"id": "new-id-1", "name": name, **params}

    def read(self, id, credentials):
        return {
            "id": id,
            "region": "sfo3",
            "size": "s-1vcpu-2gb",
            "tags": ["production", "aiform"],
        }

    def update(self, id, current, desired, credentials):
        return {"id": id, **{**current, **desired}}

    def delete(self, id, credentials):
        return None
"""

FAKE_DRIVER_SOURCE_WITH_REORDERED_TAGS_NOT_DECLARED_UNORDERED = (
    FAKE_DRIVER_SOURCE_WITH_REORDERED_TAGS.replace('    UNORDERED_FIELDS = ["tags"]\n', "")
)


@pytest.fixture
def prompts_dir(tmp_path: Path, monkeypatch) -> Path:
    directory = tmp_path / "prompts"
    directory.mkdir()
    (directory / "diff_plan.md").write_text("Categorize the diff into a plan action.\n")
    # parse_intent.md too, even though only one test reaches it: without
    # it, a test asserting "no intent parse happens" fails with a missing
    # prompt file instead of with its own assertion, which proves the
    # fixture is thin rather than the code correct.
    (directory / "parse_intent.md").write_text("Extract intent notes from the prose.\n")
    (directory / "review_driver.md").write_text("Review the driver source for correctness.\n")
    (directory / "review_plan.md").write_text("Review the plan for safety.\n")
    monkeypatch.setattr(llm, "PROMPTS_DIR", directory)
    return directory


@pytest.fixture
def drivers_dir(tmp_path: Path, monkeypatch) -> Path:
    directory = tmp_path / "drivers"
    directory.mkdir()
    monkeypatch.setattr(orchestrator, "DRIVERS_DIR", directory)
    return directory


def write_driver(
    drivers_dir: Path, provider: str, resource_type: str, source: str = FAKE_DRIVER_SOURCE
) -> Path:
    provider_dir = drivers_dir / provider
    provider_dir.mkdir(parents=True, exist_ok=True)
    path = provider_dir / f"{resource_type}.py"
    path.write_text(source)
    return path


def driver_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_aiform_md(
    path: Path,
    *,
    provider: str = "digitalocean",
    resource: str = "compute",
    name: str = "telleztec-app-01",
    params: dict | None = None,
    intent: str | None = None,
    depends_on: list[str] | None = None,
) -> str:
    if params is None:
        params = {"region": "sfo3", "size": "s-1vcpu-2gb"}
    lines = ["---", f"resource: {resource}", f"name: {name}", f"provider: {provider}"]
    if depends_on:
        lines.append("depends_on:")
        lines += [f"  - {target}" for target in depends_on]
    lines.append("params:")
    lines += [f"  {key}: {json.dumps(value)}" for key, value in params.items()]
    lines.append("---")
    # Off by default, because most callers here do not care -- but every
    # zero-LLM-call assertion in this file used to pass *because* of that
    # default rather than because of the code under test, which is how
    # #125 went unnoticed. A test that means to pin the call count must
    # pass `intent`.
    if intent:
        lines += ["", "## Intent", "", intent]
    content = "\n".join(lines) + "\n"
    path.write_text(content)
    return content


def make_driver_info(sha256: str, *, path: str = "drivers/digitalocean/compute.py") -> DriverInfo:
    return DriverInfo(
        path=path,
        sha256=sha256,
        generated_at=datetime(2026, 7, 30, 18, 22, 11, tzinfo=UTC),
    )


def make_state_entry(**overrides) -> state.StateEntry:
    from aiform.models import StateEntry

    defaults = dict(
        provider="digitalocean",
        resource_type="compute",
        name="telleztec-app-01",
        id="123456789",
        attributes={"region": "sfo3", "size": "s-1vcpu-2gb"},
        driver=make_driver_info("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b8"),
        last_applied_at="2026-07-30T18:23:05Z",
        last_refreshed_at="2026-07-31T09:10:00Z",
        aiform_md_path="examples/compute.aiform.md",
        aiform_md_sha256="abc123",
    )
    defaults.update(overrides)
    return StateEntry(**defaults)


def categorization_response(action="create", rationale="new resource", likely_replace=False) -> str:
    return json.dumps({"action": action, "rationale": rationale, "likely_replace": likely_replace})


def plan_review_response(safe_to_proceed=True, flags=None) -> str:
    return json.dumps({"safe_to_proceed": safe_to_proceed, "flags": flags or []})


def make_planned_resource(**overrides) -> "orchestrator.PlannedResource":
    defaults = dict(
        entry=PlanEntry(
            resource_key="digitalocean.compute.telleztec-app-01",
            action=PlanAction.CREATE,
            rationale="new resource",
        ),
        provider="digitalocean",
        resource_type="compute",
        name="telleztec-app-01",
        desired_params={"region": "sfo3", "size": "s-1vcpu-2gb"},
        aiform_md_path=Path("app.aiform.md"),
        current_aiform_md_sha256="filehash",
        driver=FakeDriver(create_result={"id": "new-1", "region": "sfo3", "size": "s-1vcpu-2gb"}),
        driver_info=make_driver_info("driverhash"),
        credentials={"DIGITALOCEAN_TOKEN": "x"},
        state_entry=None,
    )
    defaults.update(overrides)
    return orchestrator.PlannedResource(**defaults)


def save_state(state_path: Path, **entries) -> None:
    state.save(state.State(resources=entries), state_path)


class TestResourceKey:
    def test_assembles_provider_resource_type_name(self):
        assert (
            orchestrator.resource_key("digitalocean", "compute", "telleztec-app-01")
            == "digitalocean.compute.telleztec-app-01"
        )


class TestDiscoverFiles:
    def test_explicit_paths_returned_in_given_order(self, tmp_path: Path):
        first = tmp_path / "b.aiform.md"
        second = tmp_path / "a.aiform.md"
        first.write_text("")
        second.write_text("")

        assert orchestrator.discover_files([first, second], cwd=tmp_path) == [first, second]

    def test_none_globs_cwd_sorted(self, tmp_path: Path):
        (tmp_path / "b.aiform.md").write_text("")
        (tmp_path / "a.aiform.md").write_text("")

        result = orchestrator.discover_files(None, cwd=tmp_path)

        assert [p.name for p in result] == ["a.aiform.md", "b.aiform.md"]

    def test_empty_list_also_globs_cwd(self, tmp_path: Path):
        (tmp_path / "a.aiform.md").write_text("")

        assert len(orchestrator.discover_files([], cwd=tmp_path)) == 1

    def test_delete_marked_files_included_in_glob(self, tmp_path: Path):
        (tmp_path / "a.aiform.md").write_text("")
        (tmp_path / "AIFORM-DELETE-a.aiform.md").write_text("")

        assert len(orchestrator.discover_files(None, cwd=tmp_path)) == 2

    def test_missing_explicit_path_is_not_checked_here(self, tmp_path: Path):
        missing = tmp_path / "nope.aiform.md"

        assert orchestrator.discover_files([missing], cwd=tmp_path) == [missing]


class TestIsDeleteMarked:
    def test_true_for_prefixed_filename(self):
        assert (
            orchestrator.is_delete_marked(Path("AIFORM-DELETE-telleztec-app-01.aiform.md")) is True
        )

    def test_false_for_normal_filename(self):
        assert orchestrator.is_delete_marked(Path("telleztec-app-01.aiform.md")) is False

    def test_only_inspects_the_filename_not_full_path(self):
        assert orchestrator.is_delete_marked(Path("/some/dir/AIFORM-DELETE-x.aiform.md")) is True


class TestDriverPath:
    def test_builds_path_under_drivers_dir(self, drivers_dir: Path):
        assert orchestrator.driver_path("digitalocean", "compute") == (
            drivers_dir / "digitalocean" / "compute.py"
        )


class TestLoadDriver:
    def test_imports_and_instantiates_driver_class(self, drivers_dir: Path):
        write_driver(drivers_dir, "digitalocean", "compute")

        driver = orchestrator.load_driver("digitalocean", "compute")

        assert isinstance(driver, ResourceDriver)
        assert type(driver).__name__ == "Driver"

    def test_missing_driver_file_raises_plan_blocked_error(self, drivers_dir: Path):
        with pytest.raises(PlanBlockedError) as exc_info:
            orchestrator.load_driver("aws", "compute")
        assert "aws" in str(exc_info.value)
        assert "compute" in str(exc_info.value)

    def test_each_call_returns_a_fresh_instance(self, drivers_dir: Path):
        write_driver(drivers_dir, "digitalocean", "compute")

        first = orchestrator.load_driver("digitalocean", "compute")
        second = orchestrator.load_driver("digitalocean", "compute")

        assert first is not second

    def test_syntax_error_in_driver_source_propagates_uncaught(self, drivers_dir: Path):
        write_driver(drivers_dir, "digitalocean", "compute", source="def broken(:\n")

        with pytest.raises(SyntaxError):
            orchestrator.load_driver("digitalocean", "compute")


class TestDriverInfoFor:
    """No LLM calls on this path -- see issue #119 / specs/orchestrator.md."""

    def test_reuses_matching_hash_from_state(self, drivers_dir: Path):
        path = write_driver(drivers_dir, "digitalocean", "compute")
        trusted_info = make_driver_info(driver_sha256(path))
        entry = make_state_entry(driver=trusted_info)
        st = state.State(resources={"digitalocean.compute.telleztec-app-01": entry})

        result = orchestrator.driver_info_for("digitalocean", "compute", st)

        assert result == trusted_info

    def test_only_matches_same_provider_and_resource_type(self, drivers_dir: Path):
        path = write_driver(drivers_dir, "digitalocean", "compute")
        sha256 = driver_sha256(path)
        entry = make_state_entry(resource_type="network", driver=make_driver_info(sha256))
        st = state.State(resources={"digitalocean.network.telleztec-app-01": entry})

        result = orchestrator.driver_info_for("digitalocean", "compute", st)

        assert result.sha256 == sha256
        assert result.path == "drivers/digitalocean/compute.py"

    def test_no_matching_entry_builds_new_driver_info(self, drivers_dir: Path):
        path = write_driver(drivers_dir, "digitalocean", "compute")

        result = orchestrator.driver_info_for("digitalocean", "compute", state.State())

        assert result.sha256 == driver_sha256(path)
        assert result.path == "drivers/digitalocean/compute.py"

    def test_hash_mismatch_builds_fresh_driver_info(self, drivers_dir: Path):
        write_driver(drivers_dir, "digitalocean", "compute")
        entry = make_state_entry(driver=make_driver_info("stale-hash-does-not-match"))
        st = state.State(resources={"digitalocean.compute.telleztec-app-01": entry})

        result = orchestrator.driver_info_for("digitalocean", "compute", st)

        assert result.sha256 != "stale-hash-does-not-match"

    def test_cache_hit_logs_reused_true(self, drivers_dir: Path, caplog):
        caplog.set_level("INFO", logger="aiform.orchestrator")
        path = write_driver(drivers_dir, "digitalocean", "compute")
        entry = make_state_entry(driver=make_driver_info(driver_sha256(path)))
        st = state.State(resources={"digitalocean.compute.telleztec-app-01": entry})

        orchestrator.driver_info_for("digitalocean", "compute", st)

        record = caplog.records[0]
        assert record.reused is True

    def test_no_matching_entry_logs_reused_false(self, drivers_dir: Path, caplog):
        caplog.set_level("INFO", logger="aiform.orchestrator")
        write_driver(drivers_dir, "digitalocean", "compute")

        orchestrator.driver_info_for("digitalocean", "compute", state.State())

        record = caplog.records[0]
        assert record.reused is False


class TestRefreshResource:
    def test_success_strips_id_and_reports_not_drifted(self):
        driver = FakeDriver(read_result={"id": "123", "region": "sfo3", "status": "active"})
        entry = make_state_entry(id="123")

        attrs, drifted = orchestrator.refresh_resource(driver, entry, {"DIGITALOCEAN_TOKEN": "x"})

        assert drifted is False
        assert attrs == {"region": "sfo3", "status": "active"}
        assert "id" not in attrs

    def test_resource_not_found_returns_last_known_attributes_and_drifted(self):
        driver = FakeDriver(read_exception=ResourceNotFoundError("gone"))
        entry = make_state_entry(id="123", attributes={"region": "sfo3"})

        attrs, drifted = orchestrator.refresh_resource(driver, entry, {"DIGITALOCEAN_TOKEN": "x"})

        assert drifted is True
        assert attrs == {"region": "sfo3"}

    def test_resource_not_found_logs_a_drifted_missing_warning(self, caplog):
        caplog.set_level("INFO", logger="aiform.orchestrator")
        driver = FakeDriver(read_exception=ResourceNotFoundError("gone"))
        entry = make_state_entry(id="123")

        orchestrator.refresh_resource(driver, entry, {"DIGITALOCEAN_TOKEN": "x"})

        record = caplog.records[0]
        assert record.levelno == logging.WARNING
        assert record.drifted_missing is True
        assert record.id == "123"

    def test_success_logs_nothing(self, caplog):
        caplog.set_level("INFO", logger="aiform.orchestrator")
        driver = FakeDriver(read_result={"id": "123", "region": "sfo3"})
        entry = make_state_entry(id="123")

        orchestrator.refresh_resource(driver, entry, {"DIGITALOCEAN_TOKEN": "x"})

        assert caplog.records == []

    def test_driver_read_failure_logs_an_error_before_reraising(self, caplog):
        caplog.set_level("INFO", logger="aiform.orchestrator")
        driver = FakeDriver(read_exception=RuntimeError("network blip"))
        entry = make_state_entry(id="123")

        with pytest.raises(DriverExecutionError):
            orchestrator.refresh_resource(driver, entry, {"DIGITALOCEAN_TOKEN": "x"})

        record = caplog.records[0]
        assert record.levelno == logging.ERROR
        assert record.provider == entry.provider
        assert record.resource_type == entry.resource_type
        assert record.operation == "read"
        assert record.outcome == "error"
        assert isinstance(record.duration_ms, int)

    def test_other_exception_wrapped_in_driver_execution_error(self):
        driver = FakeDriver(read_exception=RuntimeError("connection reset"))
        entry = make_state_entry(id="123")

        with pytest.raises(DriverExecutionError) as exc_info:
            orchestrator.refresh_resource(driver, entry, {"DIGITALOCEAN_TOKEN": "x"})
        assert exc_info.value.operation == "read"
        assert exc_info.value.provider == "digitalocean"
        assert exc_info.value.resource_type == "compute"

    def test_calls_read_with_state_entry_id_and_credentials(self):
        driver = FakeDriver(read_result={"id": "123"})
        entry = make_state_entry(id="123")
        credentials = {"DIGITALOCEAN_TOKEN": "x"}

        orchestrator.refresh_resource(driver, entry, credentials)

        assert driver.calls == [("read", "123", credentials)]

    def test_read_response_missing_id_raises_driver_execution_error(self):
        driver = FakeDriver(read_result={"region": "sfo3"})
        entry = make_state_entry(id="123")

        with pytest.raises(DriverExecutionError) as exc_info:
            orchestrator.refresh_resource(driver, entry, {"DIGITALOCEAN_TOKEN": "x"})
        assert exc_info.value.operation == "read"

    def test_carries_forward_non_diffable_field_absent_from_fresh_read(self):
        # ssh_keys can never be recovered by a real read() -- rather than
        # letting the fresh (ssh_keys-less) response blank it out, the
        # prior state's last-known value must survive the refresh, so an
        # unchanged desired value still diffs as unchanged (and a genuine
        # change is still detectable -- see TestBuildCreatePlan).
        driver = FakeDriverWithNonDiffableFields(read_result={"id": "123", "region": "sfo3"})
        entry = make_state_entry(id="123", attributes={"region": "sfo3", "ssh_keys": ["key-1"]})

        attrs, drifted = orchestrator.refresh_resource(driver, entry, {"DIGITALOCEAN_TOKEN": "x"})

        assert drifted is False
        assert attrs["region"] == "sfo3"
        assert attrs["ssh_keys"] == ["key-1"]

    def test_does_not_carry_forward_when_fresh_read_already_includes_the_field(self):
        driver = FakeDriverWithNonDiffableFields(
            read_result={"id": "123", "region": "sfo3", "ssh_keys": ["fresh-value"]}
        )
        entry = make_state_entry(id="123", attributes={"region": "sfo3", "ssh_keys": ["key-1"]})

        attrs, _ = orchestrator.refresh_resource(driver, entry, {"DIGITALOCEAN_TOKEN": "x"})

        assert attrs["ssh_keys"] == ["fresh-value"]

    def test_no_carry_forward_when_prior_state_never_had_the_field(self):
        driver = FakeDriverWithNonDiffableFields(read_result={"id": "123", "region": "sfo3"})
        entry = make_state_entry(id="123", attributes={"region": "sfo3"})

        attrs, _ = orchestrator.refresh_resource(driver, entry, {"DIGITALOCEAN_TOKEN": "x"})

        assert "ssh_keys" not in attrs


class TestRefreshState:
    def test_updates_attributes_for_tracked_resources(
        self, tmp_path: Path, drivers_dir: Path, monkeypatch
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        entry = make_state_entry(id="123", attributes={"region": "stale"})
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": entry})

        result = orchestrator.refresh_state(state_path=state_path)

        updated = result.resources["digitalocean.compute.telleztec-app-01"]
        assert updated.attributes == {"region": "sfo3", "size": "s-1vcpu-2gb"}

    def test_persists_refreshed_state_to_disk(self, tmp_path: Path, drivers_dir: Path, monkeypatch):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        entry = make_state_entry(id="123", attributes={"region": "stale"})
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": entry})

        orchestrator.refresh_state(state_path=state_path)

        reloaded = state.load(state_path)
        assert (
            reloaded.resources["digitalocean.compute.telleztec-app-01"].attributes["region"]
            == "sfo3"
        )

    def test_resource_not_found_leaves_attributes_unchanged_and_tracked(
        self, tmp_path: Path, drivers_dir: Path, monkeypatch
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        entry = make_state_entry(id="MISSING", attributes={"region": "sfo3"})
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": entry})

        result = orchestrator.refresh_state(state_path=state_path)

        assert "digitalocean.compute.telleztec-app-01" in result.resources
        assert result.resources["digitalocean.compute.telleztec-app-01"].attributes == {
            "region": "sfo3"
        }

    def test_missing_credentials_raises_plan_blocked_error(
        self, tmp_path: Path, drivers_dir: Path, monkeypatch
    ):
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        write_driver(drivers_dir, "digitalocean", "compute")
        entry = make_state_entry(id="123")
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": entry})

        with pytest.raises(PlanBlockedError):
            orchestrator.refresh_state(state_path=state_path)

    def test_missing_driver_raises_plan_blocked_error(
        self, tmp_path: Path, drivers_dir: Path, monkeypatch
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        entry = make_state_entry(id="123", provider="aws")
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"aws.compute.telleztec-app-01": entry})

        with pytest.raises(PlanBlockedError):
            orchestrator.refresh_state(state_path=state_path)

    def test_empty_state_is_a_no_op(self, tmp_path: Path):
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        assert orchestrator.refresh_state(state_path=state_path).resources == {}

    def test_credentials_and_driver_resolved_once_across_shared_provider(
        self, tmp_path: Path, drivers_dir: Path, monkeypatch
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        entry1 = make_state_entry(id="123", name="app-01")
        entry2 = make_state_entry(id="123", name="app-02")
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(
            state_path,
            **{
                "digitalocean.compute.app-01": entry1,
                "digitalocean.compute.app-02": entry2,
            },
        )

        credential_calls = []
        real_resolve = orchestrator.config.resolve_credentials

        def counting_resolve(provider, *args, **kwargs):
            credential_calls.append(provider)
            return real_resolve(provider, *args, **kwargs)

        driver_calls = []
        real_load_driver = orchestrator.load_driver

        def counting_load_driver(provider, resource_type):
            driver_calls.append((provider, resource_type))
            return real_load_driver(provider, resource_type)

        monkeypatch.setattr(orchestrator.config, "resolve_credentials", counting_resolve)
        monkeypatch.setattr(orchestrator, "load_driver", counting_load_driver)

        orchestrator.refresh_state(state_path=state_path)

        assert credential_calls == ["digitalocean"]
        assert driver_calls == [("digitalocean", "compute")]


class TestBuildCreatePlan:
    def test_brand_new_resource_produces_create_entry(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = tmp_path / "app.aiform.md"
        write_aiform_md(aiform_md)
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        # Nothing is scripted. This test is named as the brand-new-resource
        # guard, so it should fail if any Anthropic call is ever
        # reintroduced here rather than passing on an unconsumed scripted
        # response the way it used to.
        client = FakeClient([])
        planned, warnings = orchestrator.build_create_plan(
            [aiform_md], state_path=state_path, client=client
        )

        assert len(planned) == 1
        pr = planned[0]
        assert pr.entry.action == PlanAction.CREATE
        assert pr.state_entry is None
        assert pr.driver is not None
        assert pr.driver_info is not None
        assert pr.credentials == {"DIGITALOCEAN_TOKEN": "dop_v1_test"}
        assert warnings == []
        assert len(client.messages.calls) == 0

    def test_existing_resource_no_op_when_unchanged_zero_llm_calls(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        driver_file = write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = tmp_path / "app.aiform.md"
        content = write_aiform_md(aiform_md)
        file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        entry = make_state_entry(
            driver=make_driver_info(driver_sha256(driver_file)), aiform_md_sha256=file_hash
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": entry})

        client = FakeClient([])
        planned, _ = orchestrator.build_create_plan(
            [aiform_md], state_path=state_path, client=client
        )

        assert planned[0].entry.action == PlanAction.NO_OP
        assert len(client.messages.calls) == 0

    def test_no_op_records_the_new_aiform_md_hash_in_state(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        # Issue #195. A prose-only edit moves the whole-file hash
        # (parser.compute_sha256 hashes the entire file), so the sha
        # conjunct of planner.py's zero-call short circuit fails and the
        # plan pays for a categorization that can only answer 'no-op'.
        # Nothing then recorded the new hash -- apply_plan() skips NO_OP
        # before any state write, and the only writes to
        # aiform_md_sha256 are the CREATE and UPDATE paths -- so the
        # resource paid that toll on *every* later plan, forever.
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        driver_file = write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = tmp_path / "app.aiform.md"
        old_content = write_aiform_md(aiform_md, intent="Runs the app tier.")
        old_hash = hashlib.sha256(old_content.encode("utf-8")).hexdigest()
        entry = make_state_entry(
            driver=make_driver_info(driver_sha256(driver_file)), aiform_md_sha256=old_hash
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": entry})

        # Same params, different prose: the diff is empty, only the hash moved.
        new_content = write_aiform_md(aiform_md, intent="Runs the primary app tier.")
        new_hash = hashlib.sha256(new_content.encode("utf-8")).hexdigest()
        assert new_hash != old_hash

        client = FakeClient(
            [
                json.dumps({"intent_notes": [{"concerns_field": "general", "guidance": "n/a"}]}),
                categorization_response(action="no-op", rationale="params unchanged"),
            ]
        )
        planned, _ = orchestrator.build_create_plan(
            [aiform_md], state_path=state_path, client=client
        )

        assert planned[0].entry.action == PlanAction.NO_OP
        saved = state.load(state_path)
        assert saved.resources["digitalocean.compute.telleztec-app-01"].aiform_md_sha256 == new_hash

    def test_second_plan_after_a_prose_only_edit_makes_zero_calls(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        # Issue #195, stated as the guarantee a user actually relies on:
        # the toll for a prose edit is paid once, not on every run. The
        # second FakeClient is empty, so any call raises IndexError rather
        # than passing on an unconsumed scripted response.
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        driver_file = write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = tmp_path / "app.aiform.md"
        old_content = write_aiform_md(aiform_md, intent="Runs the app tier.")
        entry = make_state_entry(
            driver=make_driver_info(driver_sha256(driver_file)),
            aiform_md_sha256=hashlib.sha256(old_content.encode("utf-8")).hexdigest(),
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": entry})
        write_aiform_md(aiform_md, intent="Runs the primary app tier.")

        first_client = FakeClient(
            [
                json.dumps({"intent_notes": []}),
                categorization_response(action="no-op", rationale="params unchanged"),
            ]
        )
        orchestrator.build_create_plan([aiform_md], state_path=state_path, client=first_client)

        # Pin the first run too, so this test distinguishes "the toll was
        # paid once and then cleared" from "the toll was never charged" --
        # the vacuous shape that let #125 through.
        assert len(first_client.messages.calls) == 2

        second_client = FakeClient([])
        planned, _ = orchestrator.build_create_plan(
            [aiform_md], state_path=state_path, client=second_client
        )

        assert planned[0].entry.action == PlanAction.NO_OP
        assert len(second_client.messages.calls) == 0

    def test_model_no_op_on_a_non_empty_diff_does_not_record_the_hash(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        # prompts/diff_plan.md lets the model answer "no-op" for a diff that
        # is cosmetically different but semantically identical, so a
        # model-returned NO_OP can carry a NON-empty diff. Recording the
        # hash there would be actively harmful: the diff stays non-empty, so
        # every later run still fails plan_resource()'s `not diff` conjunct
        # and still calls the model -- but parse_file() would see a matching
        # hash, skip intent extraction, and feed that call intent_notes=[]
        # forever, silently dropping the user's guidance. Caught in review of
        # the first cut of #195, which gated only on the NO_OP action.
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        driver_file = write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = tmp_path / "app.aiform.md"
        # read() returns size s-1vcpu-2gb, so this desired size is a real diff.
        write_aiform_md(
            aiform_md,
            params={"region": "sfo3", "size": "s-2vcpu-4gb"},
            intent="Prefer an in-place resize over a recreate.",
        )
        stale_hash = "0" * 64
        entry = make_state_entry(
            driver=make_driver_info(driver_sha256(driver_file)), aiform_md_sha256=stale_hash
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": entry})

        first_client = FakeClient(
            [
                json.dumps(
                    {
                        "intent_notes": [
                            {"concerns_field": "size", "guidance": "prefer in-place resize"}
                        ]
                    }
                ),
                categorization_response(action="no-op", rationale="semantically identical"),
            ]
        )
        planned, _ = orchestrator.build_create_plan(
            [aiform_md], state_path=state_path, client=first_client
        )

        assert planned[0].entry.action == PlanAction.NO_OP
        saved = state.load(state_path)
        assert (
            saved.resources["digitalocean.compute.telleztec-app-01"].aiform_md_sha256 == stale_hash
        )

        # The guidance must still reach the model on the next run.
        second_client = FakeClient(
            [
                json.dumps(
                    {
                        "intent_notes": [
                            {"concerns_field": "size", "guidance": "prefer in-place resize"}
                        ]
                    }
                ),
                categorization_response(action="no-op", rationale="semantically identical"),
            ]
        )
        orchestrator.build_create_plan([aiform_md], state_path=state_path, client=second_client)

        assert len(second_client.messages.calls) == 2
        categorization_payload = json.loads(
            second_client.messages.calls[1]["messages"][0]["content"]
        )
        assert categorization_payload["intent_notes"] == [
            {"concerns_field": "size", "guidance": "prefer in-place resize"}
        ]

    def test_non_diffable_field_mismatch_does_not_break_no_op(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        # The driver's NON_DIFFABLE_FIELDS declares ssh_keys can never be
        # recovered by read() -- refresh_resource() carries the prior
        # state's value forward instead of letting it get blanked out
        # (see TestRefreshResource), so an *unchanged* desired ssh_keys
        # value must not force a categorization call just because the
        # fake read() (like the real DO driver) never returns it.
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        driver_file = write_driver(
            drivers_dir,
            "digitalocean",
            "compute",
            source=FAKE_DRIVER_SOURCE_WITH_NON_DIFFABLE_FIELDS,
        )
        aiform_md = tmp_path / "app.aiform.md"
        content = write_aiform_md(
            aiform_md, params={"region": "sfo3", "size": "s-1vcpu-2gb", "ssh_keys": ["key-1"]}
        )
        file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        entry = make_state_entry(
            driver=make_driver_info(driver_sha256(driver_file)),
            aiform_md_sha256=file_hash,
            attributes={"region": "sfo3", "size": "s-1vcpu-2gb", "ssh_keys": ["key-1"]},
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": entry})

        client = FakeClient([])
        planned, _ = orchestrator.build_create_plan(
            [aiform_md], state_path=state_path, client=client
        )

        assert planned[0].entry.action == PlanAction.NO_OP
        assert len(client.messages.calls) == 0

    def test_genuine_non_diffable_field_change_is_still_detected_not_silently_dropped(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        # The point of carrying the prior value forward (rather than
        # excluding ssh_keys from the diff outright, an earlier -- wrong
        # -- version of this fix) is that a *genuine* edit to ssh_keys in
        # .aiform.md must still surface as a real diff and reach
        # categorize_diff(), not silently report no-op with the user's
        # intended key rotation never applied.
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        driver_file = write_driver(
            drivers_dir,
            "digitalocean",
            "compute",
            source=FAKE_DRIVER_SOURCE_WITH_NON_DIFFABLE_FIELDS,
        )
        aiform_md = tmp_path / "app.aiform.md"
        write_aiform_md(
            aiform_md, params={"region": "sfo3", "size": "s-1vcpu-2gb", "ssh_keys": ["key-B"]}
        )
        entry = make_state_entry(
            driver=make_driver_info(driver_sha256(driver_file)),
            aiform_md_sha256="stale-hash-from-before-the-edit",
            attributes={"region": "sfo3", "size": "s-1vcpu-2gb", "ssh_keys": ["key-A"]},
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": entry})

        client = FakeClient([categorization_response(action="update")])
        planned, _ = orchestrator.build_create_plan(
            [aiform_md], state_path=state_path, client=client
        )

        assert planned[0].entry.action == PlanAction.UPDATE
        assert len(client.messages.calls) == 1
        user_content = json.loads(client.messages.calls[0]["messages"][0]["content"])
        assert user_content["diff"]["ssh_keys"] == {"current": ["key-A"], "desired": ["key-B"]}

    def test_unordered_fields_reaches_plan_resource_reordered_tags_stay_no_op(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        # driver.UNORDERED_FIELDS must actually reach planner.plan_resource()
        # -- the CSP's read() returns tags in the opposite order from
        # what .aiform.md declares, and the driver declares UNORDERED_FIELDS
        # = ["tags"], so this must stay a no-op with zero LLM calls
        # (mirrors how TestNonDiffableFields threading is proven above).
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        driver_file = write_driver(
            drivers_dir,
            "digitalocean",
            "compute",
            source=FAKE_DRIVER_SOURCE_WITH_REORDERED_TAGS,
        )
        aiform_md = tmp_path / "app.aiform.md"
        content = write_aiform_md(
            aiform_md,
            params={"region": "sfo3", "size": "s-1vcpu-2gb", "tags": ["aiform", "production"]},
        )
        file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        entry = make_state_entry(
            driver=make_driver_info(driver_sha256(driver_file)),
            aiform_md_sha256=file_hash,
            attributes={"region": "sfo3", "size": "s-1vcpu-2gb", "tags": ["aiform", "production"]},
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": entry})

        client = FakeClient([])
        planned, _ = orchestrator.build_create_plan(
            [aiform_md], state_path=state_path, client=client
        )

        assert planned[0].entry.action == PlanAction.NO_OP
        assert len(client.messages.calls) == 0

    def test_driver_not_declaring_unordered_fields_is_unaffected_reordered_tags_still_diff(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        # Same reordered-tags scenario as above, but the driver does NOT
        # declare UNORDERED_FIELDS -- it must inherit the base class's
        # empty default and behave exactly as before this feature existed:
        # the reordered field is a real diff and triggers categorization.
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        driver_file = write_driver(
            drivers_dir,
            "digitalocean",
            "compute",
            source=FAKE_DRIVER_SOURCE_WITH_REORDERED_TAGS_NOT_DECLARED_UNORDERED,
        )
        aiform_md = tmp_path / "app.aiform.md"
        content = write_aiform_md(
            aiform_md,
            params={"region": "sfo3", "size": "s-1vcpu-2gb", "tags": ["aiform", "production"]},
        )
        file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        entry = make_state_entry(
            driver=make_driver_info(driver_sha256(driver_file)),
            aiform_md_sha256=file_hash,
            attributes={"region": "sfo3", "size": "s-1vcpu-2gb", "tags": ["aiform", "production"]},
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": entry})

        client = FakeClient([categorization_response(action="update")])
        planned, _ = orchestrator.build_create_plan(
            [aiform_md], state_path=state_path, client=client
        )

        assert planned[0].entry.action == PlanAction.UPDATE
        assert len(client.messages.calls) == 1

    def test_existing_resource_diff_triggers_categorization(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        driver_file = write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = tmp_path / "app.aiform.md"
        content = write_aiform_md(aiform_md, params={"region": "sfo3", "size": "s-2vcpu-4gb"})
        file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        entry = make_state_entry(
            driver=make_driver_info(driver_sha256(driver_file)), aiform_md_sha256=file_hash
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": entry})

        client = FakeClient([categorization_response(action="update")])
        planned, _ = orchestrator.build_create_plan(
            [aiform_md], state_path=state_path, client=client
        )

        assert planned[0].entry.action == PlanAction.UPDATE
        assert len(client.messages.calls) == 1

    def test_delete_marked_untracked_produces_destroy_entry_with_no_driver(self, tmp_path: Path):
        delete_path = tmp_path / "AIFORM-DELETE-telleztec-app-01.aiform.md"
        write_aiform_md(delete_path)
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        client = FakeClient([])
        planned, _ = orchestrator.build_create_plan(
            [delete_path], state_path=state_path, client=client
        )

        assert len(planned) == 1
        pr = planned[0]
        assert pr.entry.action == PlanAction.DESTROY
        assert pr.driver is None
        assert pr.driver_info is None
        assert pr.credentials is None
        assert pr.state_entry is None
        assert pr.desired_params == {}
        assert len(client.messages.calls) == 0

    def test_delete_marked_tracked_sets_state_entry(self, tmp_path: Path):
        delete_path = tmp_path / "AIFORM-DELETE-telleztec-app-01.aiform.md"
        write_aiform_md(delete_path)
        entry = make_state_entry()
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": entry})

        planned, _ = orchestrator.build_create_plan(
            [delete_path], state_path=state_path, client=FakeClient([])
        )

        assert planned[0].state_entry == entry
        assert planned[0].entry.action == PlanAction.DESTROY

    def test_untracked_resource_is_planned_create_without_asking_the_model(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        """Replaces test_structural_cross_check_blocks_update_with_no_state_entry,
        which asserted a PlanBlockedError for this exact scenario.

        That test encoded issue #117 as intended behavior: it scripted the
        model to answer 'update' for an untracked resource and required
        the plan to be blocked. But the model was only answering because
        it had been asked, and it was asked a question the caller had
        already answered -- state.resources.get(key) is None. On a live
        run the model really did answer 'update' for a brand-new domain
        resource, and a user's first `plan apply` failed with an error
        describing an internal invariant.

        The model is still scripted to answer 'update' here. It must not
        matter, because it must never be consulted.
        """
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = tmp_path / "app.aiform.md"
        write_aiform_md(aiform_md)
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        client = FakeClient([categorization_response(action="update")])
        planned, _ = orchestrator.build_create_plan(
            [aiform_md], state_path=state_path, client=client
        )

        assert [p.entry.action for p in planned] == [PlanAction.CREATE]
        assert planned[0].entry.likely_replace is False
        # No LLM call should run at all on this path any more (#119's
        # resolution removed the driver review too). If the categorization
        # request went out anyway, that is the regression this guards.
        assert len(client.messages.calls) == 0

    def test_untracked_resource_with_a_real_intent_section_still_makes_no_llm_call(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch, forbid_llm_client
    ):
        """The guard the two above only appear to provide.

        They pass because write_aiform_md() writes no `## Intent`, so
        parse_file() short-circuits before extract_intent_notes() -- not
        because the orchestrator declines to call it. That is exactly how
        #125 survived: the live suites DID write an Intent section, paid
        one intent parse, and asserted zero. This writes one and asserts
        zero, so the property is pinned in the suite CI actually runs
        rather than only in a billable live run.
        """
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = tmp_path / "app.aiform.md"
        content = write_aiform_md(aiform_md, intent="Prefer a resize over a replace.")
        assert "## Intent" in content
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        client = FakeClient([])
        planned, _ = orchestrator.build_create_plan(
            [aiform_md], state_path=state_path, client=client
        )

        assert planned[0].entry.action == PlanAction.CREATE
        assert len(client.messages.calls) == 0

    def test_untracked_resource_plans_create_even_with_no_scripted_categorization(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        """The stronger form: script nothing at all. If any call is ever
        attempted, FakeMessages.create() raises IndexError off the empty
        response list rather than silently reusing a stale answer."""
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = tmp_path / "app.aiform.md"
        write_aiform_md(aiform_md)
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        client = FakeClient([])
        planned, _ = orchestrator.build_create_plan(
            [aiform_md], state_path=state_path, client=client
        )

        assert planned[0].entry.action == PlanAction.CREATE
        assert planned[0].state_entry is None

    def test_structural_cross_check_blocks_create_with_existing_untracked_missing_state(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        driver_file = write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = tmp_path / "app.aiform.md"
        content = write_aiform_md(aiform_md, params={"region": "sfo3", "size": "s-2vcpu-4gb"})
        file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        entry = make_state_entry(
            id="123",
            driver=make_driver_info(driver_sha256(driver_file)),
            aiform_md_sha256=file_hash,
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": entry})

        client = FakeClient([categorization_response(action="create")])
        with pytest.raises(PlanBlockedError):
            orchestrator.build_create_plan([aiform_md], state_path=state_path, client=client)

    def test_drifted_missing_is_planned_create_without_asking_the_model(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        """A tracked resource that no longer exists on the provider side
        must be recreated whatever its diff says -- prompts/diff_plan.md
        already called that answer forced. Asking anyway was the sharper
        half of issue #117: neither structural cross-check fires for
        'update' on a drifted-missing resource (the first needs
        state_entry is None, the second needs CREATE), so a wrong answer
        reached apply_plan() and called driver.update() against an id
        that no longer exists -- failing mid-apply rather than at plan
        time.

        The model is scripted to answer 'update' here. It must not
        matter, because it must never be asked.
        """
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        driver_file = write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = tmp_path / "app.aiform.md"
        write_aiform_md(aiform_md)
        entry = make_state_entry(id="MISSING", driver=make_driver_info(driver_sha256(driver_file)))
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": entry})

        client = FakeClient([categorization_response(action="update")])
        planned, _ = orchestrator.build_create_plan(
            [aiform_md], state_path=state_path, client=client
        )

        assert planned[0].entry.action == PlanAction.CREATE
        assert planned[0].entry.likely_replace is False
        # The driver's sha256 already matches the state entry, so
        # driver_info_for() reuses it and no LLM call goes out at all.
        assert client.messages.calls == []

    def test_missing_credentials_raises_plan_blocked_error(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = tmp_path / "app.aiform.md"
        write_aiform_md(aiform_md)
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        client = FakeClient([])
        with pytest.raises(PlanBlockedError):
            orchestrator.build_create_plan([aiform_md], state_path=state_path, client=client)

    def test_missing_driver_raises_plan_blocked_error(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        aiform_md = tmp_path / "app.aiform.md"
        write_aiform_md(aiform_md)
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        with pytest.raises(PlanBlockedError):
            orchestrator.build_create_plan(
                [aiform_md], state_path=state_path, client=FakeClient([])
            )

    def test_driver_hash_mismatch_updates_provenance_without_a_call(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = tmp_path / "app.aiform.md"
        write_aiform_md(aiform_md)
        entry = make_state_entry(driver=make_driver_info("stale-hash"))
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": entry})

        client = FakeClient([categorization_response(action="update")])
        planned, _ = orchestrator.build_create_plan(
            [aiform_md], state_path=state_path, client=client
        )

        assert len(client.messages.calls) == 1
        assert planned[0].driver_info.sha256 != "stale-hash"

    def test_driver_and_credentials_cached_across_files_sharing_driver(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md_1 = tmp_path / "app1.aiform.md"
        aiform_md_2 = tmp_path / "app2.aiform.md"
        write_aiform_md(aiform_md_1, name="telleztec-app-01")
        write_aiform_md(aiform_md_2, name="telleztec-app-02")
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        client = FakeClient([])
        planned, _ = orchestrator.build_create_plan(
            [aiform_md_1, aiform_md_2], state_path=state_path, client=client
        )

        assert len(planned) == 2
        # driver_info_for() makes no LLM call at all, so a shared driver
        # paying for review only once (this test's original point) no
        # longer applies. What's left to prove is the caching itself: both
        # files get the exact same driver instance and credentials dict,
        # not two independently loaded/resolved copies.
        assert planned[0].driver is planned[1].driver
        assert planned[0].driver_info == planned[1].driver_info
        assert planned[0].credentials is planned[1].credentials
        assert len(client.messages.calls) == 0

    def test_state_saved_with_refreshed_attributes(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        driver_file = write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = tmp_path / "app.aiform.md"
        content = write_aiform_md(aiform_md)
        file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        entry = make_state_entry(
            driver=make_driver_info(driver_sha256(driver_file)),
            aiform_md_sha256=file_hash,
            attributes={"region": "stale", "size": "stale"},
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": entry})

        orchestrator.build_create_plan([aiform_md], state_path=state_path, client=FakeClient([]))

        reloaded = state.load(state_path)
        assert reloaded.resources["digitalocean.compute.telleztec-app-01"].attributes == {
            "region": "sfo3",
            "size": "s-1vcpu-2gb",
        }

    def test_warnings_for_untracked_resources_in_default_discover_mode(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        entry = make_state_entry()
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": entry})

        planned, warnings = orchestrator.build_create_plan(
            None, cwd=tmp_path, state_path=state_path, client=FakeClient([])
        )

        assert planned == []
        assert len(warnings) == 1
        assert "digitalocean.compute.telleztec-app-01" in warnings[0]

    def test_no_warnings_when_explicit_paths_given(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        other_entry = make_state_entry(name="other-app")
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.other-app": other_entry})
        aiform_md = tmp_path / "app.aiform.md"
        write_aiform_md(aiform_md, name="telleztec-app-01")

        client = FakeClient([])
        _, warnings = orchestrator.build_create_plan(
            [aiform_md], state_path=state_path, client=client
        )

        assert warnings == []

    def test_malformed_frontmatter_propagates_uncaught(self, tmp_path: Path):
        aiform_md = tmp_path / "bad.aiform.md"
        aiform_md.write_text("not valid frontmatter at all")
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        with pytest.raises(ValueError):
            orchestrator.build_create_plan(
                [aiform_md], state_path=state_path, client=FakeClient([])
            )


class TestBuildCreatePlanDependencyOrdering:
    def test_fan_in_orders_both_dependencies_before_the_dependent(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        db_path = tmp_path / "db.aiform.md"
        cache_path = tmp_path / "cache.aiform.md"
        app_path = tmp_path / "app.aiform.md"
        write_aiform_md(db_path, name="db-01")
        write_aiform_md(cache_path, name="cache-01")
        write_aiform_md(
            app_path,
            name="app-01",
            depends_on=["digitalocean.compute.db-01", "digitalocean.compute.cache-01"],
        )
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        # Deliberately handed out of dependency order, to prove the pass
        # reorders rather than merely preserving an already-correct order.
        client = FakeClient([])
        planned, _ = orchestrator.build_create_plan(
            [app_path, db_path, cache_path], state_path=state_path, client=client
        )

        keys = [pr.entry.resource_key for pr in planned]
        assert keys == [
            "digitalocean.compute.cache-01",
            "digitalocean.compute.db-01",
            "digitalocean.compute.app-01",
        ]
        assert all(pr.entry.action == PlanAction.CREATE for pr in planned)
        app_pr = planned[keys.index("digitalocean.compute.app-01")]
        assert app_pr.depends_on == [
            "digitalocean.compute.db-01",
            "digitalocean.compute.cache-01",
        ]
        assert len(client.messages.calls) == 0

    def test_target_only_in_state_resolves_with_no_edge(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        shared_entry = make_state_entry(name="shared-db-01")
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.shared-db-01": shared_entry})

        app_path = tmp_path / "app.aiform.md"
        write_aiform_md(app_path, name="app-01", depends_on=["digitalocean.compute.shared-db-01"])

        client = FakeClient([])
        planned, _ = orchestrator.build_create_plan(
            [app_path], state_path=state_path, client=client
        )

        assert len(planned) == 1
        assert planned[0].entry.action == PlanAction.CREATE
        assert planned[0].depends_on == ["digitalocean.compute.shared-db-01"]
        assert len(client.messages.calls) == 0

    def test_no_op_target_keeps_its_edge(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        driver_file = write_driver(drivers_dir, "digitalocean", "compute")
        db_path = tmp_path / "db.aiform.md"
        db_content = write_aiform_md(db_path, name="db-01")
        db_hash = hashlib.sha256(db_content.encode("utf-8")).hexdigest()
        db_entry = make_state_entry(
            name="db-01",
            driver=make_driver_info(driver_sha256(driver_file)),
            aiform_md_sha256=db_hash,
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.db-01": db_entry})

        app_path = tmp_path / "app.aiform.md"
        write_aiform_md(app_path, name="app-01", depends_on=["digitalocean.compute.db-01"])

        client = FakeClient([])
        planned, _ = orchestrator.build_create_plan(
            [app_path, db_path], state_path=state_path, client=client
        )

        keys = [pr.entry.resource_key for pr in planned]
        assert keys == ["digitalocean.compute.db-01", "digitalocean.compute.app-01"]
        assert planned[0].entry.action == PlanAction.NO_OP
        assert planned[1].entry.action == PlanAction.CREATE
        assert len(client.messages.calls) == 0

    def test_unchanged_dependency_graph_makes_zero_llm_calls(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        driver_file = write_driver(drivers_dir, "digitalocean", "compute")
        db_path = tmp_path / "db.aiform.md"
        cache_path = tmp_path / "cache.aiform.md"
        app_path = tmp_path / "app.aiform.md"
        db_content = write_aiform_md(db_path, name="db-01")
        cache_content = write_aiform_md(cache_path, name="cache-01")
        app_content = write_aiform_md(
            app_path,
            name="app-01",
            depends_on=["digitalocean.compute.db-01", "digitalocean.compute.cache-01"],
        )
        driver_info = make_driver_info(driver_sha256(driver_file))
        entries = {
            "digitalocean.compute.db-01": make_state_entry(
                name="db-01",
                driver=driver_info,
                aiform_md_sha256=hashlib.sha256(db_content.encode("utf-8")).hexdigest(),
            ),
            "digitalocean.compute.cache-01": make_state_entry(
                name="cache-01",
                driver=driver_info,
                aiform_md_sha256=hashlib.sha256(cache_content.encode("utf-8")).hexdigest(),
            ),
            "digitalocean.compute.app-01": make_state_entry(
                name="app-01",
                driver=driver_info,
                aiform_md_sha256=hashlib.sha256(app_content.encode("utf-8")).hexdigest(),
                depends_on=["digitalocean.compute.db-01", "digitalocean.compute.cache-01"],
            ),
        }
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **entries)

        client = FakeClient([])
        planned, _ = orchestrator.build_create_plan(
            [app_path, db_path, cache_path], state_path=state_path, client=client
        )

        assert all(pr.entry.action == PlanAction.NO_OP for pr in planned)
        keys = [pr.entry.resource_key for pr in planned]
        assert keys == [
            "digitalocean.compute.cache-01",
            "digitalocean.compute.db-01",
            "digitalocean.compute.app-01",
        ]
        assert len(client.messages.calls) == 0

    def test_duplicate_target_in_one_list_is_not_an_error_and_is_stored_verbatim(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        db_path = tmp_path / "db.aiform.md"
        app_path = tmp_path / "app.aiform.md"
        write_aiform_md(db_path, name="db-01")
        write_aiform_md(
            app_path,
            name="app-01",
            depends_on=["digitalocean.compute.db-01", "digitalocean.compute.db-01"],
        )
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        client = FakeClient([])
        planned, _ = orchestrator.build_create_plan(
            [app_path, db_path], state_path=state_path, client=client
        )

        keys = [pr.entry.resource_key for pr in planned]
        assert keys == ["digitalocean.compute.db-01", "digitalocean.compute.app-01"]
        app_pr = planned[keys.index("digitalocean.compute.app-01")]
        assert app_pr.depends_on == [
            "digitalocean.compute.db-01",
            "digitalocean.compute.db-01",
        ]

    def test_duplicate_key_across_two_files_raises_naming_both_paths(
        self, tmp_path: Path, drivers_dir: Path, monkeypatch
    ):
        # No driver written and no credential set: if the duplicate-key
        # check did not fire first, this would fail with a different error
        # (missing driver, or missing credential) instead.
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        first_path = tmp_path / "first.aiform.md"
        second_path = tmp_path / "second.aiform.md"
        write_aiform_md(first_path, name="app-01")
        write_aiform_md(second_path, name="app-01")
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        client = FakeClient([])
        with pytest.raises(PlanBlockedError) as exc_info:
            orchestrator.build_create_plan(
                [first_path, second_path], state_path=state_path, client=client
            )

        reason = exc_info.value.reason
        assert str(first_path) in reason
        assert str(second_path) in reason
        assert "no driver found" not in reason
        assert "DIGITALOCEAN_TOKEN" not in reason
        assert len(client.messages.calls) == 0

    def test_duplicate_key_live_file_and_its_own_delete_marked_copy_raises(
        self, tmp_path: Path, drivers_dir: Path, monkeypatch
    ):
        # The nastiest variant named in specs/resource_dependencies.md: a
        # user copies x.aiform.md to AIFORM-DELETE-x.aiform.md and leaves
        # the original -- same key, one live and one destroy. drivers_dir
        # is patched to an empty directory and no credential is set, same
        # as the sibling duplicate-key test above: if the check did not
        # fire first, this would fail with a missing-driver or
        # missing-credential error instead, and the reason assertions
        # below would catch that.
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        live_path = tmp_path / "app.aiform.md"
        delete_path = tmp_path / "AIFORM-DELETE-app.aiform.md"
        write_aiform_md(live_path, name="app-01")
        write_aiform_md(delete_path, name="app-01")
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        with pytest.raises(PlanBlockedError) as exc_info:
            orchestrator.build_create_plan(
                [live_path, delete_path], state_path=state_path, client=FakeClient([])
            )

        reason = exc_info.value.reason
        assert str(live_path) in reason
        assert str(delete_path) in reason
        assert "no driver found" not in reason
        assert "DIGITALOCEAN_TOKEN" not in reason

    def test_unresolvable_target_names_the_offending_target_among_several(
        self, tmp_path: Path, drivers_dir: Path, monkeypatch
    ):
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        db_path = tmp_path / "db.aiform.md"
        app_path = tmp_path / "app.aiform.md"
        write_aiform_md(db_path, name="db-01")
        write_aiform_md(
            app_path,
            name="app-01",
            depends_on=[
                "digitalocean.compute.db-01",
                "digitalocean.compute.nonexistent-01",
            ],
        )
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        client = FakeClient([])
        with pytest.raises(PlanBlockedError) as exc_info:
            orchestrator.build_create_plan(
                [db_path, app_path], state_path=state_path, client=client
            )

        reason = exc_info.value.reason
        assert "digitalocean.compute.app-01" in reason
        assert "digitalocean.compute.nonexistent-01" in reason
        assert "no driver found" not in reason
        assert "DIGITALOCEAN_TOKEN" not in reason
        assert len(client.messages.calls) == 0

    def test_delete_marked_file_exempt_from_resolution_errors(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        delete_path = tmp_path / "AIFORM-DELETE-app.aiform.md"
        write_aiform_md(
            delete_path, name="app-01", depends_on=["digitalocean.compute.long-gone-01"]
        )
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        planned, _ = orchestrator.build_create_plan(
            [delete_path], state_path=state_path, client=FakeClient([])
        )

        assert planned[0].entry.action == PlanAction.DESTROY

    def test_delete_marked_depending_on_a_live_file_is_allowed(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        live_path = tmp_path / "app.aiform.md"
        write_aiform_md(live_path, name="app-01")
        delete_path = tmp_path / "AIFORM-DELETE-old.aiform.md"
        write_aiform_md(delete_path, name="old-01", depends_on=["digitalocean.compute.app-01"])
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        client = FakeClient([])
        planned, _ = orchestrator.build_create_plan(
            [live_path, delete_path], state_path=state_path, client=client
        )

        keys = [pr.entry.resource_key for pr in planned]
        assert set(keys) == {"digitalocean.compute.app-01", "digitalocean.compute.old-01"}
        actions = {pr.entry.resource_key: pr.entry.action for pr in planned}
        assert actions["digitalocean.compute.app-01"] == PlanAction.CREATE
        assert actions["digitalocean.compute.old-01"] == PlanAction.DESTROY
        assert len(client.messages.calls) == 0

    def test_live_depends_on_same_run_delete_marked_raises(
        self, tmp_path: Path, drivers_dir: Path, monkeypatch
    ):
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        delete_path = tmp_path / "AIFORM-DELETE-db.aiform.md"
        write_aiform_md(delete_path, name="db-01")
        app_path = tmp_path / "app.aiform.md"
        write_aiform_md(app_path, name="app-01", depends_on=["digitalocean.compute.db-01"])
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        client = FakeClient([])
        with pytest.raises(PlanBlockedError) as exc_info:
            orchestrator.build_create_plan(
                [app_path, delete_path], state_path=state_path, client=client
            )

        reason = exc_info.value.reason
        assert "digitalocean.compute.app-01" in reason
        assert "digitalocean.compute.db-01" in reason
        assert "no driver found" not in reason
        assert "DIGITALOCEAN_TOKEN" not in reason
        assert len(client.messages.calls) == 0

    def test_cycle_raises_plan_blocked_error_before_driver_load_or_llm_call(
        self, tmp_path: Path, drivers_dir: Path, monkeypatch
    ):
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        a_path = tmp_path / "a.aiform.md"
        b_path = tmp_path / "b.aiform.md"
        write_aiform_md(a_path, name="a-01", depends_on=["digitalocean.compute.b-01"])
        write_aiform_md(b_path, name="b-01", depends_on=["digitalocean.compute.a-01"])
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        client = FakeClient([])
        with pytest.raises(PlanBlockedError) as exc_info:
            orchestrator.build_create_plan([a_path, b_path], state_path=state_path, client=client)

        reason = exc_info.value.reason
        assert "digitalocean.compute.a-01" in reason
        assert "digitalocean.compute.b-01" in reason
        assert "no driver found" not in reason
        assert "DIGITALOCEAN_TOKEN" not in reason
        assert len(client.messages.calls) == 0

    def test_self_dependency_raises_as_a_cycle(
        self, tmp_path: Path, drivers_dir: Path, monkeypatch
    ):
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        a_path = tmp_path / "a.aiform.md"
        write_aiform_md(a_path, name="a-01", depends_on=["digitalocean.compute.a-01"])
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        client = FakeClient([])
        with pytest.raises(PlanBlockedError) as exc_info:
            orchestrator.build_create_plan([a_path], state_path=state_path, client=client)

        reason = exc_info.value.reason
        assert "digitalocean.compute.a-01" in reason
        assert "no driver found" not in reason
        assert "DIGITALOCEAN_TOKEN" not in reason
        assert len(client.messages.calls) == 0

    def test_delete_marked_fan_in_destroyed_before_all_of_its_targets(self, tmp_path: Path):
        # The third destroy producer named in specs/resource_dependencies.md:
        # build_create_plan()'s own delete-marked branch, not just the two
        # build_destroy_plan() paths covered in TestBuildDestroyPlan below.
        db_path = tmp_path / "AIFORM-DELETE-db.aiform.md"
        cache_path = tmp_path / "AIFORM-DELETE-cache.aiform.md"
        app_path = tmp_path / "AIFORM-DELETE-app.aiform.md"
        write_aiform_md(db_path, name="db-01")
        write_aiform_md(cache_path, name="cache-01")
        write_aiform_md(
            app_path,
            name="app-01",
            depends_on=["digitalocean.compute.db-01", "digitalocean.compute.cache-01"],
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(
            state_path,
            **{
                "digitalocean.compute.db-01": make_state_entry(name="db-01"),
                "digitalocean.compute.cache-01": make_state_entry(name="cache-01"),
                "digitalocean.compute.app-01": make_state_entry(name="app-01"),
            },
        )

        # Given in dependency (create) order, to prove destroy reverses it.
        client = FakeClient([])
        planned, _ = orchestrator.build_create_plan(
            [db_path, cache_path, app_path], state_path=state_path, client=client
        )

        keys = [pr.entry.resource_key for pr in planned]
        assert keys.index("digitalocean.compute.app-01") < keys.index("digitalocean.compute.db-01")
        assert keys.index("digitalocean.compute.app-01") < keys.index(
            "digitalocean.compute.cache-01"
        )
        assert all(pr.entry.action == PlanAction.DESTROY for pr in planned)
        assert len(client.messages.calls) == 0


class TestBuildDestroyPlan:
    def test_paths_given_targets_named_resources(self, tmp_path: Path):
        aiform_md = tmp_path / "app.aiform.md"
        write_aiform_md(aiform_md)
        entry = make_state_entry()
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": entry})

        planned = orchestrator.build_destroy_plan([aiform_md], state_path=state_path)

        assert len(planned) == 1
        pr = planned[0]
        assert pr.entry.action == PlanAction.DESTROY
        assert pr.entry.resource_key == "digitalocean.compute.telleztec-app-01"
        assert pr.state_entry == entry
        assert pr.desired_params == {}
        assert pr.driver is None

    def test_no_paths_targets_all_tracked_resources(self, tmp_path: Path):
        entry1 = make_state_entry(name="app-01")
        entry2 = make_state_entry(name="app-02")
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(
            state_path,
            **{
                "digitalocean.compute.app-01": entry1,
                "digitalocean.compute.app-02": entry2,
            },
        )

        planned = orchestrator.build_destroy_plan(None, state_path=state_path)

        assert {pr.entry.resource_key for pr in planned} == {
            "digitalocean.compute.app-01",
            "digitalocean.compute.app-02",
        }

    def test_untracked_file_produces_destroy_entry_with_no_state_entry(self, tmp_path: Path):
        aiform_md = tmp_path / "app.aiform.md"
        write_aiform_md(aiform_md)
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        planned = orchestrator.build_destroy_plan([aiform_md], state_path=state_path)

        assert planned[0].state_entry is None

    def test_never_mutates_or_saves_state(self, tmp_path: Path):
        aiform_md = tmp_path / "app.aiform.md"
        write_aiform_md(aiform_md)
        entry = make_state_entry()
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": entry})
        original_content = state_path.read_text()

        orchestrator.build_destroy_plan([aiform_md], state_path=state_path)

        assert state_path.read_text() == original_content

    def test_file_driven_fan_in_destroyed_before_all_of_its_targets(self, tmp_path: Path):
        db_path = tmp_path / "db.aiform.md"
        cache_path = tmp_path / "cache.aiform.md"
        app_path = tmp_path / "app.aiform.md"
        write_aiform_md(db_path, name="db-01")
        write_aiform_md(cache_path, name="cache-01")
        write_aiform_md(
            app_path,
            name="app-01",
            depends_on=["digitalocean.compute.db-01", "digitalocean.compute.cache-01"],
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(
            state_path,
            **{
                "digitalocean.compute.db-01": make_state_entry(name="db-01"),
                "digitalocean.compute.cache-01": make_state_entry(name="cache-01"),
                "digitalocean.compute.app-01": make_state_entry(name="app-01"),
            },
        )

        # Given in dependency (create) order, to prove destroy reverses it
        # rather than merely preserving whatever order it was handed.
        planned = orchestrator.build_destroy_plan(
            [db_path, cache_path, app_path], state_path=state_path
        )

        keys = [pr.entry.resource_key for pr in planned]
        assert keys.index("digitalocean.compute.app-01") < keys.index("digitalocean.compute.db-01")
        assert keys.index("digitalocean.compute.app-01") < keys.index(
            "digitalocean.compute.cache-01"
        )
        assert all(pr.entry.action == PlanAction.DESTROY for pr in planned)

    def test_file_driven_cycle_raises_plan_blocked_error(self, tmp_path: Path):
        a_path = tmp_path / "a.aiform.md"
        b_path = tmp_path / "b.aiform.md"
        write_aiform_md(a_path, name="a-01", depends_on=["digitalocean.compute.b-01"])
        write_aiform_md(b_path, name="b-01", depends_on=["digitalocean.compute.a-01"])
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(
            state_path,
            **{
                "digitalocean.compute.a-01": make_state_entry(name="a-01"),
                "digitalocean.compute.b-01": make_state_entry(name="b-01"),
            },
        )

        with pytest.raises(PlanBlockedError):
            orchestrator.build_destroy_plan([a_path, b_path], state_path=state_path)

    def test_state_driven_fan_in_destroyed_before_all_of_its_targets(self, tmp_path: Path):
        # The invocation a user actually types: `aiform plan destroy`, no
        # file arguments -- reads StateEntry.depends_on since there are no
        # files to read frontmatter from.
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(
            state_path,
            **{
                "digitalocean.compute.db-01": make_state_entry(name="db-01"),
                "digitalocean.compute.cache-01": make_state_entry(name="cache-01"),
                "digitalocean.compute.app-01": make_state_entry(
                    name="app-01",
                    depends_on=["digitalocean.compute.db-01", "digitalocean.compute.cache-01"],
                ),
            },
        )

        planned = orchestrator.build_destroy_plan(None, state_path=state_path)

        keys = [pr.entry.resource_key for pr in planned]
        assert keys.index("digitalocean.compute.app-01") < keys.index("digitalocean.compute.db-01")
        assert keys.index("digitalocean.compute.app-01") < keys.index(
            "digitalocean.compute.cache-01"
        )
        assert all(pr.entry.action == PlanAction.DESTROY for pr in planned)

    def test_state_driven_cycle_raises_plan_blocked_error(self, tmp_path: Path):
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(
            state_path,
            **{
                "digitalocean.compute.a-01": make_state_entry(
                    name="a-01", depends_on=["digitalocean.compute.b-01"]
                ),
                "digitalocean.compute.b-01": make_state_entry(
                    name="b-01", depends_on=["digitalocean.compute.a-01"]
                ),
            },
        )

        with pytest.raises(PlanBlockedError):
            orchestrator.build_destroy_plan(None, state_path=state_path)


class TestBuildPlanSummary:
    def test_serializes_resource_key_action_rationale_likely_replace(self):
        entry = PlanEntry(
            resource_key="digitalocean.compute.telleztec-app-01",
            action=PlanAction.UPDATE,
            rationale="size changed",
            likely_replace=True,
        )
        pr = make_planned_resource(entry=entry)

        summary = json.loads(orchestrator.build_plan_summary([pr]))

        assert summary == [
            {
                "resource_key": "digitalocean.compute.telleztec-app-01",
                "action": "update",
                "rationale": "size changed",
                "likely_replace": True,
            }
        ]


class TestApplyPlan:
    def test_no_op_entry_is_skipped_and_not_persisted(self, tmp_path: Path):
        pr = make_planned_resource(
            entry=PlanEntry(
                resource_key="digitalocean.compute.telleztec-app-01",
                action=PlanAction.NO_OP,
                rationale="no changes",
            )
        )
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        result = orchestrator.apply_plan([pr], state_path=state_path, yes=True)

        assert result.executed == []
        assert result.aborted is False

    def test_create_writes_new_state_entry_and_strips_id(self, tmp_path: Path):
        driver = FakeDriver(create_result={"id": "new-1", "region": "sfo3", "size": "s-1vcpu-2gb"})
        pr = make_planned_resource(driver=driver, state_entry=None)
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        result = orchestrator.apply_plan([pr], state_path=state_path, yes=True)

        assert result.executed == [pr.entry]
        saved = state.load(state_path)
        entry = saved.resources["digitalocean.compute.telleztec-app-01"]
        assert entry.id == "new-1"
        assert entry.attributes == {"region": "sfo3", "size": "s-1vcpu-2gb"}
        assert "id" not in entry.attributes

    def test_create_persists_depends_on_to_the_new_state_entry(self, tmp_path: Path):
        driver = FakeDriver(create_result={"id": "new-1", "region": "sfo3", "size": "s-1vcpu-2gb"})
        pr = make_planned_resource(
            driver=driver,
            state_entry=None,
            depends_on=["digitalocean.compute.db-01", "digitalocean.compute.cache-01"],
        )
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        orchestrator.apply_plan([pr], state_path=state_path, yes=True)

        saved = state.load(state_path)
        entry = saved.resources["digitalocean.compute.telleztec-app-01"]
        assert entry.depends_on == ["digitalocean.compute.db-01", "digitalocean.compute.cache-01"]

    def test_create_wraps_raw_driver_exception_in_driver_execution_error(self, tmp_path: Path):
        driver = FakeDriver()

        def boom(*args, **kwargs):
            raise RuntimeError("CSP rate limited")

        driver.create = boom
        pr = make_planned_resource(driver=driver, state_entry=None)
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        with pytest.raises(DriverExecutionError) as exc_info:
            orchestrator.apply_plan([pr], state_path=state_path, yes=True)
        assert exc_info.value.operation == "create"

    def test_create_success_logs_provider_operation_and_outcome(self, tmp_path: Path, caplog):
        caplog.set_level("INFO", logger="aiform.orchestrator")
        driver = FakeDriver(create_result={"id": "new-1", "region": "sfo3"})
        pr = make_planned_resource(driver=driver, state_entry=None)
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        orchestrator.apply_plan([pr], state_path=state_path, yes=True)

        record = next(r for r in caplog.records if getattr(r, "operation", None) == "create")
        assert record.provider == "digitalocean"
        assert record.resource_type == "compute"
        assert record.outcome == "success"
        assert record.levelno == logging.INFO

    def test_create_failure_logs_error_before_raising(self, tmp_path: Path, caplog):
        caplog.set_level("INFO", logger="aiform.orchestrator")
        driver = FakeDriver()

        def boom(*args, **kwargs):
            raise RuntimeError("CSP rate limited")

        driver.create = boom
        pr = make_planned_resource(driver=driver, state_entry=None)
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        with pytest.raises(DriverExecutionError):
            orchestrator.apply_plan([pr], state_path=state_path, yes=True)

        record = next(r for r in caplog.records if getattr(r, "operation", None) == "create")
        assert record.outcome == "error"
        assert record.levelno == logging.ERROR

    def test_update_without_replace_updates_existing_entry(self, tmp_path: Path):
        driver = FakeDriver(update_result={"id": "123", "region": "sfo3", "size": "s-2vcpu-4gb"})
        existing = make_state_entry(id="123", attributes={"region": "sfo3", "size": "s-1vcpu-2gb"})
        pr = make_planned_resource(
            entry=PlanEntry(
                resource_key="digitalocean.compute.telleztec-app-01",
                action=PlanAction.UPDATE,
                rationale="resize",
            ),
            driver=driver,
            state_entry=existing,
            desired_params={"region": "sfo3", "size": "s-2vcpu-4gb"},
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": existing})

        result = orchestrator.apply_plan([pr], state_path=state_path, yes=True)

        saved = state.load(state_path)
        assert saved.resources["digitalocean.compute.telleztec-app-01"].attributes["size"] == (
            "s-2vcpu-4gb"
        )
        assert result.executed == [pr.entry]

    def test_update_without_replace_persists_depends_on_in_place(self, tmp_path: Path):
        driver = FakeDriver(update_result={"id": "123", "region": "sfo3", "size": "s-2vcpu-4gb"})
        existing = make_state_entry(
            id="123",
            attributes={"region": "sfo3", "size": "s-1vcpu-2gb"},
            depends_on=["digitalocean.compute.old-dep-01"],
        )
        pr = make_planned_resource(
            entry=PlanEntry(
                resource_key="digitalocean.compute.telleztec-app-01",
                action=PlanAction.UPDATE,
                rationale="resize",
            ),
            driver=driver,
            state_entry=existing,
            desired_params={"region": "sfo3", "size": "s-2vcpu-4gb"},
            depends_on=["digitalocean.compute.new-dep-01"],
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": existing})

        orchestrator.apply_plan([pr], state_path=state_path, yes=True)

        saved = state.load(state_path)
        entry = saved.resources["digitalocean.compute.telleztec-app-01"]
        assert entry.depends_on == ["digitalocean.compute.new-dep-01"]

    def test_update_without_replace_logs_success(self, tmp_path: Path, caplog):
        caplog.set_level("INFO", logger="aiform.orchestrator")
        driver = FakeDriver(update_result={"id": "123", "region": "sfo3", "size": "s-2vcpu-4gb"})
        existing = make_state_entry(id="123", attributes={"region": "sfo3", "size": "s-1vcpu-2gb"})
        pr = make_planned_resource(
            entry=PlanEntry(
                resource_key="digitalocean.compute.telleztec-app-01",
                action=PlanAction.UPDATE,
                rationale="resize",
            ),
            driver=driver,
            state_entry=existing,
            desired_params={"region": "sfo3", "size": "s-2vcpu-4gb"},
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": existing})

        orchestrator.apply_plan([pr], state_path=state_path, yes=True)

        record = next(r for r in caplog.records if getattr(r, "operation", None) == "update")
        assert record.outcome == "success"
        assert record.provider == "digitalocean"

    def test_update_generic_failure_logs_error_before_raising(self, tmp_path: Path, caplog):
        caplog.set_level("INFO", logger="aiform.orchestrator")
        driver = FakeDriver()

        def boom(*args, **kwargs):
            raise RuntimeError("CSP rate limited")

        driver.update = boom
        existing = make_state_entry(id="123", attributes={"region": "sfo3", "size": "s-1vcpu-2gb"})
        pr = make_planned_resource(
            entry=PlanEntry(
                resource_key="digitalocean.compute.telleztec-app-01",
                action=PlanAction.UPDATE,
                rationale="resize",
            ),
            driver=driver,
            state_entry=existing,
            desired_params={"region": "sfo3", "size": "s-2vcpu-4gb"},
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": existing})

        with pytest.raises(DriverExecutionError):
            orchestrator.apply_plan([pr], state_path=state_path, yes=True)

        record = next(r for r in caplog.records if getattr(r, "operation", None) == "update")
        assert record.outcome == "error"
        assert record.levelno == logging.ERROR

    def test_update_without_replace_corrects_stale_likely_replace_true_prediction(
        self, tmp_path: Path
    ):
        # A plan-time categorization can flag likely_replace=True as a
        # heuristic warning, but update() may still handle the diff in
        # place without raising DriverUpdateNotSupported. The executed
        # entry must report what actually happened (no replace), not the
        # stale prediction -- otherwise a caller (cli.py) reports a
        # destructive replace that never happened.
        driver = FakeDriver(update_result={"id": "123", "region": "sfo3", "size": "s-2vcpu-4gb"})
        existing = make_state_entry(id="123", attributes={"region": "sfo3", "size": "s-1vcpu-2gb"})
        pr = make_planned_resource(
            entry=PlanEntry(
                resource_key="digitalocean.compute.telleztec-app-01",
                action=PlanAction.UPDATE,
                rationale="resize",
                likely_replace=True,
            ),
            driver=driver,
            state_entry=existing,
            desired_params={"region": "sfo3", "size": "s-2vcpu-4gb"},
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": existing})
        client = FakeClient([plan_review_response(safe_to_proceed=True, flags=[])])

        result = orchestrator.apply_plan([pr], state_path=state_path, yes=True, client=client)

        assert result.executed[0].likely_replace is False
        assert pr.entry.likely_replace is True

    def test_update_not_supported_already_flagged_replaces_without_reconfirming(
        self, tmp_path: Path
    ):
        driver = FakeDriver(
            update_exception=DriverUpdateNotSupported("image change", unsupported_fields=["image"]),
            create_result={"id": "new-2", "region": "sfo3", "image": "new-image"},
        )
        existing = make_state_entry(id="123")
        entry = PlanEntry(
            resource_key="digitalocean.compute.telleztec-app-01",
            action=PlanAction.UPDATE,
            rationale="image change",
            likely_replace=True,
        )
        pr = make_planned_resource(
            entry=entry,
            driver=driver,
            state_entry=existing,
            desired_params={"region": "sfo3", "image": "new-image"},
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": existing})

        confirm_calls = []

        def confirm(prompt):
            confirm_calls.append(prompt)
            return True

        client = FakeClient([plan_review_response(safe_to_proceed=True, flags=[])])
        orchestrator.apply_plan([pr], state_path=state_path, confirm=confirm, client=client)

        saved = state.load(state_path)
        assert saved.resources["digitalocean.compute.telleztec-app-01"].id == "new-2"
        # only the top-level confirmation fires -- likely_replace=True means
        # this resource was already covered by the batch gate #2 review, so
        # no separate single-resource re-review/confirm happens.
        assert len(confirm_calls) == 1

    def test_update_not_supported_not_flagged_triggers_single_resource_review_never_skipped_by_yes(
        self, tmp_path: Path
    ):
        driver = FakeDriver(
            update_exception=DriverUpdateNotSupported("image change", unsupported_fields=["image"]),
            create_result={"id": "new-2", "region": "sfo3", "image": "new-image"},
        )
        existing = make_state_entry(id="123")
        entry = PlanEntry(
            resource_key="digitalocean.compute.telleztec-app-01",
            action=PlanAction.UPDATE,
            rationale="image change",
            likely_replace=False,
        )
        pr = make_planned_resource(
            entry=entry,
            driver=driver,
            state_entry=existing,
            desired_params={"region": "sfo3", "image": "new-image"},
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": existing})

        confirm_calls = []

        def confirm(prompt):
            confirm_calls.append(prompt)
            return True

        client = FakeClient([plan_review_response(safe_to_proceed=True, flags=[])])
        orchestrator.apply_plan(
            [pr], state_path=state_path, yes=True, confirm=confirm, client=client
        )

        # yes=True skips the top-level confirmation entirely, but the
        # mid-loop single-resource replace confirmation is never skippable.
        assert len(confirm_calls) == 1
        assert len(client.messages.calls) == 1
        saved = state.load(state_path)
        assert saved.resources["digitalocean.compute.telleztec-app-01"].id == "new-2"

    def test_on_review_single_resource_flags_are_not_mixed_with_batch_flags(self, tmp_path: Path):
        # pr1 triggers the batch review (likely_replace=True); its update()
        # succeeds normally, so no single-resource re-review happens for it.
        # pr2's update() raises DriverUpdateNotSupported with
        # likely_replace=False, triggering its own, separate re-review.
        # on_review must be called twice, each time with only that review's
        # own flags -- never the other review's, and never a running total.
        driver1 = FakeDriver(update_result={"id": "1", "region": "sfo3"})
        driver2 = FakeDriver(
            update_exception=DriverUpdateNotSupported("image change", unsupported_fields=["image"]),
            create_result={"id": "new-2", "region": "sfo3", "image": "new-image"},
        )
        existing1 = make_state_entry(id="1", name="app-01")
        existing2 = make_state_entry(id="123", name="app-02")
        entry1 = PlanEntry(
            resource_key="digitalocean.compute.app-01",
            action=PlanAction.UPDATE,
            rationale="resize",
            likely_replace=True,
        )
        entry2 = PlanEntry(
            resource_key="digitalocean.compute.app-02",
            action=PlanAction.UPDATE,
            rationale="image change",
            likely_replace=False,
        )
        pr1 = make_planned_resource(
            entry=entry1,
            driver=driver1,
            state_entry=existing1,
            name="app-01",
            desired_params={"region": "sfo3"},
        )
        pr2 = make_planned_resource(
            entry=entry2,
            driver=driver2,
            state_entry=existing2,
            name="app-02",
            desired_params={"region": "sfo3", "image": "new-image"},
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(
            state_path,
            **{
                "digitalocean.compute.app-01": existing1,
                "digitalocean.compute.app-02": existing2,
            },
        )

        batch_flag = {
            "resource_key": "digitalocean.compute.app-01",
            "concern": "batch concern",
            "severity": "warning",
        }
        single_flag = {
            "resource_key": "digitalocean.compute.app-02",
            "concern": "single concern",
            "severity": "warning",
        }
        client = FakeClient(
            [
                plan_review_response(safe_to_proceed=True, flags=[batch_flag]),
                plan_review_response(safe_to_proceed=True, flags=[single_flag]),
            ]
        )

        events = []
        result = orchestrator.apply_plan(
            [pr1, pr2],
            state_path=state_path,
            yes=True,
            confirm=lambda p: events.append(("confirm", p)) or True,
            on_review=lambda flags: events.append(("review", list(flags))),
            client=client,
        )

        assert result.aborted is False
        # batch review (pr1) fires before the loop starts; pr2's own
        # single-resource review + confirm happen when the loop reaches it.
        assert [kind for kind, _ in events] == ["review", "review", "confirm"]
        assert [flag.concern for flag in events[0][1]] == ["batch concern"]
        assert [flag.concern for flag in events[1][1]] == ["single concern"]

    def test_single_resource_review_block_flag_raises_plan_blocked_error(self, tmp_path: Path):
        driver = FakeDriver(update_exception=DriverUpdateNotSupported("image change"))
        existing = make_state_entry(id="123")
        entry = PlanEntry(
            resource_key="digitalocean.compute.telleztec-app-01",
            action=PlanAction.UPDATE,
            rationale="image change",
            likely_replace=False,
        )
        pr = make_planned_resource(entry=entry, driver=driver, state_entry=existing)
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": existing})

        block_flag = {
            "resource_key": pr.entry.resource_key,
            "concern": "production resource",
            "severity": "block",
        }
        client = FakeClient([plan_review_response(safe_to_proceed=False, flags=[block_flag])])

        with pytest.raises(PlanBlockedError):
            orchestrator.apply_plan(
                [pr], state_path=state_path, yes=True, confirm=lambda p: True, client=client
            )

    def test_destroy_tracked_deletes_removes_from_state_and_moves_to_trash(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = tmp_path / "app.aiform.md"
        write_aiform_md(aiform_md)
        existing = make_state_entry(id="123", aiform_md_path=str(aiform_md))
        entry = PlanEntry(
            resource_key="digitalocean.compute.telleztec-app-01",
            action=PlanAction.DESTROY,
            rationale="explicit destroy",
        )
        pr = orchestrator.PlannedResource(
            entry=entry,
            provider="digitalocean",
            resource_type="compute",
            name="telleztec-app-01",
            desired_params={},
            aiform_md_path=aiform_md,
            current_aiform_md_sha256=None,
            driver=None,
            driver_info=None,
            credentials=None,
            state_entry=existing,
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": existing})

        # A DESTROY action always triggers gate #2's batch review
        # (needs_review checks action type only, regardless of tracked
        # status), so a client must always be supplied here.
        client = FakeClient([plan_review_response(safe_to_proceed=True, flags=[])])
        result = orchestrator.apply_plan([pr], state_path=state_path, yes=True, client=client)

        saved = state.load(state_path)
        assert "digitalocean.compute.telleztec-app-01" not in saved.resources
        assert not aiform_md.exists()
        assert result.executed == [pr.entry]

    def test_destroy_state_write_happens_before_trash_move(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = tmp_path / "app.aiform.md"
        write_aiform_md(aiform_md)
        existing = make_state_entry(id="123", aiform_md_path=str(aiform_md))
        entry = PlanEntry(
            resource_key="digitalocean.compute.telleztec-app-01",
            action=PlanAction.DESTROY,
            rationale="explicit destroy",
        )
        pr = orchestrator.PlannedResource(
            entry=entry,
            provider="digitalocean",
            resource_type="compute",
            name="telleztec-app-01",
            desired_params={},
            aiform_md_path=aiform_md,
            current_aiform_md_sha256=None,
            driver=None,
            driver_info=None,
            credentials=None,
            state_entry=existing,
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": existing})

        def failing_move_to_trash(path, *, trash_dir=orchestrator.TRASH_DIR):
            raise RuntimeError("simulated filesystem failure during trash move")

        monkeypatch.setattr(orchestrator, "move_to_trash", failing_move_to_trash)

        client = FakeClient([plan_review_response(safe_to_proceed=True, flags=[])])
        with pytest.raises(RuntimeError, match="simulated filesystem failure"):
            orchestrator.apply_plan([pr], state_path=state_path, yes=True, client=client)

        # PLAN.md §5 apply step 4: "the trash-move happens... after the
        # state write" -- so even though the trash-move itself failed, the
        # state removal must already be durably persisted.
        saved = state.load(state_path)
        assert "digitalocean.compute.telleztec-app-01" not in saved.resources

    def test_destroy_untracked_skips_delete_and_driver_resolution(
        self, tmp_path: Path, prompts_dir: Path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        aiform_md = tmp_path / "AIFORM-DELETE-app.aiform.md"
        write_aiform_md(aiform_md)
        entry = PlanEntry(
            resource_key="digitalocean.compute.telleztec-app-01",
            action=PlanAction.DESTROY,
            rationale="untracked",
        )
        pr = orchestrator.PlannedResource(
            entry=entry,
            provider="digitalocean",
            resource_type="compute",
            name="telleztec-app-01",
            desired_params={},
            aiform_md_path=aiform_md,
            current_aiform_md_sha256=None,
            driver=None,
            driver_info=None,
            credentials=None,
            state_entry=None,
        )
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        # No drivers_dir/DIGITALOCEAN_TOKEN set up at all -- if load_driver()
        # or config.resolve_credentials() were ever called for an untracked
        # destroy, this would raise instead of silently succeeding. A
        # client IS still required though: DESTROY always triggers gate #2's
        # batch review regardless of tracked status.
        client = FakeClient([plan_review_response(safe_to_proceed=True, flags=[])])
        result = orchestrator.apply_plan([pr], state_path=state_path, yes=True, client=client)

        assert not aiform_md.exists()
        assert result.executed == [pr.entry]

    def test_gate2_skipped_when_no_destroy_or_likely_replace(self, tmp_path: Path):
        pr = make_planned_resource()
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        client = FakeClient([])
        result = orchestrator.apply_plan([pr], state_path=state_path, yes=True, client=client)

        assert len(client.messages.calls) == 0
        assert result.review_flags == []

    def test_gate2_block_flag_raises_unconditionally_even_with_yes(
        self, tmp_path: Path, drivers_dir: Path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = tmp_path / "app.aiform.md"
        write_aiform_md(aiform_md)
        existing = make_state_entry(id="123", aiform_md_path=str(aiform_md))
        entry = PlanEntry(
            resource_key="digitalocean.compute.telleztec-app-01",
            action=PlanAction.DESTROY,
            rationale="x",
        )
        pr = orchestrator.PlannedResource(
            entry=entry,
            provider="digitalocean",
            resource_type="compute",
            name="telleztec-app-01",
            desired_params={},
            aiform_md_path=aiform_md,
            current_aiform_md_sha256=None,
            driver=None,
            driver_info=None,
            credentials=None,
            state_entry=existing,
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": existing})

        block_flag = {"resource_key": pr.entry.resource_key, "concern": "prod", "severity": "block"}
        client = FakeClient([plan_review_response(safe_to_proceed=False, flags=[block_flag])])

        with pytest.raises(PlanBlockedError):
            orchestrator.apply_plan([pr], state_path=state_path, yes=True, client=client)

        saved = state.load(state_path)
        assert "digitalocean.compute.telleztec-app-01" in saved.resources

    def test_gate2_non_blocking_flags_carried_into_result(
        self, tmp_path: Path, drivers_dir: Path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = tmp_path / "app.aiform.md"
        write_aiform_md(aiform_md)
        existing = make_state_entry(id="123", aiform_md_path=str(aiform_md))
        entry = PlanEntry(
            resource_key="digitalocean.compute.telleztec-app-01",
            action=PlanAction.DESTROY,
            rationale="x",
        )
        pr = orchestrator.PlannedResource(
            entry=entry,
            provider="digitalocean",
            resource_type="compute",
            name="telleztec-app-01",
            desired_params={},
            aiform_md_path=aiform_md,
            current_aiform_md_sha256=None,
            driver=None,
            driver_info=None,
            credentials=None,
            state_entry=existing,
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": existing})

        warning_flag = {
            "resource_key": pr.entry.resource_key,
            "concern": "double check",
            "severity": "warning",
        }
        client = FakeClient([plan_review_response(safe_to_proceed=True, flags=[warning_flag])])

        result = orchestrator.apply_plan([pr], state_path=state_path, yes=True, client=client)

        assert len(result.review_flags) == 1
        assert result.review_flags[0].concern == "double check"

    def test_gate2_non_blocking_review_logs_at_info(
        self, tmp_path: Path, drivers_dir: Path, monkeypatch, caplog
    ):
        caplog.set_level("INFO", logger="aiform.orchestrator")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = tmp_path / "app.aiform.md"
        write_aiform_md(aiform_md)
        existing = make_state_entry(id="123", aiform_md_path=str(aiform_md))
        entry = PlanEntry(
            resource_key="digitalocean.compute.telleztec-app-01",
            action=PlanAction.DESTROY,
            rationale="x",
        )
        pr = orchestrator.PlannedResource(
            entry=entry,
            provider="digitalocean",
            resource_type="compute",
            name="telleztec-app-01",
            desired_params={},
            aiform_md_path=aiform_md,
            current_aiform_md_sha256=None,
            driver=None,
            driver_info=None,
            credentials=None,
            state_entry=existing,
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": existing})

        warning_flag = {
            "resource_key": pr.entry.resource_key,
            "concern": "double check",
            "severity": "warning",
        }
        client = FakeClient([plan_review_response(safe_to_proceed=True, flags=[warning_flag])])

        orchestrator.apply_plan([pr], state_path=state_path, yes=True, client=client)

        record = next(r for r in caplog.records if hasattr(r, "safe_to_proceed"))
        assert record.safe_to_proceed is True
        assert record.flags_count == 1
        assert record.levelno == logging.INFO

    def test_gate2_block_flag_logs_at_warning(
        self, tmp_path: Path, drivers_dir: Path, monkeypatch, caplog
    ):
        caplog.set_level("INFO", logger="aiform.orchestrator")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = tmp_path / "app.aiform.md"
        write_aiform_md(aiform_md)
        existing = make_state_entry(id="123", aiform_md_path=str(aiform_md))
        entry = PlanEntry(
            resource_key="digitalocean.compute.telleztec-app-01",
            action=PlanAction.DESTROY,
            rationale="x",
        )
        pr = orchestrator.PlannedResource(
            entry=entry,
            provider="digitalocean",
            resource_type="compute",
            name="telleztec-app-01",
            desired_params={},
            aiform_md_path=aiform_md,
            current_aiform_md_sha256=None,
            driver=None,
            driver_info=None,
            credentials=None,
            state_entry=existing,
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": existing})

        block_flag = {"resource_key": pr.entry.resource_key, "concern": "prod", "severity": "block"}
        client = FakeClient([plan_review_response(safe_to_proceed=False, flags=[block_flag])])

        with pytest.raises(PlanBlockedError):
            orchestrator.apply_plan([pr], state_path=state_path, yes=True, client=client)

        record = next(r for r in caplog.records if hasattr(r, "safe_to_proceed"))
        assert record.safe_to_proceed is False
        assert record.levelno == logging.WARNING

    def test_declined_confirmation_returns_aborted_with_empty_executed(self, tmp_path: Path):
        pr = make_planned_resource()
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        result = orchestrator.apply_plan([pr], state_path=state_path, confirm=lambda p: False)

        assert result.aborted is True
        assert result.executed == []
        assert state.load(state_path).resources == {}

    def test_yes_skips_top_level_confirmation(self, tmp_path: Path):
        pr = make_planned_resource()
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        def confirm(prompt):
            raise AssertionError("should not be called when yes=True")

        result = orchestrator.apply_plan([pr], state_path=state_path, yes=True, confirm=confirm)

        assert result.aborted is False

    def test_on_review_called_with_batch_flags_before_top_level_confirm(self, tmp_path: Path):
        pr = make_planned_resource(
            entry=PlanEntry(
                resource_key="digitalocean.compute.telleztec-app-01",
                action=PlanAction.DESTROY,
                rationale="x",
            )
        )
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        warning_flag = {
            "resource_key": pr.entry.resource_key,
            "concern": "double check",
            "severity": "warning",
        }
        client = FakeClient([plan_review_response(safe_to_proceed=True, flags=[warning_flag])])

        events = []
        result = orchestrator.apply_plan(
            [pr],
            state_path=state_path,
            confirm=lambda p: events.append(("confirm", p)) or False,
            on_review=lambda flags: events.append(("review", flags)),
            client=client,
        )

        assert result.aborted is True
        assert [kind for kind, _ in events] == ["review", "confirm"]
        assert [flag.concern for flag in events[0][1]] == ["double check"]

    def test_on_review_called_unconditionally_under_yes(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        aiform_md = tmp_path / "app.aiform.md"
        write_aiform_md(aiform_md)
        pr = make_planned_resource(
            entry=PlanEntry(
                resource_key="digitalocean.compute.telleztec-app-01",
                action=PlanAction.DESTROY,
                rationale="x",
            ),
            aiform_md_path=aiform_md,
        )
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        warning_flag = {
            "resource_key": pr.entry.resource_key,
            "concern": "double check",
            "severity": "warning",
        }
        client = FakeClient([plan_review_response(safe_to_proceed=True, flags=[warning_flag])])

        seen = []

        def confirm(prompt):
            raise AssertionError("should not be called when yes=True")

        result = orchestrator.apply_plan(
            [pr],
            state_path=state_path,
            yes=True,
            confirm=confirm,
            on_review=lambda flags: seen.append(flags),
            client=client,
        )

        assert result.aborted is False
        assert len(seen) == 1
        assert seen[0][0].concern == "double check"

    def test_on_review_defaults_to_noop(self, tmp_path: Path):
        pr = make_planned_resource(
            entry=PlanEntry(
                resource_key="digitalocean.compute.telleztec-app-01",
                action=PlanAction.DESTROY,
                rationale="x",
            )
        )
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        warning_flag = {
            "resource_key": pr.entry.resource_key,
            "concern": "double check",
            "severity": "warning",
        }
        client = FakeClient([plan_review_response(safe_to_proceed=True, flags=[warning_flag])])

        # No on_review passed -- must default to a no-op rather than
        # raising, the same contract confirm/default_confirm has.
        result = orchestrator.apply_plan(
            [pr], state_path=state_path, confirm=lambda p: False, client=client
        )

        assert result.aborted is True
        assert len(result.review_flags) == 1

    def test_on_review_not_called_when_no_review_needed(self, tmp_path: Path):
        pr = make_planned_resource()
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        calls = []
        orchestrator.apply_plan(
            [pr],
            state_path=state_path,
            yes=True,
            on_review=lambda flags: calls.append(flags),
            client=FakeClient([]),
        )

        assert calls == []

    def test_state_saved_after_each_resource_not_batched(self, tmp_path: Path):
        driver1 = FakeDriver(create_result={"id": "id-1", "region": "sfo3"})
        driver2 = FakeDriver()

        def boom(*args, **kwargs):
            raise RuntimeError("second resource fails")

        driver2.create = boom

        pr1 = make_planned_resource(
            entry=PlanEntry(
                resource_key="digitalocean.compute.app-01", action=PlanAction.CREATE, rationale="x"
            ),
            driver=driver1,
            name="app-01",
        )
        pr2 = make_planned_resource(
            entry=PlanEntry(
                resource_key="digitalocean.compute.app-02", action=PlanAction.CREATE, rationale="x"
            ),
            driver=driver2,
            name="app-02",
        )
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        with pytest.raises(DriverExecutionError):
            orchestrator.apply_plan([pr1, pr2], state_path=state_path, yes=True)

        saved = state.load(state_path)
        assert "digitalocean.compute.app-01" in saved.resources
        assert "digitalocean.compute.app-02" not in saved.resources

    def test_executed_excludes_no_op_entries(self, tmp_path: Path):
        no_op_entry = PlanEntry(
            resource_key="digitalocean.compute.app-01",
            action=PlanAction.NO_OP,
            rationale="unchanged",
        )
        create_entry = PlanEntry(
            resource_key="digitalocean.compute.app-02", action=PlanAction.CREATE, rationale="new"
        )
        pr_no_op = make_planned_resource(entry=no_op_entry, name="app-01")
        pr_create = make_planned_resource(
            entry=create_entry,
            name="app-02",
            driver=FakeDriver(create_result={"id": "id-2", "region": "sfo3"}),
        )
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        result = orchestrator.apply_plan([pr_no_op, pr_create], state_path=state_path, yes=True)

        assert result.executed == [create_entry]

    def test_update_without_replace_also_refreshes_last_refreshed_at(self, tmp_path: Path):
        driver = FakeDriver(update_result={"id": "123", "region": "sfo3", "size": "s-2vcpu-4gb"})
        existing = make_state_entry(
            id="123",
            attributes={"region": "sfo3", "size": "s-1vcpu-2gb"},
            last_refreshed_at="2020-01-01T00:00:00Z",
        )
        pr = make_planned_resource(
            entry=PlanEntry(
                resource_key="digitalocean.compute.telleztec-app-01",
                action=PlanAction.UPDATE,
                rationale="resize",
            ),
            driver=driver,
            state_entry=existing,
            desired_params={"region": "sfo3", "size": "s-2vcpu-4gb"},
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": existing})

        orchestrator.apply_plan([pr], state_path=state_path, yes=True)

        saved = state.load(state_path)
        refreshed = saved.resources["digitalocean.compute.telleztec-app-01"].last_refreshed_at
        assert refreshed.year != 2020

    def test_replace_reports_likely_replace_true_in_executed_even_if_not_originally_flagged(
        self, tmp_path: Path
    ):
        driver = FakeDriver(
            update_exception=DriverUpdateNotSupported("image change"),
            create_result={"id": "new-2", "region": "sfo3", "image": "new-image"},
        )
        existing = make_state_entry(id="123")
        entry = PlanEntry(
            resource_key="digitalocean.compute.telleztec-app-01",
            action=PlanAction.UPDATE,
            rationale="image change",
            likely_replace=False,
        )
        pr = make_planned_resource(
            entry=entry,
            driver=driver,
            state_entry=existing,
            desired_params={"region": "sfo3", "image": "new-image"},
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": existing})

        client = FakeClient([plan_review_response(safe_to_proceed=True, flags=[])])
        result = orchestrator.apply_plan(
            [pr], state_path=state_path, yes=True, confirm=lambda p: True, client=client
        )

        assert result.executed[0].likely_replace is True
        # the original PlanEntry object passed in is untouched
        assert entry.likely_replace is False

    def test_replace_removes_stale_state_entry_before_attempting_create(self, tmp_path: Path):
        driver = FakeDriver(update_exception=DriverUpdateNotSupported("image change"))

        def boom(*args, **kwargs):
            raise RuntimeError("CSP create quota exceeded")

        driver.create = boom
        existing = make_state_entry(id="123")
        entry = PlanEntry(
            resource_key="digitalocean.compute.telleztec-app-01",
            action=PlanAction.UPDATE,
            rationale="image change",
            likely_replace=True,
        )
        pr = make_planned_resource(entry=entry, driver=driver, state_entry=existing)
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": existing})

        client = FakeClient([plan_review_response(safe_to_proceed=True, flags=[])])
        with pytest.raises(DriverExecutionError):
            orchestrator.apply_plan([pr], state_path=state_path, yes=True, client=client)

        # the old resource is verifiably gone (delete() succeeded) -- state
        # must not still claim the old, now-nonexistent id/attributes.
        saved = state.load(state_path)
        assert "digitalocean.compute.telleztec-app-01" not in saved.resources

    # The next two pin which `operation` a failure on the replace path
    # reports. They pass against the code as it stands -- they are
    # characterization tests, not red-first ones, and exist because the
    # UPDATE arm's two exception handlers are siblings: `except Exception`
    # does NOT cover the delete()/create() calls inside `except
    # DriverUpdateNotSupported`, since Python never re-enters a sibling
    # handler. So those failures surface as "delete"/"create", unwrapped by
    # the update handler. Flattening the two handlers, or pulling those
    # calls under a broader try, would relabel both as "update" -- and
    # test_replace_removes_stale_state_entry_before_attempting_create above
    # would not catch it, because it only asserts the exception *type*.
    def test_replace_create_failure_reports_create_not_update_as_the_operation(
        self, tmp_path: Path
    ):
        driver = FakeDriver(update_exception=DriverUpdateNotSupported("image change"))

        def boom(*args, **kwargs):
            raise RuntimeError("CSP create quota exceeded")

        driver.create = boom
        existing = make_state_entry(id="123")
        entry = PlanEntry(
            resource_key="digitalocean.compute.telleztec-app-01",
            action=PlanAction.UPDATE,
            rationale="image change",
            likely_replace=True,
        )
        pr = make_planned_resource(entry=entry, driver=driver, state_entry=existing)
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": existing})

        client = FakeClient([plan_review_response(safe_to_proceed=True, flags=[])])
        with pytest.raises(DriverExecutionError) as exc_info:
            orchestrator.apply_plan([pr], state_path=state_path, yes=True, client=client)

        assert exc_info.value.operation == "create"
        assert "during create" in str(exc_info.value)

    def test_replace_delete_failure_reports_delete_not_update_as_the_operation(
        self, tmp_path: Path
    ):
        driver = FakeDriver(
            update_exception=DriverUpdateNotSupported("image change"),
            delete_exception=RuntimeError("CSP delete refused"),
        )
        existing = make_state_entry(id="123")
        entry = PlanEntry(
            resource_key="digitalocean.compute.telleztec-app-01",
            action=PlanAction.UPDATE,
            rationale="image change",
            likely_replace=True,
        )
        pr = make_planned_resource(entry=entry, driver=driver, state_entry=existing)
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": existing})

        client = FakeClient([plan_review_response(safe_to_proceed=True, flags=[])])
        with pytest.raises(DriverExecutionError) as exc_info:
            orchestrator.apply_plan([pr], state_path=state_path, yes=True, client=client)

        assert exc_info.value.operation == "delete"
        # delete() failed, so the entry must still be tracked: the
        # checkpoint save only runs once the CSP resource is verifiably gone.
        saved = state.load(state_path)
        assert saved.resources["digitalocean.compute.telleztec-app-01"].id == "123"

    def test_batch_review_safe_to_proceed_false_with_no_block_flag_still_raises(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = tmp_path / "app.aiform.md"
        write_aiform_md(aiform_md)
        existing = make_state_entry(id="123", aiform_md_path=str(aiform_md))
        entry = PlanEntry(
            resource_key="digitalocean.compute.telleztec-app-01",
            action=PlanAction.DESTROY,
            rationale="x",
        )
        pr = orchestrator.PlannedResource(
            entry=entry,
            provider="digitalocean",
            resource_type="compute",
            name="telleztec-app-01",
            desired_params={},
            aiform_md_path=aiform_md,
            current_aiform_md_sha256=None,
            driver=None,
            driver_info=None,
            credentials=None,
            state_entry=existing,
        )
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": existing})

        # safe_to_proceed=False but only a non-block flag -- a schema-valid
        # response that would otherwise slip past a block-flags-only check.
        warning_flag = {
            "resource_key": pr.entry.resource_key,
            "concern": "risky",
            "severity": "warning",
        }
        client = FakeClient([plan_review_response(safe_to_proceed=False, flags=[warning_flag])])

        with pytest.raises(PlanBlockedError):
            orchestrator.apply_plan([pr], state_path=state_path, yes=True, client=client)

        saved = state.load(state_path)
        assert "digitalocean.compute.telleztec-app-01" in saved.resources

    def test_create_driver_response_missing_id_raises_driver_execution_error(self, tmp_path: Path):
        driver = FakeDriver(create_result={"region": "sfo3"})
        pr = make_planned_resource(driver=driver, state_entry=None)
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        with pytest.raises(DriverExecutionError) as exc_info:
            orchestrator.apply_plan([pr], state_path=state_path, yes=True)
        assert exc_info.value.operation == "create"

    def test_update_of_resource_no_longer_in_state_raises_plan_blocked_error(self, tmp_path: Path):
        driver = FakeDriver(update_result={"id": "123", "region": "sfo3"})
        existing = make_state_entry(id="123")
        pr = make_planned_resource(
            entry=PlanEntry(
                resource_key="digitalocean.compute.telleztec-app-01",
                action=PlanAction.UPDATE,
                rationale="resize",
            ),
            driver=driver,
            state_entry=existing,
            desired_params={"region": "sfo3"},
        )
        state_path = tmp_path / ".aiform" / "state.json"
        # state.json does NOT contain the entry -- stale relative to `pr`,
        # simulating state having changed since this plan was built.
        state.save(state.State(), state_path)

        with pytest.raises(PlanBlockedError):
            orchestrator.apply_plan([pr], state_path=state_path, yes=True)

    def test_destroy_of_resource_no_longer_in_state_raises_plan_blocked_error(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = tmp_path / "app.aiform.md"
        write_aiform_md(aiform_md)
        existing = make_state_entry(id="123", aiform_md_path=str(aiform_md))
        entry = PlanEntry(
            resource_key="digitalocean.compute.telleztec-app-01",
            action=PlanAction.DESTROY,
            rationale="x",
        )
        pr = orchestrator.PlannedResource(
            entry=entry,
            provider="digitalocean",
            resource_type="compute",
            name="telleztec-app-01",
            desired_params={},
            aiform_md_path=aiform_md,
            current_aiform_md_sha256=None,
            driver=None,
            driver_info=None,
            credentials=None,
            state_entry=existing,
        )
        state_path = tmp_path / ".aiform" / "state.json"
        # state.json does NOT contain the entry, unlike `pr.state_entry`.
        state.save(state.State(), state_path)

        client = FakeClient([plan_review_response(safe_to_proceed=True, flags=[])])
        with pytest.raises(PlanBlockedError):
            orchestrator.apply_plan([pr], state_path=state_path, yes=True, client=client)


class TestMoveToTrash:
    def test_moves_file_with_utc_timestamp_prefix(self, tmp_path: Path):
        src = tmp_path / "app.aiform.md"
        src.write_text("content")
        trash_dir = tmp_path / "trash"

        dest = orchestrator.move_to_trash(src, trash_dir=trash_dir)

        assert not src.exists()
        assert dest.exists()
        assert dest.read_text() == "content"
        assert dest.name.endswith("-app.aiform.md")
        assert dest.parent == trash_dir

    def test_creates_trash_dir_if_missing(self, tmp_path: Path):
        src = tmp_path / "app.aiform.md"
        src.write_text("x")
        trash_dir = tmp_path / "nested" / "trash"

        orchestrator.move_to_trash(src, trash_dir=trash_dir)

        assert trash_dir.exists()

    def test_collision_appends_numeric_suffix_never_overwrites(self, tmp_path: Path):
        trash_dir = tmp_path / "trash"
        trash_dir.mkdir()
        src = tmp_path / "app.aiform.md"
        src.write_text("new content")

        # Pre-occupy the name move_to_trash() would compute for "now",
        # forcing a real collision regardless of exact call timing.
        utcnow = datetime.now(UTC)
        colliding_name = f"{utcnow:%Y%m%dT%H%M%SZ}-app.aiform.md"
        (trash_dir / colliding_name).write_text("already here")

        dest = orchestrator.move_to_trash(src, trash_dir=trash_dir)

        assert dest.name != colliding_name
        assert dest.exists()
        assert (trash_dir / colliding_name).read_text() == "already here"
        assert dest.read_text() == "new content"


# Reproducing #163 faithfully needs a real terminal: the keystroke has to sit
# in the tty driver's input queue while the program is still busy, which no
# in-process stdin double can imitate.
_CONFIRM_CHILD = """\
import time
from aiform.orchestrator import default_confirm

print("READY", flush=True)
time.sleep(1.0)
print("PROMPTING", flush=True)
print("ANSWER", default_confirm("Apply this plan?"), flush=True)
"""


class TestDefaultConfirm:
    def test_keystroke_typed_before_the_prompt_is_not_read_as_the_answer(self, tmp_path: Path):
        script = tmp_path / "child.py"
        script.write_text(_CONFIRM_CHILD)
        repo_root = Path(__file__).resolve().parents[1]

        master, slave = pty.openpty()
        proc = subprocess.Popen(
            [sys.executable, str(script)],
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=repo_root,
            env=os.environ | {"PYTHONPATH": str(repo_root)},
        )
        os.close(slave)
        try:
            assert proc.stdout.readline().strip() == "READY"
            # Typed during the gate-#2 review wait, long before the prompt.
            os.write(master, b"y\n")

            assert proc.stdout.readline().strip() == "PROMPTING"
            # input() writes and flushes its prompt to stdout only after the
            # flush has run, so seeing those bytes on the pipe is proof the
            # child is past it -- no sleep, no timing window. An empty read
            # means the child exited instead, which the final assert catches.
            seen = ""
            while not seen.endswith("(y/n): "):
                char = proc.stdout.read(1)
                if not char:
                    break
                seen += char

            with contextlib.suppress(OSError):
                # The real answer, typed against the prompt that is now
                # visible. Pre-fix the child has already answered "y" and
                # exited, so writing to the pty raises rather than arriving.
                os.write(master, b"n\n")

            out = proc.communicate(timeout=30)[0]
        finally:
            os.close(master)
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

        assert "ANSWER False" in out

    def test_blank_or_unrecognized_answer_reprompts_instead_of_defaulting(self, monkeypatch):
        answers = iter(["", "maybe", "yy", "n"])
        seen_prompts = []

        def fake_input(prompt=""):
            seen_prompts.append(prompt)
            return next(answers)

        monkeypatch.setattr("builtins.input", fake_input)

        result = orchestrator.default_confirm("Apply this plan?")

        assert result is False
        with pytest.raises(StopIteration):
            next(answers)
        assert seen_prompts == ["Apply this plan? (y/n): "] * 4

    def test_eventual_y_answer_is_returned_true(self, monkeypatch):
        answers = iter(["", "Y"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

        result = orchestrator.default_confirm("Apply this plan?")

        assert result is True

    def test_whitespace_padded_answer_is_stripped(self, monkeypatch):
        answers = iter([" n "])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

        result = orchestrator.default_confirm("Apply this plan?")

        assert result is False

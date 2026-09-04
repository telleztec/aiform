# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import hashlib
import json
import logging
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aiform import llm, orchestrator, state
from aiform.driver import DriverUpdateNotSupported, ResourceDriver
from aiform.exceptions import DriverExecutionError, PlanBlockedError, ResourceNotFoundError
from aiform.models import DriverInfo, DriverReview, PlanAction, PlanEntry


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


# Written to disk for tests that exercise load_driver()/ensure_driver_trusted()/
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
) -> str:
    if params is None:
        params = {"region": "sfo3", "size": "s-1vcpu-2gb"}
    lines = ["---", f"resource: {resource}", f"name: {name}", f"provider: {provider}", "params:"]
    lines += [f"  {key}: {json.dumps(value)}" for key, value in params.items()]
    lines.append("---")
    content = "\n".join(lines) + "\n"
    path.write_text(content)
    return content


def make_driver_info(
    sha256: str, *, approved: bool = True, path: str = "drivers/digitalocean/compute.py"
) -> DriverInfo:
    return DriverInfo(
        path=path,
        sha256=sha256,
        generated_at=datetime(2026, 7, 30, 18, 22, 11, tzinfo=UTC),
        code_review=DriverReview(
            approved=approved,
            concerns=[],
            blocking_issues=[] if approved else ["needs fixing"],
            reviewed_at=datetime(2026, 7, 30, 18, 22, 40, tzinfo=UTC),
            model="claude-opus-5",
        ),
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


def approve_response() -> str:
    return json.dumps({"approved": True, "concerns": [], "blocking_issues": []})


def reject_response(blocking_issues: list[str] | None = None) -> str:
    return json.dumps(
        {"approved": False, "concerns": [], "blocking_issues": blocking_issues or ["bad driver"]}
    )


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


class TestEnsureDriverTrusted:
    def test_reuses_trusted_hash_from_state_zero_llm_calls(
        self, drivers_dir: Path, prompts_dir: Path
    ):
        path = write_driver(drivers_dir, "digitalocean", "compute")
        trusted_info = make_driver_info(driver_sha256(path))
        entry = make_state_entry(driver=trusted_info)
        st = state.State(resources={"digitalocean.compute.telleztec-app-01": entry})
        client = FakeClient([])

        result = orchestrator.ensure_driver_trusted("digitalocean", "compute", st, client=client)

        assert result == trusted_info
        assert len(client.messages.calls) == 0

    def test_only_matches_same_provider_and_resource_type(
        self, drivers_dir: Path, prompts_dir: Path
    ):
        path = write_driver(drivers_dir, "digitalocean", "compute")
        sha256 = driver_sha256(path)
        entry = make_state_entry(resource_type="network", driver=make_driver_info(sha256))
        st = state.State(resources={"digitalocean.network.telleztec-app-01": entry})
        client = FakeClient([approve_response()])

        result = orchestrator.ensure_driver_trusted("digitalocean", "compute", st, client=client)

        assert len(client.messages.calls) == 1
        assert result.sha256 == sha256

    def test_no_matching_entry_reviews_and_returns_new_driver_info(
        self, drivers_dir: Path, prompts_dir: Path
    ):
        path = write_driver(drivers_dir, "digitalocean", "compute")
        client = FakeClient([approve_response()])

        result = orchestrator.ensure_driver_trusted(
            "digitalocean", "compute", state.State(), client=client
        )

        assert result.sha256 == driver_sha256(path)
        assert result.path == "drivers/digitalocean/compute.py"
        assert result.code_review.approved is True

    def test_hash_mismatch_triggers_re_review(self, drivers_dir: Path, prompts_dir: Path):
        write_driver(drivers_dir, "digitalocean", "compute")
        entry = make_state_entry(driver=make_driver_info("stale-hash-does-not-match"))
        st = state.State(resources={"digitalocean.compute.telleztec-app-01": entry})
        client = FakeClient([approve_response()])

        result = orchestrator.ensure_driver_trusted("digitalocean", "compute", st, client=client)

        assert len(client.messages.calls) == 1
        assert result.sha256 != "stale-hash-does-not-match"

    def test_not_approved_raises_plan_blocked_error_naming_the_concerns(
        self, drivers_dir: Path, prompts_dir: Path
    ):
        write_driver(drivers_dir, "digitalocean", "compute")
        client = FakeClient([reject_response(["reads ANTHROPIC_API_KEY"])])

        with pytest.raises(PlanBlockedError) as exc_info:
            orchestrator.ensure_driver_trusted(
                "digitalocean", "compute", state.State(), client=client
            )
        assert "reads ANTHROPIC_API_KEY" in str(exc_info.value)

    def test_cache_hit_logs_reused_true(self, drivers_dir: Path, prompts_dir: Path, caplog):
        caplog.set_level("INFO", logger="aiform.orchestrator")
        path = write_driver(drivers_dir, "digitalocean", "compute")
        entry = make_state_entry(driver=make_driver_info(driver_sha256(path)))
        st = state.State(resources={"digitalocean.compute.telleztec-app-01": entry})

        orchestrator.ensure_driver_trusted("digitalocean", "compute", st, client=FakeClient([]))

        record = caplog.records[0]
        assert record.reused is True

    def test_real_review_logs_reused_false_and_approved(
        self, drivers_dir: Path, prompts_dir: Path, caplog
    ):
        caplog.set_level("INFO", logger="aiform.orchestrator")
        write_driver(drivers_dir, "digitalocean", "compute")

        orchestrator.ensure_driver_trusted(
            "digitalocean", "compute", state.State(), client=FakeClient([approve_response()])
        )

        record = caplog.records[0]
        assert record.reused is False
        assert record.approved is True


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

        client = FakeClient([approve_response(), categorization_response(action="create")])
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

        client = FakeClient([approve_response(), categorization_response(action="update")])
        planned, _ = orchestrator.build_create_plan(
            [aiform_md], state_path=state_path, client=client
        )

        assert [p.entry.action for p in planned] == [PlanAction.CREATE]
        assert planned[0].entry.likely_replace is False
        # Gate #1's driver review is the only call that may run. A second
        # call means the categorization request went out anyway, which is
        # the regression this guards -- the zero-diff no-op short-circuit
        # has the same "cheap path makes no LLM call" property.
        assert len(client.messages.calls) == 1

    def test_untracked_resource_plans_create_even_with_no_scripted_categorization(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        """The stronger form: script gate #1 and nothing else. If the
        categorization call is ever reattempted, FakeMessages.create()
        raises IndexError off the empty response list rather than
        silently reusing a stale answer."""
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = tmp_path / "app.aiform.md"
        write_aiform_md(aiform_md)
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        client = FakeClient([approve_response()])
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

    def test_structural_cross_check_allows_create_when_drifted_missing(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        driver_file = write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = tmp_path / "app.aiform.md"
        write_aiform_md(aiform_md)
        entry = make_state_entry(id="MISSING", driver=make_driver_info(driver_sha256(driver_file)))
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": entry})

        client = FakeClient([categorization_response(action="create")])
        planned, _ = orchestrator.build_create_plan(
            [aiform_md], state_path=state_path, client=client
        )

        assert planned[0].entry.action == PlanAction.CREATE

    def test_missing_credentials_raises_plan_blocked_error(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = tmp_path / "app.aiform.md"
        write_aiform_md(aiform_md)
        state_path = tmp_path / ".aiform" / "state.json"
        state.save(state.State(), state_path)

        client = FakeClient([approve_response()])
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

    def test_driver_hash_mismatch_triggers_re_review(
        self, tmp_path: Path, drivers_dir: Path, prompts_dir: Path, monkeypatch
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = tmp_path / "app.aiform.md"
        write_aiform_md(aiform_md)
        entry = make_state_entry(driver=make_driver_info("stale-hash"))
        state_path = tmp_path / ".aiform" / "state.json"
        save_state(state_path, **{"digitalocean.compute.telleztec-app-01": entry})

        client = FakeClient([approve_response(), categorization_response(action="update")])
        planned, _ = orchestrator.build_create_plan(
            [aiform_md], state_path=state_path, client=client
        )

        assert len(client.messages.calls) == 2
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

        client = FakeClient([approve_response()])
        planned, _ = orchestrator.build_create_plan(
            [aiform_md_1, aiform_md_2], state_path=state_path, client=client
        )

        assert len(planned) == 2
        # One call total: gate #1 reviews the shared driver once, and both
        # resources are untracked so neither is categorized. This asserted
        # 3 before -- gate #1 plus a categorization per file -- which was
        # the behavior issue #117 removed. What the test is actually for
        # is that the driver and credentials are cached across files, and
        # a single gate #1 call for two files still shows exactly that.
        assert len(client.messages.calls) == 1

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

        client = FakeClient([approve_response(), categorization_response(action="create")])
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

# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import json
import logging
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from aiform import config, observability, orchestrator, state
from aiform.driver import CapabilityNotSupported, ResourceDriver
from aiform.exceptions import PlanBlockedError, ResourceNotFoundError
from aiform.models import DriverInfo, HealthReport, HealthStatus, MetricKind, Sample, StateEntry


def make_state_entry(**overrides) -> StateEntry:
    defaults = dict(
        provider="digitalocean",
        resource_type="compute",
        name="web-01",
        id="123456789",
        attributes={"region": "sfo3", "size": "s-1vcpu-2gb"},
        driver=DriverInfo(
            path="drivers/digitalocean/compute.py",
            sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b8",
            generated_at=datetime(2026, 9, 10, 14, 2, 11, tzinfo=UTC),
        ),
        last_applied_at="2026-09-10T14:02:11Z",
        last_refreshed_at="2026-09-10T14:02:11Z",
        aiform_md_path="examples/web.aiform.md",
        aiform_md_sha256="abc123",
    )
    defaults.update(overrides)
    return StateEntry(**defaults)


def make_state(*entries: StateEntry) -> state.State:
    return state.State(
        resources={f"{e.provider}.{e.resource_type}.{e.name}": e for e in (entries or ())}
    )


def write_state(path: Path, *entries: StateEntry) -> Path:
    state.save(make_state(*entries), path)
    return path


class StubDriver(ResourceDriver):
    """Only health()/metrics()/read() are exercised here -- the four
    abstract methods exist so the class can be instantiated at all."""

    PARAM_SCHEMA = {"type": "object", "properties": {}}

    def __init__(
        self,
        *,
        health_result=None,
        health_exception=None,
        metrics_result=None,
        metrics_exception=None,
        read_result=None,
        read_exception=None,
    ):
        self.health_result = health_result
        self.health_exception = health_exception
        self.metrics_result = metrics_result
        self.metrics_exception = metrics_exception
        self.read_result = read_result
        self.read_exception = read_exception
        self.health_calls: list[tuple] = []
        self.metrics_calls: list[tuple] = []
        self.read_calls: list[tuple] = []

    def create(self, name, params, credentials):
        raise AssertionError("create() must never be called by an observability command")

    def update(self, id, current, desired, credentials):
        raise AssertionError("update() must never be called by an observability command")

    def delete(self, id, credentials):
        raise AssertionError("delete() must never be called by an observability command")

    def read(self, id, credentials):
        self.read_calls.append((id, credentials))
        if self.read_exception is not None:
            raise self.read_exception
        if self.read_result is None:
            raise AssertionError("read() called on a stub with no read_result")
        return dict(self.read_result)

    def health(self, id, credentials):
        self.health_calls.append((id, credentials))
        if self.health_exception is not None:
            raise self.health_exception
        if self.health_result is None:
            return super().health(id, credentials)
        return self.health_result

    def metrics(self, id, credentials):
        self.metrics_calls.append((id, credentials))
        if self.metrics_exception is not None:
            raise self.metrics_exception
        if self.metrics_result is None:
            return super().metrics(id, credentials)
        return list(self.metrics_result)


@pytest.fixture
def stub_environment(monkeypatch):
    """Patch the two seams collect() reaches out through -- driver
    loading and credential resolution -- and hand back the registry a
    test fills in. Keyed by (provider, resource_type), the same key
    collect() caches on."""
    drivers: dict[tuple[str, str], object] = {}
    load_calls: list[tuple[str, str]] = []
    credential_calls: list[str] = []

    def fake_load_driver(provider, resource_type):
        load_calls.append((provider, resource_type))
        try:
            return drivers[(provider, resource_type)]
        except KeyError:
            raise PlanBlockedError(
                f"no driver found for (provider={provider!r}, resource_type={resource_type!r})"
            ) from None

    def fake_resolve_credentials(provider):
        credential_calls.append(provider)
        return {"DIGITALOCEAN_TOKEN": "tok"}

    monkeypatch.setattr(orchestrator, "load_driver", fake_load_driver)
    monkeypatch.setattr(config, "resolve_credentials", fake_resolve_credentials)
    return {
        "drivers": drivers,
        "load_calls": load_calls,
        "credential_calls": credential_calls,
    }


OK_REPORT = HealthReport(
    status=HealthStatus.OK,
    summary="active, public v4 203.0.113.10",
    observations={"status": "active"},
)
FAILING_REPORT = HealthReport(
    status=HealthStatus.FAILING,
    summary='status is "off"',
    observations={"status": "off", "locked": "false", "last_action": "power_off"},
)


class TestResolveName:
    def test_resolves_a_name_to_its_one_state_key(self):
        st = make_state(make_state_entry(name="web-01"))
        assert observability.resolve_name("web-01", st) == "digitalocean.compute.web-01"

    def test_raises_naming_what_is_tracked_when_nothing_matches(self):
        st = make_state(make_state_entry(name="web-01"), make_state_entry(name="db-01"))
        with pytest.raises(ValueError) as excinfo:
            observability.resolve_name("cache-01", st)
        message = str(excinfo.value)
        assert "cache-01" in message
        assert "web-01" in message
        assert "db-01" in message

    def test_raises_listing_candidates_when_more_than_one_matches(self):
        # A droplet and the firewall in front of it can share a name;
        # guessing answers confidently about the thing you did not ask about.
        st = make_state(
            make_state_entry(name="web-01", resource_type="compute"),
            make_state_entry(name="web-01", resource_type="firewall", id="fw-1"),
        )
        with pytest.raises(ValueError) as excinfo:
            observability.resolve_name("web-01", st)
        message = str(excinfo.value)
        assert "digitalocean.compute.web-01" in message
        assert "digitalocean.firewall.web-01" in message

    def test_raises_on_an_empty_state(self):
        with pytest.raises(ValueError):
            observability.resolve_name("web-01", make_state())


class TestCollectScope:
    def test_sweeps_every_tracked_resource_when_keys_is_none(self, tmp_path, stub_environment):
        stub_environment["drivers"][("digitalocean", "compute")] = StubDriver(
            health_result=OK_REPORT
        )
        path = write_state(
            tmp_path / "state.json",
            make_state_entry(name="web-01"),
            make_state_entry(name="db-01", id="987"),
        )
        result = observability.collect(state_path=path, want_metrics=False)
        assert [r.resource_key for r in result.readings] == [
            "digitalocean.compute.web-01",
            "digitalocean.compute.db-01",
        ]

    def test_sweeps_only_the_named_keys(self, tmp_path, stub_environment):
        stub_environment["drivers"][("digitalocean", "compute")] = StubDriver(
            health_result=OK_REPORT
        )
        path = write_state(
            tmp_path / "state.json",
            make_state_entry(name="web-01"),
            make_state_entry(name="db-01", id="987"),
        )
        result = observability.collect(
            keys=["digitalocean.compute.db-01"], state_path=path, want_metrics=False
        )
        assert [r.resource_key for r in result.readings] == ["digitalocean.compute.db-01"]

    def test_a_missing_state_file_is_an_empty_result_not_an_error(self, tmp_path):
        result = observability.collect(state_path=tmp_path / "absent.json")
        assert result.readings == []
        assert result.errors == []

    def test_carries_the_resources_identity_onto_each_reading(self, tmp_path, stub_environment):
        stub_environment["drivers"][("digitalocean", "compute")] = StubDriver(
            health_result=OK_REPORT
        )
        path = write_state(tmp_path / "state.json", make_state_entry())
        reading = observability.collect(state_path=path, want_metrics=False).readings[0]
        assert (reading.provider, reading.resource_type, reading.name, reading.id) == (
            "digitalocean",
            "compute",
            "web-01",
            "123456789",
        )

    def test_records_elapsed_seconds(self, tmp_path, stub_environment):
        stub_environment["drivers"][("digitalocean", "compute")] = StubDriver(
            health_result=OK_REPORT
        )
        path = write_state(tmp_path / "state.json", make_state_entry())
        result = observability.collect(state_path=path, want_metrics=False)
        assert isinstance(result.elapsed_seconds, float)
        assert result.elapsed_seconds >= 0.0


class TestCollectCallsOnlyWhatWasAskedFor:
    def _collect(self, tmp_path, stub_environment, **flags):
        driver = StubDriver(
            health_result=OK_REPORT,
            metrics_result=[Sample(name="memory_bytes", kind=MetricKind.GAUGE, value=1.0)],
        )
        stub_environment["drivers"][("digitalocean", "compute")] = driver
        path = write_state(tmp_path / "state.json", make_state_entry())
        observability.collect(state_path=path, **flags)
        return driver

    def test_check_calls_health_only(self, tmp_path, stub_environment):
        driver = self._collect(tmp_path, stub_environment, want_health=True, want_metrics=False)
        assert len(driver.health_calls) == 1
        assert driver.metrics_calls == []

    def test_metrics_calls_metrics_only(self, tmp_path, stub_environment):
        driver = self._collect(tmp_path, stub_environment, want_health=False, want_metrics=True)
        assert driver.health_calls == []
        assert len(driver.metrics_calls) == 1

    def test_neither_flag_calls_neither_method(self, tmp_path, stub_environment):
        driver = self._collect(tmp_path, stub_environment, want_health=False, want_metrics=False)
        assert driver.health_calls == []
        assert driver.metrics_calls == []

    def test_the_driver_receives_the_resources_id_and_the_providers_credentials(
        self, tmp_path, stub_environment
    ):
        driver = self._collect(tmp_path, stub_environment, want_health=True, want_metrics=False)
        assert driver.health_calls == [("123456789", {"DIGITALOCEAN_TOKEN": "tok"})]


class TestCollectCaching:
    def test_loads_each_driver_once_and_resolves_credentials_once_per_provider(
        self, tmp_path, stub_environment
    ):
        stub_environment["drivers"][("digitalocean", "compute")] = StubDriver(
            health_result=OK_REPORT
        )
        path = write_state(
            tmp_path / "state.json",
            make_state_entry(name="web-01"),
            make_state_entry(name="db-01", id="987"),
            make_state_entry(name="cache-01", id="654"),
        )
        observability.collect(state_path=path, want_metrics=False)
        assert stub_environment["load_calls"] == [("digitalocean", "compute")]
        assert stub_environment["credential_calls"] == ["digitalocean"]


class TestCollectNeverWritesState:
    def test_state_file_is_byte_identical_after_a_sweep(self, tmp_path, stub_environment):
        stub_environment["drivers"][("digitalocean", "compute")] = StubDriver(
            health_result=OK_REPORT,
            metrics_result=[Sample(name="memory_bytes", kind=MetricKind.GAUGE, value=1.0)],
        )
        path = write_state(tmp_path / "state.json", make_state_entry())
        before = path.read_bytes()
        observability.collect(state_path=path)
        assert path.read_bytes() == before

    def test_no_backup_file_is_written(self, tmp_path, stub_environment):
        stub_environment["drivers"][("digitalocean", "compute")] = StubDriver(
            health_result=OK_REPORT
        )
        path = write_state(tmp_path / "state.json", make_state_entry())
        (tmp_path / "state.json.backup").unlink(missing_ok=True)
        observability.collect(state_path=path, want_metrics=False)
        assert not (tmp_path / "state.json.backup").exists()


class TestCollectMakesZeroLLMCalls:
    def test_a_full_sweep_constructs_no_llm_client(
        self, tmp_path, stub_environment, forbid_llm_client
    ):
        # forbid_llm_client is opt-in rather than autouse, so a test that
        # forgets to request it asserts nothing. Requested explicitly here
        # per specs/driver_observability.md's rule 4.
        stub_environment["drivers"][("digitalocean", "compute")] = StubDriver(
            health_result=OK_REPORT,
            metrics_result=[Sample(name="memory_bytes", kind=MetricKind.GAUGE, value=1.0)],
        )
        path = write_state(tmp_path / "state.json", make_state_entry())
        observability.collect(state_path=path)


class TestCollectPartialFailure:
    def _one(self, tmp_path, stub_environment, driver, **flags):
        stub_environment["drivers"][("digitalocean", "compute")] = driver
        path = write_state(tmp_path / "state.json", make_state_entry())
        return observability.collect(state_path=path, **flags).readings[0]

    def test_health_declining_records_the_reason_and_still_attempts_metrics(
        self, tmp_path, stub_environment
    ):
        driver = StubDriver(
            health_exception=CapabilityNotSupported("health", "no status is reported"),
            metrics_result=[Sample(name="memory_bytes", kind=MetricKind.GAUGE, value=1.0)],
        )
        reading = self._one(tmp_path, stub_environment, driver)
        assert reading.health is None
        assert reading.health_unsupported == "no status is reported"
        assert [s.name for s in reading.samples] == ["memory_bytes"]
        assert reading.errors == []

    def test_health_raising_resource_not_found_is_failing_and_not_an_error(
        self, tmp_path, stub_environment
    ):
        # The resource being gone is the finding, not a failure to observe.
        driver = StubDriver(read_exception=None, health_exception=ResourceNotFoundError("gone"))
        reading = self._one(tmp_path, stub_environment, driver, want_metrics=False)
        assert reading.health.status is HealthStatus.FAILING
        assert reading.health.summary == "resource not found"
        assert reading.errors == []

    def test_health_raising_anything_else_is_unknown_and_recorded_as_an_error(
        self, tmp_path, stub_environment
    ):
        driver = StubDriver(health_exception=TimeoutError("read timed out after 30s"))
        reading = self._one(tmp_path, stub_environment, driver, want_metrics=False)
        assert reading.health.status is HealthStatus.UNKNOWN
        assert "read timed out after 30s" in reading.health.summary
        assert any("read timed out after 30s" in e for e in reading.errors)

    def test_health_raising_does_not_skip_metrics(self, tmp_path, stub_environment):
        driver = StubDriver(
            health_exception=TimeoutError("boom"),
            metrics_result=[Sample(name="memory_bytes", kind=MetricKind.GAUGE, value=1.0)],
        )
        reading = self._one(tmp_path, stub_environment, driver)
        assert [s.name for s in reading.samples] == ["memory_bytes"]

    def test_metrics_declining_records_the_reason_and_leaves_health_untouched(
        self, tmp_path, stub_environment
    ):
        driver = StubDriver(
            health_result=OK_REPORT,
            metrics_exception=CapabilityNotSupported("metrics", "no monitoring endpoint"),
        )
        reading = self._one(tmp_path, stub_environment, driver)
        assert reading.samples == []
        assert reading.samples_unsupported == "no monitoring endpoint"
        assert reading.health is OK_REPORT
        assert reading.errors == []

    def test_metrics_raising_resource_not_found_is_recorded_as_an_error(
        self, tmp_path, stub_environment
    ):
        # metrics never calls health(), so a vanished resource would
        # otherwise print an empty block and exit 0 with nothing recorded.
        driver = StubDriver(metrics_exception=ResourceNotFoundError("gone"))
        reading = self._one(tmp_path, stub_environment, driver, want_health=False)
        assert reading.samples == []
        assert reading.errors == ["resource not found"]

    def test_metrics_raising_anything_else_is_recorded_as_an_error(
        self, tmp_path, stub_environment
    ):
        driver = StubDriver(metrics_exception=RuntimeError("HTTP 503"))
        reading = self._one(tmp_path, stub_environment, driver, want_health=False)
        assert reading.samples == []
        assert any("HTTP 503" in e for e in reading.errors)

    def test_a_missing_driver_file_is_recorded_and_does_not_abort_the_sweep(
        self, tmp_path, stub_environment
    ):
        stub_environment["drivers"][("digitalocean", "compute")] = StubDriver(
            health_result=OK_REPORT
        )
        path = write_state(
            tmp_path / "state.json",
            make_state_entry(name="web-01"),
            make_state_entry(name="zone-01", resource_type="domain", id="example.com"),
        )
        result = observability.collect(state_path=path, want_metrics=False)
        by_key = {r.resource_key: r for r in result.readings}
        broken = by_key["digitalocean.domain.zone-01"]
        assert broken.health is None
        assert broken.health_unsupported is None
        assert any("no driver found" in e for e in broken.errors)
        # The rest still render -- a single broken driver must not blank
        # the report.
        assert by_key["digitalocean.compute.web-01"].health is OK_REPORT

    def test_unresolvable_credentials_are_recorded_per_resource(
        self, tmp_path, stub_environment, monkeypatch
    ):
        stub_environment["drivers"][("digitalocean", "compute")] = StubDriver(
            health_result=OK_REPORT
        )

        def refuse(provider):
            raise RuntimeError("DIGITALOCEAN_TOKEN is not set")

        monkeypatch.setattr(config, "resolve_credentials", refuse)
        path = write_state(tmp_path / "state.json", make_state_entry())
        reading = observability.collect(state_path=path, want_metrics=False).readings[0]
        assert reading.health is None
        assert any("DIGITALOCEAN_TOKEN" in e for e in reading.errors)

    def test_every_recorded_error_is_logged_at_warning(self, tmp_path, stub_environment, caplog):
        driver = StubDriver(health_exception=TimeoutError("boom"))
        with caplog.at_level(logging.WARNING, logger="aiform"):
            self._one(tmp_path, stub_environment, driver, want_metrics=False)
        assert any(record.levelno == logging.WARNING for record in caplog.records)


class TestSampleValidation:
    def _samples(self, tmp_path, stub_environment, samples):
        stub_environment["drivers"][("digitalocean", "compute")] = StubDriver(
            metrics_result=samples
        )
        path = write_state(tmp_path / "state.json", make_state_entry())
        return observability.collect(state_path=path, want_health=False).readings[0]

    def test_a_valid_sample_survives(self, tmp_path, stub_environment):
        reading = self._samples(
            tmp_path,
            stub_environment,
            [Sample(name="memory_bytes", kind=MetricKind.GAUGE, value=1.0)],
        )
        assert [s.name for s in reading.samples] == ["memory_bytes"]
        assert reading.errors == []

    @pytest.mark.parametrize("bad_name", ["cpu%", "disk-free", "1_bytes", "", "a b"])
    def test_an_invalid_metric_name_drops_that_sample(self, tmp_path, stub_environment, bad_name):
        reading = self._samples(
            tmp_path,
            stub_environment,
            [
                Sample(name=bad_name, kind=MetricKind.GAUGE, value=1.0),
                Sample(name="memory_bytes", kind=MetricKind.GAUGE, value=2.0),
            ],
        )
        assert [s.name for s in reading.samples] == ["memory_bytes"]
        assert len(reading.errors) == 1
        assert bad_name in reading.errors[0] or "name" in reading.errors[0]

    def test_a_colon_is_a_legal_metric_name_character(self, tmp_path, stub_environment):
        reading = self._samples(
            tmp_path,
            stub_environment,
            [Sample(name="node:memory_bytes", kind=MetricKind.GAUGE, value=1.0)],
        )
        assert [s.name for s in reading.samples] == ["node:memory_bytes"]

    def test_an_invalid_label_name_drops_that_sample(self, tmp_path, stub_environment):
        reading = self._samples(
            tmp_path,
            stub_environment,
            [
                Sample(
                    name="filesystem_free_bytes",
                    kind=MetricKind.GAUGE,
                    value=1.0,
                    labels={"mount-point": "/"},
                )
            ],
        )
        assert reading.samples == []
        assert len(reading.errors) == 1
        assert "mount-point" in reading.errors[0]

    def test_a_colon_is_not_a_legal_label_name_character(self, tmp_path, stub_environment):
        reading = self._samples(
            tmp_path,
            stub_environment,
            [Sample(name="x_bytes", kind=MetricKind.GAUGE, value=1.0, labels={"a:b": "1"})],
        )
        assert reading.samples == []

    def test_a_label_value_may_be_arbitrary_utf8(self, tmp_path, stub_environment):
        reading = self._samples(
            tmp_path,
            stub_environment,
            [
                Sample(
                    name="x_bytes",
                    kind=MetricKind.GAUGE,
                    value=1.0,
                    labels={"mount": '/vol with "quotes" and ünïcode'},
                )
            ],
        )
        assert len(reading.samples) == 1

    def test_a_counter_whose_name_does_not_end_in_total_is_dropped(
        self, tmp_path, stub_environment
    ):
        reading = self._samples(
            tmp_path,
            stub_environment,
            [Sample(name="requests", kind=MetricKind.COUNTER, value=5.0)],
        )
        assert reading.samples == []
        assert "_total" in reading.errors[0]

    def test_a_counter_ending_in_total_survives(self, tmp_path, stub_environment):
        reading = self._samples(
            tmp_path,
            stub_environment,
            [Sample(name="requests_total", kind=MetricKind.COUNTER, value=5.0)],
        )
        assert [s.name for s in reading.samples] == ["requests_total"]

    def test_a_gauge_may_end_in_total(self, tmp_path, stub_environment):
        reading = self._samples(
            tmp_path,
            stub_environment,
            [Sample(name="memory_total", kind=MetricKind.GAUGE, value=5.0)],
        )
        assert [s.name for s in reading.samples] == ["memory_total"]

    @pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
    def test_a_non_finite_value_is_dropped(self, tmp_path, stub_environment, bad_value):
        reading = self._samples(
            tmp_path,
            stub_environment,
            [Sample(name="cpu_percent", kind=MetricKind.GAUGE, value=bad_value)],
        )
        assert reading.samples == []
        assert len(reading.errors) == 1

    @pytest.mark.parametrize("identity", ["provider", "resource_type", "name", "id"])
    def test_a_driver_supplied_identity_label_drops_that_sample_only(
        self, tmp_path, stub_environment, identity
    ):
        # Deliberately not a silent overwrite, which would hide the
        # driver bug; and not the resource's whole set, matching how
        # every other validation failure is scoped.
        reading = self._samples(
            tmp_path,
            stub_environment,
            [
                Sample(
                    name="x_bytes", kind=MetricKind.GAUGE, value=1.0, labels={identity: "spoofed"}
                ),
                Sample(name="y_bytes", kind=MetricKind.GAUGE, value=2.0),
            ],
        )
        assert [s.name for s in reading.samples] == ["y_bytes"]
        assert identity in reading.errors[0]

    def test_one_error_is_recorded_per_rejected_sample(self, tmp_path, stub_environment):
        reading = self._samples(
            tmp_path,
            stub_environment,
            [
                Sample(name="cpu%", kind=MetricKind.GAUGE, value=1.0),
                Sample(name="disk-free", kind=MetricKind.GAUGE, value=2.0),
            ],
        )
        assert reading.samples == []
        assert len(reading.errors) == 2


class TestFamilyCollision:
    def test_one_name_with_two_kinds_across_drivers_drops_the_whole_family(
        self, tmp_path, stub_environment
    ):
        stub_environment["drivers"][("digitalocean", "compute")] = StubDriver(
            metrics_result=[
                Sample(name="queue_depth_total", kind=MetricKind.GAUGE, value=1.0),
                Sample(name="memory_bytes", kind=MetricKind.GAUGE, value=2.0),
            ]
        )
        stub_environment["drivers"][("digitalocean", "firewall")] = StubDriver(
            metrics_result=[Sample(name="queue_depth_total", kind=MetricKind.COUNTER, value=3.0)]
        )
        path = write_state(
            tmp_path / "state.json",
            make_state_entry(name="web-01"),
            make_state_entry(name="web-fw", resource_type="firewall", id="fw-1"),
        )
        result = observability.collect(state_path=path, want_health=False)
        surviving = {s.name for r in result.readings for s in r.samples}
        assert surviving == {"memory_bytes"}
        assert len(result.errors) == 1
        assert "queue_depth_total" in result.errors[0]
        assert "compute" in result.errors[0] and "firewall" in result.errors[0]

    def test_a_family_rejection_goes_to_the_top_level_not_a_resource(
        self, tmp_path, stub_environment
    ):
        # It belongs to neither driver alone, so it has no
        # ResourceReading to hang on.
        stub_environment["drivers"][("digitalocean", "compute")] = StubDriver(
            metrics_result=[Sample(name="queue_depth_total", kind=MetricKind.GAUGE, value=1.0)]
        )
        stub_environment["drivers"][("digitalocean", "firewall")] = StubDriver(
            metrics_result=[Sample(name="queue_depth_total", kind=MetricKind.COUNTER, value=3.0)]
        )
        path = write_state(
            tmp_path / "state.json",
            make_state_entry(name="web-01"),
            make_state_entry(name="web-fw", resource_type="firewall", id="fw-1"),
        )
        result = observability.collect(state_path=path, want_health=False)
        assert all(r.errors == [] for r in result.readings)

    def test_the_same_name_and_kind_from_two_drivers_is_fine(self, tmp_path, stub_environment):
        stub_environment["drivers"][("digitalocean", "compute")] = StubDriver(
            metrics_result=[Sample(name="rule_count", kind=MetricKind.GAUGE, value=1.0)]
        )
        stub_environment["drivers"][("digitalocean", "firewall")] = StubDriver(
            metrics_result=[Sample(name="rule_count", kind=MetricKind.GAUGE, value=4.0)]
        )
        path = write_state(
            tmp_path / "state.json",
            make_state_entry(name="web-01"),
            make_state_entry(name="web-fw", resource_type="firewall", id="fw-1"),
        )
        result = observability.collect(state_path=path, want_health=False)
        assert result.errors == []
        assert len([s for r in result.readings for s in r.samples]) == 2


class TestStatusFor:
    def _setup(self, tmp_path, stub_environment, driver, *, source: str | None = None):
        stub_environment["drivers"][("digitalocean", "compute")] = driver
        md_path = tmp_path / "web.aiform.md"
        if source is not None:
            md_path.write_text(source, encoding="utf-8")
        path = write_state(
            tmp_path / "state.json",
            make_state_entry(aiform_md_path=str(md_path)),
        )
        return path

    SOURCE = """\
---
resource: compute
name: web-01
provider: digitalocean
params:
  region: sfo3
  size: s-1vcpu-2gb
---

## Intent

Runs the app.
"""

    def test_reports_the_four_independent_answers(self, tmp_path, stub_environment):
        driver = StubDriver(
            health_result=FAILING_REPORT,
            read_result={"id": "123456789", "region": "sfo3", "size": "s-1vcpu-2gb"},
        )
        path = self._setup(tmp_path, stub_environment, driver, source=self.SOURCE)
        report = observability.status_for("digitalocean.compute.web-01", state_path=path)
        assert report.deployed_at == datetime(2026, 9, 10, 14, 2, 11, tzinfo=UTC)
        assert report.id == "123456789"
        assert report.live == "present"
        assert report.config.in_sync is True
        assert report.health is FAILING_REPORT

    def test_carries_the_resources_provider_and_type(self, tmp_path, stub_environment):
        # A consumer reading `status` should not have to split
        # resource_key on '.' to recover what `metrics` hands it outright.
        driver = StubDriver(
            health_result=OK_REPORT,
            read_result={"id": "123456789", "region": "sfo3", "size": "s-1vcpu-2gb"},
        )
        path = self._setup(tmp_path, stub_environment, driver, source=self.SOURCE)
        report = observability.status_for("digitalocean.compute.web-01", state_path=path)
        assert report.provider == "digitalocean"
        assert report.resource_type == "compute"
        assert report.name == "web-01"

    def test_config_names_the_spec_file_it_diffed_against(self, tmp_path, stub_environment):
        driver = StubDriver(
            health_result=OK_REPORT,
            read_result={"id": "123456789", "region": "sfo3", "size": "s-1vcpu-2gb"},
        )
        path = self._setup(tmp_path, stub_environment, driver, source=self.SOURCE)
        report = observability.status_for("digitalocean.compute.web-01", state_path=path)
        assert report.config.spec_file == str(tmp_path / "web.aiform.md")
        assert report.config.drifted_fields == []
        assert report.config.detail is None

    def test_a_resource_can_be_failing_while_in_sync(self, tmp_path, stub_environment):
        # check and status fail independently; collapsing them into one
        # verdict would lose exactly this distinction.
        driver = StubDriver(
            health_result=FAILING_REPORT,
            read_result={"id": "123456789", "region": "sfo3", "size": "s-1vcpu-2gb"},
        )
        path = self._setup(tmp_path, stub_environment, driver, source=self.SOURCE)
        report = observability.status_for("digitalocean.compute.web-01", state_path=path)
        assert report.health.status is HealthStatus.FAILING
        assert report.config.in_sync is True

    def test_a_resource_can_be_ok_while_drifted(self, tmp_path, stub_environment):
        driver = StubDriver(
            health_result=OK_REPORT,
            read_result={"id": "123456789", "region": "sfo3", "size": "s-4vcpu-8gb"},
        )
        path = self._setup(tmp_path, stub_environment, driver, source=self.SOURCE)
        report = observability.status_for("digitalocean.compute.web-01", state_path=path)
        assert report.health.status is HealthStatus.OK
        assert report.config.in_sync is False
        assert report.config.drifted_fields == ["size"]

    def test_names_every_drifted_field(self, tmp_path, stub_environment):
        driver = StubDriver(
            health_result=OK_REPORT,
            read_result={"id": "123456789", "region": "nyc1", "size": "s-4vcpu-8gb"},
        )
        path = self._setup(tmp_path, stub_environment, driver, source=self.SOURCE)
        report = observability.status_for("digitalocean.compute.web-01", state_path=path)
        assert report.config.drifted_fields == ["region", "size"]

    def test_a_missing_source_file_reports_no_source_file_found(self, tmp_path, stub_environment):
        # Silence on a drift question reads as no drift, which is the
        # opposite of what is known.
        driver = StubDriver(
            health_result=OK_REPORT,
            read_result={"id": "123456789", "region": "sfo3", "size": "s-1vcpu-2gb"},
        )
        path = self._setup(tmp_path, stub_environment, driver, source=None)
        report = observability.status_for("digitalocean.compute.web-01", state_path=path)
        assert report.config.in_sync is None
        assert report.config.detail == "no source file found"
        assert report.live == "present"
        assert report.health is OK_REPORT

    def test_a_gone_resource_still_reports_when_it_was_deployed(self, tmp_path, stub_environment):
        # The point of the command in this case: it distinguishes a
        # resource that was deleted from one that never existed.
        driver = StubDriver(
            read_exception=ResourceNotFoundError("gone"),
            health_exception=ResourceNotFoundError("gone"),
        )
        path = self._setup(tmp_path, stub_environment, driver, source=self.SOURCE)
        report = observability.status_for("digitalocean.compute.web-01", state_path=path)
        assert report.live == "missing on the provider"
        assert report.config.in_sync is None
        assert report.config.detail == "not applicable: resource is gone"
        assert report.deployed_at == datetime(2026, 9, 10, 14, 2, 11, tzinfo=UTC)
        assert report.id == "123456789"
        assert report.health.status is HealthStatus.FAILING

    def test_a_read_failure_is_reported_on_the_live_line(self, tmp_path, stub_environment):
        driver = StubDriver(
            health_result=OK_REPORT, read_exception=RuntimeError("HTTP 503 from the API")
        )
        path = self._setup(tmp_path, stub_environment, driver, source=self.SOURCE)
        report = observability.status_for("digitalocean.compute.web-01", state_path=path)
        assert "HTTP 503" in report.live
        assert report.health is OK_REPORT

    def test_never_writes_state(self, tmp_path, stub_environment):
        driver = StubDriver(
            health_result=OK_REPORT,
            read_result={"id": "123456789", "region": "nyc1", "size": "s-4vcpu-8gb"},
        )
        path = self._setup(tmp_path, stub_environment, driver, source=self.SOURCE)
        before = path.read_bytes()
        observability.status_for("digitalocean.compute.web-01", state_path=path)
        assert path.read_bytes() == before

    def test_makes_zero_llm_calls_even_though_it_parses_the_source_file(
        self, tmp_path, stub_environment, forbid_llm_client
    ):
        # parse_frontmatter() is pure; extract_intent_notes() is the part
        # that calls a model, and status never needs the prose.
        driver = StubDriver(
            health_result=OK_REPORT,
            read_result={"id": "123456789", "region": "sfo3", "size": "s-1vcpu-2gb"},
        )
        path = self._setup(tmp_path, stub_environment, driver, source=self.SOURCE)
        observability.status_for("digitalocean.compute.web-01", state_path=path)

    def test_raises_for_a_key_that_is_not_tracked(self, tmp_path, stub_environment):
        path = write_state(tmp_path / "state.json", make_state_entry())
        with pytest.raises(ValueError):
            observability.status_for("digitalocean.compute.absent", state_path=path)


class TestRenderCheckText:
    def _reading(self, key="digitalocean.compute.web-01", name="web-01", **kwargs):
        defaults = dict(
            resource_key=key,
            provider="digitalocean",
            resource_type="compute",
            name=name,
            id="123",
            health=None,
            health_unsupported=None,
            samples=[],
            samples_unsupported=None,
            errors=[],
        )
        defaults.update(kwargs)
        return observability.ResourceReading(**defaults)

    def test_one_line_per_resource(self):
        text, _ = observability.render_check([self._reading(health=OK_REPORT)], "text")
        assert text == "ok  compute  digitalocean.compute.web-01  active, public v4 203.0.113.10"

    def test_the_resource_type_is_its_own_column(self):
        # Buried as the middle segment of a dot-joined state key, the type
        # is only recoverable by a reader who already knows the
        # convention and parses it out by hand.
        text, _ = observability.render_check(
            [
                self._reading(
                    key="digitalocean.domain.cloudaiform.com",
                    name="cloudaiform.com",
                    resource_type="domain",
                    health_unsupported="this driver does not implement health()",
                )
            ],
            "text",
        )
        assert text.split("  ")[1] == "domain"

    def test_observations_are_hidden_when_the_verdict_is_ok(self):
        # An earlier version of this was `A or B` with B always true.
        report = HealthReport(
            status=HealthStatus.OK, summary="fine", observations={"status": "active"}
        )
        text, _ = observability.render_check([self._reading(health=report)], "text")
        assert text == "ok  compute  digitalocean.compute.web-01  fine"

    def test_observations_print_indented_four_spaces_when_the_verdict_is_not_ok(self):
        text, _ = observability.render_check([self._reading(health=FAILING_REPORT)], "text")
        assert text == (
            'failing  compute  digitalocean.compute.web-01  status is "off"\n'
            "    status       off\n"
            "    locked       false\n"
            "    last_action  power_off"
        )

    def test_columns_are_padded_to_the_widest_value_across_the_whole_output(self):
        text, _ = observability.render_check(
            [
                self._reading(health=OK_REPORT),
                self._reading(
                    key="digitalocean.firewall.fw",
                    name="fw",
                    resource_type="firewall",
                    health=HealthReport(status=HealthStatus.DEGRADED, summary="pending changes"),
                ),
            ],
            "text",
        )
        lines = text.splitlines()
        assert lines[0] == (
            "ok        compute   digitalocean.compute.web-01  active, public v4 203.0.113.10"
        )
        assert lines[1] == "degraded  firewall  digitalocean.firewall.fw     pending changes"
        assert lines[-1] == "2 of 2 resources reported a health verdict"

    def test_no_line_has_trailing_whitespace(self):
        text, _ = observability.render_check(
            [self._reading(health=FAILING_REPORT), self._reading(key="a.b.c", name="c")], "text"
        )
        assert all(line == line.rstrip() for line in text.splitlines())

    def test_an_unsupported_resource_is_listed_as_unsupported(self):
        text, _ = observability.render_check(
            [self._reading(health_unsupported="no status is reported")], "text"
        )
        assert text.startswith(
            "unsupported  compute  digitalocean.compute.web-01  no status is reported"
        )

    def test_the_coverage_line_is_last_and_unindented_in_the_fleet_form(self):
        text, _ = observability.render_check(
            [
                self._reading(health=OK_REPORT),
                self._reading(key="a.b.c", name="c", health_unsupported="nope"),
                self._reading(key="a.b.d", name="d", health=FAILING_REPORT),
            ],
            "text",
        )
        assert text.splitlines()[-1] == "2 of 3 resources reported a health verdict; 1 unsupported"

    def test_no_coverage_line_in_the_single_form(self):
        text, _ = observability.render_check([self._reading(health=OK_REPORT)], "text")
        assert "resources reported a health verdict" not in text

    def test_an_empty_fleet_renders_a_no_resources_tracked_line(self):
        text, code = observability.render_check([], "text")
        assert text == "no resources tracked"
        assert code == 2


class TestRenderCheckExitCode:
    def _reading(self, health=None, health_unsupported=None, errors=None):
        return observability.ResourceReading(
            resource_key="digitalocean.compute.web-01",
            provider="digitalocean",
            resource_type="compute",
            name="web-01",
            id="123",
            health=health,
            health_unsupported=health_unsupported,
            samples=[],
            samples_unsupported=None,
            errors=errors or [],
        )

    def test_zero_when_every_verdict_is_ok(self):
        _, code = observability.render_check(
            [self._reading(health=OK_REPORT), self._reading(health=OK_REPORT)], "text"
        )
        assert code == 0

    @pytest.mark.parametrize(
        "status", [HealthStatus.DEGRADED, HealthStatus.FAILING, HealthStatus.UNKNOWN]
    )
    def test_one_when_any_verdict_is_not_ok(self, status):
        _, code = observability.render_check(
            [
                self._reading(health=OK_REPORT),
                self._reading(health=HealthReport(status=status, summary="x")),
            ],
            "text",
        )
        assert code == 1

    def test_two_when_no_resource_produced_a_verdict(self):
        _, code = observability.render_check(
            [self._reading(health_unsupported="nope"), self._reading(health_unsupported="nope")],
            "text",
        )
        assert code == 2

    def test_a_declining_driver_does_not_fail_the_aggregate(self):
        # Otherwise the fleet form is unusable until every driver
        # implements health().
        _, code = observability.render_check(
            [self._reading(health=OK_REPORT), self._reading(health_unsupported="nope")], "text"
        )
        assert code == 0

    def test_two_on_an_empty_list(self):
        # A gate that passes because it checked nothing is the failure
        # mode worth designing against.
        _, code = observability.render_check([], "text")
        assert code == 2


class TestRenderCheckJson:
    def _reading(self, **kwargs):
        defaults = dict(
            resource_key="digitalocean.compute.web-01",
            provider="digitalocean",
            resource_type="compute",
            name="web-01",
            id="123",
            health=None,
            health_unsupported=None,
            samples=[],
            samples_unsupported=None,
            errors=[],
        )
        defaults.update(kwargs)
        return observability.ResourceReading(**defaults)

    def test_shape(self):
        text, _ = observability.render_check([self._reading(health=FAILING_REPORT)], "json")
        doc = json.loads(text)
        assert doc["resources"][0]["resource_key"] == "digitalocean.compute.web-01"
        assert doc["resources"][0]["name"] == "web-01"
        assert doc["resources"][0]["status"] == "failing"
        assert doc["resources"][0]["summary"] == 'status is "off"'
        assert doc["resources"][0]["observations"]["status"] == "off"

    def test_identity_matches_the_block_metrics_json_emits(self):
        # A script parsing `metrics --format json` gets provider,
        # resource_type and id outright; parsing `check --format json` it
        # had to split resource_key on '.' for the same information.
        text, _ = observability.render_check([self._reading(health=OK_REPORT)], "json")
        resource = json.loads(text)["resources"][0]
        assert resource["provider"] == "digitalocean"
        assert resource["resource_type"] == "compute"
        assert resource["id"] == "123"

    def test_observations_are_always_present_in_json_even_when_ok(self):
        text, _ = observability.render_check([self._reading(health=OK_REPORT)], "json")
        assert json.loads(text)["resources"][0]["observations"] == {"status": "active"}

    def test_coverage_is_a_field_not_a_stray_line(self):
        text, _ = observability.render_check(
            [self._reading(health=OK_REPORT), self._reading(health_unsupported="nope")], "json"
        )
        doc = json.loads(text)
        assert doc["coverage"] == {"reporting": 1, "total": 2, "unsupported": 1}

    @pytest.mark.parametrize(
        "statuses,expected",
        [
            ([HealthStatus.OK, HealthStatus.OK], "ok"),
            ([HealthStatus.OK, HealthStatus.DEGRADED], "degraded"),
            ([HealthStatus.DEGRADED, HealthStatus.UNKNOWN], "unknown"),
            ([HealthStatus.UNKNOWN, HealthStatus.FAILING], "failing"),
            ([HealthStatus.FAILING, HealthStatus.DEGRADED], "failing"),
        ],
    )
    def test_worst_status_orders_ok_degraded_unknown_failing(self, statuses, expected):
        text, _ = observability.render_check(
            [self._reading(health=HealthReport(status=s, summary="x")) for s in statuses], "json"
        )
        assert json.loads(text)["worst_status"] == expected

    def test_worst_status_is_null_when_nothing_reported(self):
        text, code = observability.render_check([self._reading(health_unsupported="nope")], "json")
        assert json.loads(text)["worst_status"] is None
        assert code == 2

    def test_json_is_a_single_parseable_document(self):
        text, _ = observability.render_check([self._reading(health=OK_REPORT)], "json")
        json.loads(text)


class TestRenderMetricsText:
    def _reading(self, key="digitalocean.compute.web-01", name="web-01", **kwargs):
        defaults = dict(
            resource_key=key,
            provider="digitalocean",
            resource_type="compute",
            name=name,
            id="123",
            health=None,
            health_unsupported=None,
            samples=[],
            samples_unsupported=None,
            errors=[],
        )
        defaults.update(kwargs)
        return observability.ResourceReading(**defaults)

    def test_single_form_prints_rows_alone_with_no_header(self):
        text = observability.render_metrics(
            [
                self._reading(
                    samples=[
                        Sample(name="memory_bytes", kind=MetricKind.GAUGE, value=2147483648.0),
                        Sample(name="cpu_percent", kind=MetricKind.GAUGE, value=41.2),
                    ]
                )
            ],
            "text",
        )
        assert text == ("memory_bytes  2147483648\ncpu_percent   41.2")

    def test_fleet_form_heads_each_resource_with_its_key_and_indents_two_spaces(self):
        text = observability.render_metrics(
            [
                self._reading(
                    samples=[
                        Sample(name="memory_bytes", kind=MetricKind.GAUGE, value=2147483648.0),
                        Sample(name="cpu_percent", kind=MetricKind.GAUGE, value=41.2),
                    ]
                ),
                self._reading(
                    key="digitalocean.firewall.web-fw",
                    name="web-fw",
                    samples=[
                        Sample(name="rule_count", kind=MetricKind.GAUGE, value=4.0),
                        Sample(name="attached_count", kind=MetricKind.GAUGE, value=1.0),
                    ],
                ),
            ],
            "text",
        )
        assert text == (
            "digitalocean.compute.web-01\n"
            "  memory_bytes    2147483648\n"
            "  cpu_percent     41.2\n"
            "digitalocean.firewall.web-fw\n"
            "  rule_count      4\n"
            "  attached_count  1"
        )

    def test_widths_span_every_resource_not_each_block(self):
        # attached_count sets the name column for web-01's rows too --
        # one alignment for the whole output.
        text = observability.render_metrics(
            [
                self._reading(samples=[Sample(name="a_b", kind=MetricKind.GAUGE, value=1.0)]),
                self._reading(
                    key="x.y.z",
                    name="z",
                    samples=[
                        Sample(name="a_very_long_name_here", kind=MetricKind.GAUGE, value=2.0)
                    ],
                ),
            ],
            "text",
        )
        first_row = text.splitlines()[1]
        assert first_row == "  a_b                    1"

    def test_same_name_samples_distinguished_by_bracketed_label_value(self):
        text = observability.render_metrics(
            [
                self._reading(
                    samples=[
                        Sample(
                            name="cpu_seconds_total",
                            kind=MetricKind.COUNTER,
                            value=1066.41,
                            labels={"mode": "idle"},
                        ),
                        Sample(
                            name="cpu_seconds_total",
                            kind=MetricKind.COUNTER,
                            value=2.75,
                            labels={"mode": "iowait"},
                        ),
                        Sample(
                            name="memory_total_bytes",
                            kind=MetricKind.GAUGE,
                            value=2063581184.0,
                        ),
                    ]
                )
            ],
            "text",
        )
        assert text == (
            "cpu_seconds_total[idle]    1066.41\n"
            "cpu_seconds_total[iowait]  2.75\n"
            "memory_total_bytes         2063581184"
        )

    def test_multiple_labels_join_values_ordered_by_key_not_insertion_order(self):
        # filesystem_free_bytes/filesystem_size_bytes are the real multi-label
        # case (specs/digitalocean_compute.md): device, fstype, mountpoint.
        # Insertion order here is deliberately not key order, to prove the
        # join sorts rather than echoing dict order.
        text = observability.render_metrics(
            [
                self._reading(
                    samples=[
                        Sample(
                            name="filesystem_free_bytes",
                            kind=MetricKind.GAUGE,
                            value=19875528704.0,
                            labels={"mountpoint": "/", "device": "/dev/vda1", "fstype": "ext4"},
                        )
                    ]
                )
            ],
            "text",
        )
        assert text == "filesystem_free_bytes[/dev/vda1,ext4,/]  19875528704"

    def test_an_integral_float_prints_without_a_decimal_part(self):
        text = observability.render_metrics(
            [self._reading(samples=[Sample(name="x_bytes", kind=MetricKind.GAUGE, value=4.0)])],
            "text",
        )
        assert text.endswith(" 4")

    def test_a_non_integral_float_prints_its_shortest_round_trip_form(self):
        text = observability.render_metrics(
            [self._reading(samples=[Sample(name="x_pct", kind=MetricKind.GAUGE, value=41.2)])],
            "text",
        )
        assert text.endswith(" 41.2")

    def test_no_thousands_separators_and_no_unit_scaling(self):
        text = observability.render_metrics(
            [
                self._reading(
                    samples=[Sample(name="x_bytes", kind=MetricKind.GAUGE, value=2147483648.0)]
                )
            ],
            "text",
        )
        assert "2147483648" in text
        assert "," not in text and "GiB" not in text

    def test_a_declined_capability_prints_one_unsupported_line(self):
        text = observability.render_metrics(
            [self._reading(samples_unsupported="no monitoring endpoint")], "text"
        )
        assert text == "unsupported: no monitoring endpoint"

    def test_a_resource_with_no_samples_prints_no_samples(self):
        text = observability.render_metrics([self._reading()], "text")
        assert text == "no samples"

    def test_a_placeholder_line_is_indented_under_its_header_in_the_fleet_form(self):
        text = observability.render_metrics(
            [
                self._reading(samples=[Sample(name="a_b", kind=MetricKind.GAUGE, value=1.0)]),
                self._reading(key="x.y.z", name="z", samples_unsupported="nope"),
            ],
            "text",
        )
        assert text.splitlines()[-1] == "  unsupported: nope"

    def test_errors_are_printed(self):
        text = observability.render_metrics(
            [self._reading(errors=["dropped sample 'cpu%': name is not a valid metric name"])],
            "text",
        )
        assert "dropped sample 'cpu%'" in text

    def test_no_line_has_trailing_whitespace(self):
        text = observability.render_metrics(
            [
                self._reading(samples=[Sample(name="a_b", kind=MetricKind.GAUGE, value=1.0)]),
                self._reading(key="x.y.z", name="z", samples_unsupported="nope"),
            ],
            "text",
        )
        assert all(line == line.rstrip() for line in text.splitlines())

    def test_an_empty_list_renders_empty(self):
        assert observability.render_metrics([], "text") == ""


class TestRenderMetricsJson:
    def test_shape(self):
        reading = observability.ResourceReading(
            resource_key="digitalocean.compute.web-01",
            provider="digitalocean",
            resource_type="compute",
            name="web-01",
            id="123456789",
            health=None,
            health_unsupported=None,
            samples=[Sample(name="memory_bytes", kind=MetricKind.GAUGE, value=2147483648.0)],
            samples_unsupported=None,
            errors=["dropped sample 'cpu%': name is not a valid metric name"],
        )
        doc = json.loads(observability.render_metrics([reading], "json"))
        assert doc["resources"][0] == {
            "resource_key": "digitalocean.compute.web-01",
            "provider": "digitalocean",
            "resource_type": "compute",
            "name": "web-01",
            "id": "123456789",
            "samples": [
                {"name": "memory_bytes", "kind": "gauge", "value": 2147483648.0, "labels": {}}
            ],
            "samples_unsupported": None,
            "errors": ["dropped sample 'cpu%': name is not a valid metric name"],
        }

    def test_names_are_bare_as_the_driver_returned_them(self):
        reading = observability.ResourceReading(
            resource_key="digitalocean.compute.web-01",
            provider="digitalocean",
            resource_type="compute",
            name="web-01",
            id="123",
            health=None,
            health_unsupported=None,
            samples=[Sample(name="memory_bytes", kind=MetricKind.GAUGE, value=1.0)],
            samples_unsupported=None,
            errors=[],
        )
        doc = json.loads(observability.render_metrics([reading], "json"))
        assert doc["resources"][0]["samples"][0]["name"] == "memory_bytes"
        assert doc["resources"][0]["samples"][0]["labels"] == {}


def make_status_report(**overrides) -> observability.StatusReport:
    defaults = dict(
        resource_key="digitalocean.compute.web-01",
        provider="digitalocean",
        resource_type="compute",
        name="web-01",
        id="123456789",
        deployed_at=datetime(2026, 9, 10, 14, 2, 11, tzinfo=UTC),
        live="present",
        config=observability.ConfigStatus(
            in_sync=True,
            spec_file="examples/web.aiform.md",
            drifted_fields=[],
            detail=None,
        ),
        health=FAILING_REPORT,
        health_unsupported=None,
    )
    defaults.update(overrides)
    return observability.StatusReport(**defaults)


class TestRenderStatus:
    def _report(self, **kwargs):
        return make_status_report(**kwargs)

    def test_single_form_prints_the_labelled_rows(self):
        text = observability.render_status([self._report()], "text")
        assert text == (
            "type      compute\n"
            "deployed  2026-09-10T14:02:11Z, id 123456789\n"
            "live      present\n"
            "config    in sync with examples/web.aiform.md\n"
            'health    failing — status is "off"'
        )

    def test_the_type_row_names_the_kind_of_resource(self):
        text = observability.render_status(
            [
                self._report(
                    resource_key="digitalocean.domain.cloudaiform.com",
                    resource_type="domain",
                    name="cloudaiform.com",
                )
            ],
            "text",
        )
        assert text.splitlines()[0] == "type      domain"

    def test_the_deployed_row_is_composed_from_the_structured_fields(self):
        # The text form keeps its sentence; only the JSON gains structure.
        text = observability.render_status(
            [
                self._report(
                    deployed_at=datetime(2026, 9, 17, 22, 45, 36, tzinfo=UTC), id="601532562"
                )
            ],
            "text",
        )
        assert "deployed  2026-09-17T22:45:36Z, id 601532562" in text

    def test_a_non_utc_deployed_at_is_converted_rather_than_stamped_z(self):
        text = observability.render_status(
            [
                self._report(
                    deployed_at=datetime(
                        2026, 9, 10, 14, 2, 11, tzinfo=timezone(timedelta(hours=5))
                    )
                )
            ],
            "text",
        )
        assert "deployed  2026-09-10T09:02:11Z, id 123456789" in text

    def test_the_config_row_composes_the_drift_sentence(self):
        text = observability.render_status(
            [
                self._report(
                    config=observability.ConfigStatus(
                        in_sync=False,
                        spec_file="examples/web.aiform.md",
                        drifted_fields=["region", "size"],
                        detail=None,
                    )
                )
            ],
            "text",
        )
        assert "config    2 fields drifted: region, size" in text

    def test_the_config_row_singularises_one_drifted_field(self):
        text = observability.render_status(
            [
                self._report(
                    config=observability.ConfigStatus(
                        in_sync=False,
                        spec_file="examples/web.aiform.md",
                        drifted_fields=["size"],
                        detail=None,
                    )
                )
            ],
            "text",
        )
        assert "config    1 field drifted: size" in text

    def test_the_config_row_prints_the_detail_when_nothing_could_be_diffed(self):
        text = observability.render_status(
            [
                self._report(
                    config=observability.ConfigStatus(
                        in_sync=None,
                        spec_file="examples/web.aiform.md",
                        drifted_fields=[],
                        detail="not applicable: resource is gone",
                    )
                )
            ],
            "text",
        )
        assert "config    not applicable: resource is gone" in text

    def test_fleet_form_heads_each_resource_with_its_key(self):
        text = observability.render_status(
            [self._report(), self._report(resource_key="x.y.z", name="z")], "text"
        )
        lines = text.splitlines()
        assert lines[0] == "digitalocean.compute.web-01"
        assert lines[1].startswith("  type      ")
        assert lines[6] == "x.y.z"

    def test_an_unsupported_health_renders_its_reason(self):
        text = observability.render_status(
            [self._report(health=None, health_unsupported="no status is reported")], "text"
        )
        assert text.splitlines()[-1] == "health    unsupported: no status is reported"

    def test_no_line_has_trailing_whitespace(self):
        text = observability.render_status([self._report(), self._report()], "text")
        assert all(line == line.rstrip() for line in text.splitlines())

    def test_json_shape(self):
        doc = json.loads(observability.render_status([self._report()], "json"))
        assert doc["resources"][0] == {
            "resource_key": "digitalocean.compute.web-01",
            "provider": "digitalocean",
            "resource_type": "compute",
            "name": "web-01",
            "id": "123456789",
            "deployed_at": "2026-09-10T14:02:11Z",
            "live": "present",
            "config": {
                "in_sync": True,
                "spec_file": "examples/web.aiform.md",
                "drifted_fields": [],
                "detail": None,
            },
            "health": {
                "status": "failing",
                "summary": 'status is "off"',
                "observations": {
                    "status": "off",
                    "locked": "false",
                    "last_action": "power_off",
                },
            },
            "health_unsupported": None,
        }

    def test_json_names_the_drifted_fields_rather_than_counting_them_in_prose(self):
        doc = json.loads(
            observability.render_status(
                [
                    self._report(
                        config=observability.ConfigStatus(
                            in_sync=False,
                            spec_file="examples/web.aiform.md",
                            drifted_fields=["region", "size"],
                            detail=None,
                        )
                    )
                ],
                "json",
            )
        )
        assert doc["resources"][0]["config"] == {
            "in_sync": False,
            "spec_file": "examples/web.aiform.md",
            "drifted_fields": ["region", "size"],
            "detail": None,
        }

    def test_json_health_is_null_when_unsupported(self):
        doc = json.loads(
            observability.render_status([self._report(health=None, health_unsupported="x")], "json")
        )
        assert doc["resources"][0]["health"] is None
        assert doc["resources"][0]["health_unsupported"] == "x"


class TestUnknownFormat:
    def test_every_renderer_rejects_an_unknown_format(self):
        for render, payload in (
            (observability.render_check, []),
            (observability.render_metrics, []),
            (observability.render_status, []),
        ):
            with pytest.raises(ValueError):
                render(payload, "yaml")


class TestStatusForLoadsTheDriverOnce:
    def test_the_driver_module_is_exec_d_once_for_one_resource(
        self, tmp_path, stub_environment, monkeypatch
    ):
        # orchestrator.load_driver() execs the driver file on every call,
        # so composing the three live steps independently would exec it
        # three times to answer about one resource.
        stub_environment["drivers"][("digitalocean", "compute")] = StubDriver(
            health_result=OK_REPORT,
            read_result={"id": "123456789", "region": "sfo3", "size": "s-1vcpu-2gb"},
        )
        md = tmp_path / "web.aiform.md"
        md.write_text(TestStatusFor.SOURCE, encoding="utf-8")
        path = write_state(tmp_path / "state.json", make_state_entry(aiform_md_path=str(md)))
        observability.status_for("digitalocean.compute.web-01", state_path=path)
        assert stub_environment["load_calls"] == [("digitalocean", "compute")]
        assert stub_environment["credential_calls"] == ["digitalocean"]

    def test_a_missing_driver_answers_every_live_line_once(self, tmp_path, stub_environment):
        path = write_state(tmp_path / "state.json", make_state_entry())
        report = observability.status_for("digitalocean.compute.web-01", state_path=path)
        assert "no driver found" in report.live
        assert report.config.in_sync is None
        assert report.config.detail == "not applicable: the resource could not be read"
        assert report.health is None
        assert report.deployed_at == datetime(2026, 9, 10, 14, 2, 11, tzinfo=UTC)
        assert report.id == "123456789"


class TestRenderMetricsElapsedSeconds:
    def _reading(self):
        return observability.ResourceReading(
            resource_key="digitalocean.compute.web-01",
            provider="digitalocean",
            resource_type="compute",
            name="web-01",
            id="123",
            health=None,
            health_unsupported=None,
            samples=[Sample(name="memory_bytes", kind=MetricKind.GAUGE, value=1.0)],
            samples_unsupported=None,
            errors=[],
        )

    def test_json_carries_elapsed_seconds_and_top_level_errors(self):
        doc = json.loads(
            observability.render_metrics(
                [self._reading()],
                "json",
                elapsed_seconds=0.83,
                errors=["dropped family 'x_total': a says gauge, b says counter"],
            )
        )
        assert doc["elapsed_seconds"] == 0.83
        assert doc["errors"] == ["dropped family 'x_total': a says gauge, b says counter"]

    def test_a_family_error_is_visible_in_the_text_form_too(self):
        text = observability.render_metrics(
            [self._reading()], "text", errors=["dropped family 'x_total': a vs b"]
        )
        assert text.splitlines()[-1] == "dropped family 'x_total': a vs b"


class TestAgainstARealDriverOnDisk:
    """The stub tests above patch load_driver; these do not, so they
    exercise the real dynamic-import path against a driver file on disk.

    `domain` is the subject rather than `compute`: compute now implements
    both methods, so it no longer exercises the base-class decline at
    all. These two tests asserted "every shipped driver declines" and
    failed loudly the moment that stopped being true, which is what they
    were for.

    Credentials are still stubbed: resolving them for real reads
    DIGITALOCEAN_TOKEN, which .envrc exports here and CI does not -- a
    test green here and red there is worse than no test."""

    @pytest.fixture(autouse=True)
    def _stub_credentials(self, monkeypatch):
        monkeypatch.setattr(
            config, "resolve_credentials", lambda provider: {"DIGITALOCEAN_TOKEN": "tok"}
        )

    def _decliner(self, tmp_path):
        return write_state(
            tmp_path / "state.json",
            make_state_entry(resource_type="domain", name="example.com", id="example.com"),
        )

    def test_a_real_driver_declines_both_and_the_sweep_still_renders(self, tmp_path):
        result = observability.collect(state_path=self._decliner(tmp_path))
        reading = result.readings[0]
        assert reading.health is None
        assert "does not implement health()" in reading.health_unsupported
        assert reading.samples == []
        assert "does not implement metrics()" in reading.samples_unsupported
        assert reading.errors == []

    def test_check_over_a_fleet_of_decliners_exits_two(self, tmp_path):
        result = observability.collect(state_path=self._decliner(tmp_path), want_metrics=False)
        text, code = observability.render_check(result.readings, "text", fleet=True)
        # Leniency about some resources declining must not become a gate
        # that passes having assessed nothing.
        assert code == 2
        assert text.splitlines()[-1] == "0 of 1 resources reported a health verdict; 1 unsupported"

    def test_the_compute_driver_no_longer_declines(self, tmp_path, monkeypatch):
        # The other half of the change above, asserted rather than
        # implied: compute answers both questions now, so a future
        # regression that reverted it would not quietly satisfy the two
        # decline tests above.
        driver = orchestrator.load_driver("digitalocean", "compute")
        base_health = type(driver).__mro__[1].health
        assert type(driver).health is not base_health


class TestReviewRound1Regressions:
    """One test per correctness finding from the first /code-review pass.
    Each was a real failure mode, so each gets an assertion rather than a
    fixed line of code and a note."""

    def _reading(self, tmp_path, stub_environment, driver, **flags):
        stub_environment["drivers"][("digitalocean", "compute")] = driver
        path = write_state(tmp_path / "state.json", make_state_entry())
        return observability.collect(state_path=path, **flags).readings[0]

    def test_a_trailing_newline_does_not_pass_the_metric_name_check(
        self, tmp_path, stub_environment
    ):
        # `$` also matches just before a trailing newline, so this
        # validated and then rendered as one sample split across two
        # lines with every other row padded to the inflated width.
        reading = self._reading(
            tmp_path,
            stub_environment,
            StubDriver(
                metrics_result=[Sample(name="cpu_percent\n", kind=MetricKind.GAUGE, value=1.0)]
            ),
            want_health=False,
        )
        assert reading.samples == []
        assert len(reading.errors) == 1

    def test_a_trailing_newline_does_not_pass_the_label_name_check(
        self, tmp_path, stub_environment
    ):
        reading = self._reading(
            tmp_path,
            stub_environment,
            StubDriver(
                metrics_result=[
                    Sample(
                        name="x_bytes", kind=MetricKind.GAUGE, value=1.0, labels={"mount\n": "/"}
                    )
                ]
            ),
            want_health=False,
        )
        assert reading.samples == []

    def test_metrics_returning_the_wrong_type_does_not_abort_the_sweep(
        self, tmp_path, stub_environment
    ):
        # Previously raised AttributeError out of collect(), taking every
        # other resource's reading with it.
        stub_environment["drivers"][("digitalocean", "compute")] = StubDriver(
            metrics_result=[{"name": "memory_bytes"}]
        )
        stub_environment["drivers"][("digitalocean", "firewall")] = StubDriver(
            metrics_result=[Sample(name="rule_count", kind=MetricKind.GAUGE, value=4.0)]
        )
        path = write_state(
            tmp_path / "state.json",
            make_state_entry(name="web-01"),
            make_state_entry(name="fw", resource_type="firewall", id="f1"),
        )
        result = observability.collect(state_path=path, want_health=False)
        by_key = {r.resource_key: r for r in result.readings}
        assert by_key["digitalocean.compute.web-01"].samples == []
        assert "list[Sample]" in by_key["digitalocean.compute.web-01"].errors[0]
        assert [s.name for s in by_key["digitalocean.firewall.fw"].samples] == ["rule_count"]

    def test_health_returning_the_wrong_type_is_unknown_not_a_renderer_crash(
        self, tmp_path, stub_environment
    ):
        reading = self._reading(
            tmp_path,
            stub_environment,
            StubDriver(health_result={"status": "ok"}),
            want_metrics=False,
        )
        assert reading.health.status is HealthStatus.UNKNOWN
        assert "not HealthReport" in reading.health.summary
        observability.render_check([reading], "text")

    def test_a_driver_returning_unknown_is_recorded_as_a_driver_bug(
        self, tmp_path, stub_environment
    ):
        # driver.py's docstring forbids it: UNKNOWN means aiform could
        # not find out, and only collect() knows that.
        reading = self._reading(
            tmp_path,
            stub_environment,
            StubDriver(health_result=HealthReport(status=HealthStatus.UNKNOWN, summary="dunno")),
            want_metrics=False,
        )
        assert reading.health.status is HealthStatus.UNKNOWN
        assert any("must let its own failures propagate" in e for e in reading.errors)

    def test_a_broken_driver_file_is_recorded_rather_than_propagating(
        self, tmp_path, stub_environment, monkeypatch
    ):
        # load_driver() converts only FileNotFoundError, so a SyntaxError
        # or a bad import used to blank the whole report.
        def explode(provider, resource_type):
            raise SyntaxError("invalid syntax (compute.py, line 12)")

        monkeypatch.setattr(orchestrator, "load_driver", explode)
        path = write_state(tmp_path / "state.json", make_state_entry())
        result = observability.collect(state_path=path)
        assert "invalid syntax" in result.readings[0].errors[0]

    def test_a_multi_line_error_renders_as_one_line_per_resource(self, tmp_path, stub_environment):
        # A continuation line at column zero reads as another resource's
        # row in the fleet form.
        reading = self._reading(
            tmp_path,
            stub_environment,
            StubDriver(health_exception=RuntimeError("line one\nline two")),
            want_metrics=False,
        )
        text, _ = observability.render_check([reading], "text", fleet=False)
        assert len(text.splitlines()) == 1

    def test_an_errored_resource_is_labelled_error_not_unsupported(
        self, tmp_path, stub_environment
    ):
        # "unsupported" contradicted the coverage line printed directly
        # beneath it, which counts only real declines. `network` has no
        # driver file, so this is the no-verdict-no-decline case rather
        # than a decline.
        path = write_state(tmp_path / "state.json", make_state_entry(resource_type="network"))
        result = observability.collect(state_path=path, want_metrics=False)
        text, code = observability.render_check(result.readings, "text", fleet=True)
        assert text.splitlines()[0].startswith("error  ")
        assert text.splitlines()[-1] == "0 of 1 resources reported a health verdict"
        assert code == 2


class TestStatusForRound1Regressions:
    SOURCE = TestStatusFor.SOURCE

    def _report(
        self, tmp_path, stub_environment, driver, *, source=SOURCE, path_name="web.aiform.md"
    ):
        stub_environment["drivers"][("digitalocean", "compute")] = driver
        md = tmp_path / path_name
        if source is not None:
            md.write_text(source, encoding="utf-8")
        state_path = write_state(tmp_path / "state.json", make_state_entry(aiform_md_path=str(md)))
        return observability.status_for("digitalocean.compute.web-01", state_path=state_path)

    def test_a_read_failure_does_not_claim_the_resource_is_gone(self, tmp_path, stub_environment):
        # live said "HTTP 503" while config said "resource is gone" --
        # the spec reserves that wording for ResourceNotFoundError.
        report = self._report(
            tmp_path,
            stub_environment,
            StubDriver(health_result=OK_REPORT, read_exception=RuntimeError("HTTP 503")),
        )
        assert "HTTP 503" in report.live
        assert report.config.detail == "not applicable: the resource could not be read"

    def test_a_gone_resource_still_says_gone(self, tmp_path, stub_environment):
        report = self._report(
            tmp_path,
            stub_environment,
            StubDriver(
                health_exception=ResourceNotFoundError("gone"),
                read_exception=ResourceNotFoundError("gone"),
            ),
        )
        assert report.live == "missing on the provider"
        assert report.config.detail == "not applicable: resource is gone"

    def test_a_malformed_source_file_is_not_reported_as_missing(self, tmp_path, stub_environment):
        report = self._report(
            tmp_path,
            stub_environment,
            StubDriver(health_result=OK_REPORT, read_result=dict(LIVE_ATTRS)),
            source="not frontmatter at all\n",
        )
        assert report.config.in_sync is None
        assert report.config.detail.startswith("source file is malformed")

    def test_an_undecodable_source_file_is_reported_not_raised(self, tmp_path, stub_environment):
        # read_text(encoding="utf-8-sig") raises UnicodeDecodeError on
        # undecodable bytes -- a ValueError subclass, not an OSError, so
        # it must land in the same branch as a malformed-frontmatter
        # ValueError. Undetected, this crashes status_for() outright
        # instead of reporting the one resource, which in status_reports()
        # would blank every other resource's line too.
        stub_environment["drivers"][("digitalocean", "compute")] = StubDriver(
            health_result=OK_REPORT, read_result=dict(LIVE_ATTRS)
        )
        md = tmp_path / "web.aiform.md"
        md.write_bytes(b"---\nprovider: digitalocean\n\xff\xfe\n---\n")
        state_path = write_state(tmp_path / "state.json", make_state_entry(aiform_md_path=str(md)))

        report = observability.status_for("digitalocean.compute.web-01", state_path=state_path)

        assert report.config.detail.startswith("source file is malformed")

    def test_a_repurposed_source_file_is_not_diffed_against_this_resource(
        self, tmp_path, stub_environment
    ):
        # The plan path matches by frontmatter, not by the recorded path,
        # so diffing this droplet against another resource's params would
        # have status and plan disagreeing.
        other = TestStatusFor.SOURCE.replace("name: web-01", "name: other-01")
        report = self._report(
            tmp_path,
            stub_environment,
            StubDriver(health_result=OK_REPORT, read_result=dict(LIVE_ATTRS)),
            source=other,
        )
        assert "now declares digitalocean.compute.other-01" in report.config.detail

    def test_a_non_utc_last_applied_at_is_converted_rather_than_stamped_z(
        self, tmp_path, stub_environment
    ):
        stub_environment["drivers"][("digitalocean", "compute")] = StubDriver(
            health_result=OK_REPORT, read_result=dict(LIVE_ATTRS)
        )
        md = tmp_path / "web.aiform.md"
        md.write_text(TestStatusFor.SOURCE, encoding="utf-8")
        state_path = write_state(
            tmp_path / "state.json",
            make_state_entry(aiform_md_path=str(md), last_applied_at="2026-09-10T14:02:11+05:00"),
        )
        report = observability.status_for("digitalocean.compute.web-01", state_path=state_path)
        # The offset is preserved on the report and normalised where it is
        # stamped, which is now the renderer rather than status_for().
        assert report.deployed_at.utcoffset() == timedelta(hours=5)
        doc = json.loads(observability.render_status([report], "json"))
        assert doc["resources"][0]["deployed_at"] == "2026-09-10T09:02:11Z"


LIVE_ATTRS = {"id": "123456789", "region": "sfo3", "size": "s-1vcpu-2gb"}


class TestClaimsTheFirstReviewFoundUntested:
    def test_check_json_carries_the_decline_reason(self):
        reading = observability.ResourceReading(
            resource_key="digitalocean.compute.web-01",
            provider="digitalocean",
            resource_type="compute",
            name="web-01",
            id="1",
            health=None,
            health_unsupported="no status is reported",
            samples=[],
            samples_unsupported=None,
            errors=[],
        )
        doc = json.loads(observability.render_check([reading], "json")[0])
        # Without this a declining resource says status: null with
        # nothing saying why.
        assert doc["resources"][0]["status"] is None
        assert doc["resources"][0]["unsupported"] == "no status is reported"

    def test_check_json_carries_an_errored_resources_errors(self):
        reading = observability.ResourceReading(
            resource_key="digitalocean.network.n1",
            provider="digitalocean",
            resource_type="network",
            name="n1",
            id="1",
            health=None,
            health_unsupported=None,
            samples=[],
            samples_unsupported=None,
            errors=["no driver found for (provider='digitalocean', resource_type='network')"],
        )
        doc = json.loads(observability.render_check([reading], "json")[0])
        # It has no verdict and no decline, so without `errors` it would
        # disappear from the document entirely.
        assert doc["resources"][0]["unsupported"] is None
        assert "no driver found" in doc["resources"][0]["errors"][0]

    def test_a_non_diffable_field_does_not_report_permanent_drift(self, tmp_path, stub_environment):
        # status' config line goes through refresh_resource() precisely
        # so a write-only field is carried forward from state, exactly as
        # on the plan path. Without it every droplet declaring ssh_keys
        # reports drift forever -- the shape of #133.
        class SSHKeyDriver(StubDriver):
            NON_DIFFABLE_FIELDS = ["ssh_keys"]

        source = """\
---
resource: compute
name: web-01
provider: digitalocean
params:
  region: sfo3
  size: s-1vcpu-2gb
  ssh_keys:
    - "aa:bb:cc"
---
"""
        md = tmp_path / "web.aiform.md"
        md.write_text(source, encoding="utf-8")
        # read() cannot return ssh_keys -- DO's droplet GET has no such
        # field -- which is the whole reason the carry-forward exists.
        stub_environment["drivers"][("digitalocean", "compute")] = SSHKeyDriver(
            health_result=OK_REPORT,
            read_result={"id": "123456789", "region": "sfo3", "size": "s-1vcpu-2gb"},
        )
        path = write_state(
            tmp_path / "state.json",
            make_state_entry(
                aiform_md_path=str(md),
                attributes={"region": "sfo3", "size": "s-1vcpu-2gb", "ssh_keys": ["aa:bb:cc"]},
            ),
        )
        report = observability.status_for("digitalocean.compute.web-01", state_path=path)
        assert report.config.in_sync is True
        assert report.config.drifted_fields == []

    def test_unresolvable_credentials_answer_every_live_line_once(
        self, tmp_path, stub_environment, monkeypatch
    ):
        stub_environment["drivers"][("digitalocean", "compute")] = StubDriver(
            health_result=OK_REPORT
        )

        def refuse(provider):
            raise RuntimeError("DIGITALOCEAN_TOKEN not found")

        monkeypatch.setattr(config, "resolve_credentials", refuse)
        path = write_state(tmp_path / "state.json", make_state_entry())
        report = observability.status_for("digitalocean.compute.web-01", state_path=path)
        assert "DIGITALOCEAN_TOKEN" in report.live
        assert report.config.detail == "not applicable: the resource could not be read"
        assert report.health is None
        assert report.deployed_at == datetime(2026, 9, 10, 14, 2, 11, tzinfo=UTC)
        assert report.id == "123456789"


class TestReviewRound2Regressions:
    def _reading(self, **kwargs):
        defaults = dict(
            resource_key="digitalocean.compute.web-01",
            provider="digitalocean",
            resource_type="compute",
            name="web-01",
            id="1",
            health=None,
            health_unsupported=None,
            samples=[],
            samples_unsupported=None,
            errors=[],
        )
        defaults.update(kwargs)
        return observability.ResourceReading(**defaults)

    def test_a_multi_line_driver_summary_stays_one_line(self):
        # The first fix collapsed exception text but not the driver's own
        # summary, which is just as free-form.
        report = HealthReport(
            status=HealthStatus.FAILING, summary='status is "off"\nlast_action power_off'
        )
        text, _ = observability.render_check([self._reading(health=report)], "text", fleet=False)
        assert len(text.splitlines()) == 1

    def test_a_multi_line_decline_reason_stays_one_line(self):
        text, _ = observability.render_check(
            [self._reading(health_unsupported="no signal\nand none coming")], "text", fleet=False
        )
        assert len(text.splitlines()) == 1

    def test_a_multi_line_observation_value_does_not_inflate_the_column_width(self):
        report = HealthReport(
            status=HealthStatus.FAILING,
            summary="down",
            observations={"status": "off", "detail": "line one\nline two"},
        )
        text, _ = observability.render_check([self._reading(health=report)], "text", fleet=False)
        assert len(text.splitlines()) == 3
        assert all(line == line.rstrip() for line in text.splitlines())

    def test_a_multi_line_observation_key_does_not_inflate_the_column_width(self):
        # The case the code comment describes, which the value-only test
        # above does not reach: an uncollapsed key sets `width` for every
        # other row.
        report = HealthReport(
            status=HealthStatus.FAILING,
            summary="down",
            observations={"status": "off", "last\naction": "power_off"},
        )
        text, _ = observability.render_check([self._reading(health=report)], "text", fleet=False)
        assert text.splitlines()[1:] == [
            "    status       off",
            "    last action  power_off",
        ]

    def test_a_multi_line_metrics_decline_stays_one_line(self):
        text = observability.render_metrics(
            [self._reading(samples_unsupported="no endpoint\nfor this kind")], "text", fleet=False
        )
        assert len(text.splitlines()) == 1

    def test_a_multi_line_status_health_line_stays_one_line(self):
        report = make_status_report(
            health=HealthReport(status=HealthStatus.FAILING, summary="a\nb")
        )
        text = observability.render_status([report], "text", fleet=False)
        assert len(text.splitlines()) == 5

    def test_the_family_error_names_the_counter_first(self, tmp_path, stub_environment):
        # The claims are sorted by MetricKind, and "counter" < "gauge" --
        # the spec's example had the order backwards.
        stub_environment["drivers"][("digitalocean", "compute")] = StubDriver(
            metrics_result=[Sample(name="q_total", kind=MetricKind.GAUGE, value=1.0)]
        )
        stub_environment["drivers"][("digitalocean", "firewall")] = StubDriver(
            metrics_result=[Sample(name="q_total", kind=MetricKind.COUNTER, value=2.0)]
        )
        path = write_state(
            tmp_path / "state.json",
            make_state_entry(name="web-01"),
            make_state_entry(name="fw", resource_type="firewall", id="f1"),
        )
        result = observability.collect(state_path=path, want_health=False)
        assert result.errors == [
            "dropped family 'q_total': digitalocean.firewall says counter, "
            "digitalocean.compute says gauge"
        ]


class TestStatusReports:
    """The fleet form. status_for() in a loop loaded state once per
    resource and handed each call throwaway caches, so N droplets cost N
    exec_module()s and N credential resolutions."""

    def _fleet(self, tmp_path, stub_environment, n=3):
        stub_environment["drivers"][("digitalocean", "compute")] = StubDriver(
            health_result=OK_REPORT, read_result=dict(LIVE_ATTRS)
        )
        md = tmp_path / "web.aiform.md"
        md.write_text(TestStatusFor.SOURCE, encoding="utf-8")
        entries = [
            make_state_entry(name=f"web-{i:02d}", id=str(i), aiform_md_path=str(md))
            for i in range(n)
        ]
        return write_state(tmp_path / "state.json", *entries)

    def test_reports_every_tracked_resource_when_keys_is_none(self, tmp_path, stub_environment):
        path = self._fleet(tmp_path, stub_environment)
        reports = observability.status_reports(state_path=path)
        assert [r.name for r in reports] == ["web-00", "web-01", "web-02"]

    def test_loads_the_driver_once_for_the_whole_fleet(self, tmp_path, stub_environment):
        path = self._fleet(tmp_path, stub_environment)
        observability.status_reports(state_path=path)
        assert stub_environment["load_calls"] == [("digitalocean", "compute")]
        assert stub_environment["credential_calls"] == ["digitalocean"]

    def test_status_for_in_a_loop_is_what_this_replaces(self, tmp_path, stub_environment):
        # The behaviour being fixed, asserted so the fix cannot silently
        # regress to it: one load per resource.
        path = self._fleet(tmp_path, stub_environment)
        for i in range(3):
            observability.status_for(f"digitalocean.compute.web-{i:02d}", state_path=path)
        assert len(stub_environment["load_calls"]) == 3

    def test_reports_only_the_named_keys(self, tmp_path, stub_environment):
        path = self._fleet(tmp_path, stub_environment)
        reports = observability.status_reports(["digitalocean.compute.web-01"], state_path=path)
        assert [r.name for r in reports] == ["web-01"]

    def test_an_empty_state_reports_nothing(self, tmp_path, stub_environment):
        path = write_state(tmp_path / "state.json")
        assert observability.status_reports(state_path=path) == []

    def test_raises_for_an_untracked_key(self, tmp_path, stub_environment):
        path = self._fleet(tmp_path, stub_environment)
        with pytest.raises(ValueError):
            observability.status_reports(["digitalocean.compute.absent"], state_path=path)

    def test_writes_no_state(self, tmp_path, stub_environment):
        path = self._fleet(tmp_path, stub_environment)
        before = path.read_bytes()
        observability.status_reports(state_path=path)
        assert path.read_bytes() == before

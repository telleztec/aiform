# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""health()/metrics() for the DigitalOcean compute driver.

Every monitoring payload here is loaded from a recorded probe transcript
(probes/transcripts/digitalocean_compute_monitoring/), never hand-written
-- so a fixture cannot encode a belief nobody checked against the live
API. See specs/driver_creation.md.
"""

import urllib.error
import urllib.request

import pytest

from aiform.exceptions import ResourceNotFoundError
from aiform.models import HealthStatus, MetricKind
from drivers.digitalocean.compute import (
    METRIC_WINDOW_SECONDS,
    OBSERVE_TIMEOUT_SECONDS,
    Driver,
)
from tests.drivers.test_digitalocean_compute import (
    BASE_URL,
    CREDENTIALS,
    FakeHTTPResponse,
    FakeUrlopen,
    droplet_url,
    http_error,
    make_droplet,
)
from tests.drivers.transcripts import response_body

SESSION = "digitalocean_compute_monitoring"

# The exact bodies DigitalOcean returned during the probe session.
MEMORY_TOTAL = response_body(SESSION, "01")
CPU = response_body(SESSION, "02")
MEMORY_AVAILABLE = response_body(SESSION, "03")
FILESYSTEM_FREE = response_body(SESSION, "06")
FILESYSTEM_SIZE = response_body(SESSION, "07")
LOAD_1 = response_body(SESSION, "08")
LOAD_5 = response_body(SESSION, "09")
LOAD_15 = response_body(SESSION, "10")
NO_AGENT = response_body(SESSION, "23")
UNAUTHORIZED = response_body(SESSION, "14")

TRANSCRIPT_BY_METRIC = {
    "memory_total": MEMORY_TOTAL,
    "memory_available": MEMORY_AVAILABLE,
    "filesystem_free": FILESYSTEM_FREE,
    "filesystem_size": FILESYSTEM_SIZE,
    "load_1": LOAD_1,
    "load_5": LOAD_5,
    "load_15": LOAD_15,
    "cpu": CPU,
}


def metric_url(metric: str, droplet_id: str, start: int, end: int) -> str:
    return (
        f"{BASE_URL}/monitoring/metrics/droplet/{metric}"
        f"?host_id={droplet_id}&start={start}&end={end}"
    )


@pytest.fixture
def fake_urlopen(monkeypatch) -> FakeUrlopen:
    # Defined here rather than imported: importing a fixture by name
    # shadows it in every test signature that requests it (ruff F811),
    # and the three sibling driver test modules each declare their own
    # for the same reason.
    fake = FakeUrlopen()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake


@pytest.fixture
def frozen_window(monkeypatch):
    """Pin time so a test can name the exact URL the driver will build."""
    now = 1789483140
    monkeypatch.setattr("drivers.digitalocean.compute.time.time", lambda: now)
    return now - METRIC_WINDOW_SECONDS, now


def script_all_metrics(fake: FakeUrlopen, window, droplet_id="123", bodies=None):
    start, end = window
    bodies = bodies or TRANSCRIPT_BY_METRIC
    for metric, body in bodies.items():
        fake.script("GET", metric_url(metric, droplet_id, start, end), FakeHTTPResponse(200, body))


class TestHealth:
    def test_an_active_droplet_with_a_public_address_is_ok(self, fake_urlopen):
        fake_urlopen.script("GET", droplet_url("123"), FakeHTTPResponse(200, make_droplet()))
        report = Driver().health("123", CREDENTIALS)
        assert report.status is HealthStatus.OK
        assert "active" in report.summary
        assert "203.0.113.10" in report.summary

    def test_observations_carry_what_the_driver_actually_saw(self, fake_urlopen):
        fake_urlopen.script(
            "GET", droplet_url("123"), FakeHTTPResponse(200, make_droplet(monitoring_enabled=True))
        )
        report = Driver().health("123", CREDENTIALS)
        assert report.observations["status"] == "active"
        assert report.observations["locked"] == "false"
        assert report.observations["region"] == "sfo3"
        assert report.observations["ipv4_address"] == "203.0.113.10"

    def test_a_powered_off_droplet_is_failing(self, fake_urlopen):
        fake_urlopen.script(
            "GET", droplet_url("123"), FakeHTTPResponse(200, make_droplet(status="off"))
        )
        report = Driver().health("123", CREDENTIALS)
        assert report.status is HealthStatus.FAILING
        assert report.summary == 'status is "off"'
        assert report.observations["status"] == "off"

    def test_an_archived_droplet_is_failing(self, fake_urlopen):
        fake_urlopen.script(
            "GET", droplet_url("123"), FakeHTTPResponse(200, make_droplet(status="archive"))
        )
        assert Driver().health("123", CREDENTIALS).status is HealthStatus.FAILING

    def test_a_still_provisioning_droplet_is_degraded(self, fake_urlopen):
        # Probe 25: a droplet seconds after create reports status "new".
        # Not FAILING -- nothing is wrong, it just is not ready.
        fake_urlopen.script(
            "GET", droplet_url("123"), FakeHTTPResponse(200, make_droplet(status="new"))
        )
        report = Driver().health("123", CREDENTIALS)
        assert report.status is HealthStatus.DEGRADED
        assert "provisioning" in report.summary

    def test_an_active_droplet_with_no_public_address_is_degraded(self, fake_urlopen):
        fake_urlopen.script(
            "GET", droplet_url("123"), FakeHTTPResponse(200, make_droplet(public_ip=None))
        )
        report = Driver().health("123", CREDENTIALS)
        assert report.status is HealthStatus.DEGRADED
        assert "public" in report.summary

    def test_a_locked_droplet_is_degraded(self, fake_urlopen):
        droplet = make_droplet()
        droplet["droplet"]["locked"] = True
        fake_urlopen.script("GET", droplet_url("123"), FakeHTTPResponse(200, droplet))
        report = Driver().health("123", CREDENTIALS)
        assert report.status is HealthStatus.DEGRADED
        assert "locked" in report.summary

    def test_an_unmodelled_status_is_degraded_not_ok(self, fake_urlopen):
        # Never OK on a status this driver does not model: the verdict
        # would assert health it has no basis for.
        fake_urlopen.script(
            "GET", droplet_url("123"), FakeHTTPResponse(200, make_droplet(status="migrating"))
        )
        report = Driver().health("123", CREDENTIALS)
        assert report.status is HealthStatus.DEGRADED
        assert "migrating" in report.summary

    def test_a_missing_droplet_raises_resource_not_found(self, fake_urlopen):
        fake_urlopen.script("GET", droplet_url("123"), http_error(droplet_url("123"), 404))
        with pytest.raises(ResourceNotFoundError):
            Driver().health("123", CREDENTIALS)

    def test_any_other_http_error_propagates(self, fake_urlopen):
        # A driver does not classify its own failures; collect() converts
        # the exception into UNKNOWN and keeps the error text.
        fake_urlopen.script("GET", droplet_url("123"), http_error(droplet_url("123"), 503))
        with pytest.raises(urllib.error.HTTPError):
            Driver().health("123", CREDENTIALS)

    def test_never_returns_unknown(self, fake_urlopen):
        for status in ("active", "off", "new", "archive", "migrating"):
            fake_urlopen.script(
                "GET", droplet_url("123"), FakeHTTPResponse(200, make_droplet(status=status))
            )
            assert Driver().health("123", CREDENTIALS).status is not HealthStatus.UNKNOWN

    def test_issues_exactly_one_request(self, fake_urlopen):
        fake_urlopen.script("GET", droplet_url("123"), FakeHTTPResponse(200, make_droplet()))
        Driver().health("123", CREDENTIALS)
        assert len(fake_urlopen.calls) == 1

    def test_uses_the_shorter_observe_timeout(self, fake_urlopen):
        # A health() reusing the driver's 30s REQUEST_TIMEOUT_SECONDS
        # silently gets a bound six times the spec's per-resource target.
        fake_urlopen.script("GET", droplet_url("123"), FakeHTTPResponse(200, make_droplet()))
        Driver().health("123", CREDENTIALS)
        assert fake_urlopen.calls[0]["timeout"] == OBSERVE_TIMEOUT_SECONDS


class TestMetrics:
    def test_returns_the_documented_families(self, fake_urlopen, frozen_window):
        script_all_metrics(fake_urlopen, frozen_window)
        samples = Driver().metrics("123", CREDENTIALS)
        names = {s.name for s in samples}
        assert names == {
            "memory_total_bytes",
            "memory_available_bytes",
            "filesystem_free_bytes",
            "filesystem_size_bytes",
            "load1",
            "load5",
            "load15",
            "cpu_seconds_total",
        }

    def test_values_come_from_the_newest_point_of_the_series(self, fake_urlopen, frozen_window):
        script_all_metrics(fake_urlopen, frozen_window)
        samples = {s.name: s for s in Driver().metrics("123", CREDENTIALS)}
        newest = float(MEMORY_TOTAL["data"]["result"][0]["values"][-1][1])
        assert samples["memory_total_bytes"].value == newest

    def test_json_string_values_are_parsed_to_floats(self, fake_urlopen, frozen_window):
        script_all_metrics(fake_urlopen, frozen_window)
        for sample in Driver().metrics("123", CREDENTIALS):
            assert isinstance(sample.value, float)

    def test_cpu_is_a_counter_with_one_sample_per_mode(self, fake_urlopen, frozen_window):
        # Probe 02: eight series labelled by mode. Cumulative seconds --
        # a COUNTER under the amended counter-honesty rule, where a reset
        # at reboot is a handled condition rather than a disqualifier.
        script_all_metrics(fake_urlopen, frozen_window)
        cpu = [s for s in Driver().metrics("123", CREDENTIALS) if s.name == "cpu_seconds_total"]
        assert len(cpu) == 8
        assert {s.kind for s in cpu} == {MetricKind.COUNTER}
        assert {s.labels["mode"] for s in cpu} == {
            "idle",
            "iowait",
            "irq",
            "nice",
            "softirq",
            "steal",
            "system",
            "user",
        }

    def test_every_non_cpu_family_is_a_gauge(self, fake_urlopen, frozen_window):
        script_all_metrics(fake_urlopen, frozen_window)
        for sample in Driver().metrics("123", CREDENTIALS):
            if sample.name != "cpu_seconds_total":
                assert sample.kind is MetricKind.GAUGE

    def test_the_host_id_label_is_stripped(self, fake_urlopen, frozen_window):
        # Every series DigitalOcean returns carries host_id, which is
        # identity the output already prints beside the samples -- and a
        # future exporter cannot stamp its own if the driver got there
        # first.
        script_all_metrics(fake_urlopen, frozen_window)
        for sample in Driver().metrics("123", CREDENTIALS):
            assert "host_id" not in sample.labels

    def test_filesystem_samples_keep_their_distinguishing_labels(self, fake_urlopen, frozen_window):
        # Probe 06/07: device, fstype and mountpoint are what tell two
        # filesystems apart, so they are not identity and must survive.
        script_all_metrics(fake_urlopen, frozen_window)
        free = [
            s for s in Driver().metrics("123", CREDENTIALS) if s.name == "filesystem_free_bytes"
        ]
        assert free and set(free[0].labels) == {"device", "fstype", "mountpoint"}

    def test_a_droplet_whose_agent_never_reported_yields_no_samples(
        self, fake_urlopen, frozen_window
    ):
        # Probe 23/24: 200 with an empty result, not an error. The normal
        # case for any droplet younger than the agent's first push.
        script_all_metrics(
            fake_urlopen, frozen_window, bodies=dict.fromkeys(TRANSCRIPT_BY_METRIC, NO_AGENT)
        )
        assert Driver().metrics("123", CREDENTIALS) == []

    def test_one_empty_family_does_not_suppress_the_others(self, fake_urlopen, frozen_window):
        bodies = dict(TRANSCRIPT_BY_METRIC)
        bodies["load_5"] = NO_AGENT
        script_all_metrics(fake_urlopen, frozen_window, bodies=bodies)
        names = {s.name for s in Driver().metrics("123", CREDENTIALS)}
        assert "load5" not in names
        assert "load1" in names

    def test_no_sample_carries_an_identity_label(self, fake_urlopen, frozen_window):
        script_all_metrics(fake_urlopen, frozen_window)
        for sample in Driver().metrics("123", CREDENTIALS):
            assert not {"provider", "resource_type", "name", "id"} & set(sample.labels)

    def test_a_counter_name_ends_in_total_and_a_gauge_does_not_have_to(
        self, fake_urlopen, frozen_window
    ):
        script_all_metrics(fake_urlopen, frozen_window)
        for sample in Driver().metrics("123", CREDENTIALS):
            if sample.kind is MetricKind.COUNTER:
                assert sample.name.endswith("_total")

    def test_an_unauthorized_response_propagates_rather_than_claiming_the_droplet_is_gone(
        self, fake_urlopen, frozen_window
    ):
        # Probes 14 and 15: DigitalOcean answers 401 for a host_id that
        # does not exist AND for a malformed one -- it never 404s here.
        # So metrics() structurally cannot tell "gone" from "bad token",
        # and must not guess: raising ResourceNotFoundError would report
        # a live droplet as deleted whenever the token was wrong.
        start, end = frozen_window
        url = metric_url("memory_total", "123", start, end)
        fake_urlopen.script("GET", url, http_error(url, 401, UNAUTHORIZED))
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            Driver().metrics("123", CREDENTIALS)
        assert excinfo.value.code == 401

    def test_does_not_raise_resource_not_found(self, fake_urlopen, frozen_window):
        start, end = frozen_window
        url = metric_url("memory_total", "123", start, end)
        fake_urlopen.script("GET", url, http_error(url, 401, UNAUTHORIZED))
        with pytest.raises(Exception) as excinfo:
            Driver().metrics("123", CREDENTIALS)
        assert not isinstance(excinfo.value, ResourceNotFoundError)

    def test_uses_the_shorter_observe_timeout(self, fake_urlopen, frozen_window):
        script_all_metrics(fake_urlopen, frozen_window)
        Driver().metrics("123", CREDENTIALS)
        assert {call["timeout"] for call in fake_urlopen.calls} == {OBSERVE_TIMEOUT_SECONDS}

    def test_issues_one_request_per_family_and_no_more(self, fake_urlopen, frozen_window):
        script_all_metrics(fake_urlopen, frozen_window)
        Driver().metrics("123", CREDENTIALS)
        assert len(fake_urlopen.calls) == len(TRANSCRIPT_BY_METRIC)

    def test_every_request_is_a_get(self, fake_urlopen, frozen_window):
        # Read-only: GET/HEAD against the control plane and nothing else.
        script_all_metrics(fake_urlopen, frozen_window)
        Driver().metrics("123", CREDENTIALS)
        assert {call["method"] for call in fake_urlopen.calls} == {"GET"}

    def test_the_window_is_wide_enough_for_more_than_one_point(self, fake_urlopen, frozen_window):
        # Probe 12: the series steps every 120s and the newest point can
        # be ~100s old, so a window narrower than that returns nothing.
        assert METRIC_WINDOW_SECONDS >= 240


class TestBothMethodsAreReadOnly:
    def test_neither_writes_nor_mutates(self, fake_urlopen, frozen_window):
        fake_urlopen.script("GET", droplet_url("123"), FakeHTTPResponse(200, make_droplet()))
        script_all_metrics(fake_urlopen, frozen_window)
        driver = Driver()
        driver.health("123", CREDENTIALS)
        driver.metrics("123", CREDENTIALS)
        assert {call["method"] for call in fake_urlopen.calls} == {"GET"}

    def test_the_driver_still_declines_nothing_it_implements(self):
        # Both are overridden, so neither should raise the base class's
        # decline any more.
        assert Driver().health.__qualname__.startswith("Driver")
        assert Driver().metrics.__qualname__.startswith("Driver")

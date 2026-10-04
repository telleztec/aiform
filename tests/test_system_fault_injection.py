# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Offline tests for `tests/system/fault_injection.py` -- see
specs/system_test_interrupt.md.

These run in the DEFAULT pytest run, against a fake `urlopen`. The injector
decides where the live interrupt suite cuts a run off, so a bug in it makes a
live stage pass without having tested anything (an injection point that never
fires) or fire somewhere other than the spec says. Neither is visible from the
live suite's own result, and the live suite is billable.
"""

import email.message
import io
import json
import threading
import time
import types
import urllib.error
import urllib.request

import pytest

from aiform import state
from tests.system.fault_injection import (
    Fault,
    InjectedInterrupt,
    describe_provider_errors,
    fail_request,
    interrupt_after_request,
    interrupt_after_state_save,
    interrupt_before_request,
    rewrite_responses,
    skip_driver_sleeps,
)


class FakeResponse:
    status = 200

    def __init__(self, body: bytes):
        self._body = io.BytesIO(body)
        self.closed = False

    def read(self, *args):
        return self._body.read(*args)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


@pytest.fixture
def provider(monkeypatch):
    """A fake urlopen that records what actually reached the 'provider'."""
    reached: list[tuple[str, str]] = []
    bodies: dict[tuple[str, str], bytes] = {}
    responses: list[FakeResponse] = []
    errors: list[BaseException] = []

    def fake_urlopen(request, *args, **kwargs):
        if isinstance(request, str):
            method, url = "GET", request
        else:
            method, url = request.get_method(), request.full_url
        reached.append((method, url))
        if errors:
            raise errors.pop(0)
        body = bodies.get((method, url), b"{}")
        response = FakeResponse(body)
        responses.append(response)
        return response

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return types.SimpleNamespace(reached=reached, bodies=bodies, responses=responses, errors=errors)


def blocked_until(release: threading.Event):
    def urlopen(*args, **kwargs):
        release.wait(5)
        return FakeResponse(b"{}")

    return urlopen


def call(method: str, url: str):
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())


DROPLETS = "https://api.digitalocean.com/v2/droplets"


def invalid_key_identifiers(url: str) -> urllib.error.HTTPError:
    body = b'{"id": "unprocessable_entity", "message": "are invalid key identifiers"}'
    return urllib.error.HTTPError(
        url, 422, "Unprocessable", email.message.Message(), io.BytesIO(body)
    )


class TestInterruptAfterRequest:
    def test_the_provider_call_completes_and_then_the_interrupt_is_raised(self, provider):
        with interrupt_after_request("POST", r"/v2/droplets$") as fault:
            with pytest.raises(InjectedInterrupt):
                call("POST", DROPLETS)

        assert provider.reached == [("POST", DROPLETS)]
        assert fault.fired is True

    def test_the_interrupt_is_a_keyboard_interrupt_that_escapes_except_exception(self, provider):
        swallowed = False
        with interrupt_after_request("POST", r"/v2/droplets$"):
            try:
                call("POST", DROPLETS)
            except Exception:
                swallowed = True
            except KeyboardInterrupt:
                pass
        assert not swallowed
        assert issubclass(InjectedInterrupt, KeyboardInterrupt)
        assert not issubclass(InjectedInterrupt, Exception)

    def test_the_faulted_response_body_is_kept_as_parsed_json(self, provider):
        provider.bodies[("POST", DROPLETS)] = b'{"droplet": {"id": 4242}}'
        with interrupt_after_request("POST", r"/v2/droplets$") as fault:
            with pytest.raises(InjectedInterrupt):
                call("POST", DROPLETS)
        assert fault.response == {"droplet": {"id": 4242}}

    def test_the_real_response_is_closed_before_raising(self, provider):
        with interrupt_after_request("POST", r"/v2/droplets$"):
            with pytest.raises(InjectedInterrupt):
                call("POST", DROPLETS)
        assert all(response.closed for response in provider.responses)

    def test_a_different_method_on_the_same_url_is_not_a_match(self, provider):
        with interrupt_after_request("POST", r"/v2/droplets$") as fault:
            assert call("GET", DROPLETS) == {}
        assert fault.fired is False

    def test_a_different_url_with_the_same_method_is_not_a_match(self, provider):
        with interrupt_after_request("DELETE", r"/v2/droplets/\d+$") as fault:
            assert call("DELETE", "https://api.digitalocean.com/v2/firewalls/abc") == {}
        assert fault.fired is False

    def test_the_url_is_matched_as_a_regex_search_not_an_equality(self, provider):
        with interrupt_after_request("GET", r"/v2/droplets/\d+$") as fault:
            with pytest.raises(InjectedInterrupt):
                call("GET", f"{DROPLETS}/777")
        assert fault.fired is True

    def test_only_the_requested_occurrence_fires(self, provider):
        with interrupt_after_request("GET", r"/v2/droplets/\d+$", occurrence=3) as fault:
            call("GET", f"{DROPLETS}/1")
            call("GET", f"{DROPLETS}/2")
            assert fault.fired is False
            with pytest.raises(InjectedInterrupt):
                call("GET", f"{DROPLETS}/3")
        assert fault.fired is True

    def test_a_response_predicate_decides_which_matches_count(self, provider):
        url = f"{DROPLETS}/9"
        provider.bodies[("GET", url)] = b'{"droplet": {"status": "active"}}'
        with interrupt_after_request(
            "GET",
            r"/v2/droplets/\d+$",
            response_predicate=lambda body: body["droplet"]["status"] != "active",
        ) as fault:
            assert call("GET", url) == {"droplet": {"status": "active"}}
            assert fault.fired is False

            provider.bodies[("GET", url)] = b'{"droplet": {"status": "new"}}'
            with pytest.raises(InjectedInterrupt):
                call("GET", url)
        assert fault.response == {"droplet": {"status": "new"}}

    def test_a_non_firing_match_still_hands_the_caller_a_readable_body(self, provider):
        url = f"{DROPLETS}/9"
        provider.bodies[("GET", url)] = b'{"droplet": {"status": "active"}}'
        with interrupt_after_request(
            "GET", r"/v2/droplets/\d+$", response_predicate=lambda body: False
        ):
            request = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(request) as response:
                assert json.loads(response.read()) == {"droplet": {"status": "active"}}
                assert response.status == 200

    def test_it_fires_at_most_once(self, provider):
        with interrupt_after_request("GET", r"/v2/droplets/\d+$") as fault:
            with pytest.raises(InjectedInterrupt):
                call("GET", f"{DROPLETS}/1")
            assert call("GET", f"{DROPLETS}/2") == {}
        assert fault.fired is True

    def test_a_bare_string_url_is_matched_as_a_get(self, provider):
        with interrupt_after_request("GET", r"/v2/droplets$") as fault:
            with pytest.raises(InjectedInterrupt):
                urllib.request.urlopen(DROPLETS)
        assert fault.fired is True

    def test_seen_lists_every_request_made_while_installed(self, provider):
        with interrupt_after_request("DELETE", r"/never-matches$") as fault:
            call("GET", f"{DROPLETS}/1")
            call("POST", DROPLETS)
        assert fault.seen == [("GET", f"{DROPLETS}/1"), ("POST", DROPLETS)]

    def test_a_request_that_raises_never_counts_as_a_match(self, monkeypatch):
        def failing(request, *args, **kwargs):
            raise urllib.error.HTTPError(request.full_url, 422, "nope", {}, None)

        monkeypatch.setattr(urllib.request, "urlopen", failing)
        with interrupt_after_request("POST", r"/v2/droplets$") as fault:
            with pytest.raises(urllib.error.HTTPError):
                call("POST", DROPLETS)
        assert fault.fired is False

    def test_a_body_that_is_not_json_does_not_stop_it_firing(self, provider):
        provider.bodies[("POST", DROPLETS)] = b"not json"
        with interrupt_after_request("POST", r"/v2/droplets$") as fault:
            with pytest.raises(InjectedInterrupt):
                call_raw = urllib.request.Request(DROPLETS, method="POST")
                urllib.request.urlopen(call_raw)
        assert fault.fired is True
        assert fault.response is None

    def test_urlopen_is_restored_on_a_normal_exit(self, provider):
        installed = urllib.request.urlopen
        with interrupt_after_request("POST", r"/v2/droplets$"):
            assert urllib.request.urlopen is not installed
        assert urllib.request.urlopen is installed

    def test_urlopen_is_restored_when_the_interrupt_escapes(self, provider):
        installed = urllib.request.urlopen
        with pytest.raises(InjectedInterrupt):
            with interrupt_after_request("POST", r"/v2/droplets$"):
                call("POST", DROPLETS)
        assert urllib.request.urlopen is installed


class TestInterruptBeforeRequest:
    def test_the_provider_is_never_reached(self, provider):
        with interrupt_before_request("PUT", r"/v2/firewalls/abc$") as fault:
            with pytest.raises(InjectedInterrupt):
                call("PUT", "https://api.digitalocean.com/v2/firewalls/abc")
        assert provider.reached == []
        assert fault.fired is True

    def test_other_requests_go_through(self, provider):
        with interrupt_before_request("PUT", r"/v2/firewalls/abc$") as fault:
            call("GET", "https://api.digitalocean.com/v2/firewalls/abc")
        assert provider.reached == [("GET", "https://api.digitalocean.com/v2/firewalls/abc")]
        assert fault.fired is False

    def test_only_the_requested_occurrence_fires(self, provider):
        url = "https://api.digitalocean.com/v2/firewalls/abc"
        with interrupt_before_request("PUT", r"/v2/firewalls/abc$", occurrence=2):
            call("PUT", url)
            with pytest.raises(InjectedInterrupt):
                call("PUT", url)
        assert provider.reached == [("PUT", url)]

    def test_urlopen_is_restored(self, provider):
        installed = urllib.request.urlopen
        with interrupt_before_request("PUT", r"/x$"):
            pass
        assert urllib.request.urlopen is installed


class TestInterruptAfterStateSave:
    @pytest.fixture
    def saved(self, monkeypatch):
        saves: list[object] = []

        def fake_save(st, path=None):
            saves.append(st)

        monkeypatch.setattr(state, "save", fake_save)
        return saves

    def test_the_real_save_runs_and_then_the_interrupt_is_raised(self, saved):
        marker = types.SimpleNamespace(resources={"a": 1})
        with interrupt_after_state_save(lambda st: "a" in st.resources) as fault:
            with pytest.raises(InjectedInterrupt):
                state.save(marker, "ignored")
        assert saved == [marker]
        assert fault.fired is True

    def test_every_save_is_recorded_so_a_stage_that_never_fires_can_be_diagnosed(self, saved):
        with interrupt_after_state_save(lambda st: "b" in st.resources) as fault:
            state.save(types.SimpleNamespace(resources={}), "ignored")
            state.save(types.SimpleNamespace(resources={"a": 1, "z": 2}), "ignored")
        assert fault.seen == [("state.save", ""), ("state.save", "a,z")]

    def test_a_save_whose_state_fails_the_predicate_does_not_fire(self, saved):
        with interrupt_after_state_save(lambda st: "a" in st.resources) as fault:
            state.save(types.SimpleNamespace(resources={}), "ignored")
        assert len(saved) == 1
        assert fault.fired is False

    def test_the_predicate_picks_the_save_not_a_count_of_saves(self, saved):
        with interrupt_after_state_save(lambda st: "a" in st.resources) as fault:
            state.save(types.SimpleNamespace(resources={}), "ignored")
            state.save(types.SimpleNamespace(resources={}), "ignored")
            with pytest.raises(InjectedInterrupt):
                state.save(types.SimpleNamespace(resources={"a": 1}), "ignored")
        assert len(saved) == 3
        assert fault.fired is True

    def test_only_the_requested_occurrence_fires(self, saved):
        with interrupt_after_state_save(lambda st: True, occurrence=2):
            state.save(types.SimpleNamespace(resources={}), "ignored")
            with pytest.raises(InjectedInterrupt):
                state.save(types.SimpleNamespace(resources={}), "ignored")

    def test_it_fires_at_most_once(self, saved):
        with interrupt_after_state_save(lambda st: True):
            with pytest.raises(InjectedInterrupt):
                state.save(types.SimpleNamespace(resources={}), "ignored")
            state.save(types.SimpleNamespace(resources={}), "ignored")
        assert len(saved) == 2

    def test_save_is_restored_even_when_the_interrupt_escapes(self, saved):
        installed = state.save
        with pytest.raises(InjectedInterrupt):
            with interrupt_after_state_save(lambda st: True):
                state.save(types.SimpleNamespace(resources={}), "ignored")
        assert state.save is installed


SLOW = 0.3
DEADLINE = 0.05


def a_timeout(**kwargs):
    return fail_request(
        "POST", r"/v2/droplets$", "timeout", delay=SLOW, deadline=DEADLINE, **kwargs
    )


class TestFailRequestTimeout:
    def test_the_caller_gives_up_at_the_deadline_with_a_timeout_error(self, provider):
        with a_timeout() as fault:
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                call("POST", DROPLETS)
            elapsed = time.monotonic() - started
            fault.join()

        assert DEADLINE * 0.9 <= elapsed < SLOW

    def test_the_error_carries_nothing_from_the_request(self, provider):
        # A real urllib timeout says only "timed out". A message that echoed the
        # URL would put the resource id in the output on the injector's say-so,
        # and a stage asserting the driver reports the id would pass on that.
        with a_timeout() as fault:
            with pytest.raises(TimeoutError) as raised:
                call("POST", DROPLETS)
            fault.join()

        assert str(raised.value) == "timed out"

    def test_the_provider_has_not_been_reached_when_the_caller_gives_up(self, provider):
        with a_timeout() as fault:
            with pytest.raises(TimeoutError):
                call("POST", DROPLETS)
            assert provider.reached == []
            fault.join()

    def test_the_provider_still_acts_after_the_caller_has_given_up(self, provider):
        with a_timeout() as fault:
            with pytest.raises(TimeoutError):
                call("POST", DROPLETS)
            fault.join()

        assert provider.reached == [("POST", DROPLETS)]
        assert fault.fired is True

    def test_the_late_response_is_recorded_so_the_caller_can_learn_the_resource_id(self, provider):
        provider.bodies[("POST", DROPLETS)] = b'{"droplet": {"id": 4242}}'
        with a_timeout() as fault:
            with pytest.raises(TimeoutError):
                call("POST", DROPLETS)
            assert fault.response is None
            fault.join()

        assert fault.response == {"droplet": {"id": 4242}}
        assert all(response.closed for response in provider.responses)

    def test_a_late_response_that_is_not_json_is_recorded_as_none(self, provider):
        provider.bodies[("POST", DROPLETS)] = b"not json"
        with a_timeout() as fault:
            with pytest.raises(TimeoutError):
                call("POST", DROPLETS)
            fault.join()
        assert fault.response is None

    def test_a_failed_late_call_is_recorded_and_makes_the_block_fail_loudly(self, monkeypatch):
        def refusing(request, *args, **kwargs):
            raise urllib.error.URLError("refused")

        monkeypatch.setattr(urllib.request, "urlopen", refusing)
        with pytest.raises(RuntimeError, match="late provider call failed") as raised:
            with a_timeout() as fault:
                with pytest.raises(TimeoutError):
                    call("POST", DROPLETS)
        assert isinstance(fault.worker_error, urllib.error.URLError)
        assert raised.value.__cause__ is fault.worker_error

    def test_join_reports_a_failed_late_call_so_a_stage_cannot_pass_without_the_provider_acting(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
        )
        with pytest.raises(RuntimeError, match="late provider call failed"):
            with a_timeout() as fault:
                with pytest.raises(TimeoutError):
                    call("POST", DROPLETS)
                with pytest.raises(RuntimeError, match="late provider call failed"):
                    fault.join()

    def test_a_request_that_does_not_match_passes_straight_through(self, provider):
        with a_timeout() as fault:
            started = time.monotonic()
            assert call("GET", DROPLETS) == {}
            assert time.monotonic() - started < DEADLINE
        assert fault.fired is False
        assert provider.reached == [("GET", DROPLETS)]

    def test_only_the_requested_occurrence_loses_the_race(self, provider):
        with a_timeout(occurrence=2) as fault:
            assert call("POST", DROPLETS) == {}
            assert fault.fired is False
            with pytest.raises(TimeoutError):
                call("POST", DROPLETS)
            fault.join()
        assert fault.fired is True
        assert provider.reached == [("POST", DROPLETS)] * 2

    def test_it_fires_at_most_once(self, provider):
        with a_timeout() as fault:
            with pytest.raises(TimeoutError):
                call("POST", DROPLETS)
            assert call("POST", DROPLETS) == {}
            fault.join()
        assert provider.reached == [("POST", DROPLETS)] * 2

    def test_a_provider_that_does_not_act_is_never_called(self, provider):
        with a_timeout(provider_acts=False) as fault:
            with pytest.raises(TimeoutError):
                call("POST", DROPLETS)
            fault.join()
        assert provider.reached == []
        assert fault.fired is True
        assert fault.response is None

    def test_seen_lists_every_request_made_while_installed(self, provider):
        with a_timeout() as fault:
            call("GET", DROPLETS)
            with pytest.raises(TimeoutError):
                call("POST", DROPLETS)
            fault.join()
        assert fault.seen == [("GET", DROPLETS), ("POST", DROPLETS)]

    def test_a_timing_option_the_kind_does_not_know_is_rejected(self):
        with pytest.raises(TypeError):
            fail_request("POST", r"/x$", "timeout", sleepiness=3)

    def test_the_deadline_must_be_shorter_than_the_delay(self):
        with pytest.raises(ValueError, match="delay"):
            fail_request("POST", r"/x$", "timeout", delay=0.1, deadline=0.1)

    def test_an_unknown_failure_kind_is_rejected_with_the_known_ones_named(self):
        with pytest.raises(ValueError, match="timeout"):
            fail_request("POST", r"/x$", "meltdown")

    def test_a_bare_string_url_is_matched_as_a_get(self, provider):
        with fail_request("GET", r"/v2/droplets$", "timeout", delay=SLOW, deadline=DEADLINE) as f:
            with pytest.raises(TimeoutError):
                urllib.request.urlopen(DROPLETS, timeout=5)
            f.join()
        assert provider.reached == [("GET", DROPLETS)]


class TestFailRequestWorkerLifetime:
    def test_a_bounded_join_reports_a_worker_that_will_not_finish(self, monkeypatch):
        release = threading.Event()
        monkeypatch.setattr(urllib.request, "urlopen", blocked_until(release))
        try:
            with a_timeout() as fault:
                with pytest.raises(TimeoutError):
                    call("POST", DROPLETS)
                started = time.monotonic()
                with pytest.raises(RuntimeError, match="still running"):
                    fault.join(timeout=0.1)
                assert time.monotonic() - started < 2
                release.set()
                fault.join()
        finally:
            release.set()

    def test_a_bounded_join_names_every_worker_still_running(self):
        release = threading.Event()
        fault = Fault()
        for request in ("POST /v2/droplets", "GET /v2/droplets/1"):
            worker = threading.Thread(target=release.wait, daemon=True)
            worker.start()
            fault._workers.append((worker, request))
        try:
            with pytest.raises(RuntimeError) as raised:
                fault.join(timeout=0.05)
            assert "POST /v2/droplets" in str(raised.value)
            assert "GET /v2/droplets/1" in str(raised.value)
        finally:
            release.set()

    def test_leaving_the_block_joins_the_worker_even_when_the_body_raises(self, provider):
        with pytest.raises(ValueError, match="boom"):
            with a_timeout():
                with pytest.raises(TimeoutError):
                    call("POST", DROPLETS)
                raise ValueError("boom")

        assert provider.reached == [("POST", DROPLETS)]

    def test_a_failing_body_is_not_replaced_by_a_hung_worker(self, monkeypatch):
        release = threading.Event()
        monkeypatch.setattr(urllib.request, "urlopen", blocked_until(release))
        try:
            with pytest.raises(AssertionError, match="the real finding") as raised:
                with fail_request(
                    "POST",
                    r"/v2/droplets$",
                    "timeout",
                    delay=SLOW,
                    deadline=DEADLINE,
                    join_timeout=0.1,
                ):
                    with pytest.raises(TimeoutError):
                        call("POST", DROPLETS)
                    raise AssertionError("the real finding")
        finally:
            release.set()
        assert any("still running" in note for note in raised.value.__notes__)

    def test_a_failing_body_is_not_replaced_by_a_failed_late_call(self, monkeypatch):
        monkeypatch.setattr(
            urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
        )
        with pytest.raises(AssertionError, match="the real finding") as raised:
            with a_timeout():
                with pytest.raises(TimeoutError):
                    call("POST", DROPLETS)
                raise AssertionError("the real finding")
        assert any("late provider call failed" in note for note in raised.value.__notes__)

    def test_a_hung_worker_is_reported_with_the_request_to_hunt_for(self, monkeypatch):
        release = threading.Event()
        monkeypatch.setattr(urllib.request, "urlopen", blocked_until(release))
        try:
            with a_timeout() as fault:
                with pytest.raises(TimeoutError):
                    call("POST", DROPLETS)
                with pytest.raises(RuntimeError) as raised:
                    fault.join(timeout=0.1)
                release.set()
                fault.join()
        finally:
            release.set()
        message = str(raised.value)
        assert f"POST {DROPLETS}" in message
        assert "by name" in message

    def test_urlopen_is_restored_on_exit_even_with_a_slow_worker(self, provider):
        installed = urllib.request.urlopen
        with a_timeout():
            assert urllib.request.urlopen is not installed
            with pytest.raises(TimeoutError):
                call("POST", DROPLETS)
        assert urllib.request.urlopen is installed

    def test_exit_does_not_wait_forever_on_a_hung_worker(self, monkeypatch):
        release = threading.Event()
        monkeypatch.setattr(urllib.request, "urlopen", blocked_until(release))
        started = time.monotonic()
        try:
            with pytest.raises(RuntimeError, match="still running"):
                with fail_request(
                    "POST",
                    r"/v2/droplets$",
                    "timeout",
                    delay=SLOW,
                    deadline=DEADLINE,
                    join_timeout=0.1,
                ):
                    with pytest.raises(TimeoutError):
                        call("POST", DROPLETS)
        finally:
            release.set()
        assert time.monotonic() - started < 3


class TestFailureKindsOwnTheirOptions:
    def test_a_kind_without_timing_options_is_neither_given_nor_checked_against_them(
        self, provider, monkeypatch
    ):
        from tests.system import fault_injection

        seen_options = {}

        def reset(fault, real, request, args, kwargs, *, provider_acts, **options):
            seen_options.update(options)
            raise ConnectionResetError("injected")

        monkeypatch.setitem(fault_injection._FAILURES, "stand_in", (reset, lambda **options: None))
        with fail_request("POST", r"/v2/droplets$", "stand_in", provider_acts=False) as fault:
            with pytest.raises(ConnectionResetError):
                call("POST", DROPLETS)
        assert fault.fired is True
        assert seen_options == {}
        assert provider.reached == []


class TestFailRequestReset:
    def test_the_caller_sees_a_connection_reset_wrapped_in_a_url_error(self, provider):
        with fail_request("POST", r"/v2/droplets$", "reset") as fault:
            with pytest.raises(urllib.error.URLError) as raised:
                call("POST", DROPLETS)
        assert isinstance(raised.value.reason, ConnectionResetError)
        assert fault.fired is True

    def test_the_provider_acts_before_the_connection_drops(self, provider):
        provider.bodies[("POST", DROPLETS)] = b'{"droplet": {"id": 4242}}'
        with fail_request("POST", r"/v2/droplets$", "reset") as fault:
            with pytest.raises(urllib.error.URLError):
                call("POST", DROPLETS)
        assert provider.reached == [("POST", DROPLETS)]
        assert fault.response == {"droplet": {"id": 4242}}
        assert all(response.closed for response in provider.responses)

    def test_a_provider_that_does_not_act_is_never_called(self, provider):
        with fail_request("POST", r"/v2/droplets$", "reset", provider_acts=False) as fault:
            with pytest.raises(urllib.error.URLError):
                call("POST", DROPLETS)
        assert provider.reached == []
        assert fault.response is None

    def test_a_timing_option_is_rejected(self):
        with pytest.raises(TypeError):
            fail_request("POST", r"/x$", "reset", delay=1)


@pytest.mark.parametrize(
    ("kind", "status"), [("http_500", 500), ("http_503", 503)], ids=["http_500", "http_503"]
)
class TestFailRequestServerError:
    def test_the_caller_sees_an_http_error_with_that_status(self, provider, kind, status):
        with fail_request("POST", r"/v2/droplets$", kind) as fault:
            with pytest.raises(urllib.error.HTTPError) as raised:
                call("POST", DROPLETS)
        assert raised.value.code == status
        assert raised.value.url == DROPLETS
        assert fault.fired is True

    def test_the_synthetic_error_body_is_readable_json_with_a_message(self, provider, kind, status):
        with fail_request("POST", r"/v2/droplets$", kind):
            with pytest.raises(urllib.error.HTTPError) as raised:
                call("POST", DROPLETS)
        assert isinstance(json.loads(raised.value.read())["message"], str)

    def test_the_real_call_runs_and_its_response_is_discarded_but_recorded(
        self, provider, kind, status
    ):
        provider.bodies[("POST", DROPLETS)] = b'{"droplet": {"id": 4242}}'
        with fail_request("POST", r"/v2/droplets$", kind) as fault:
            with pytest.raises(urllib.error.HTTPError) as raised:
                call("POST", DROPLETS)
        assert provider.reached == [("POST", DROPLETS)]
        assert fault.response == {"droplet": {"id": 4242}}
        assert b"4242" not in raised.value.read()
        assert all(response.closed for response in provider.responses)

    def test_a_provider_that_does_not_act_is_never_called(self, provider, kind, status):
        with fail_request("POST", r"/v2/droplets$", kind, provider_acts=False) as fault:
            with pytest.raises(urllib.error.HTTPError):
                call("POST", DROPLETS)
        assert provider.reached == []
        assert fault.response is None

    def test_only_the_requested_occurrence_fails(self, provider, kind, status):
        with fail_request("GET", r"/v2/droplets/\d+$", kind, occurrence=2) as fault:
            call("GET", DROPLET_42)
            assert fault.fired is False
            with pytest.raises(urllib.error.HTTPError):
                call("GET", DROPLET_42)
            call("GET", DROPLET_42)
        assert provider.reached == [("GET", DROPLET_42)] * 3


def _is_connection_reset(error: BaseException) -> bool:
    return isinstance(error.reason, ConnectionResetError)


SYNTHETIC = {
    "http_503": lambda error: isinstance(error, urllib.error.HTTPError) and error.code == 503,
    "http_500": lambda error: isinstance(error, urllib.error.HTTPError) and error.code == 500,
    "reset": _is_connection_reset,
}


@pytest.mark.parametrize("kind", list(SYNTHETIC))
class TestFailRequestWhenTheProviderItselfFails:
    def test_the_real_error_reaches_the_caller_and_the_fault_is_not_counted_as_fired(
        self, provider, kind
    ):
        real = invalid_key_identifiers(DROPLETS)
        provider.errors.append(real)
        with fail_request("POST", r"/v2/droplets$", kind) as fault:
            with pytest.raises(urllib.error.HTTPError) as raised:
                call("POST", DROPLETS)
        assert raised.value is real
        assert not SYNTHETIC[kind](raised.value)
        assert fault.fired is False
        assert fault.provider_errors == [real]

    def test_the_next_matching_call_gets_the_synthetic_failure_with_the_real_body(
        self, provider, kind
    ):
        provider.errors.append(invalid_key_identifiers(DROPLETS))
        provider.bodies[("POST", DROPLETS)] = b'{"droplet": {"id": 4242}}'
        with fail_request("POST", r"/v2/droplets$", kind) as fault:
            with pytest.raises(urllib.error.HTTPError):
                call("POST", DROPLETS)
            with pytest.raises(urllib.error.URLError) as raised:
                call("POST", DROPLETS)
        assert SYNTHETIC[kind](raised.value)
        assert fault.fired is True
        assert fault.response == {"droplet": {"id": 4242}}
        assert len(fault.provider_errors) == 1

    def test_it_still_fires_at_most_once_afterwards(self, provider, kind):
        provider.errors.append(invalid_key_identifiers(DROPLETS))
        with fail_request("POST", r"/v2/droplets$", kind) as fault:
            with pytest.raises(urllib.error.HTTPError):
                call("POST", DROPLETS)
            with pytest.raises(urllib.error.URLError):
                call("POST", DROPLETS)
            call("POST", DROPLETS)
            call("POST", DROPLETS)
        assert fault.fired is True
        assert provider.reached == [("POST", DROPLETS)] * 4

    def test_an_error_on_the_occurrence_does_not_use_it_up(self, provider, kind):
        with fail_request("GET", r"/v2/droplets/\d+$", kind, occurrence=2) as fault:
            call("GET", DROPLET_42)
            provider.errors.append(invalid_key_identifiers(DROPLET_42))
            with pytest.raises(urllib.error.HTTPError) as raised:
                call("GET", DROPLET_42)
            assert not SYNTHETIC[kind](raised.value)
            assert fault.fired is False
            with pytest.raises(urllib.error.URLError) as synthetic:
                call("GET", DROPLET_42)
            call("GET", DROPLET_42)
        assert SYNTHETIC[kind](synthetic.value)
        assert fault.fired is True

    def test_a_real_call_that_succeeds_leaves_provider_errors_empty(self, provider, kind):
        with fail_request("POST", r"/v2/droplets$", kind) as fault:
            with pytest.raises(urllib.error.URLError):
                call("POST", DROPLETS)
        assert fault.fired is True
        assert fault.provider_errors == []

    def test_a_body_that_cannot_be_read_after_the_provider_answered_does_not_unfire(
        self, monkeypatch, kind
    ):
        broken = FakeResponse(b"{}")

        def unreadable(*args):
            raise OSError("body lost")

        broken.read = unreadable
        monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: broken)
        with fail_request("POST", r"/v2/droplets$", kind) as fault:
            with pytest.raises(OSError, match="body lost"):
                call("POST", DROPLETS)
        assert fault.fired is True
        assert fault.provider_errors == []
        assert broken.closed is True

    def test_a_provider_that_does_not_act_records_no_provider_error(self, provider, kind):
        with fail_request("POST", r"/v2/droplets$", kind, provider_acts=False) as fault:
            with pytest.raises(urllib.error.URLError):
                call("POST", DROPLETS)
        assert fault.provider_errors == []


class TestProviderErrorDiagnostics:
    def test_the_caller_can_still_read_the_whole_body_of_the_real_error(self, provider):
        provider.errors.append(invalid_key_identifiers(DROPLETS))
        with fail_request("POST", r"/v2/droplets$", "http_503"):
            with pytest.raises(urllib.error.HTTPError) as raised:
                call("POST", DROPLETS)
        assert json.loads(raised.value.read()) == {
            "id": "unprocessable_entity",
            "message": "are invalid key identifiers",
        }

    def test_the_description_carries_the_status_url_and_body_message(self, provider):
        provider.errors.append(invalid_key_identifiers(DROPLETS))
        with fail_request("POST", r"/v2/droplets$", "http_503") as fault:
            with pytest.raises(urllib.error.HTTPError):
                call("POST", DROPLETS)
        text = describe_provider_errors(fault)
        assert "HTTPError" in text
        assert "422" in text
        assert DROPLETS in text
        assert "are invalid key identifiers" in text

    def test_the_description_does_not_consume_the_body_either(self, provider):
        provider.errors.append(invalid_key_identifiers(DROPLETS))
        with fail_request("POST", r"/v2/droplets$", "http_503") as fault:
            with pytest.raises(urllib.error.HTTPError) as raised:
                call("POST", DROPLETS)
        describe_provider_errors(fault)
        describe_provider_errors(fault)
        assert b"are invalid key identifiers" in raised.value.read()

    def test_a_long_body_is_cut_to_a_bounded_length(self, provider):
        body = b'{"message": "' + b"x" * 5000 + b'"}'
        provider.errors.append(
            urllib.error.HTTPError(DROPLETS, 422, "U", email.message.Message(), io.BytesIO(body))
        )
        with fail_request("POST", r"/v2/droplets$", "http_503") as fault:
            with pytest.raises(urllib.error.HTTPError):
                call("POST", DROPLETS)
        assert len(describe_provider_errors(fault)) < 1000

    def test_an_error_that_is_not_an_http_error_is_named_and_its_text_kept(self, provider):
        provider.errors.append(urllib.error.URLError("no route"))
        with fail_request("POST", r"/v2/droplets$", "http_503") as fault:
            with pytest.raises(urllib.error.URLError):
                call("POST", DROPLETS)
        text = describe_provider_errors(fault)
        assert "URLError" in text
        assert "no route" in text

    def test_an_http_error_without_a_body_is_still_described(self, provider):
        provider.errors.append(urllib.error.HTTPError(DROPLETS, 502, "Bad Gateway", None, None))
        with fail_request("POST", r"/v2/droplets$", "http_503") as fault:
            with pytest.raises(urllib.error.HTTPError):
                call("POST", DROPLETS)
        text = describe_provider_errors(fault)
        assert "502" in text
        assert DROPLETS in text

    def test_no_errors_is_said_plainly(self):
        assert describe_provider_errors(Fault()) == "none"


class TestFailRequestRateLimited:
    def test_the_caller_sees_a_429(self, provider):
        with fail_request("POST", r"/v2/droplets$", "http_429", provider_acts=False) as fault:
            with pytest.raises(urllib.error.HTTPError) as raised:
                call("POST", DROPLETS)
        assert raised.value.code == 429
        assert fault.fired is True

    def test_the_provider_never_sees_the_call(self, provider):
        with fail_request("POST", r"/v2/droplets$", "http_429", provider_acts=False) as fault:
            with pytest.raises(urllib.error.HTTPError):
                call("POST", DROPLETS)
        assert provider.reached == []
        assert fault.response is None

    def test_a_provider_that_acts_is_a_contradiction_and_is_rejected_at_the_call(self):
        with pytest.raises(ValueError, match="http_429"):
            fail_request("POST", r"/x$", "http_429")


class TestFailRequestBodyPattern:
    def test_only_a_request_whose_body_matches_counts(self, provider, monkeypatch):
        url = DROPLETS + "/42/actions"
        with fail_request("POST", r"/actions$", "http_503", body_pattern=r'"type":\s*"resize"'):
            request = urllib.request.Request(url, data=b'{"type": "power_off"}', method="POST")
            urllib.request.urlopen(request, timeout=5).close()
            request = urllib.request.Request(url, data=b'{"type": "resize"}', method="POST")
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(request, timeout=5)
        assert len(provider.reached) == 2

    def test_a_request_without_a_body_never_matches_a_body_pattern(self, provider):
        with fail_request("GET", r"/v2/droplets$", "http_503", body_pattern="x") as fault:
            call("GET", DROPLETS)
        assert fault.fired is False

    def test_a_timeout_is_matched_on_the_body_too(self, provider):
        url = DROPLETS + "/42/actions"
        with fail_request(
            "POST",
            r"/actions$",
            "timeout",
            body_pattern="resize",
            delay=SLOW,
            deadline=DEADLINE,
        ) as fault:
            request = urllib.request.Request(url, data=b'{"type": "power_on"}', method="POST")
            urllib.request.urlopen(request, timeout=5).close()
            assert fault.fired is False
            request = urllib.request.Request(url, data=b'{"type": "resize"}', method="POST")
            with pytest.raises(TimeoutError):
                urllib.request.urlopen(request, timeout=5)
            fault.join()
        assert fault.fired is True


DROPLET_42 = "https://api.digitalocean.com/v2/droplets/42"


def reports_new(body):
    body["droplet"]["status"] = "new"
    return body


class TestRewriteResponses:
    @pytest.fixture(autouse=True)
    def an_active_droplet(self, provider):
        provider.bodies[("GET", DROPLET_42)] = b'{"droplet": {"id": 42, "status": "active"}}'

    def test_every_match_hands_the_caller_the_rewritten_body(self, provider):
        with rewrite_responses("GET", r"/v2/droplets/\d+$", reports_new):
            first = call("GET", DROPLET_42)
            second = call("GET", DROPLET_42)

        assert first == second == {"droplet": {"id": 42, "status": "new"}}

    def test_the_provider_is_reached_by_every_matching_call(self, provider):
        with rewrite_responses("GET", r"/v2/droplets/\d+$", reports_new) as fault:
            call("GET", DROPLET_42)
            call("GET", DROPLET_42)

        assert provider.reached == [("GET", DROPLET_42)] * 2
        assert fault.fired is True

    def test_the_real_body_is_kept_so_the_caller_can_learn_the_resource_id(self, provider):
        with rewrite_responses("GET", r"/v2/droplets/\d+$", reports_new) as fault:
            call("GET", DROPLET_42)

        assert fault.response == {"droplet": {"id": 42, "status": "active"}}

    def test_a_non_match_is_returned_untouched(self, provider):
        provider.bodies[("POST", DROPLETS)] = b'{"droplet": {"id": 7, "status": "new"}}'
        with rewrite_responses("GET", r"/v2/droplets/\d+$", reports_new) as fault:
            assert call("POST", DROPLETS) == {"droplet": {"id": 7, "status": "new"}}

        assert fault.fired is False

    def test_a_body_that_is_not_json_is_returned_untouched(self, provider):
        provider.bodies[("GET", DROPLET_42)] = b"not json"
        with rewrite_responses("GET", r"/v2/droplets/\d+$", reports_new) as fault:
            with pytest.raises(ValueError):
                call("GET", DROPLET_42)

        assert fault.fired is False

    def test_urlopen_is_restored_afterwards(self, provider):
        installed = urllib.request.urlopen
        with rewrite_responses("GET", r"/v2/droplets/\d+$", reports_new):
            assert urllib.request.urlopen is not installed
        assert urllib.request.urlopen is installed


def called_from(module_name: str, function: str, seconds: float) -> None:
    """Run `time.sleep(seconds)` inside a function called `function`, in code
    whose module is `module_name`, the way load_driver() names a driver it
    exec's from disk."""
    namespace = {"__name__": module_name, "time": time}
    exec(f"def {function}(seconds):\n    time.sleep(seconds)", namespace)
    namespace[function](seconds)


DRIVER = "aiform_driver_digitalocean_compute"


class TestSkipDriverSleeps:
    def test_a_named_function_of_a_driver_loaded_from_disk_does_not_wait(self):
        with skip_driver_sleeps({"_poll_until"}):
            started = time.monotonic()
            called_from(DRIVER, "_poll_until", 2.0)
            assert time.monotonic() - started < 1.0

    def test_another_function_of_the_same_driver_still_waits(self):
        with skip_driver_sleeps({"_poll_until"}):
            started = time.monotonic()
            called_from(DRIVER, "_create_droplet", 0.2)
            assert time.monotonic() - started >= 0.2

    def test_the_named_function_of_any_other_module_still_waits(self):
        with skip_driver_sleeps({"_poll_until"}):
            started = time.monotonic()
            called_from("some_other_module", "_poll_until", 0.2)
            assert time.monotonic() - started >= 0.2

    def test_the_real_sleep_is_restored_afterwards(self):
        installed = time.sleep
        with skip_driver_sleeps({"_poll_until"}):
            assert time.sleep is not installed
        assert time.sleep is installed

    def test_the_real_sleep_is_restored_when_the_body_raises(self):
        installed = time.sleep
        with pytest.raises(RuntimeError):
            with skip_driver_sleeps({"_poll_until"}):
                raise RuntimeError("boom")
        assert time.sleep is installed

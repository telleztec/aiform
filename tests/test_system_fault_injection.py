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

import io
import json
import types
import urllib.error
import urllib.request

import pytest

from aiform import state
from tests.system.fault_injection import (
    InjectedInterrupt,
    interrupt_after_request,
    interrupt_after_state_save,
    interrupt_before_request,
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

    def fake_urlopen(request, *args, **kwargs):
        if isinstance(request, str):
            method, url = "GET", request
        else:
            method, url = request.get_method(), request.full_url
        reached.append((method, url))
        body = bodies.get((method, url), b"{}")
        response = FakeResponse(body)
        responses.append(response)
        return response

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return types.SimpleNamespace(reached=reached, bodies=bodies, responses=responses)


def call(method: str, url: str):
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())


DROPLETS = "https://api.digitalocean.com/v2/droplets"


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

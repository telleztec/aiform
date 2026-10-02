# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Offline tests for `tests/system/provider_profile.py` -- see
specs/system_test_interrupt.md, "Interface".

The profile is the only place a stage learns what a provider's create and poll
calls look like and where the resource id sits in a response. A pattern that
matches too much cuts the wrong call off; one that matches too little never
fires, and the live stage passes without having tested anything.
"""

import io
import json
import re
import urllib.request

import pytest

from tests.system.fault_injection import fail_request
from tests.system.provider_profile import DIGITALOCEAN, ProviderProfile

CREATE = "https://api.digitalocean.com/v2/droplets"
POLL = "https://api.digitalocean.com/v2/droplets/4242"


def matches(method: str, url_pattern: str, request_method: str, url: str) -> bool:
    return method == request_method and re.search(url_pattern, url) is not None


class TestDigitalOceanRequests:
    def test_the_create_request_is_the_droplet_post(self):
        assert matches(*DIGITALOCEAN.create, "POST", CREATE)

    @pytest.mark.parametrize(
        "method, url",
        [
            ("GET", CREATE),
            ("POST", POLL),
            ("POST", f"{POLL}/actions"),
            ("POST", "https://api.digitalocean.com/v2/firewalls"),
        ],
    )
    def test_the_create_request_does_not_match_anything_else(self, method, url):
        assert not matches(*DIGITALOCEAN.create, method, url)

    def test_the_poll_request_is_the_get_of_one_droplet(self):
        assert matches(*DIGITALOCEAN.poll, "GET", POLL)

    @pytest.mark.parametrize(
        "method, url",
        [
            ("GET", CREATE),
            ("GET", f"{POLL}/actions"),
            ("GET", "https://api.digitalocean.com/v2/firewalls/abc"),
            ("DELETE", POLL),
        ],
    )
    def test_the_poll_request_does_not_match_anything_else(self, method, url):
        assert not matches(*DIGITALOCEAN.poll, method, url)


class TestDigitalOceanResourceId:
    def test_it_is_read_from_a_create_or_poll_response_as_a_string(self):
        assert DIGITALOCEAN.resource_id({"droplet": {"id": 4242, "name": "web"}}) == "4242"

    @pytest.mark.parametrize("body", [None, {}, {"droplet": {}}, {"droplet": None}, {"x": 1}])
    def test_a_response_without_one_yields_none(self, body):
        assert DIGITALOCEAN.resource_id(body) is None


class TestDigitalOceanNotReady:
    def test_a_poll_body_reports_the_resource_new_and_keeps_the_rest(self):
        body = {"droplet": {"id": 4, "status": "active", "name": "web"}, "links": {}}
        assert DIGITALOCEAN.not_ready(body) == {
            "droplet": {"id": 4, "status": "new", "name": "web"},
            "links": {},
        }

    def test_the_body_it_is_given_is_not_mutated(self):
        body = {"droplet": {"id": 4, "status": "active"}}
        DIGITALOCEAN.not_ready(body)
        assert body["droplet"]["status"] == "active"

    @pytest.mark.parametrize("body", [{}, {"x": 1}, {"droplet": None}])
    def test_a_body_with_no_resource_is_returned_as_it_is(self, body):
        assert DIGITALOCEAN.not_ready(body) == body


def test_the_poll_loop_is_the_function_whose_sleeps_a_stage_may_skip():
    assert DIGITALOCEAN.poll_loop == "_poll_until"


class TestWithTheInjector:
    def test_the_create_request_is_what_fail_request_fires_on(self, monkeypatch):
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            lambda request, *a, **k: Response(json.dumps({"droplet": {"id": 7}}).encode()),
        )
        method, pattern = DIGITALOCEAN.create
        with fail_request(method, pattern, "timeout", delay=0.2, deadline=0.02) as fault:
            with pytest.raises(TimeoutError):
                urllib.request.urlopen(urllib.request.Request(CREATE, method="POST"))
            fault.join()
        assert DIGITALOCEAN.resource_id(fault.response) == "7"


def test_a_profile_is_a_plain_value():
    profile = ProviderProfile(
        name="x",
        create=("POST", r"/things$"),
        poll=("GET", r"/things/\d+$"),
        resource_id=lambda body: None,
        list_owned=lambda token: [],
        not_ready=lambda body: body,
        poll_loop="poll",
    )
    assert profile.name == "x"

# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Tests for drivers/digitalocean/domain.py -- see
specs/digitalocean_domain.md. Mirrors test_digitalocean_compute.py's
FakeUrlopen harness exactly.

Two production-shape facts drive several tests below (see
aiform/orchestrator.py's apply_plan()/refresh_resource()):

- `current` passed to update() is always a full attributes dict shaped
  like create()/read()'s return: {"id", "ttl", "records"}.
- `desired` passed to update() is `resource_spec.params` -- the raw
  `params:` block from .aiform.md -- which for this driver ordinarily
  has only a "records" key. Nothing upstream validates params against
  PARAM_SCHEMA (driver.py's docstring claims the orchestrator does; it
  does not), so a user CAN write an extra key such as a stray `ttl:`
  and have it arrive here. The driver rejects it with ValueError --
  never DriverUpdateNotSupported, which the orchestrator would answer
  by destroying and recreating the whole zone.
"""

import io
import json
import time
import urllib.error
import urllib.request
from email.message import Message

import pytest

from aiform.compare import unordered_equal
from aiform.driver import DriverUpdateNotSupported
from aiform.exceptions import ResourceNotFoundError
from aiform.planner import diff_attributes
from drivers.digitalocean.domain import Driver

BASE_URL = "https://api.digitalocean.com/v2"
CREDENTIALS = {"DIGITALOCEAN_TOKEN": "dop_v1_test"}
DOMAIN = "example.com"


def domains_url() -> str:
    return f"{BASE_URL}/domains"


def domain_url(name: str) -> str:
    return f"{BASE_URL}/domains/{name}"


def records_url(name: str) -> str:
    return f"{BASE_URL}/domains/{name}/records"


def record_url(name: str, record_id) -> str:
    return f"{BASE_URL}/domains/{name}/records/{record_id}"


def records_first_page_url(name: str, per_page: int = 200) -> str:
    # drivers/digitalocean/_common.py's fetch_all_pages() appends
    # per_page to a url with no existing query string as a bare
    # "?per_page=<n>" -- see specs/digitalocean_pagination.md's
    # "per_page injection" behavior. Both read() and update()'s live
    # listing are assumed to route the records GET through it, per
    # specs/digitalocean_domain.md's "Record listing goes through
    # drivers/digitalocean/_common.py's fetch_all_pages()".
    return f"{records_url(name)}?per_page={per_page}"


def do_domain(name=DOMAIN, ttl=1800, zone_file="junk zone file text") -> dict:
    return {"domain": {"name": name, "ttl": ttl, "zone_file": zone_file}}


def do_record(
    id=1,
    type="A",
    name="@",
    data="203.0.113.10",
    ttl=1800,
    priority=None,
    port=None,
    weight=None,
    flags=None,
    tag=None,
) -> dict:
    return {
        "id": id,
        "type": type,
        "name": name,
        "data": data,
        "priority": priority,
        "port": port,
        "weight": weight,
        "flags": flags,
        "tag": tag,
        "ttl": ttl,
    }


class FakeHTTPResponse:
    def __init__(self, status, body):
        self.status = status
        if body is None:
            self._body = b""
        elif isinstance(body, bytes):
            self._body = body
        else:
            self._body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body

    def getcode(self):
        return self.status


def http_error(url: str, code: int, body: dict | None = None) -> urllib.error.HTTPError:
    payload = json.dumps(body).encode() if body is not None else b""
    return urllib.error.HTTPError(url, code, "error", Message(), io.BytesIO(payload))


class FakeUrlopen:
    """Routes by (method, url). A registered script list is consumed one
    item per call until only one item remains, which then repeats
    forever -- see test_digitalocean_compute.py, mirrored verbatim.
    """

    def __init__(self):
        self._scripts: dict[tuple[str, str], list] = {}
        self.calls: list[dict] = []

    def script(self, method: str, url: str, *responses) -> None:
        self._scripts[(method, url)] = list(responses)

    def __call__(self, request: urllib.request.Request, *args, **kwargs):
        method = request.get_method()
        url = request.full_url
        raw_body = request.data
        body = json.loads(raw_body) if raw_body else None
        self.calls.append(
            {
                "method": method,
                "url": url,
                "body": body,
                "authorization": request.get_header("Authorization"),
                "content_type": request.get_header("Content-type"),
            }
        )

        queue = self._scripts.get((method, url))
        if not queue:
            raise AssertionError(f"unscripted request: {method} {url}")
        result = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def fake_urlopen(monkeypatch) -> FakeUrlopen:
    fake = FakeUrlopen()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda seconds: None)


@pytest.fixture
def driver() -> Driver:
    return Driver()


def script_create_zone(fake_urlopen, name=DOMAIN, ttl=1800):
    fake_urlopen.script(
        "POST", domains_url(), FakeHTTPResponse(201, {"domain": {"name": name, "ttl": ttl}})
    )


class TestClassAttributes:
    def test_param_schema_shape(self, driver):
        schema = driver.PARAM_SCHEMA
        assert schema["type"] == "object"
        assert schema["required"] == ["records"]
        records_schema = schema["properties"]["records"]
        assert records_schema["type"] == "array"
        item_schema = records_schema["items"]
        assert item_schema["type"] == "object"
        assert item_schema["required"] == ["type", "name", "data", "ttl"]
        assert item_schema["additionalProperties"] is False
        assert item_schema["properties"]["type"]["enum"] == [
            "A",
            "AAAA",
            "CAA",
            "CNAME",
            "MX",
            "NS",
            "SRV",
            "TXT",
        ]

    def test_no_tags_key_in_param_schema(self, driver):
        # DigitalOcean domains have no tagging API at all -- see
        # specs/digitalocean_domain.md's "Resource tagging convention"
        # cross-reference.
        assert "tags" not in driver.PARAM_SCHEMA["properties"]

    def test_likely_replace_fields_is_empty(self, driver):
        assert driver.LIKELY_REPLACE_FIELDS == []

    def test_non_diffable_fields_is_empty(self, driver):
        assert driver.NON_DIFFABLE_FIELDS == []

    def test_unordered_fields_is_records(self, driver):
        assert driver.UNORDERED_FIELDS == ["records"]


class TestLogging:
    def test_logger_is_a_real_descendant_of_the_aiform_logger(self):
        from drivers.digitalocean.domain import logger as driver_logger

        assert driver_logger.name == "aiform.driver.digitalocean.domain"
        node = driver_logger
        while node.parent is not None:
            node = node.parent
            if node.name == "aiform":
                return
        pytest.fail("driver logger is not a descendant of the 'aiform' logger")


class TestCreate:
    def test_posts_domain_with_only_name_no_ip_address(self, driver, fake_urlopen):
        script_create_zone(fake_urlopen)
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))
        fake_urlopen.script(
            "GET", records_first_page_url(DOMAIN), FakeHTTPResponse(200, {"domain_records": []})
        )

        driver.create(DOMAIN, {"records": []}, CREDENTIALS)

        create_call = fake_urlopen.calls[0]
        assert create_call["method"] == "POST"
        assert create_call["url"] == domains_url()
        assert create_call["body"] == {"name": DOMAIN}
        assert "ip_address" not in create_call["body"]
        assert create_call["authorization"] == "Bearer dop_v1_test"

    def test_posts_one_record_per_record_in_the_users_given_order(self, driver, fake_urlopen):
        records = [
            {"type": "A", "name": "@", "data": "203.0.113.10", "ttl": 1800},
            {"type": "A", "name": "www", "data": "203.0.113.10", "ttl": 1800},
        ]
        script_create_zone(fake_urlopen)
        fake_urlopen.script(
            "POST",
            records_url(DOMAIN),
            FakeHTTPResponse(201, {"domain_record": do_record(id=1, name="@")}),
            FakeHTTPResponse(201, {"domain_record": do_record(id=2, name="www")}),
        )
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))
        fake_urlopen.script(
            "GET",
            records_first_page_url(DOMAIN),
            FakeHTTPResponse(
                200,
                {
                    "domain_records": [
                        do_record(id=1, name="@"),
                        do_record(id=2, name="www"),
                    ]
                },
            ),
        )

        driver.create(DOMAIN, {"records": records}, CREDENTIALS)

        record_posts = [
            c
            for c in fake_urlopen.calls
            if c["method"] == "POST" and c["url"] == records_url(DOMAIN)
        ]
        assert len(record_posts) == 2
        assert record_posts[0]["body"]["name"] == "@"
        assert record_posts[1]["body"]["name"] == "www"

    def test_returns_id_as_the_domain_name_with_attrs_from_a_fresh_read(self, driver, fake_urlopen):
        records = [{"type": "A", "name": "@", "data": "203.0.113.10", "ttl": 1800}]
        script_create_zone(fake_urlopen, ttl=1800)
        # The POST record response deliberately differs from the read()
        # that follows -- proves create() discards it and rebuilds
        # attributes from a fresh read(), not from the POST bodies.
        fake_urlopen.script(
            "POST",
            records_url(DOMAIN),
            FakeHTTPResponse(
                201, {"domain_record": do_record(id=1, name="@", data="0.0.0.0", ttl=60)}
            ),
        )
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain(ttl=1800)))
        fake_urlopen.script(
            "GET",
            records_first_page_url(DOMAIN),
            FakeHTTPResponse(
                200, {"domain_records": [do_record(id=1, name="@", data="203.0.113.10", ttl=1800)]}
            ),
        )

        result = driver.create(DOMAIN, {"records": records}, CREDENTIALS)

        assert result["id"] == DOMAIN
        assert result["ttl"] == 1800
        assert result["records"] == [
            {"type": "A", "name": "@", "data": "203.0.113.10", "ttl": 1800}
        ]

    def test_empty_records_list_is_valid(self, driver, fake_urlopen):
        script_create_zone(fake_urlopen)
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))
        fake_urlopen.script(
            "GET", records_first_page_url(DOMAIN), FakeHTTPResponse(200, {"domain_records": []})
        )

        result = driver.create(DOMAIN, {"records": []}, CREDENTIALS)

        assert result["records"] == []


class TestCreateRollback:
    def test_record_post_failure_deletes_the_zone_and_reraises_the_original_error(
        self, driver, fake_urlopen
    ):
        records = [{"type": "A", "name": "@", "data": "203.0.113.10", "ttl": 1800}]
        script_create_zone(fake_urlopen)
        fake_urlopen.script(
            "POST",
            records_url(DOMAIN),
            http_error(records_url(DOMAIN), 422, {"message": "invalid record"}),
        )
        fake_urlopen.script("DELETE", domain_url(DOMAIN), FakeHTTPResponse(204, None))

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            driver.create(DOMAIN, {"records": records}, CREDENTIALS)

        assert excinfo.value.code == 422
        assert any(
            c["method"] == "DELETE" and c["url"] == domain_url(DOMAIN) for c in fake_urlopen.calls
        )

    def test_second_record_failing_still_rolls_back_the_whole_zone(self, driver, fake_urlopen):
        records = [
            {"type": "A", "name": "@", "data": "203.0.113.10", "ttl": 1800},
            {"type": "A", "name": "www", "data": "203.0.113.10", "ttl": 1800},
        ]
        script_create_zone(fake_urlopen)
        fake_urlopen.script(
            "POST",
            records_url(DOMAIN),
            FakeHTTPResponse(201, {"domain_record": do_record(id=1, name="@")}),
            http_error(records_url(DOMAIN), 422, {"message": "invalid record"}),
        )
        fake_urlopen.script("DELETE", domain_url(DOMAIN), FakeHTTPResponse(204, None))

        with pytest.raises(urllib.error.HTTPError):
            driver.create(DOMAIN, {"records": records}, CREDENTIALS)

        assert any(
            c["method"] == "DELETE" and c["url"] == domain_url(DOMAIN) for c in fake_urlopen.calls
        )

    def test_zone_already_exists_422_propagates_without_any_rollback(self, driver, fake_urlopen):
        fake_urlopen.script(
            "POST",
            domains_url(),
            http_error(domains_url(), 422, {"message": "domain already exists"}),
        )

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            driver.create(DOMAIN, {"records": []}, CREDENTIALS)

        assert excinfo.value.code == 422
        assert not any(c["method"] == "DELETE" for c in fake_urlopen.calls)

    def test_rollback_delete_failure_raises_runtime_error_naming_both_errors(
        self, driver, fake_urlopen
    ):
        records = [{"type": "A", "name": "@", "data": "203.0.113.10", "ttl": 1800}]
        script_create_zone(fake_urlopen)
        fake_urlopen.script(
            "POST",
            records_url(DOMAIN),
            http_error(records_url(DOMAIN), 422, {"message": "invalid record"}),
        )
        fake_urlopen.script(
            "DELETE",
            domain_url(DOMAIN),
            http_error(domain_url(DOMAIN), 500, {"message": "internal error"}),
        )

        with pytest.raises(RuntimeError) as excinfo:
            driver.create(DOMAIN, {"records": records}, CREDENTIALS)

        message = str(excinfo.value)
        assert "invalid record" in message
        assert "internal error" in message


class TestRead:
    def test_404_on_domain_get_raises_resource_not_found_error(self, driver, fake_urlopen):
        fake_urlopen.script("GET", domain_url(DOMAIN), http_error(domain_url(DOMAIN), 404))

        with pytest.raises(ResourceNotFoundError) as excinfo:
            driver.read(DOMAIN, CREDENTIALS)

        assert DOMAIN in str(excinfo.value)

    def test_other_non_2xx_on_domain_get_propagates(self, driver, fake_urlopen):
        fake_urlopen.script("GET", domain_url(DOMAIN), http_error(domain_url(DOMAIN), 500))

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            driver.read(DOMAIN, CREDENTIALS)

        assert excinfo.value.code == 500

    def test_records_listing_is_paginated_through(self, driver, fake_urlopen):
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))
        page1 = [
            do_record(id=i, type="A", name=f"host{i}", data=f"203.0.113.{i}") for i in range(1, 21)
        ]
        page2 = [
            do_record(id=i, type="A", name=f"host{i}", data=f"203.0.113.{i}") for i in range(21, 25)
        ]
        next_url = f"{records_url(DOMAIN)}?page=2&per_page=200"
        fake_urlopen.script(
            "GET",
            records_first_page_url(DOMAIN),
            FakeHTTPResponse(
                200, {"domain_records": page1, "links": {"pages": {"next": next_url}}}
            ),
        )
        fake_urlopen.script("GET", next_url, FakeHTTPResponse(200, {"domain_records": page2}))

        result = driver.read(DOMAIN, CREDENTIALS)

        assert len(result["records"]) == 24

    def test_soa_record_is_dropped(self, driver, fake_urlopen):
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))
        fake_urlopen.script(
            "GET",
            records_first_page_url(DOMAIN),
            FakeHTTPResponse(
                200,
                {
                    "domain_records": [
                        do_record(id=1, type="SOA", name="@", data=""),
                        do_record(id=2, type="A", name="@", data="203.0.113.10"),
                    ]
                },
            ),
        )

        result = driver.read(DOMAIN, CREDENTIALS)

        assert all(r["type"] != "SOA" for r in result["records"])
        assert len(result["records"]) == 1

    def test_apex_ns_records_pointing_at_digitalocean_nameservers_are_dropped(
        self, driver, fake_urlopen
    ):
        # Verified live: DigitalOcean stores and returns these WITHOUT a
        # trailing dot ("ns1.digitalocean.com"). This exact dotless shape
        # is the B1 regression case -- a dot-anchored filter never
        # matched it and let the zone's own nameservers leak into read()
        # as ordinary, deletable records.
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))
        fake_urlopen.script(
            "GET",
            records_first_page_url(DOMAIN),
            FakeHTTPResponse(
                200,
                {
                    "domain_records": [
                        do_record(id=1, type="NS", name="@", data="ns1.digitalocean.com"),
                        do_record(id=2, type="NS", name="@", data="ns2.digitalocean.com"),
                        do_record(id=3, type="NS", name="@", data="ns3.digitalocean.com"),
                    ]
                },
            ),
        )

        result = driver.read(DOMAIN, CREDENTIALS)

        assert result["records"] == []

    def test_delegated_subdomain_ns_record_is_kept(self, driver, fake_urlopen):
        # This fixture deliberately uses the DOTTED form, which the live
        # probe showed DigitalOcean does not actually return. It is kept
        # non-realistic on purpose: the filter is dot-insensitive, so this
        # pins that dot-insensitivity does not make it over-match and
        # swallow a delegated record the user genuinely manages. The
        # realistic dotless shape is covered by the apex tests above.
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))
        fake_urlopen.script(
            "GET",
            records_first_page_url(DOMAIN),
            FakeHTTPResponse(
                200,
                {
                    "domain_records": [
                        do_record(id=1, type="NS", name="dev", data="ns1.digitalocean.com.")
                    ]
                },
            ),
        )

        result = driver.read(DOMAIN, CREDENTIALS)

        assert result["records"] == [
            {"type": "NS", "name": "dev", "data": "ns1.digitalocean.com.", "ttl": 1800}
        ]

    def test_apex_ns_record_pointing_elsewhere_is_kept(self, driver, fake_urlopen):
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))
        fake_urlopen.script(
            "GET",
            records_first_page_url(DOMAIN),
            FakeHTTPResponse(
                200,
                {
                    "domain_records": [
                        do_record(id=1, type="NS", name="@", data="ns1.otherhost.net.")
                    ]
                },
            ),
        )

        result = driver.read(DOMAIN, CREDENTIALS)

        assert result["records"] == [
            {"type": "NS", "name": "@", "data": "ns1.otherhost.net.", "ttl": 1800}
        ]

    def test_a_record_projection_strips_id_and_null_fields(self, driver, fake_urlopen):
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))
        fake_urlopen.script(
            "GET",
            records_first_page_url(DOMAIN),
            FakeHTTPResponse(
                200, {"domain_records": [do_record(id=42, type="A", name="@", data="203.0.113.10")]}
            ),
        )

        result = driver.read(DOMAIN, CREDENTIALS)

        assert result["records"] == [
            {"type": "A", "name": "@", "data": "203.0.113.10", "ttl": 1800}
        ]
        assert "id" not in result["records"][0]
        assert "priority" not in result["records"][0]

    def test_mx_record_projection_keeps_priority_only(self, driver, fake_urlopen):
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))
        fake_urlopen.script(
            "GET",
            records_first_page_url(DOMAIN),
            FakeHTTPResponse(
                200,
                {
                    "domain_records": [
                        do_record(id=1, type="MX", name="@", data="mail.example.com.", priority=10)
                    ]
                },
            ),
        )

        result = driver.read(DOMAIN, CREDENTIALS)

        assert result["records"] == [
            {"type": "MX", "name": "@", "data": "mail.example.com.", "ttl": 1800, "priority": 10}
        ]

    def test_srv_record_projection_keeps_priority_port_weight(self, driver, fake_urlopen):
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))
        fake_urlopen.script(
            "GET",
            records_first_page_url(DOMAIN),
            FakeHTTPResponse(
                200,
                {
                    "domain_records": [
                        do_record(
                            id=1,
                            type="SRV",
                            name="_sip._tcp",
                            data="sipserver.example.com.",
                            priority=10,
                            port=5060,
                            weight=5,
                        )
                    ]
                },
            ),
        )

        result = driver.read(DOMAIN, CREDENTIALS)

        assert result["records"] == [
            {
                "type": "SRV",
                "name": "_sip._tcp",
                "data": "sipserver.example.com.",
                "ttl": 1800,
                "priority": 10,
                "port": 5060,
                "weight": 5,
            }
        ]

    def test_caa_record_projection_keeps_flags_and_tag(self, driver, fake_urlopen):
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))
        fake_urlopen.script(
            "GET",
            records_first_page_url(DOMAIN),
            FakeHTTPResponse(
                200,
                {
                    "domain_records": [
                        do_record(
                            id=1,
                            type="CAA",
                            name="@",
                            data="letsencrypt.org",
                            flags=0,
                            tag="issue",
                        )
                    ]
                },
            ),
        )

        result = driver.read(DOMAIN, CREDENTIALS)

        assert result["records"] == [
            {
                "type": "CAA",
                "name": "@",
                "data": "letsencrypt.org",
                "ttl": 1800,
                "flags": 0,
                "tag": "issue",
            }
        ]

    def test_records_are_sorted_by_the_documented_tuple(self, driver, fake_urlopen):
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))
        fake_urlopen.script(
            "GET",
            records_first_page_url(DOMAIN),
            FakeHTTPResponse(
                200,
                {
                    "domain_records": [
                        do_record(id=1, type="A", name="www", data="203.0.113.10"),
                        do_record(id=2, type="A", name="@", data="203.0.113.10"),
                        do_record(id=3, type="MX", name="@", data="mail.example.com.", priority=10),
                    ]
                },
            ),
        )

        result = driver.read(DOMAIN, CREDENTIALS)

        assert [(r["type"], r["name"]) for r in result["records"]] == [
            ("A", "@"),
            ("A", "www"),
            ("MX", "@"),
        ]

    def test_zone_file_excluded_but_ttl_included(self, driver, fake_urlopen):
        fake_urlopen.script(
            "GET",
            domain_url(DOMAIN),
            FakeHTTPResponse(200, do_domain(ttl=3600, zone_file="a full zone file")),
        )
        fake_urlopen.script(
            "GET", records_first_page_url(DOMAIN), FakeHTTPResponse(200, {"domain_records": []})
        )

        result = driver.read(DOMAIN, CREDENTIALS)

        assert "zone_file" not in result
        assert result["ttl"] == 3600


class TestZeroDiffInvariant:
    def test_read_result_matches_the_users_records_as_a_multiset(self, driver, fake_urlopen):
        # MX/NS/CNAME data is written and returned WITHOUT a trailing dot
        # -- verified live, DigitalOcean stores and returns these
        # dotless, and this driver's validation now requires the same
        # dotless form from the user (a trailing dot is rejected, and
        # aiform appends the one DO's API demands only on the wire). Both
        # sides here use that one canonical form, which is what makes
        # the zero-diff invariant actually checkable.
        user_records = [
            {"type": "A", "name": "@", "data": "203.0.113.10", "ttl": 1800},
            {"type": "A", "name": "www", "data": "203.0.113.10", "ttl": 1800},
            {"type": "CNAME", "name": "blog", "data": "example.com", "ttl": 1800},
            {"type": "MX", "name": "@", "data": "mail.example.com", "ttl": 1800, "priority": 10},
            {"type": "TXT", "name": "@", "data": '"v=spf1 -all"', "ttl": 1800},
        ]
        # Different order, DO-assigned ids, explicit nulls, and the
        # auto-created SOA/NS records all present -- exactly the live
        # shape read() must reduce back to `user_records` as a multiset.
        do_records = [
            do_record(id=100, type="SOA", name="@", data="1800"),
            do_record(id=101, type="NS", name="@", data="ns1.digitalocean.com"),
            do_record(id=102, type="NS", name="@", data="ns2.digitalocean.com"),
            do_record(id=103, type="NS", name="@", data="ns3.digitalocean.com"),
            do_record(id=4, type="TXT", name="@", data='"v=spf1 -all"'),
            do_record(id=3, type="MX", name="@", data="mail.example.com", priority=10),
            do_record(id=5, type="CNAME", name="blog", data="example.com"),
            do_record(id=2, type="A", name="www", data="203.0.113.10"),
            do_record(id=1, type="A", name="@", data="203.0.113.10"),
        ]
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))
        fake_urlopen.script(
            "GET",
            records_first_page_url(DOMAIN),
            FakeHTTPResponse(200, {"domain_records": do_records}),
        )

        result = driver.read(DOMAIN, CREDENTIALS)

        assert unordered_equal(user_records, result["records"])
        assert (
            diff_attributes(
                {"records": result["records"]},
                {"records": user_records},
                unordered_fields=["records"],
            )
            == {}
        )


class TestUpdateRejectsNonRecordsDiffs:
    """An unknown top-level param is a ValueError, never a replace.

    The orchestrator answers DriverUpdateNotSupported by destroying and
    recreating the resource -- here, an entire live DNS zone and every
    record in it. An earlier draft of specs/digitalocean_domain.md said
    "any key other than records differing raises
    DriverUpdateNotSupported"; since apply_plan() passes the user's raw
    params block as `desired`, the only way such a diff arises is a user
    typing a key this driver doesn't support. That would have destroyed
    a zone over a stray `ttl:`. prompts/review_driver.md item 4 calls
    this class of over-broad refusal a blocking issue.
    """

    def test_an_unknown_top_level_param_raises_value_error_with_zero_calls(
        self, driver, fake_urlopen
    ):
        current = {"id": DOMAIN, "ttl": 1800, "records": []}
        desired = {"records": [], "ttl": 3600}

        with pytest.raises(ValueError) as excinfo:
            driver.update(DOMAIN, current, desired, CREDENTIALS)

        assert "ttl" in str(excinfo.value)
        assert fake_urlopen.calls == []

    def test_an_unknown_top_level_param_is_never_a_replace(self, driver, fake_urlopen):
        current = {"id": DOMAIN, "ttl": 1800, "records": []}
        desired = {"records": [], "ttl": 3600}

        with pytest.raises(Exception) as excinfo:
            driver.update(DOMAIN, current, desired, CREDENTIALS)

        assert not isinstance(excinfo.value, DriverUpdateNotSupported)

    def test_records_only_diff_never_raises_driver_update_not_supported(self, driver, fake_urlopen):
        current = {"id": DOMAIN, "ttl": 1800, "records": []}
        desired = {"records": [{"type": "A", "name": "@", "data": "203.0.113.10", "ttl": 1800}]}
        fake_urlopen.script(
            "GET", records_first_page_url(DOMAIN), FakeHTTPResponse(200, {"domain_records": []})
        )
        fake_urlopen.script(
            "POST",
            records_url(DOMAIN),
            FakeHTTPResponse(201, {"domain_record": do_record(id=1, name="@")}),
        )
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))

        driver.update(DOMAIN, current, desired, CREDENTIALS)


class TestUpdateValidatesBeforeMutating:
    def test_malformed_desired_records_raise_value_error_before_any_mutation(
        self, driver, fake_urlopen
    ):
        current = {"id": DOMAIN, "ttl": 1800, "records": []}
        desired = {
            "records": [
                {"type": "A", "name": "@", "data": "203.0.113.10", "ttl": 1800, "priority": 10}
            ]
        }

        with pytest.raises(ValueError):
            driver.update(DOMAIN, current, desired, CREDENTIALS)

        assert fake_urlopen.calls == []


class TestUpdateReconciliation:
    def test_single_valued_group_differing_in_data_is_put_not_delete_and_post(
        self, driver, fake_urlopen
    ):
        current = {
            "id": DOMAIN,
            "ttl": 1800,
            "records": [{"type": "A", "name": "www", "data": "203.0.113.10", "ttl": 1800}],
        }
        desired = {"records": [{"type": "A", "name": "www", "data": "203.0.113.20", "ttl": 1800}]}
        fake_urlopen.script(
            "GET",
            records_first_page_url(DOMAIN),
            FakeHTTPResponse(
                200,
                {"domain_records": [do_record(id=7, type="A", name="www", data="203.0.113.10")]},
            ),
            FakeHTTPResponse(
                200,
                {"domain_records": [do_record(id=7, type="A", name="www", data="203.0.113.20")]},
            ),
        )
        fake_urlopen.script(
            "PUT",
            record_url(DOMAIN, 7),
            FakeHTTPResponse(
                200, {"domain_record": do_record(id=7, type="A", name="www", data="203.0.113.20")}
            ),
        )
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))

        result = driver.update(DOMAIN, current, desired, CREDENTIALS)

        put_calls = [c for c in fake_urlopen.calls if c["method"] == "PUT"]
        assert len(put_calls) == 1
        assert put_calls[0]["url"] == record_url(DOMAIN, 7)
        assert not [c for c in fake_urlopen.calls if c["method"] in ("POST", "DELETE")]
        assert result["records"] == [
            {"type": "A", "name": "www", "data": "203.0.113.20", "ttl": 1800}
        ]

    def test_multi_record_group_adds_via_post_and_removes_via_delete(self, driver, fake_urlopen):
        current = {
            "id": DOMAIN,
            "ttl": 1800,
            "records": [
                {"type": "TXT", "name": "@", "data": '"a"', "ttl": 1800},
                {"type": "TXT", "name": "@", "data": '"b"', "ttl": 1800},
            ],
        }
        desired = {
            "records": [
                {"type": "TXT", "name": "@", "data": '"a"', "ttl": 1800},
                {"type": "TXT", "name": "@", "data": '"c"', "ttl": 1800},
            ]
        }
        fake_urlopen.script(
            "GET",
            records_first_page_url(DOMAIN),
            FakeHTTPResponse(
                200,
                {
                    "domain_records": [
                        do_record(id=1, type="TXT", name="@", data='"a"'),
                        do_record(id=2, type="TXT", name="@", data='"b"'),
                    ]
                },
            ),
            FakeHTTPResponse(
                200,
                {
                    "domain_records": [
                        do_record(id=1, type="TXT", name="@", data='"a"'),
                        do_record(id=3, type="TXT", name="@", data='"c"'),
                    ]
                },
            ),
        )
        fake_urlopen.script(
            "POST",
            records_url(DOMAIN),
            FakeHTTPResponse(
                201, {"domain_record": do_record(id=3, type="TXT", name="@", data='"c"')}
            ),
        )
        fake_urlopen.script("DELETE", record_url(DOMAIN, 2), FakeHTTPResponse(204, None))
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))

        driver.update(DOMAIN, current, desired, CREDENTIALS)

        post_calls = [
            c
            for c in fake_urlopen.calls
            if c["method"] == "POST" and c["url"] == records_url(DOMAIN)
        ]
        delete_calls = [c for c in fake_urlopen.calls if c["method"] == "DELETE"]
        assert len(post_calls) == 1
        assert post_calls[0]["body"]["data"] == '"c"'
        assert len(delete_calls) == 1
        assert delete_calls[0]["url"] == record_url(DOMAIN, 2)

    def test_multi_record_group_puts_matched_pairs_differing_only_in_ttl(
        self, driver, fake_urlopen
    ):
        # Both records in the group move from ttl 1800 to 3600 together
        # -- not just one of them -- because RFC 2181 §5.2 requires every
        # record in an RRset to share one ttl (see
        # TestRRsetTtlConsistency), so 'desired' could never legitimately
        # carry mail1 at 3600 and mail2 still at 1800. What this test
        # still pins down: the "matches except ttl" pairing must match
        # each desired record against the RIGHT current record by data,
        # not just grab whichever one is left -- id=1 gets mail1's new
        # ttl, id=2 gets mail2's, never crossed.
        current = {
            "id": DOMAIN,
            "ttl": 1800,
            "records": [
                {
                    "type": "MX",
                    "name": "@",
                    "data": "mail1.example.com",
                    "ttl": 1800,
                    "priority": 10,
                },
                {
                    "type": "MX",
                    "name": "@",
                    "data": "mail2.example.com",
                    "ttl": 1800,
                    "priority": 20,
                },
            ],
        }
        desired = {
            "records": [
                {
                    "type": "MX",
                    "name": "@",
                    "data": "mail1.example.com",
                    "ttl": 3600,
                    "priority": 10,
                },
                {
                    "type": "MX",
                    "name": "@",
                    "data": "mail2.example.com",
                    "ttl": 3600,
                    "priority": 20,
                },
            ]
        }
        fake_urlopen.script(
            "GET",
            records_first_page_url(DOMAIN),
            FakeHTTPResponse(
                200,
                {
                    "domain_records": [
                        do_record(id=1, type="MX", name="@", data="mail1.example.com", priority=10),
                        do_record(id=2, type="MX", name="@", data="mail2.example.com", priority=20),
                    ]
                },
            ),
            FakeHTTPResponse(
                200,
                {
                    "domain_records": [
                        do_record(
                            id=1,
                            type="MX",
                            name="@",
                            data="mail1.example.com",
                            ttl=3600,
                            priority=10,
                        ),
                        do_record(
                            id=2,
                            type="MX",
                            name="@",
                            data="mail2.example.com",
                            ttl=3600,
                            priority=20,
                        ),
                    ]
                },
            ),
        )
        fake_urlopen.script(
            "PUT",
            record_url(DOMAIN, 1),
            FakeHTTPResponse(
                200,
                {
                    "domain_record": do_record(
                        id=1, type="MX", name="@", data="mail1.example.com", ttl=3600, priority=10
                    )
                },
            ),
        )
        fake_urlopen.script(
            "PUT",
            record_url(DOMAIN, 2),
            FakeHTTPResponse(
                200,
                {
                    "domain_record": do_record(
                        id=2, type="MX", name="@", data="mail2.example.com", ttl=3600, priority=20
                    )
                },
            ),
        )
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))

        driver.update(DOMAIN, current, desired, CREDENTIALS)

        put_calls = {c["url"]: c["body"] for c in fake_urlopen.calls if c["method"] == "PUT"}
        assert put_calls[record_url(DOMAIN, 1)]["data"] == "mail1.example.com."
        assert put_calls[record_url(DOMAIN, 1)]["ttl"] == 3600
        assert put_calls[record_url(DOMAIN, 2)]["data"] == "mail2.example.com."
        assert put_calls[record_url(DOMAIN, 2)]["ttl"] == 3600
        assert not [c for c in fake_urlopen.calls if c["method"] in ("POST", "DELETE")]

    def test_order_of_operations_is_put_then_post_then_delete(self, driver, fake_urlopen):
        current = {
            "id": DOMAIN,
            "ttl": 1800,
            "records": [
                {"type": "A", "name": "www", "data": "203.0.113.10", "ttl": 1800},
                {"type": "TXT", "name": "@", "data": '"old"', "ttl": 1800},
            ],
        }
        desired = {
            "records": [
                {"type": "A", "name": "www", "data": "203.0.113.20", "ttl": 1800},
                {"type": "TXT", "name": "@", "data": '"new"', "ttl": 1800},
            ]
        }
        fake_urlopen.script(
            "GET",
            records_first_page_url(DOMAIN),
            FakeHTTPResponse(
                200,
                {
                    "domain_records": [
                        do_record(id=1, type="A", name="www", data="203.0.113.10"),
                        do_record(id=2, type="TXT", name="@", data='"old"'),
                    ]
                },
            ),
            FakeHTTPResponse(
                200,
                {
                    "domain_records": [
                        do_record(id=1, type="A", name="www", data="203.0.113.20"),
                        do_record(id=3, type="TXT", name="@", data='"new"'),
                    ]
                },
            ),
        )
        fake_urlopen.script(
            "PUT",
            record_url(DOMAIN, 1),
            FakeHTTPResponse(
                200, {"domain_record": do_record(id=1, type="A", name="www", data="203.0.113.20")}
            ),
        )
        fake_urlopen.script(
            "POST",
            records_url(DOMAIN),
            FakeHTTPResponse(
                201, {"domain_record": do_record(id=3, type="TXT", name="@", data='"new"')}
            ),
        )
        fake_urlopen.script("DELETE", record_url(DOMAIN, 2), FakeHTTPResponse(204, None))
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))

        driver.update(DOMAIN, current, desired, CREDENTIALS)

        mutating = [
            c["method"] for c in fake_urlopen.calls if c["method"] in ("PUT", "POST", "DELETE")
        ]
        assert mutating == ["PUT", "POST", "DELETE"]

    def test_no_records_diff_makes_no_mutating_calls(self, driver, fake_urlopen):
        current = {
            "id": DOMAIN,
            "ttl": 1800,
            "records": [{"type": "A", "name": "@", "data": "203.0.113.10", "ttl": 1800}],
        }
        desired = {"records": [{"type": "A", "name": "@", "data": "203.0.113.10", "ttl": 1800}]}
        fake_urlopen.script(
            "GET",
            records_first_page_url(DOMAIN),
            FakeHTTPResponse(
                200, {"domain_records": [do_record(id=1, type="A", name="@", data="203.0.113.10")]}
            ),
        )
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))

        driver.update(DOMAIN, current, desired, CREDENTIALS)

        assert not [c for c in fake_urlopen.calls if c["method"] in ("PUT", "POST", "DELETE")]

    def test_aaaa_is_single_valued_so_a_data_change_is_a_put(self, driver, fake_urlopen):
        # Pins a judgment call the spec's prose named only A and CNAME for.
        # AAAA has the same address-record shape as A, so treating it as
        # multi-valued would be an arbitrary asymmetry -- a user editing an
        # IPv6 address would get a delete/create pair and a resolution gap
        # where the IPv4 equivalent gets an in-place edit.
        current = {
            "id": DOMAIN,
            "ttl": 1800,
            "records": [{"type": "AAAA", "name": "www", "data": "2001:db8::1", "ttl": 1800}],
        }
        desired = {"records": [{"type": "AAAA", "name": "www", "data": "2001:db8::2", "ttl": 1800}]}
        fake_urlopen.script(
            "GET",
            records_first_page_url(DOMAIN),
            FakeHTTPResponse(
                200,
                {"domain_records": [do_record(id=1, type="AAAA", name="www", data="2001:db8::1")]},
            ),
            FakeHTTPResponse(
                200,
                {"domain_records": [do_record(id=1, type="AAAA", name="www", data="2001:db8::2")]},
            ),
        )
        fake_urlopen.script(
            "PUT",
            record_url(DOMAIN, 1),
            FakeHTTPResponse(
                200, {"domain_record": do_record(id=1, type="AAAA", name="www", data="2001:db8::2")}
            ),
        )
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))

        driver.update(DOMAIN, current, desired, CREDENTIALS)

        assert [c["method"] for c in fake_urlopen.calls if c["method"] in ("PUT", "POST", "DELETE")]
        assert not [c for c in fake_urlopen.calls if c["method"] in ("POST", "DELETE")]

    def test_round_robin_a_records_use_the_set_path_not_the_single_valued_path(
        self, driver, fake_urlopen
    ):
        # Two A records at one name is ordinary round-robin DNS, so the
        # type alone must not imply single-valued: the count condition is
        # what keeps this correct. Dropping one of two addresses has to be
        # a DELETE of that address, never a PUT rewriting the survivor.
        current = {
            "id": DOMAIN,
            "ttl": 1800,
            "records": [
                {"type": "A", "name": "@", "data": "203.0.113.10", "ttl": 1800},
                {"type": "A", "name": "@", "data": "203.0.113.11", "ttl": 1800},
            ],
        }
        desired = {"records": [{"type": "A", "name": "@", "data": "203.0.113.10", "ttl": 1800}]}
        fake_urlopen.script(
            "GET",
            records_first_page_url(DOMAIN),
            FakeHTTPResponse(
                200,
                {
                    "domain_records": [
                        do_record(id=1, type="A", name="@", data="203.0.113.10"),
                        do_record(id=2, type="A", name="@", data="203.0.113.11"),
                    ]
                },
            ),
            FakeHTTPResponse(
                200,
                {"domain_records": [do_record(id=1, type="A", name="@", data="203.0.113.10")]},
            ),
        )
        fake_urlopen.script("DELETE", record_url(DOMAIN, 2), FakeHTTPResponse(204, None))
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))

        driver.update(DOMAIN, current, desired, CREDENTIALS)

        mutations = [
            (c["method"], c["url"])
            for c in fake_urlopen.calls
            if c["method"] in ("PUT", "POST", "DELETE")
        ]
        assert mutations == [("DELETE", record_url(DOMAIN, 2))]


class TestValidationEdgeCases:
    """All ValueError, all raised before any API call -- exercised via
    create(), since specs/digitalocean_domain.md's update() runs "the
    same checks as create()" (step 2) before any mutation.
    """

    def test_cname_data_with_a_trailing_dot_raises(self, driver, fake_urlopen):
        # Canonical form is dotless -- DigitalOcean stores and returns it
        # that way, and aiform appends the dot the API requires only on
        # the wire. A user-written trailing dot is rejected rather than
        # silently stripped: read() can only ever return the dotless
        # form, so a stripped-not-rejected dotted value would produce a
        # permanent phantom diff against the raw file.
        params = {
            "records": [{"type": "CNAME", "name": "www", "data": "example.com.", "ttl": 1800}]
        }

        with pytest.raises(ValueError) as excinfo:
            driver.create(DOMAIN, params, CREDENTIALS)

        assert "example.com" in str(excinfo.value)
        assert fake_urlopen.calls == []

    def test_mx_data_with_a_trailing_dot_raises(self, driver, fake_urlopen):
        params = {
            "records": [
                {
                    "type": "MX",
                    "name": "@",
                    "data": "mail.example.com.",
                    "ttl": 1800,
                    "priority": 10,
                }
            ]
        }

        with pytest.raises(ValueError):
            driver.create(DOMAIN, params, CREDENTIALS)

        assert fake_urlopen.calls == []

    def test_ns_data_with_a_trailing_dot_raises(self, driver, fake_urlopen):
        params = {
            "records": [{"type": "NS", "name": "dev", "data": "ns1.otherhost.net.", "ttl": 1800}]
        }

        with pytest.raises(ValueError):
            driver.create(DOMAIN, params, CREDENTIALS)

        assert fake_urlopen.calls == []

    def test_srv_data_with_a_trailing_dot_raises(self, driver, fake_urlopen):
        params = {
            "records": [
                {
                    "type": "SRV",
                    "name": "_sip._tcp",
                    "data": "sipserver.example.com.",
                    "ttl": 1800,
                    "priority": 10,
                    "port": 5060,
                    "weight": 5,
                }
            ]
        }

        with pytest.raises(ValueError):
            driver.create(DOMAIN, params, CREDENTIALS)

        assert fake_urlopen.calls == []

    def test_caa_data_with_a_trailing_dot_raises(self, driver, fake_urlopen):
        # CAA's data is a CA domain (e.g. "letsencrypt.org"), and
        # DigitalOcean's trailing-dot requirement applies to it exactly
        # like CNAME/MX/NS/SRV -- verified live, not documented anywhere
        # aiform's original spec draft looked.
        params = {
            "records": [
                {
                    "type": "CAA",
                    "name": "@",
                    "data": "letsencrypt.org.",
                    "ttl": 1800,
                    "flags": 0,
                    "tag": "issue",
                }
            ]
        }

        with pytest.raises(ValueError):
            driver.create(DOMAIN, params, CREDENTIALS)

        assert fake_urlopen.calls == []

    def test_cname_data_that_is_a_relative_bare_label_raises_naming_the_qualified_form(
        self, driver, fake_urlopen
    ):
        params = {"records": [{"type": "CNAME", "name": "www", "data": "www", "ttl": 1800}]}

        with pytest.raises(ValueError) as excinfo:
            driver.create(DOMAIN, params, CREDENTIALS)

        assert "www" in str(excinfo.value)
        assert fake_urlopen.calls == []

    def test_data_of_at_sign_is_accepted_as_the_apex_and_sent_unmodified(
        self, driver, fake_urlopen
    ):
        # "@" has no dot at all but must not be rejected as relative --
        # it's the one documented shorthand DigitalOcean accepts for the
        # apex, and it is sent to the API exactly as written, with no
        # dot appended.
        params = {"records": [{"type": "NS", "name": "dev", "data": "@", "ttl": 1800}]}
        script_create_zone(fake_urlopen)
        fake_urlopen.script(
            "POST",
            records_url(DOMAIN),
            FakeHTTPResponse(
                201, {"domain_record": do_record(id=1, type="NS", name="dev", data="@")}
            ),
        )
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))
        fake_urlopen.script(
            "GET",
            records_first_page_url(DOMAIN),
            FakeHTTPResponse(
                200, {"domain_records": [do_record(id=1, type="NS", name="dev", data="@")]}
            ),
        )

        driver.create(DOMAIN, params, CREDENTIALS)

        post_call = next(
            c
            for c in fake_urlopen.calls
            if c["method"] == "POST" and c["url"] == records_url(DOMAIN)
        )
        assert post_call["body"]["data"] == "@"

    def test_dotless_cname_mx_ns_srv_caa_data_is_sent_with_a_trailing_dot_appended(
        self, driver, fake_urlopen
    ):
        # The dot DigitalOcean's API requires is appended by the driver,
        # at the wire boundary only -- the user writes (and read()
        # returns) the dotless canonical form.
        records = [
            {"type": "CNAME", "name": "www", "data": "example.com", "ttl": 1800},
            {"type": "MX", "name": "@", "data": "mail.example.com", "ttl": 1800, "priority": 10},
            {"type": "NS", "name": "dev", "data": "ns1.otherhost.net", "ttl": 1800},
            {
                "type": "SRV",
                "name": "_sip._tcp",
                "data": "sip.example.com",
                "ttl": 1800,
                "priority": 10,
                "port": 5060,
                "weight": 5,
            },
            {
                "type": "CAA",
                "name": "@",
                "data": "letsencrypt.org",
                "ttl": 1800,
                "flags": 0,
                "tag": "issue",
            },
        ]
        script_create_zone(fake_urlopen)
        fake_urlopen.script(
            "POST",
            records_url(DOMAIN),
            *(FakeHTTPResponse(201, {"domain_record": do_record(id=i)}) for i in range(1, 6)),
        )
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))
        fake_urlopen.script(
            "GET", records_first_page_url(DOMAIN), FakeHTTPResponse(200, {"domain_records": []})
        )

        driver.create(DOMAIN, {"records": records}, CREDENTIALS)

        record_posts = [
            c
            for c in fake_urlopen.calls
            if c["method"] == "POST" and c["url"] == records_url(DOMAIN)
        ]
        assert [c["body"]["data"] for c in record_posts] == [
            "example.com.",
            "mail.example.com.",
            "ns1.otherhost.net.",
            "sip.example.com.",
            "letsencrypt.org.",
        ]

    def test_a_record_data_does_not_require_a_trailing_dot(self, driver, fake_urlopen):
        # The trailing-dot rule applies only to CNAME/MX/NS/SRV; A/AAAA/TXT
        # data is a literal value, not a hostname.
        params = {"records": [{"type": "A", "name": "@", "data": "203.0.113.10", "ttl": 1800}]}
        script_create_zone(fake_urlopen)
        fake_urlopen.script(
            "POST",
            records_url(DOMAIN),
            FakeHTTPResponse(201, {"domain_record": do_record(id=1, name="@")}),
        )
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))
        fake_urlopen.script(
            "GET",
            records_first_page_url(DOMAIN),
            FakeHTTPResponse(200, {"domain_records": [do_record(id=1, name="@")]}),
        )

        driver.create(DOMAIN, params, CREDENTIALS)

    def test_record_type_outside_the_enum_raises_naming_the_type(self, driver, fake_urlopen):
        params = {"records": [{"type": "PTR", "name": "@", "data": "203.0.113.10", "ttl": 1800}]}

        with pytest.raises(ValueError) as excinfo:
            driver.create(DOMAIN, params, CREDENTIALS)

        assert "PTR" in str(excinfo.value)
        assert fake_urlopen.calls == []

    def test_soa_record_type_is_rejected(self, driver, fake_urlopen):
        params = {
            "records": [
                {
                    "type": "SOA",
                    "name": "@",
                    "data": "ns1.digitalocean.com. hostmaster.example.com. 1 1 1 1 1",
                    "ttl": 1800,
                }
            ]
        }

        with pytest.raises(ValueError) as excinfo:
            driver.create(DOMAIN, params, CREDENTIALS)

        assert "SOA" in str(excinfo.value)
        assert fake_urlopen.calls == []

    def test_field_not_valid_for_its_record_type_raises(self, driver, fake_urlopen):
        params = {
            "records": [
                {"type": "A", "name": "@", "data": "203.0.113.10", "ttl": 1800, "priority": 10}
            ]
        }

        with pytest.raises(ValueError) as excinfo:
            driver.create(DOMAIN, params, CREDENTIALS)

        assert "priority" in str(excinfo.value)
        assert fake_urlopen.calls == []

    def test_missing_required_priority_on_mx_raises(self, driver, fake_urlopen):
        params = {"records": [{"type": "MX", "name": "@", "data": "mail.example.com", "ttl": 1800}]}

        with pytest.raises(ValueError) as excinfo:
            driver.create(DOMAIN, params, CREDENTIALS)

        assert "priority" in str(excinfo.value)
        assert fake_urlopen.calls == []

    def test_missing_required_fields_on_srv_raises(self, driver, fake_urlopen):
        params = {
            "records": [
                {
                    "type": "SRV",
                    "name": "_sip._tcp",
                    "data": "sipserver.example.com",
                    "ttl": 1800,
                    "priority": 10,
                }
            ]
        }

        with pytest.raises(ValueError):
            driver.create(DOMAIN, params, CREDENTIALS)

        assert fake_urlopen.calls == []

    def test_missing_required_fields_on_caa_raises(self, driver, fake_urlopen):
        params = {"records": [{"type": "CAA", "name": "@", "data": '0 issue "x"', "ttl": 1800}]}

        with pytest.raises(ValueError):
            driver.create(DOMAIN, params, CREDENTIALS)

        assert fake_urlopen.calls == []

    def test_records_not_a_list_raises(self, driver, fake_urlopen):
        with pytest.raises(ValueError):
            driver.create(DOMAIN, {"records": "not-a-list"}, CREDENTIALS)

        assert fake_urlopen.calls == []

    def test_records_element_not_a_dict_raises(self, driver, fake_urlopen):
        with pytest.raises(ValueError):
            driver.create(DOMAIN, {"records": ["not-a-dict"]}, CREDENTIALS)

        assert fake_urlopen.calls == []

    def test_duplicate_records_raise(self, driver, fake_urlopen):
        record = {"type": "A", "name": "@", "data": "203.0.113.10", "ttl": 1800}
        params = {"records": [dict(record), dict(record)]}

        with pytest.raises(ValueError):
            driver.create(DOMAIN, params, CREDENTIALS)

        assert fake_urlopen.calls == []

    def test_ip_address_present_in_params_raises_naming_it(self, driver, fake_urlopen):
        params = {"records": [], "ip_address": "203.0.113.10"}

        with pytest.raises(ValueError) as excinfo:
            driver.create(DOMAIN, params, CREDENTIALS)

        assert "ip_address" in str(excinfo.value)
        assert fake_urlopen.calls == []


class TestDelete:
    def test_deletes_domain_by_id(self, driver, fake_urlopen):
        fake_urlopen.script("DELETE", domain_url(DOMAIN), FakeHTTPResponse(204, None))

        driver.delete(DOMAIN, CREDENTIALS)

        assert fake_urlopen.calls[0]["method"] == "DELETE"
        assert fake_urlopen.calls[0]["url"] == domain_url(DOMAIN)
        assert fake_urlopen.calls[0]["authorization"] == "Bearer dop_v1_test"

    def test_204_returns_none(self, driver, fake_urlopen):
        fake_urlopen.script("DELETE", domain_url(DOMAIN), FakeHTTPResponse(204, None))

        result = driver.delete(DOMAIN, CREDENTIALS)

        assert result is None

    def test_404_is_idempotent_success(self, driver, fake_urlopen):
        fake_urlopen.script("DELETE", domain_url(DOMAIN), http_error(domain_url(DOMAIN), 404))

        result = driver.delete(DOMAIN, CREDENTIALS)

        assert result is None
        assert len(fake_urlopen.calls) == 1


class TestUpdatePreservesDoManagedRecords:
    """B1 regression: DigitalOcean returns its auto-created SOA and apex
    NS records in the same listing as user-managed records. A filter
    that fails to match them lets update()'s reconciliation see them as
    "the user removed these", issuing DELETE against the zone's own
    nameservers.
    """

    def test_no_mutating_call_is_issued_against_the_auto_created_soa_or_apex_ns_records(
        self, driver, fake_urlopen
    ):
        current = {
            "id": DOMAIN,
            "ttl": 1800,
            "records": [{"type": "A", "name": "@", "data": "203.0.113.10", "ttl": 1800}],
        }
        desired = {"records": [{"type": "A", "name": "@", "data": "203.0.113.10", "ttl": 1800}]}
        fake_urlopen.script(
            "GET",
            records_first_page_url(DOMAIN),
            FakeHTTPResponse(
                200,
                {
                    "domain_records": [
                        do_record(id=1, type="A", name="@", data="203.0.113.10"),
                        do_record(id=100, type="SOA", name="@", data="1800"),
                        do_record(id=101, type="NS", name="@", data="ns1.digitalocean.com"),
                        do_record(id=102, type="NS", name="@", data="ns2.digitalocean.com"),
                        do_record(id=103, type="NS", name="@", data="ns3.digitalocean.com"),
                    ]
                },
            ),
        )
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))

        driver.update(DOMAIN, current, desired, CREDENTIALS)

        assert not [c for c in fake_urlopen.calls if c["method"] in ("PUT", "POST", "DELETE")]


class TestCaaMultipleTagsAtSameName:
    """B3 regression: identity keyed on (type, name, data) alone silently
    drops records that share a data value while differing elsewhere.
    CAA `issue` and `issuewild` for the same CA is the standard
    Let's Encrypt setup and differs only in `tag` -- verified live to
    coexist at one name.
    """

    def _caa(self, tag, ttl=1800):
        return {
            "type": "CAA",
            "name": "@",
            "data": "letsencrypt.org",
            "ttl": ttl,
            "flags": 0,
            "tag": tag,
        }

    def _do_caa(self, id, tag):
        return do_record(id=id, type="CAA", name="@", data="letsencrypt.org", flags=0, tag=tag)

    def test_adding_issuewild_alongside_issue_posts_exactly_one_record(self, driver, fake_urlopen):
        current = {"id": DOMAIN, "ttl": 1800, "records": [self._caa("issue")]}
        desired = {"records": [self._caa("issue"), self._caa("issuewild")]}
        fake_urlopen.script(
            "GET",
            records_first_page_url(DOMAIN),
            FakeHTTPResponse(200, {"domain_records": [self._do_caa(1, "issue")]}),
            FakeHTTPResponse(
                200,
                {"domain_records": [self._do_caa(1, "issue"), self._do_caa(2, "issuewild")]},
            ),
        )
        fake_urlopen.script(
            "POST",
            records_url(DOMAIN),
            FakeHTTPResponse(201, {"domain_record": self._do_caa(2, "issuewild")}),
        )
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))

        driver.update(DOMAIN, current, desired, CREDENTIALS)

        mutating = [c for c in fake_urlopen.calls if c["method"] in ("PUT", "POST", "DELETE")]
        assert len(mutating) == 1
        assert mutating[0]["method"] == "POST"
        assert mutating[0]["body"]["tag"] == "issuewild"

    def test_removing_issuewild_deletes_only_that_record_and_leaves_issue_untouched(
        self, driver, fake_urlopen
    ):
        current = {
            "id": DOMAIN,
            "ttl": 1800,
            "records": [self._caa("issue"), self._caa("issuewild")],
        }
        desired = {"records": [self._caa("issue")]}
        fake_urlopen.script(
            "GET",
            records_first_page_url(DOMAIN),
            FakeHTTPResponse(
                200,
                {"domain_records": [self._do_caa(1, "issue"), self._do_caa(2, "issuewild")]},
            ),
            FakeHTTPResponse(200, {"domain_records": [self._do_caa(1, "issue")]}),
        )
        fake_urlopen.script("DELETE", record_url(DOMAIN, 2), FakeHTTPResponse(204, None))
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))

        driver.update(DOMAIN, current, desired, CREDENTIALS)

        mutating = [
            (c["method"], c["url"])
            for c in fake_urlopen.calls
            if c["method"] in ("PUT", "POST", "DELETE")
        ]
        assert mutating == [("DELETE", record_url(DOMAIN, 2))]


class TestSrvSameTargetDifferentPorts:
    """B3's SRV counterpart to the CAA case above -- two SRV records
    sharing a target and differing only by port.
    """

    def _srv(self, port, ttl=1800):
        return {
            "type": "SRV",
            "name": "_sip._tcp",
            "data": "sipserver.example.com",
            "ttl": ttl,
            "priority": 10,
            "port": port,
            "weight": 5,
        }

    def _do_srv(self, id, port):
        return do_record(
            id=id,
            type="SRV",
            name="_sip._tcp",
            data="sipserver.example.com",
            priority=10,
            port=port,
            weight=5,
        )

    def test_adding_a_second_port_posts_exactly_one_record(self, driver, fake_urlopen):
        current = {"id": DOMAIN, "ttl": 1800, "records": [self._srv(5060)]}
        desired = {"records": [self._srv(5060), self._srv(5061)]}
        fake_urlopen.script(
            "GET",
            records_first_page_url(DOMAIN),
            FakeHTTPResponse(200, {"domain_records": [self._do_srv(1, 5060)]}),
            FakeHTTPResponse(
                200, {"domain_records": [self._do_srv(1, 5060), self._do_srv(2, 5061)]}
            ),
        )
        fake_urlopen.script(
            "POST",
            records_url(DOMAIN),
            FakeHTTPResponse(201, {"domain_record": self._do_srv(2, 5061)}),
        )
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))

        driver.update(DOMAIN, current, desired, CREDENTIALS)

        mutating = [c for c in fake_urlopen.calls if c["method"] in ("PUT", "POST", "DELETE")]
        assert len(mutating) == 1
        assert mutating[0]["method"] == "POST"
        assert mutating[0]["body"]["port"] == 5061

    def test_removing_a_port_deletes_only_that_record(self, driver, fake_urlopen):
        current = {"id": DOMAIN, "ttl": 1800, "records": [self._srv(5060), self._srv(5061)]}
        desired = {"records": [self._srv(5060)]}
        fake_urlopen.script(
            "GET",
            records_first_page_url(DOMAIN),
            FakeHTTPResponse(
                200, {"domain_records": [self._do_srv(1, 5060), self._do_srv(2, 5061)]}
            ),
            FakeHTTPResponse(200, {"domain_records": [self._do_srv(1, 5060)]}),
        )
        fake_urlopen.script("DELETE", record_url(DOMAIN, 2), FakeHTTPResponse(204, None))
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))

        driver.update(DOMAIN, current, desired, CREDENTIALS)

        mutating = [
            (c["method"], c["url"])
            for c in fake_urlopen.calls
            if c["method"] in ("PUT", "POST", "DELETE")
        ]
        assert mutating == [("DELETE", record_url(DOMAIN, 2))]


class TestScalarTypeValidation:
    """C1: ttl/priority/port/weight/flags must be int (and not bool);
    type/name/data/tag must be str. Mirrors compute.py's
    _reject_malformed_values(). Must run before duplicate detection,
    which would otherwise crash on an unhashable value with a bare
    TypeError.
    """

    def test_ttl_as_a_string_raises(self, driver, fake_urlopen):
        params = {"records": [{"type": "A", "name": "@", "data": "203.0.113.10", "ttl": "1800"}]}

        with pytest.raises(ValueError):
            driver.create(DOMAIN, params, CREDENTIALS)

        assert fake_urlopen.calls == []

    def test_ttl_as_a_bool_raises(self, driver, fake_urlopen):
        params = {"records": [{"type": "A", "name": "@", "data": "203.0.113.10", "ttl": True}]}

        with pytest.raises(ValueError):
            driver.create(DOMAIN, params, CREDENTIALS)

        assert fake_urlopen.calls == []

    def test_priority_as_a_string_raises(self, driver, fake_urlopen):
        params = {
            "records": [
                {
                    "type": "MX",
                    "name": "@",
                    "data": "mail.example.com",
                    "ttl": 1800,
                    "priority": "10",
                }
            ]
        }

        with pytest.raises(ValueError):
            driver.create(DOMAIN, params, CREDENTIALS)

        assert fake_urlopen.calls == []

    def test_name_as_a_non_string_raises(self, driver, fake_urlopen):
        params = {"records": [{"type": "A", "name": 1, "data": "203.0.113.10", "ttl": 1800}]}

        with pytest.raises(ValueError):
            driver.create(DOMAIN, params, CREDENTIALS)

        assert fake_urlopen.calls == []

    def test_unhashable_data_raises_value_error_not_a_bare_type_error(self, driver, fake_urlopen):
        # A malformed 'data' (e.g. a YAML list) must be rejected by the
        # scalar-type check before the duplicate-detection step, which
        # would otherwise crash trying to hash it.
        params = {
            "records": [{"type": "A", "name": "@", "data": ["not", "a", "string"], "ttl": 1800}]
        }

        with pytest.raises(ValueError):
            driver.create(DOMAIN, params, CREDENTIALS)

        assert fake_urlopen.calls == []


class TestRRsetTtlConsistency:
    """C2: DigitalOcean silently rectifies a mismatched TTL within an
    RRset (verified live: posting a second A record at the same name
    with a different ttl changed the existing one), so a local mismatch
    would diff forever against a value the user never wrote.
    """

    def test_two_a_records_at_the_same_name_with_different_ttl_raises(self, driver, fake_urlopen):
        params = {
            "records": [
                {"type": "A", "name": "@", "data": "203.0.113.10", "ttl": 1800},
                {"type": "A", "name": "@", "data": "203.0.113.11", "ttl": 3600},
            ]
        }

        with pytest.raises(ValueError):
            driver.create(DOMAIN, params, CREDENTIALS)

        assert fake_urlopen.calls == []

    def test_records_at_different_names_may_have_different_ttl(self, driver, fake_urlopen):
        params = {
            "records": [
                {"type": "A", "name": "@", "data": "203.0.113.10", "ttl": 1800},
                {"type": "A", "name": "www", "data": "203.0.113.11", "ttl": 3600},
            ]
        }
        script_create_zone(fake_urlopen)
        fake_urlopen.script(
            "POST",
            records_url(DOMAIN),
            FakeHTTPResponse(201, {"domain_record": do_record(id=1, name="@")}),
            FakeHTTPResponse(201, {"domain_record": do_record(id=2, name="www")}),
        )
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))
        fake_urlopen.script(
            "GET", records_first_page_url(DOMAIN), FakeHTTPResponse(200, {"domain_records": []})
        )

        driver.create(DOMAIN, params, CREDENTIALS)


class TestUserWrittenApexNsRejected:
    """C6: read() filters out apex NS records pointing at DigitalOcean's
    own nameservers, so a user who copies them into 'records' gets a
    record that is permanently "missing" -- re-POSTed on every apply,
    and rejected with a 422 on the very first create() that triggers the
    zone rollback.
    """

    def test_apex_ns_pointing_at_a_digitalocean_nameserver_raises(self, driver, fake_urlopen):
        params = {
            "records": [{"type": "NS", "name": "@", "data": "ns1.digitalocean.com", "ttl": 1800}]
        }

        with pytest.raises(ValueError) as excinfo:
            driver.create(DOMAIN, params, CREDENTIALS)

        assert "digitalocean" in str(excinfo.value).lower()
        assert fake_urlopen.calls == []

    @pytest.mark.parametrize(
        "data",
        ["NS1.DIGITALOCEAN.COM", "Ns1.DigitalOcean.Com", "ns1.digitalocean.com."],
    )
    def test_apex_ns_match_is_case_and_dot_insensitive(self, driver, fake_urlopen, data):
        # Hostnames are case-insensitive, so these all name the same
        # nameserver. Matching only the exact lowercase dotless spelling
        # would let one through validation, whereupon read() drops it as
        # DO-managed and it becomes a permanently "missing" record,
        # re-POSTed on every apply -- the same near-miss shape as the
        # trailing-dot bug this filter already had once.
        params = {"records": [{"type": "NS", "name": "@", "data": data, "ttl": 1800}]}

        with pytest.raises(ValueError) as excinfo:
            driver.create(DOMAIN, params, CREDENTIALS)

        assert "digitalocean" in str(excinfo.value).lower()
        assert fake_urlopen.calls == []

    def test_delegated_subdomain_ns_pointing_at_a_digitalocean_nameserver_is_allowed(
        self, driver, fake_urlopen
    ):
        # Only the apex (name == "@") is DO-managed; a delegated
        # subdomain NS record pointing at a DO nameserver is a genuine
        # user-managed record (e.g. delegating a subzone to a second DO
        # account) and must not be rejected.
        params = {
            "records": [{"type": "NS", "name": "dev", "data": "ns1.digitalocean.com", "ttl": 1800}]
        }
        script_create_zone(fake_urlopen)
        fake_urlopen.script(
            "POST",
            records_url(DOMAIN),
            FakeHTTPResponse(
                201,
                {
                    "domain_record": do_record(
                        id=1, type="NS", name="dev", data="ns1.digitalocean.com"
                    )
                },
            ),
        )
        fake_urlopen.script("GET", domain_url(DOMAIN), FakeHTTPResponse(200, do_domain()))
        fake_urlopen.script(
            "GET",
            records_first_page_url(DOMAIN),
            FakeHTTPResponse(
                200,
                {
                    "domain_records": [
                        do_record(id=1, type="NS", name="dev", data="ns1.digitalocean.com")
                    ]
                },
            ),
        )

        driver.create(DOMAIN, params, CREDENTIALS)


class TestUpdateFoldsDoErrorMessages:
    """C4: update()'s PUT/POST/DELETE HTTPErrors should fold DigitalOcean's
    error message in, mirroring create()'s existing _post_record path.
    """

    def test_put_failure_folds_the_do_message_into_the_exception(self, driver, fake_urlopen):
        current = {
            "id": DOMAIN,
            "ttl": 1800,
            "records": [{"type": "A", "name": "www", "data": "203.0.113.10", "ttl": 1800}],
        }
        desired = {"records": [{"type": "A", "name": "www", "data": "203.0.113.20", "ttl": 1800}]}
        fake_urlopen.script(
            "GET",
            records_first_page_url(DOMAIN),
            FakeHTTPResponse(
                200,
                {"domain_records": [do_record(id=7, type="A", name="www", data="203.0.113.10")]},
            ),
        )
        fake_urlopen.script(
            "PUT",
            record_url(DOMAIN, 7),
            http_error(record_url(DOMAIN, 7), 422, {"message": "bad edit"}),
        )

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            driver.update(DOMAIN, current, desired, CREDENTIALS)

        assert "bad edit" in str(excinfo.value)

    def test_post_failure_folds_the_do_message_into_the_exception(self, driver, fake_urlopen):
        current = {"id": DOMAIN, "ttl": 1800, "records": []}
        desired = {"records": [{"type": "A", "name": "@", "data": "203.0.113.10", "ttl": 1800}]}
        fake_urlopen.script(
            "GET", records_first_page_url(DOMAIN), FakeHTTPResponse(200, {"domain_records": []})
        )
        fake_urlopen.script(
            "POST",
            records_url(DOMAIN),
            http_error(records_url(DOMAIN), 422, {"message": "bad create"}),
        )

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            driver.update(DOMAIN, current, desired, CREDENTIALS)

        assert "bad create" in str(excinfo.value)

    def test_delete_failure_folds_the_do_message_into_the_exception(self, driver, fake_urlopen):
        current = {
            "id": DOMAIN,
            "ttl": 1800,
            "records": [{"type": "TXT", "name": "@", "data": '"a"', "ttl": 1800}],
        }
        desired = {"records": []}
        fake_urlopen.script(
            "GET",
            records_first_page_url(DOMAIN),
            FakeHTTPResponse(
                200, {"domain_records": [do_record(id=9, type="TXT", name="@", data='"a"')]}
            ),
        )
        fake_urlopen.script(
            "DELETE",
            record_url(DOMAIN, 9),
            http_error(record_url(DOMAIN, 9), 500, {"message": "internal trouble"}),
        )

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            driver.update(DOMAIN, current, desired, CREDENTIALS)

        assert "internal trouble" in str(excinfo.value)

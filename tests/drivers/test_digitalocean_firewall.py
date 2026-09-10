# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Tests for drivers/digitalocean/firewall.py -- see
specs/digitalocean_firewall.md. Mirrors test_digitalocean_domain.py's
FakeUrlopen harness.

Every payload below comes from tests/drivers/transcripts.py, i.e. from a
response DigitalOcean actually sent during
`probes/digitalocean_firewall.py --mutate`. That is the point: a
hand-written fixture can only encode what its author already believed,
and this driver exists partly because several of those beliefs were
wrong (int ports are coerced, not rejected; `action` is server-added and
absent from the published schema).
"""

import io
import json
import urllib.error
import urllib.request
from email.message import Message

import pytest

from aiform.exceptions import ResourceNotFoundError
from aiform.planner import diff_attributes
from drivers.digitalocean.firewall import Driver
from tests.drivers import transcripts

BASE_URL = "https://api.digitalocean.com/v2"
CREDENTIALS = {"DIGITALOCEAN_TOKEN": "dop_v1_test"}
SESSION = "digitalocean_firewall"
NAME = "web-firewall"


def firewalls_url() -> str:
    return f"{BASE_URL}/firewalls"


def firewall_url(fid: str) -> str:
    return f"{BASE_URL}/firewalls/{fid}"


def created_payload() -> dict:
    return transcripts.response_body(SESSION, "02-")


def firewall_id() -> str:
    return created_payload()["firewall"]["id"]


def minimal_rule() -> dict:
    """One inbound rule, exactly as DigitalOcean returned it."""
    return dict(created_payload()["firewall"]["inbound_rules"][0])


def minimal_params() -> dict:
    return {
        "inbound_rules": [minimal_rule()],
        "outbound_rules": [],
        "droplet_ids": [],
        "tags": [],
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
    """Routes by (method, url) -- mirrors test_digitalocean_domain.py."""

    def __init__(self):
        self._scripts: dict[tuple[str, str], list] = {}
        self.calls: list[dict] = []

    def script(self, method: str, url: str, *responses) -> None:
        self._scripts[(method, url)] = list(responses)

    def __call__(self, request: urllib.request.Request, *args, **kwargs):
        method = request.get_method()
        url = request.full_url
        raw_body = request.data
        self.calls.append(
            {
                "method": method,
                "url": url,
                "body": json.loads(raw_body) if raw_body else None,
                "authorization": request.get_header("Authorization"),
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


@pytest.fixture
def driver() -> Driver:
    return Driver()


def script_read(fake_urlopen, payload=None):
    fake_urlopen.script(
        "GET", firewall_url(firewall_id()), FakeHTTPResponse(200, payload or created_payload())
    )


class TestClassAttributes:
    def test_param_schema_is_closed_at_both_levels(self, driver):
        schema = driver.PARAM_SCHEMA
        assert schema["additionalProperties"] is False
        rule = schema["properties"]["inbound_rules"]["items"]
        assert rule["additionalProperties"] is False
        assert set(rule["required"]) == {"protocol", "ports", "action", "sources"}

    def test_every_managed_field_is_required(self, driver):
        # droplet_ids and tags included deliberately: an omitted key is
        # invisible to diff_attributes() but still sent as [] by the
        # whole-object PUT, so omission would silently clear them.
        assert set(driver.PARAM_SCHEMA["required"]) == {
            "inbound_rules",
            "outbound_rules",
            "droplet_ids",
            "tags",
        }

    def test_unordered_fields_covers_every_collection(self, driver):
        assert set(driver.UNORDERED_FIELDS) == {
            "inbound_rules",
            "outbound_rules",
            "droplet_ids",
            "tags",
        }

    def test_nothing_forces_a_replace_and_nothing_is_non_diffable(self, driver):
        assert driver.LIKELY_REPLACE_FIELDS == []
        assert driver.NON_DIFFABLE_FIELDS == []


class TestCreate:
    def test_posts_the_firewall_and_returns_the_projection(self, driver, fake_urlopen):
        fake_urlopen.script("POST", firewalls_url(), FakeHTTPResponse(202, created_payload()))
        script_read(fake_urlopen)

        result = driver.create(NAME, minimal_params(), CREDENTIALS)

        posted = fake_urlopen.calls[0]
        assert posted["body"]["name"] == NAME
        assert posted["authorization"] == "Bearer dop_v1_test"
        assert result["id"] == firewall_id()
        assert result["inbound_rules"] == [minimal_rule()]

    def test_does_not_poll_because_an_unattached_firewall_is_already_succeeded(
        self, driver, fake_urlopen
    ):
        # specs/digitalocean_firewall.md: create returns status
        # "succeeded" immediately when unattached, so unlike compute.py
        # there is nothing to converge. Exactly one POST and one GET.
        fake_urlopen.script("POST", firewalls_url(), FakeHTTPResponse(202, created_payload()))
        script_read(fake_urlopen)

        driver.create(NAME, minimal_params(), CREDENTIALS)

        assert [c["method"] for c in fake_urlopen.calls] == ["POST", "GET"]

    def test_validates_before_calling_anything(self, driver, fake_urlopen):
        with pytest.raises(ValueError):
            driver.create(NAME, {"inbound_rules": [], "outbound_rules": []}, CREDENTIALS)
        assert fake_urlopen.calls == []


class TestRead:
    def test_drops_server_set_churn_fields(self, driver, fake_urlopen):
        script_read(fake_urlopen)
        result = driver.read(firewall_id(), CREDENTIALS)
        assert set(result) == {
            "id",
            "name",
            "inbound_rules",
            "outbound_rules",
            "droplet_ids",
            "tags",
        }
        for churn in ("status", "created_at", "pending_changes"):
            assert churn not in result

    def test_keeps_name_because_the_whole_object_put_needs_it(self, driver, fake_urlopen):
        # name is stable (rename is not exposed) so it cannot churn
        # state.json, and diff_attributes() only iterates `desired`, so
        # an extra key in read()'s output can never produce a diff.
        # Carrying it means update() needs no extra GET.
        script_read(fake_urlopen)
        assert (
            driver.read(firewall_id(), CREDENTIALS)["name"] == created_payload()["firewall"]["name"]
        )

    def test_keeps_the_server_added_action_field(self, driver, fake_urlopen):
        # DigitalOcean adds action to every rule and it is absent from
        # the published schema; dropping it here would make read()'s
        # rules unequal to the user's params forever.
        script_read(fake_urlopen)
        result = driver.read(firewall_id(), CREDENTIALS)
        assert result["inbound_rules"][0]["action"] == "allow"

    def test_missing_firewall_raises_resource_not_found(self, driver, fake_urlopen):
        fake_urlopen.script("GET", firewall_url("gone"), http_error(firewall_url("gone"), 404))
        with pytest.raises(ResourceNotFoundError):
            driver.read("gone", CREDENTIALS)

    def test_other_http_errors_propagate(self, driver, fake_urlopen):
        fake_urlopen.script("GET", firewall_url("x"), http_error(firewall_url("x"), 500))
        with pytest.raises(urllib.error.HTTPError):
            driver.read("x", CREDENTIALS)


class TestZeroDiffInvariant:
    def test_read_output_does_not_diff_against_the_params_a_user_wrote(self, driver, fake_urlopen):
        # The whole point: apply, then re-plan, must be a no-op.
        #
        # Both halves come from transcript 11-, the one probe whose
        # REQUEST already carried a fully user-shaped rule (`action`
        # included, which DigitalOcean would otherwise have added). Using
        # its request body as params and its response body as what read()
        # sees is what makes this a genuine round-trip. Comparing read()'s
        # projection of a response against that same response proves
        # nothing: a projection bug is present on both sides and cancels
        # out.
        sent = transcripts.request_body(SESSION, "11-")
        got = transcripts.response_body(SESSION, "11-")
        params = {key: value for key, value in sent.items() if key != "name"}
        fake_urlopen.script("GET", firewall_url(got["firewall"]["id"]), FakeHTTPResponse(200, got))

        current = driver.read(got["firewall"]["id"], CREDENTIALS)

        assert diff_attributes(current, params, unordered_fields=driver.UNORDERED_FIELDS) == {}

    def test_the_user_written_params_are_actually_accepted_by_validation(self, driver):
        # Guards the test above: if the recorded request body were not a
        # legal params block, the round-trip would be proving nothing
        # about anything a user could really write.
        sent = transcripts.request_body(SESSION, "11-")
        params = {key: value for key, value in sent.items() if key != "name"}
        driver._validate_params(params)

    def test_rule_order_alone_is_not_a_diff(self, driver, fake_urlopen):
        # Through the driver's own UNORDERED_FIELDS, not unordered_equal
        # directly: asserting on the helper passes unchanged even if the
        # declaration is empty, which is the thing under test.
        second = {
            "protocol": "tcp",
            "ports": "80",
            "action": "allow",
            "sources": {"addresses": ["0.0.0.0/0"]},
        }
        params = minimal_params()
        params["inbound_rules"] = [params["inbound_rules"][0], second]
        payload = created_payload()
        payload["firewall"]["inbound_rules"] = [
            second,
            payload["firewall"]["inbound_rules"][0],
        ]
        fake_urlopen.script("GET", firewall_url(firewall_id()), FakeHTTPResponse(200, payload))

        current = driver.read(firewall_id(), CREDENTIALS)

        assert diff_attributes(current, params, unordered_fields=Driver.UNORDERED_FIELDS) == {}


class TestUpdate:
    def test_is_a_single_put_then_a_read(self, driver, fake_urlopen):
        fake_urlopen.script(
            "PUT", firewall_url(firewall_id()), FakeHTTPResponse(200, created_payload())
        )
        script_read(fake_urlopen)

        driver.update(firewall_id(), driver_current(), minimal_params(), CREDENTIALS)

        assert [c["method"] for c in fake_urlopen.calls] == ["PUT", "GET"]

    def test_put_body_carries_every_field_because_put_replaces_wholesale(
        self, driver, fake_urlopen
    ):
        # Verified live for tags (transcripts 24-26): a PUT omitting a
        # tag the firewall actually had reads back []. The same is
        # INFERRED for droplet_ids -- same PUT, same replace semantics --
        # but not observed, since no probe ever attaches a droplet.
        # Every managed field therefore goes out every time.
        fake_urlopen.script(
            "PUT", firewall_url(firewall_id()), FakeHTTPResponse(200, created_payload())
        )
        script_read(fake_urlopen)

        driver.update(firewall_id(), driver_current(), minimal_params(), CREDENTIALS)

        body = fake_urlopen.calls[0]["body"]
        assert set(body) >= {"name", "inbound_rules", "outbound_rules", "droplet_ids", "tags"}

    def test_validates_before_mutating(self, driver, fake_urlopen):
        with pytest.raises(ValueError):
            driver.update(firewall_id(), driver_current(), {"inbound_rules": []}, CREDENTIALS)
        assert fake_urlopen.calls == []


def driver_current() -> dict:
    return {
        "id": firewall_id(),
        "name": created_payload()["firewall"]["name"],
        **minimal_params(),
    }


class TestDelete:
    def test_deletes(self, driver, fake_urlopen):
        fake_urlopen.script("DELETE", firewall_url("f1"), FakeHTTPResponse(204, None))
        assert driver.delete("f1", CREDENTIALS) is None

    def test_is_idempotent_because_a_second_delete_404s(self, driver, fake_urlopen):
        fake_urlopen.script("DELETE", firewall_url("f1"), http_error(firewall_url("f1"), 404))
        assert driver.delete("f1", CREDENTIALS) is None

    def test_other_errors_propagate(self, driver, fake_urlopen):
        fake_urlopen.script("DELETE", firewall_url("f1"), http_error(firewall_url("f1"), 500))
        with pytest.raises(urllib.error.HTTPError):
            driver.delete("f1", CREDENTIALS)


class TestValidationRejectsSilentlyRewrittenValues:
    """Each of these is accepted by DigitalOcean and stored as something
    else, which would diff forever. Transcripts 04, 05, 09, 10."""

    def _params(self, rule):
        return {"inbound_rules": [rule], "outbound_rules": [], "droplet_ids": [], "tags": []}

    def test_int_port_is_rejected_naming_the_string_form(self, driver):
        rule = {**minimal_rule(), "ports": 22}
        with pytest.raises(ValueError, match="'22'"):
            driver.create(NAME, self._params(rule), CREDENTIALS)

    def test_uppercase_protocol_is_rejected_naming_the_lowercase_form(self, driver):
        rule = {**minimal_rule(), "protocol": "TCP"}
        with pytest.raises(ValueError, match="'tcp'"):
            driver.create(NAME, self._params(rule), CREDENTIALS)

    def test_ports_all_is_rejected_naming_zero(self, driver):
        rule = {**minimal_rule(), "ports": "all"}
        with pytest.raises(ValueError, match="'0'"):
            driver.create(NAME, self._params(rule), CREDENTIALS)

    def test_missing_action_is_rejected(self, driver):
        rule = {k: v for k, v in minimal_rule().items() if k != "action"}
        with pytest.raises(ValueError, match="action"):
            driver.create(NAME, self._params(rule), CREDENTIALS)

    def test_missing_ports_is_rejected_even_for_icmp(self, driver):
        rule = {"protocol": "icmp", "action": "allow", "sources": {"addresses": ["0.0.0.0/0"]}}
        with pytest.raises(ValueError, match="ports"):
            driver.create(NAME, self._params(rule), CREDENTIALS)


class TestValidationRejectsFootguns:
    def test_zero_rules_is_rejected_locally(self, driver):
        with pytest.raises(ValueError, match="at least one rule"):
            driver.create(
                NAME,
                {"inbound_rules": [], "outbound_rules": [], "droplet_ids": [], "tags": []},
                CREDENTIALS,
            )

    def test_empty_sources_is_rejected(self, driver):
        rule = {**minimal_rule(), "sources": {}}
        with pytest.raises(ValueError, match="sources"):
            driver.create(
                NAME,
                {"inbound_rules": [rule], "outbound_rules": [], "droplet_ids": [], "tags": []},
                CREDENTIALS,
            )

    def test_unknown_top_level_key_is_rejected(self, driver):
        params = {**minimal_params(), "name": "oops"}
        with pytest.raises(ValueError, match="name"):
            driver.create(NAME, params, CREDENTIALS)

    def test_outbound_rule_requires_destinations_not_sources(self, driver):
        params = {
            "inbound_rules": [],
            "outbound_rules": [{**minimal_rule()}],
            "droplet_ids": [],
            "tags": [],
        }
        with pytest.raises(ValueError, match="destinations"):
            driver.create(NAME, params, CREDENTIALS)


class TestScalarListValidation:
    def test_droplet_ids_must_be_ints(self, driver):
        params = {**minimal_params(), "droplet_ids": ["123"]}
        with pytest.raises(ValueError, match="droplet_ids"):
            driver.create(NAME, params, CREDENTIALS)

    def test_a_bool_is_not_an_int_even_though_python_says_so(self, driver):
        params = {**minimal_params(), "droplet_ids": [True]}
        with pytest.raises(ValueError, match="droplet_ids"):
            driver.create(NAME, params, CREDENTIALS)

    def test_tags_must_be_strings(self, driver):
        params = {**minimal_params(), "tags": [7]}
        with pytest.raises(ValueError, match="tags"):
            driver.create(NAME, params, CREDENTIALS)


class TestNestedTargetValidation:
    """PARAM_SCHEMA is never enforced upstream, so these are the only
    guard against the int-port bug recurring inside a rule's target."""

    def _params(self, target):
        rule = {"protocol": "tcp", "ports": "22", "action": "allow", "sources": target}
        return {"inbound_rules": [rule], "outbound_rules": [], "droplet_ids": [], "tags": []}

    def test_a_string_droplet_id_inside_sources_is_rejected(self, driver):
        with pytest.raises(ValueError, match=r"sources\.droplet_ids\[0\]"):
            driver.create(NAME, self._params({"droplet_ids": ["123"]}), CREDENTIALS)

    def test_a_bool_droplet_id_inside_sources_is_rejected(self, driver):
        with pytest.raises(ValueError, match=r"sources\.droplet_ids\[0\]"):
            driver.create(NAME, self._params({"droplet_ids": [True]}), CREDENTIALS)

    def test_a_non_string_address_is_rejected(self, driver):
        with pytest.raises(ValueError, match=r"sources\.addresses\[0\]"):
            driver.create(NAME, self._params({"addresses": [1]}), CREDENTIALS)

    def test_an_empty_sub_list_is_rejected(self, driver):
        with pytest.raises(ValueError, match="matches no traffic"):
            driver.create(NAME, self._params({"addresses": []}), CREDENTIALS)

    def test_an_unknown_target_key_is_rejected(self, driver):
        with pytest.raises(ValueError, match="unsupported key"):
            driver.create(NAME, self._params({"nonsense": ["x"]}), CREDENTIALS)


class TestOutboundRules:
    def test_destinations_are_projected_for_outbound_rules(self, driver, fake_urlopen):
        outbound = {
            "protocol": "udp",
            "ports": "53",
            "action": "allow",
            "destinations": {"addresses": ["0.0.0.0/0"]},
        }
        payload = {"firewall": {**created_payload()["firewall"], "outbound_rules": [outbound]}}
        script_read(fake_urlopen, payload)
        assert driver.read(firewall_id(), CREDENTIALS)["outbound_rules"] == [outbound]

    def test_an_outbound_rule_written_with_sources_names_the_real_mistake(self, driver):
        params = {
            "inbound_rules": [],
            "outbound_rules": [minimal_rule()],
            "droplet_ids": [],
            "tags": [],
        }
        with pytest.raises(ValueError, match="must use 'destinations' instead"):
            driver.create(NAME, params, CREDENTIALS)


class TestErrorMessagesAreFoldedIn:
    """The 422 messages the spec advertises ("tag <name> does not exist",
    "droplet does not exist") are only useful if they reach the user."""

    def test_create_folds_the_digitalocean_message_into_the_error(self, driver, fake_urlopen):
        fake_urlopen.script(
            "POST",
            firewalls_url(),
            http_error(firewalls_url(), 422, {"message": "tag nope does not exist"}),
        )
        with pytest.raises(urllib.error.HTTPError, match="tag nope does not exist"):
            driver.create(NAME, minimal_params(), CREDENTIALS)

    def test_update_folds_the_digitalocean_message_into_the_error(self, driver, fake_urlopen):
        url = firewall_url(firewall_id())
        fake_urlopen.script("PUT", url, http_error(url, 422, {"message": "droplet does not exist"}))
        with pytest.raises(urllib.error.HTTPError, match="droplet does not exist"):
            driver.update(firewall_id(), driver_current(), minimal_params(), CREDENTIALS)

    def test_a_malformed_error_body_does_not_crash_error_handling(self, driver, fake_urlopen):
        fake_urlopen.script("POST", firewalls_url(), http_error(firewalls_url(), 500, None))
        with pytest.raises(urllib.error.HTTPError):
            driver.create(NAME, minimal_params(), CREDENTIALS)


class TestUpdateWithoutAName:
    def test_a_state_entry_missing_name_fails_before_mutating(self, driver, fake_urlopen):
        current = {k: v for k, v in driver_current().items() if k != "name"}
        with pytest.raises(ValueError, match="no 'name'"):
            driver.update(firewall_id(), current, minimal_params(), CREDENTIALS)
        assert fake_urlopen.calls == []


class TestLoggingUnderAConfiguredHandler:
    """`logger.info(..., extra=...)` only builds a LogRecord when a
    handler has the level enabled, so a reserved-attribute collision is
    invisible to a suite that never configures logging. It cost a live
    system-test failure once; this pins it at unit speed.

    `caplog.set_level` attaching a handler is what gives the test its
    teeth: stdlib `makeRecord` raises on a colliding `extra` key, so any
    key the driver adds later that shadows a LogRecord attribute fails
    this test at the `create()` call, before its own assertions run."""

    def test_create_logs_without_colliding_with_a_reserved_attribute(
        self, driver, fake_urlopen, caplog
    ):
        caplog.set_level("INFO", logger="aiform.driver.digitalocean.firewall")
        fake_urlopen.script("POST", firewalls_url(), FakeHTTPResponse(202, created_payload()))
        script_read(fake_urlopen)

        driver.create(NAME, minimal_params(), CREDENTIALS)

        record = next(r for r in caplog.records if r.name.endswith("digitalocean.firewall"))
        assert record.firewall_name == NAME
        assert record.id == firewall_id()


class TestRejectionsWithNoTranscriptBehindThem:
    """Two rejections that are conservative readings rather than observed
    behavior, so nothing in probes/ pins them: what DigitalOcean does with
    an empty `ports` is unprobed, and `action` is required by this driver
    rather than by DigitalOcean."""

    def test_an_empty_ports_string_is_rejected(self, driver, fake_urlopen):
        params = minimal_params()
        params["inbound_rules"][0]["ports"] = ""
        with pytest.raises(ValueError, match="'ports' is empty"):
            driver.create(NAME, params, CREDENTIALS)
        assert fake_urlopen.calls == []

    def test_the_missing_action_error_names_both_spellings(self, driver, fake_urlopen):
        # Probe 12 stored action="deny", so a hint naming only "allow"
        # would send a user writing a deny rule to the wrong value.
        params = minimal_params()
        del params["inbound_rules"][0]["action"]
        with pytest.raises(ValueError, match="allow.*deny"):
            driver.create(NAME, params, CREDENTIALS)
        assert fake_urlopen.calls == []


class TestNestedTargetListOrder:
    """`unordered_equal` is top-level only -- a rule is compared through
    `canonical_key()`, which serializes any list nested inside it
    positionally. So `UNORDERED_FIELDS` makes rule *order* free but does
    nothing for the order of `sources.addresses` inside a rule, and
    DigitalOcean is under no obligation to return one as written (its
    Terraform provider models all five target keys as sets)."""

    def test_a_reordered_nested_list_from_the_api_is_not_a_diff(self, driver, fake_urlopen):
        params = minimal_params()
        params["inbound_rules"][0]["sources"] = {"addresses": ["0.0.0.0/0", "10.0.0.0/8"]}
        payload = created_payload()
        payload["firewall"]["inbound_rules"][0]["sources"] = {
            "addresses": ["10.0.0.0/8", "0.0.0.0/0"]
        }
        fake_urlopen.script("GET", firewall_url(firewall_id()), FakeHTTPResponse(200, payload))

        current = driver.read(firewall_id(), CREDENTIALS)

        assert diff_attributes(current, params, unordered_fields=Driver.UNORDERED_FIELDS) == {}

    def test_an_unsorted_nested_list_is_rejected_naming_the_sorted_spelling(self, driver):
        params = minimal_params()
        params["inbound_rules"][0]["sources"] = {"addresses": ["10.0.0.0/8", "0.0.0.0/0"]}
        with pytest.raises(ValueError, match=r"sorted.*'0\.0\.0\.0/0'"):
            driver.create(NAME, params, CREDENTIALS)


class TestCreateRollsBackAfterTheResourceExists:
    """Between the POST and the return, the firewall is live but nothing
    has recorded its id -- so anything that raises there leaks a resource
    no state file knows about and no teardown can find. The live system
    test's first run did exactly that."""

    def test_a_failing_read_back_deletes_the_created_firewall(self, driver, fake_urlopen):
        fake_urlopen.script("POST", firewalls_url(), FakeHTTPResponse(202, created_payload()))
        url = firewall_url(firewall_id())
        fake_urlopen.script("GET", url, http_error(url, 500))
        fake_urlopen.script("DELETE", url, FakeHTTPResponse(204, None))

        with pytest.raises(urllib.error.HTTPError):
            driver.create(NAME, minimal_params(), CREDENTIALS)

        assert [c["method"] for c in fake_urlopen.calls] == ["POST", "GET", "DELETE"], (
            "a failure after the POST must roll the firewall back"
        )

    def test_a_failing_rollback_says_the_firewall_is_orphaned(self, driver, fake_urlopen):
        fake_urlopen.script("POST", firewalls_url(), FakeHTTPResponse(202, created_payload()))
        url = firewall_url(firewall_id())
        fake_urlopen.script("GET", url, http_error(url, 500))
        fake_urlopen.script("DELETE", url, http_error(url, 500, {"message": "firewall is in use"}))

        with pytest.raises(RuntimeError, match="orphaned") as excinfo:
            driver.create(NAME, minimal_params(), CREDENTIALS)
        # DigitalOcean's own reason survives into the orphan message.
        assert "firewall is in use" in str(excinfo.value)

    def test_a_rollback_swallows_a_404_because_delete_is_idempotent(self, driver, fake_urlopen):
        fake_urlopen.script("POST", firewalls_url(), FakeHTTPResponse(202, created_payload()))
        url = firewall_url(firewall_id())
        fake_urlopen.script("GET", url, http_error(url, 500))
        fake_urlopen.script("DELETE", url, http_error(url, 404))

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            driver.create(NAME, minimal_params(), CREDENTIALS)
        assert excinfo.value.code == 500

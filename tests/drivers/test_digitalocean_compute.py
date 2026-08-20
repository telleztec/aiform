import io
import json
import logging
import time
import urllib.error
import urllib.request
from email.message import Message

import pytest

from aiform.driver import DriverUpdateNotSupported
from aiform.exceptions import ResourceNotFoundError
from drivers.digitalocean.compute import Driver

BASE_URL = "https://api.digitalocean.com/v2"
CREDENTIALS = {"DIGITALOCEAN_TOKEN": "dop_v1_test"}
NAME = "telleztec-app-01"

BASE_PARAMS = {
    "region": "sfo3",
    "size": "s-1vcpu-2gb",
    "image": "ubuntu-24-04-x64",
}


def droplets_url() -> str:
    return f"{BASE_URL}/droplets"


def droplet_url(droplet_id: str) -> str:
    return f"{BASE_URL}/droplets/{droplet_id}"


def actions_url(droplet_id: str) -> str:
    return f"{BASE_URL}/droplets/{droplet_id}/actions"


def make_droplet(
    id=123,
    status="active",
    region="sfo3",
    size="s-1vcpu-2gb",
    image="ubuntu-24-04-x64",
    tags=None,
    monitoring_enabled=False,
    backups_enabled=False,
    public_ip="203.0.113.10",
    private_ip="10.0.0.5",
    include_features_key=True,
) -> dict:
    networks_v4 = []
    if private_ip:
        networks_v4.append({"ip_address": private_ip, "type": "private"})
    if public_ip:
        networks_v4.append({"ip_address": public_ip, "type": "public"})
    droplet = {
        "id": id,
        "status": status,
        "region": {"slug": region, "name": region},
        "size_slug": size,
        "image": {"slug": image, "name": image},
        "tags": tags if tags is not None else [],
        "networks": {"v4": networks_v4, "v6": []},
    }
    if include_features_key:
        features = []
        if backups_enabled:
            features.append("backups")
        if monitoring_enabled:
            features.append("monitoring")
        droplet["features"] = features
    return {"droplet": droplet}


def make_attrs(**overrides) -> dict:
    base = {
        "id": "123",
        "status": "active",
        "region": "sfo3",
        "size": "s-1vcpu-2gb",
        "image": "ubuntu-24-04-x64",
        "tags": ["aiform"],
        "ipv4_address": "203.0.113.10",
        "ssh_keys": ["key-1"],
        "backups": False,
        "monitoring": True,
    }
    base.update(overrides)
    return base


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
    item per call until only one item remains, which then repeats forever
    -- lets a test express either a fixed sequence of transitions (each
    consumed once) or an unchanging/never-transitioning poll target
    (a single-item script) without needing to know a driver's exact
    internal retry-attempt count.
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


def action_calls(fake: FakeUrlopen, droplet_id: str) -> list[dict]:
    url = actions_url(droplet_id)
    return [c for c in fake.calls if c["url"] == url and c["method"] == "POST"]


class TestCreate:
    def test_posts_to_droplets_endpoint_with_bearer_token(self, driver, fake_urlopen):
        fake_urlopen.script(
            "POST", droplets_url(), FakeHTTPResponse(202, make_droplet(id=555, status="new"))
        )
        fake_urlopen.script("GET", droplet_url("555"), FakeHTTPResponse(200, make_droplet(id=555)))

        driver.create(NAME, BASE_PARAMS, CREDENTIALS)

        call = fake_urlopen.calls[0]
        assert call["method"] == "POST"
        assert call["url"] == droplets_url()
        assert call["authorization"] == "Bearer dop_v1_test"
        assert call["content_type"] == "application/json"

    def test_request_body_includes_params(self, driver, fake_urlopen):
        params = {**BASE_PARAMS, "ssh_keys": ["key-1"], "backups": True, "tags": ["aiform"]}
        fake_urlopen.script(
            "POST", droplets_url(), FakeHTTPResponse(202, make_droplet(id=555, status="new"))
        )
        fake_urlopen.script("GET", droplet_url("555"), FakeHTTPResponse(200, make_droplet(id=555)))

        driver.create(NAME, params, CREDENTIALS)

        body = fake_urlopen.calls[0]["body"]
        assert body["name"] == NAME
        assert body["region"] == params["region"]
        assert body["size"] == params["size"]
        assert body["image"] == params["image"]
        assert body["ssh_keys"] == params["ssh_keys"]
        assert body["backups"] is True
        assert body["tags"] == params["tags"]

    def test_returns_flattened_attributes_from_the_converged_poll_response(
        self, driver, fake_urlopen
    ):
        # The initial POST response (still "new") is deliberately discarded
        # by create() -- the returned attributes must reflect the final,
        # converged GET, not DO's transient 202 body.
        fake_urlopen.script(
            "POST",
            droplets_url(),
            FakeHTTPResponse(
                202,
                make_droplet(
                    id=555,
                    status="new",
                    region="sfo3",
                    size="s-1vcpu-2gb",
                    image="ubuntu-24-04-x64",
                    tags=["aiform"],
                    public_ip=None,
                    private_ip=None,
                ),
            ),
        )
        fake_urlopen.script(
            "GET",
            droplet_url("555"),
            FakeHTTPResponse(
                200,
                make_droplet(
                    id=555,
                    status="active",
                    region="sfo3",
                    size="s-1vcpu-2gb",
                    image="ubuntu-24-04-x64",
                    tags=["aiform"],
                    public_ip=None,
                    private_ip=None,
                ),
            ),
        )

        result = driver.create(NAME, BASE_PARAMS, CREDENTIALS)

        assert result["id"] == "555"
        assert result["status"] == "active"
        assert result["region"] == "sfo3"
        assert result["size"] == "s-1vcpu-2gb"
        assert result["image"] == "ubuntu-24-04-x64"
        assert result["tags"] == ["aiform"]
        assert result["ipv4_address"] is None

    def test_ipv4_address_extracted_once_assigned_during_polling(self, driver, fake_urlopen):
        # A real public IP is often not yet assigned on DO's initial 202 --
        # it shows up once the droplet finishes provisioning.
        fake_urlopen.script(
            "POST",
            droplets_url(),
            FakeHTTPResponse(202, make_droplet(id=555, status="new", public_ip=None)),
        )
        fake_urlopen.script(
            "GET",
            droplet_url("555"),
            FakeHTTPResponse(
                200,
                make_droplet(
                    id=555, status="active", public_ip="203.0.113.10", private_ip="10.0.0.5"
                ),
            ),
        )

        result = driver.create(NAME, BASE_PARAMS, CREDENTIALS)

        assert result["ipv4_address"] == "203.0.113.10"

    def test_ipv4_address_is_none_when_only_a_private_network_entry_exists(
        self, driver, fake_urlopen
    ):
        fake_urlopen.script(
            "POST",
            droplets_url(),
            FakeHTTPResponse(202, make_droplet(id=555, status="new", public_ip=None)),
        )
        fake_urlopen.script(
            "GET",
            droplet_url("555"),
            FakeHTTPResponse(
                200,
                make_droplet(id=555, status="active", public_ip=None, private_ip="10.0.0.5"),
            ),
        )

        result = driver.create(NAME, BASE_PARAMS, CREDENTIALS)

        assert result["ipv4_address"] is None

    def test_echoes_ssh_keys_backups_monitoring_from_params(self, driver, fake_urlopen):
        params = {
            **BASE_PARAMS,
            "ssh_keys": ["juan-macbook-ed25519"],
            "backups": True,
            "monitoring": True,
        }
        fake_urlopen.script(
            "POST", droplets_url(), FakeHTTPResponse(202, make_droplet(id=555, status="new"))
        )
        fake_urlopen.script("GET", droplet_url("555"), FakeHTTPResponse(200, make_droplet(id=555)))

        result = driver.create(NAME, params, CREDENTIALS)

        assert result["ssh_keys"] == ["juan-macbook-ed25519"]
        assert result["backups"] is True
        assert result["monitoring"] is True

    def test_defaults_missing_optional_params(self, driver, fake_urlopen):
        fake_urlopen.script(
            "POST", droplets_url(), FakeHTTPResponse(202, make_droplet(id=555, status="new"))
        )
        fake_urlopen.script("GET", droplet_url("555"), FakeHTTPResponse(200, make_droplet(id=555)))

        result = driver.create(NAME, BASE_PARAMS, CREDENTIALS)

        assert result["ssh_keys"] == []
        assert result["backups"] is False
        assert result["monitoring"] is False

    def test_makes_exactly_one_post_call(self, driver, fake_urlopen):
        fake_urlopen.script(
            "POST", droplets_url(), FakeHTTPResponse(202, make_droplet(id=555, status="new"))
        )
        fake_urlopen.script("GET", droplet_url("555"), FakeHTTPResponse(200, make_droplet(id=555)))

        driver.create(NAME, BASE_PARAMS, CREDENTIALS)

        post_calls = [c for c in fake_urlopen.calls if c["method"] == "POST"]
        assert len(post_calls) == 1


class TestCreatePollsUntilActive:
    def test_polls_the_new_droplet_until_active_before_returning(self, driver, fake_urlopen):
        fake_urlopen.script(
            "POST", droplets_url(), FakeHTTPResponse(202, make_droplet(id=555, status="new"))
        )
        fake_urlopen.script(
            "GET",
            droplet_url("555"),
            FakeHTTPResponse(200, make_droplet(id=555, status="new")),
            FakeHTTPResponse(200, make_droplet(id=555, status="new")),
            FakeHTTPResponse(200, make_droplet(id=555, status="active")),
        )

        result = driver.create(NAME, BASE_PARAMS, CREDENTIALS)

        get_calls = [c for c in fake_urlopen.calls if c["method"] == "GET"]
        assert len(get_calls) == 3
        assert result["status"] == "active"

    def test_poll_timeout_raises_timeout_error_naming_id(self, driver, fake_urlopen):
        fake_urlopen.script(
            "POST", droplets_url(), FakeHTTPResponse(202, make_droplet(id=555, status="new"))
        )
        # Never transitions away from "new" -- the create poll can't succeed.
        fake_urlopen.script(
            "GET", droplet_url("555"), FakeHTTPResponse(200, make_droplet(id=555, status="new"))
        )

        with pytest.raises(TimeoutError) as excinfo:
            driver.create(NAME, BASE_PARAMS, CREDENTIALS)

        assert "555" in str(excinfo.value)


class TestRead:
    def test_gets_droplet_by_id(self, driver, fake_urlopen):
        fake_urlopen.script("GET", droplet_url("123"), FakeHTTPResponse(200, make_droplet()))

        driver.read("123", CREDENTIALS)

        assert fake_urlopen.calls[0]["method"] == "GET"
        assert fake_urlopen.calls[0]["url"] == droplet_url("123")
        assert fake_urlopen.calls[0]["authorization"] == "Bearer dop_v1_test"

    def test_returns_flattened_attributes(self, driver, fake_urlopen):
        fake_urlopen.script(
            "GET",
            droplet_url("123"),
            FakeHTTPResponse(
                200,
                make_droplet(
                    id=123,
                    status="active",
                    region="sfo3",
                    size="s-1vcpu-2gb",
                    image="ubuntu-24-04-x64",
                    tags=["aiform"],
                    public_ip="203.0.113.10",
                ),
            ),
        )

        result = driver.read("123", CREDENTIALS)

        assert result["id"] == "123"
        assert result["status"] == "active"
        assert result["region"] == "sfo3"
        assert result["size"] == "s-1vcpu-2gb"
        assert result["image"] == "ubuntu-24-04-x64"
        assert result["tags"] == ["aiform"]
        assert result["ipv4_address"] == "203.0.113.10"

    def test_monitoring_recovered_from_features(self, driver, fake_urlopen):
        fake_urlopen.script(
            "GET", droplet_url("123"), FakeHTTPResponse(200, make_droplet(monitoring_enabled=True))
        )

        result = driver.read("123", CREDENTIALS)

        assert result["monitoring"] is True

    def test_monitoring_false_when_absent_from_features(self, driver, fake_urlopen):
        fake_urlopen.script(
            "GET",
            droplet_url("123"),
            FakeHTTPResponse(200, make_droplet(monitoring_enabled=False)),
        )

        result = driver.read("123", CREDENTIALS)

        assert result["monitoring"] is False

    def test_missing_features_key_does_not_raise(self, driver, fake_urlopen):
        fake_urlopen.script(
            "GET",
            droplet_url("123"),
            FakeHTTPResponse(200, make_droplet(include_features_key=False)),
        )

        result = driver.read("123", CREDENTIALS)

        assert result["monitoring"] is False

    def test_ssh_keys_is_not_included(self, driver, fake_urlopen):
        fake_urlopen.script("GET", droplet_url("123"), FakeHTTPResponse(200, make_droplet()))

        result = driver.read("123", CREDENTIALS)

        assert "ssh_keys" not in result

    def test_backups_recovered_from_features(self, driver, fake_urlopen):
        fake_urlopen.script(
            "GET", droplet_url("123"), FakeHTTPResponse(200, make_droplet(backups_enabled=True))
        )

        result = driver.read("123", CREDENTIALS)

        assert result["backups"] is True

    def test_backups_false_when_absent_from_features(self, driver, fake_urlopen):
        fake_urlopen.script(
            "GET", droplet_url("123"), FakeHTTPResponse(200, make_droplet(backups_enabled=False))
        )

        result = driver.read("123", CREDENTIALS)

        assert result["backups"] is False

    def test_missing_features_key_backups_does_not_raise(self, driver, fake_urlopen):
        fake_urlopen.script(
            "GET",
            droplet_url("123"),
            FakeHTTPResponse(200, make_droplet(include_features_key=False)),
        )

        result = driver.read("123", CREDENTIALS)

        assert result["backups"] is False

    def test_404_raises_resource_not_found_error_naming_id(self, driver, fake_urlopen):
        fake_urlopen.script("GET", droplet_url("123"), http_error(droplet_url("123"), 404))

        with pytest.raises(ResourceNotFoundError) as excinfo:
            driver.read("123", CREDENTIALS)

        assert "123" in str(excinfo.value)
        assert len(fake_urlopen.calls) == 1

    def test_makes_exactly_one_api_call_on_success(self, driver, fake_urlopen):
        fake_urlopen.script("GET", droplet_url("123"), FakeHTTPResponse(200, make_droplet()))

        driver.read("123", CREDENTIALS)

        assert len(fake_urlopen.calls) == 1


class TestDelete:
    def test_deletes_droplet_by_id(self, driver, fake_urlopen):
        fake_urlopen.script("DELETE", droplet_url("123"), FakeHTTPResponse(204, None))

        driver.delete("123", CREDENTIALS)

        assert fake_urlopen.calls[0]["method"] == "DELETE"
        assert fake_urlopen.calls[0]["url"] == droplet_url("123")
        assert fake_urlopen.calls[0]["authorization"] == "Bearer dop_v1_test"

    def test_204_returns_none(self, driver, fake_urlopen):
        fake_urlopen.script("DELETE", droplet_url("123"), FakeHTTPResponse(204, None))

        result = driver.delete("123", CREDENTIALS)

        assert result is None

    def test_404_is_idempotent_success(self, driver, fake_urlopen):
        fake_urlopen.script("DELETE", droplet_url("123"), http_error(droplet_url("123"), 404))

        result = driver.delete("123", CREDENTIALS)

        assert result is None
        assert len(fake_urlopen.calls) == 1

    def test_makes_exactly_one_api_call(self, driver, fake_urlopen):
        fake_urlopen.script("DELETE", droplet_url("123"), FakeHTTPResponse(204, None))

        driver.delete("123", CREDENTIALS)

        assert len(fake_urlopen.calls) == 1


class TestUpdateRejectsNonSizeDiffs:
    @pytest.mark.parametrize(
        "field,value",
        [
            ("region", "nyc3"),
            ("image", "ubuntu-22-04-x64"),
            ("ssh_keys", ["a-different-key"]),
            ("backups", True),
            ("monitoring", False),
            ("tags", ["something-else"]),
        ],
    )
    def test_non_size_field_change_raises_unsupported(self, driver, fake_urlopen, field, value):
        current = make_attrs()
        desired = make_attrs(**{field: value})

        with pytest.raises(DriverUpdateNotSupported) as excinfo:
            driver.update("123", current, desired, CREDENTIALS)

        assert field in excinfo.value.unsupported_fields

    def test_size_plus_another_field_changing_together_is_unsupported(self, driver, fake_urlopen):
        current = make_attrs()
        desired = make_attrs(size="s-2vcpu-4gb", region="nyc3")

        with pytest.raises(DriverUpdateNotSupported) as excinfo:
            driver.update("123", current, desired, CREDENTIALS)

        assert "region" in excinfo.value.unsupported_fields

    def test_rejecting_a_non_size_diff_makes_no_api_calls(self, driver, fake_urlopen):
        current = make_attrs()
        desired = make_attrs(image="ubuntu-22-04-x64")

        with pytest.raises(DriverUpdateNotSupported):
            driver.update("123", current, desired, CREDENTIALS)

        assert fake_urlopen.calls == []

    def test_no_diff_at_all_does_not_raise_or_call(self, driver, fake_urlopen):
        current = make_attrs()
        desired = make_attrs()

        # No size change and nothing else changed either -- update() isn't
        # expected to be called with a true no-op by the orchestrator, but
        # it shouldn't explode if it is.
        driver.update("123", current, desired, CREDENTIALS)


class TestUpdateUnmodeledStatus:
    @pytest.mark.parametrize("status", ["new", "archive"])
    def test_resize_from_unmodeled_status_raises_unsupported_naming_size(
        self, driver, fake_urlopen, status
    ):
        current = make_attrs(status=status)
        desired = make_attrs(size="s-2vcpu-4gb")

        with pytest.raises(DriverUpdateNotSupported) as excinfo:
            driver.update("123", current, desired, CREDENTIALS)

        assert "size" in excinfo.value.unsupported_fields
        assert fake_urlopen.calls == []


class TestUpdateResizeInPlace:
    def test_resize_from_active_powers_off_then_resizes_then_powers_on(self, driver, fake_urlopen):
        current = make_attrs(status="active", size="s-1vcpu-2gb")
        desired = make_attrs(size="s-2vcpu-4gb")

        fake_urlopen.script(
            "POST",
            actions_url("123"),
            FakeHTTPResponse(201, {"action": {"id": 1, "status": "in-progress"}}),
            FakeHTTPResponse(201, {"action": {"id": 2, "status": "in-progress"}}),
            FakeHTTPResponse(201, {"action": {"id": 3, "status": "in-progress"}}),
        )
        fake_urlopen.script(
            "GET",
            droplet_url("123"),
            FakeHTTPResponse(200, make_droplet(status="off", size="s-1vcpu-2gb")),
            FakeHTTPResponse(200, make_droplet(status="off", size="s-2vcpu-4gb")),
            FakeHTTPResponse(200, make_droplet(status="active", size="s-2vcpu-4gb")),
        )

        driver.update("123", current, desired, CREDENTIALS)

        calls = action_calls(fake_urlopen, "123")
        types = [c["body"]["type"] for c in calls]
        assert types == ["power_off", "resize", "power_on"]

    def test_resize_body_uses_disk_false(self, driver, fake_urlopen):
        current = make_attrs(status="off", size="s-1vcpu-2gb")
        desired = make_attrs(size="s-2vcpu-4gb")

        fake_urlopen.script(
            "POST",
            actions_url("123"),
            FakeHTTPResponse(201, {"action": {"id": 1, "status": "in-progress"}}),
            FakeHTTPResponse(201, {"action": {"id": 2, "status": "in-progress"}}),
        )
        fake_urlopen.script(
            "GET",
            droplet_url("123"),
            FakeHTTPResponse(200, make_droplet(status="off", size="s-2vcpu-4gb")),
            FakeHTTPResponse(200, make_droplet(status="active", size="s-2vcpu-4gb")),
        )

        driver.update("123", current, desired, CREDENTIALS)

        resize_call = next(
            c for c in action_calls(fake_urlopen, "123") if c["body"]["type"] == "resize"
        )
        assert resize_call["body"]["disk"] is False
        assert resize_call["body"]["size"] == "s-2vcpu-4gb"
        assert resize_call["content_type"] == "application/json"

    def test_resize_from_off_skips_power_off(self, driver, fake_urlopen):
        current = make_attrs(status="off", size="s-1vcpu-2gb")
        desired = make_attrs(size="s-2vcpu-4gb")

        fake_urlopen.script(
            "POST",
            actions_url("123"),
            FakeHTTPResponse(201, {"action": {"id": 1, "status": "in-progress"}}),
            FakeHTTPResponse(201, {"action": {"id": 2, "status": "in-progress"}}),
        )
        fake_urlopen.script(
            "GET",
            droplet_url("123"),
            FakeHTTPResponse(200, make_droplet(status="off", size="s-2vcpu-4gb")),
            FakeHTTPResponse(200, make_droplet(status="active", size="s-2vcpu-4gb")),
        )

        driver.update("123", current, desired, CREDENTIALS)

        types = [c["body"]["type"] for c in action_calls(fake_urlopen, "123")]
        assert "power_off" not in types
        assert types == ["resize", "power_on"]

    def test_successful_resize_returns_attributes_echoed_from_desired(self, driver, fake_urlopen):
        current = make_attrs(status="active", size="s-1vcpu-2gb")
        desired = make_attrs(size="s-2vcpu-4gb", ssh_keys=["key-1"], backups=False, monitoring=True)

        fake_urlopen.script(
            "POST",
            actions_url("123"),
            FakeHTTPResponse(201, {"action": {"id": 1, "status": "in-progress"}}),
            FakeHTTPResponse(201, {"action": {"id": 2, "status": "in-progress"}}),
            FakeHTTPResponse(201, {"action": {"id": 3, "status": "in-progress"}}),
        )
        # Live GET reports monitoring=False the whole way through, deliberately
        # the opposite of `desired`'s monitoring=True -- if update() ever
        # returns a bare read()-shaped dict instead of echoing ssh_keys/
        # backups/monitoring from `desired`, this must fail.
        fake_urlopen.script(
            "GET",
            droplet_url("123"),
            FakeHTTPResponse(
                200, make_droplet(status="off", size="s-1vcpu-2gb", monitoring_enabled=False)
            ),
            FakeHTTPResponse(
                200, make_droplet(status="off", size="s-2vcpu-4gb", monitoring_enabled=False)
            ),
            FakeHTTPResponse(
                200, make_droplet(status="active", size="s-2vcpu-4gb", monitoring_enabled=False)
            ),
        )

        result = driver.update("123", current, desired, CREDENTIALS)

        assert result["size"] == "s-2vcpu-4gb"
        assert result["status"] == "active"
        assert result["ssh_keys"] == ["key-1"]
        assert result["backups"] is False
        assert result["monitoring"] is True

    def test_optional_fields_omitted_from_desired_are_not_a_diff_and_are_preserved(
        self, driver, fake_urlopen
    ):
        current = make_attrs(status="off", size="s-1vcpu-2gb", tags=["aiform"], backups=True)
        desired = {
            k: v for k, v in make_attrs(size="s-2vcpu-4gb").items() if k not in ("tags", "backups")
        }

        fake_urlopen.script(
            "POST",
            actions_url("123"),
            FakeHTTPResponse(201, {"action": {"id": 1, "status": "in-progress"}}),
            FakeHTTPResponse(201, {"action": {"id": 2, "status": "in-progress"}}),
        )
        fake_urlopen.script(
            "GET",
            droplet_url("123"),
            FakeHTTPResponse(200, make_droplet(status="off", size="s-2vcpu-4gb")),
            FakeHTTPResponse(200, make_droplet(status="active", size="s-2vcpu-4gb")),
        )

        # `desired` doesn't mention "tags" or "backups" (the user's aiform.md
        # never set them) -- that must not be treated as wanting them
        # changed (which would force an unnecessary destroy+recreate for
        # what should be a safe in-place resize), and once the resize
        # succeeds, the returned attrs must preserve `current`'s value for
        # the omitted "backups" field rather than resetting it to a bare
        # default.
        result = driver.update("123", current, desired, CREDENTIALS)

        assert result["backups"] is True

    def test_resize_rejected_powers_back_on_before_raising(self, driver, fake_urlopen):
        current = make_attrs(status="active", size="s-1vcpu-2gb")
        desired = make_attrs(size="s-2vcpu-4gb")

        fake_urlopen.script(
            "POST",
            actions_url("123"),
            FakeHTTPResponse(201, {"action": {"id": 1, "status": "in-progress"}}),  # power_off
            http_error(actions_url("123"), 422, {"message": "disk size cannot be decreased"}),
            FakeHTTPResponse(201, {"action": {"id": 3, "status": "in-progress"}}),  # power_on
        )
        fake_urlopen.script(
            "GET",
            droplet_url("123"),
            FakeHTTPResponse(200, make_droplet(status="off", size="s-1vcpu-2gb")),
            FakeHTTPResponse(200, make_droplet(status="active", size="s-1vcpu-2gb")),
        )

        with pytest.raises(DriverUpdateNotSupported) as excinfo:
            driver.update("123", current, desired, CREDENTIALS)

        assert "size" in excinfo.value.unsupported_fields
        assert "disk size cannot be decreased" in str(excinfo.value)
        types = [c["body"]["type"] for c in action_calls(fake_urlopen, "123")]
        assert types == ["power_off", "resize", "power_on"]

    def test_resize_rejected_from_off_does_not_power_on(self, driver, fake_urlopen):
        # A droplet that started "off" (the user's own choice) is left off
        # on a rejected resize -- it isn't powered on as a side effect of a
        # failure it never asked for, unlike the from-"active" case above.
        current = make_attrs(status="off", size="s-1vcpu-2gb")
        desired = make_attrs(size="s-2vcpu-4gb")

        fake_urlopen.script(
            "POST",
            actions_url("123"),
            http_error(actions_url("123"), 422, {"message": "disk size cannot be decreased"}),
        )

        with pytest.raises(DriverUpdateNotSupported) as excinfo:
            driver.update("123", current, desired, CREDENTIALS)

        assert "size" in excinfo.value.unsupported_fields
        types = [c["body"]["type"] for c in action_calls(fake_urlopen, "123")]
        assert types == ["resize"]
        assert fake_urlopen.calls == action_calls(fake_urlopen, "123")

    def test_resize_rejected_with_no_body_omits_message_suffix(self, driver, fake_urlopen):
        # No DO JSON body to extract a message from -- the reason string
        # must not append a bare ": <HTTP reason phrase>" suffix just
        # because exc.msg always has *some* value. A fix for an earlier
        # /code-review finding (reuse the already-enriched exc.msg
        # instead of re-extracting) accidentally made this unconditional;
        # caught by a second /code-review pass.
        current = make_attrs(status="off", size="s-1vcpu-2gb")
        desired = make_attrs(size="s-2vcpu-4gb")

        fake_urlopen.script(
            "POST",
            actions_url("123"),
            http_error(actions_url("123"), 422, body=None),
        )

        with pytest.raises(DriverUpdateNotSupported) as excinfo:
            driver.update("123", current, desired, CREDENTIALS)

        message = str(excinfo.value)
        assert message == "DigitalOcean rejected an in-place resize of droplet 123 to 's-2vcpu-4gb'"
        assert ":" not in message

    def test_resize_transient_error_powers_back_on_then_reraises(self, driver, fake_urlopen):
        # A 429/5xx/401 isn't DO telling us the resize itself is invalid --
        # it's a transient or unrelated CSP failure. Misclassifying it as
        # DriverUpdateNotSupported would trigger a destructive
        # destroy+recreate for a resize that might have succeeded on
        # retry. Caught by /code-review (gate #1).
        current = make_attrs(status="active", size="s-1vcpu-2gb")
        desired = make_attrs(size="s-2vcpu-4gb")

        fake_urlopen.script(
            "POST",
            actions_url("123"),
            FakeHTTPResponse(201, {"action": {"id": 1, "status": "in-progress"}}),  # power_off
            http_error(actions_url("123"), 429, {"message": "too many requests"}),
            FakeHTTPResponse(201, {"action": {"id": 3, "status": "in-progress"}}),  # power_on
        )
        fake_urlopen.script(
            "GET",
            droplet_url("123"),
            FakeHTTPResponse(200, make_droplet(status="off", size="s-1vcpu-2gb")),
            FakeHTTPResponse(200, make_droplet(status="active", size="s-1vcpu-2gb")),
        )

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            driver.update("123", current, desired, CREDENTIALS)

        assert excinfo.value.code == 429
        # DO's own diagnostic message is folded into the re-raised
        # HTTPError's .msg, not silently dropped -- caught by /code-review.
        assert "too many requests" in str(excinfo.value)
        types = [c["body"]["type"] for c in action_calls(fake_urlopen, "123")]
        assert types == ["power_off", "resize", "power_on"]

    def test_resize_compounding_failure_raises_runtime_error_not_masked(self, driver, fake_urlopen):
        # If the power-on restore call itself fails after the resize
        # already failed, the restore's own exception must not silently
        # replace/mask the original resize failure -- caught by
        # /code-review.
        current = make_attrs(status="active", size="s-1vcpu-2gb")
        desired = make_attrs(size="s-2vcpu-4gb")

        fake_urlopen.script(
            "POST",
            actions_url("123"),
            FakeHTTPResponse(201, {"action": {"id": 1, "status": "in-progress"}}),  # power_off
            http_error(actions_url("123"), 429, {"message": "too many requests"}),
            http_error(actions_url("123"), 503, {"message": "service unavailable"}),  # power_on
        )
        fake_urlopen.script(
            "GET",
            droplet_url("123"),
            FakeHTTPResponse(200, make_droplet(status="off", size="s-1vcpu-2gb")),
        )

        with pytest.raises(RuntimeError) as excinfo:
            driver.update("123", current, desired, CREDENTIALS)

        message = str(excinfo.value)
        assert "123" in message
        assert "too many requests" in message
        assert "service unavailable" in message
        assert excinfo.value.__cause__ is not None
        assert excinfo.value.__cause__.code == 429

    def test_resize_restore_unexpected_exception_type_propagates_unwrapped(
        self, driver, fake_urlopen
    ):
        # The restore-after-failure except clause is scoped to
        # (URLError, TimeoutError, http.client.HTTPException, OSError,
        # JSONDecodeError) -- matching tests/system/conftest.py's
        # wait_until_droplet_gone() for the identical urlopen/read/
        # json.loads call shape (fc2dd1d) -- not bare Exception. A
        # genuinely unexpected exception type (anything else) must
        # propagate immediately, not get folded into the generic
        # "restore also failed" RuntimeError, which would make an
        # unrelated bug harder to distinguish from a real DO-API
        # restore failure. Caught by /code-review.
        current = make_attrs(status="active", size="s-1vcpu-2gb")
        desired = make_attrs(size="s-2vcpu-4gb")

        fake_urlopen.script(
            "POST",
            actions_url("123"),
            FakeHTTPResponse(201, {"action": {"id": 1, "status": "in-progress"}}),  # power_off
            http_error(actions_url("123"), 429, {"message": "too many requests"}),
            ValueError("something unrelated broke"),  # power_on
        )
        fake_urlopen.script(
            "GET",
            droplet_url("123"),
            FakeHTTPResponse(200, make_droplet(status="off", size="s-1vcpu-2gb")),
        )

        with pytest.raises(ValueError, match="something unrelated broke"):
            driver.update("123", current, desired, CREDENTIALS)

    def test_resize_restore_url_error_is_folded_into_compounding_failure(
        self, driver, fake_urlopen
    ):
        # URLError, http.client.HTTPException, OSError, and
        # JSONDecodeError all propagate unwrapped from the same
        # urlopen/read/json.loads shape _request() uses (established
        # against this same DO API in fc2dd1d) -- an earlier version of
        # this except clause only caught (HTTPError, TimeoutError),
        # which would have let a restore-time URLError silently lose
        # the original resize failure's context, same as the bare
        # except Exception this whole fix replaced. Caught by
        # /code-review (second pass).
        current = make_attrs(status="active", size="s-1vcpu-2gb")
        desired = make_attrs(size="s-2vcpu-4gb")

        fake_urlopen.script(
            "POST",
            actions_url("123"),
            FakeHTTPResponse(201, {"action": {"id": 1, "status": "in-progress"}}),  # power_off
            http_error(actions_url("123"), 429, {"message": "too many requests"}),
            urllib.error.URLError("connection refused"),  # power_on
        )
        fake_urlopen.script(
            "GET",
            droplet_url("123"),
            FakeHTTPResponse(200, make_droplet(status="off", size="s-1vcpu-2gb")),
        )

        with pytest.raises(RuntimeError) as excinfo:
            driver.update("123", current, desired, CREDENTIALS)

        message = str(excinfo.value)
        assert "123" in message
        assert "too many requests" in message
        assert "connection refused" in message
        assert excinfo.value.__cause__ is not None
        assert excinfo.value.__cause__.code == 429

    def test_resize_server_error_reraises_not_unsupported(self, driver, fake_urlopen):
        current = make_attrs(status="off", size="s-1vcpu-2gb")
        desired = make_attrs(size="s-2vcpu-4gb")

        fake_urlopen.script(
            "POST",
            actions_url("123"),
            http_error(actions_url("123"), 500, {"message": "internal error"}),
        )

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            driver.update("123", current, desired, CREDENTIALS)

        assert excinfo.value.code == 500
        assert "internal error" in str(excinfo.value)
        # Started "off" -- no power-off/power-on calls should have happened.
        types = [c["body"]["type"] for c in action_calls(fake_urlopen, "123")]
        assert types == ["resize"]

    def test_resize_with_falsy_size_value_in_desired_raises_unsupported(self, driver, fake_urlopen):
        # `size` present with a falsy value (e.g. an explicit `size:` with no
        # value in aiform.md's YAML, parsed as None) is a different scenario
        # from `size` being absent from `desired` entirely -- the latter is
        # unreachable in production, since `size` is PARAM_SCHEMA-required
        # and the orchestrator validates `params` against that schema before
        # update() is ever called, and is therefore no longer diffed at all
        # (an absent optional key is never part of the diff -- see
        # diff_fields' scoping to desired's own keys). This scenario, by
        # contrast, still produces a real "size" diff entry, so it still
        # needs to hit the target_size guard below.
        current = make_attrs(status="active", size="s-1vcpu-2gb")
        desired = make_attrs(size=None)

        with pytest.raises(DriverUpdateNotSupported) as excinfo:
            driver.update("123", current, desired, CREDENTIALS)

        assert "size" in excinfo.value.unsupported_fields
        assert fake_urlopen.calls == []

    def test_power_off_poll_timeout_raises_timeout_error_naming_id(self, driver, fake_urlopen):
        current = make_attrs(status="active", size="s-1vcpu-2gb")
        desired = make_attrs(size="s-2vcpu-4gb")

        fake_urlopen.script(
            "POST",
            actions_url("123"),
            FakeHTTPResponse(201, {"action": {"id": 1, "status": "in-progress"}}),
        )
        # Never transitions away from "active" -- power-off poll can't succeed.
        fake_urlopen.script(
            "GET", droplet_url("123"), FakeHTTPResponse(200, make_droplet(status="active"))
        )

        with pytest.raises(TimeoutError) as excinfo:
            driver.update("123", current, desired, CREDENTIALS)

        assert "123" in str(excinfo.value)

    def test_resize_poll_timeout_raises_timeout_error_naming_id(self, driver, fake_urlopen):
        current = make_attrs(status="off", size="s-1vcpu-2gb")
        desired = make_attrs(size="s-2vcpu-4gb")

        fake_urlopen.script(
            "POST",
            actions_url("123"),
            FakeHTTPResponse(201, {"action": {"id": 1, "status": "in-progress"}}),
        )
        # size_slug never changes to the target -- resize-completion poll
        # can't succeed.
        fake_urlopen.script(
            "GET",
            droplet_url("123"),
            FakeHTTPResponse(200, make_droplet(status="off", size="s-1vcpu-2gb")),
        )

        with pytest.raises(TimeoutError) as excinfo:
            driver.update("123", current, desired, CREDENTIALS)

        assert "123" in str(excinfo.value)
        types = [c["body"]["type"] for c in action_calls(fake_urlopen, "123")]
        assert "power_on" not in types


class TestLogging:
    def test_logger_is_a_real_descendant_of_the_aiform_logger(self):
        # The actual hazard this whole class guards against: load_driver()
        # execs this file with a synthetic module name that is NOT a
        # dotted descendant of "aiform" -- logging.getLogger(__name__)
        # would silently produce a logger aiform/log.py's configure()
        # never attaches a handler to. Verified structurally here, not
        # just by trusting the module-level logger's literal string.
        from drivers.digitalocean.compute import logger as driver_logger

        assert driver_logger.name == "aiform.driver.digitalocean.compute"
        node = driver_logger
        while node.parent is not None:
            node = node.parent
            if node.name == "aiform":
                return
        pytest.fail("driver logger is not a descendant of the 'aiform' logger")

    def test_poll_success_logs_step_attempts_and_outcome(self, driver, fake_urlopen, caplog):
        caplog.set_level("INFO", logger="aiform.driver.digitalocean.compute")
        current = make_attrs(status="off", size="s-1vcpu-2gb")
        desired = make_attrs(size="s-2vcpu-4gb")

        fake_urlopen.script(
            "POST",
            actions_url("123"),
            FakeHTTPResponse(201, {"action": {"id": 1, "status": "in-progress"}}),
            FakeHTTPResponse(201, {"action": {"id": 2, "status": "in-progress"}}),
        )
        fake_urlopen.script(
            "GET",
            droplet_url("123"),
            FakeHTTPResponse(200, make_droplet(status="off", size="s-1vcpu-2gb")),
            FakeHTTPResponse(200, make_droplet(status="off", size="s-2vcpu-4gb")),
            FakeHTTPResponse(200, make_droplet(status="active", size="s-2vcpu-4gb")),
        )

        driver.update("123", current, desired, CREDENTIALS)

        resize_step = next(r for r in caplog.records if getattr(r, "step", None) == "resize")
        assert resize_step.outcome == "success"
        assert resize_step.attempts_used == 2
        assert resize_step.id == "123"
        assert resize_step.levelno == logging.INFO

    def test_poll_timeout_logs_before_raising(self, driver, fake_urlopen, caplog):
        caplog.set_level("INFO", logger="aiform.driver.digitalocean.compute")
        current = make_attrs(status="active", size="s-1vcpu-2gb")
        desired = make_attrs(size="s-2vcpu-4gb")

        fake_urlopen.script(
            "POST",
            actions_url("123"),
            FakeHTTPResponse(201, {"action": {"id": 1, "status": "in-progress"}}),
        )
        fake_urlopen.script(
            "GET", droplet_url("123"), FakeHTTPResponse(200, make_droplet(status="active"))
        )

        with pytest.raises(TimeoutError):
            driver.update("123", current, desired, CREDENTIALS)

        record = next(r for r in caplog.records if getattr(r, "step", None) == "power-off")
        assert record.outcome == "timeout"
        assert record.attempts_used == 30
        assert record.levelno == logging.ERROR

    def test_entering_resize_logs_current_and_target_context(self, driver, fake_urlopen, caplog):
        caplog.set_level("INFO", logger="aiform.driver.digitalocean.compute")
        current = make_attrs(status="off", size="s-1vcpu-2gb")
        desired = make_attrs(size="s-2vcpu-4gb")

        fake_urlopen.script(
            "POST",
            actions_url("123"),
            FakeHTTPResponse(201, {"action": {"id": 1, "status": "in-progress"}}),
            FakeHTTPResponse(201, {"action": {"id": 2, "status": "in-progress"}}),
        )
        fake_urlopen.script(
            "GET",
            droplet_url("123"),
            FakeHTTPResponse(200, make_droplet(status="off", size="s-2vcpu-4gb")),
            FakeHTTPResponse(200, make_droplet(status="active", size="s-2vcpu-4gb")),
        )

        driver.update("123", current, desired, CREDENTIALS)

        record = next(r for r in caplog.records if getattr(r, "current_size", None) is not None)
        assert record.status == "off"
        assert record.current_size == "s-1vcpu-2gb"
        assert record.target_size == "s-2vcpu-4gb"

    def test_resize_rejection_logs_http_status_and_do_message(self, driver, fake_urlopen, caplog):
        caplog.set_level("INFO", logger="aiform.driver.digitalocean.compute")
        current = make_attrs(status="active", size="s-1vcpu-2gb")
        desired = make_attrs(size="s-2vcpu-4gb")

        fake_urlopen.script(
            "POST",
            actions_url("123"),
            FakeHTTPResponse(201, {"action": {"id": 1, "status": "in-progress"}}),
            http_error(actions_url("123"), 422, {"message": "disk size cannot be decreased"}),
            FakeHTTPResponse(201, {"action": {"id": 3, "status": "in-progress"}}),
        )
        fake_urlopen.script(
            "GET",
            droplet_url("123"),
            FakeHTTPResponse(200, make_droplet(status="off", size="s-1vcpu-2gb")),
            FakeHTTPResponse(200, make_droplet(status="active", size="s-1vcpu-2gb")),
        )

        with pytest.raises(DriverUpdateNotSupported):
            driver.update("123", current, desired, CREDENTIALS)

        record = next(r for r in caplog.records if getattr(r, "http_status", None) is not None)
        assert record.levelno == logging.WARNING
        assert record.http_status == 422
        assert record.do_message == "disk size cannot be decreased"
        assert record.target_size == "s-2vcpu-4gb"
        assert "falling back to destroy+recreate" in record.getMessage()

    def test_resize_transient_error_warning_does_not_claim_destroy_recreate(
        self, driver, fake_urlopen, caplog
    ):
        # The resize-rejection warning and the transient-error warning
        # share one except block but must say different things -- a
        # transient/unrelated failure (429 here) re-raises without
        # triggering a replace, so its log line must not claim "falling
        # back to destroy+recreate" the way the genuine-rejection branch
        # does. Found merging this driver's structured-logging work
        # (which only ever had the rejection branch to log) together
        # with the resize-classification fix (which added the re-raise
        # branch this logging never accounted for).
        caplog.set_level("INFO", logger="aiform.driver.digitalocean.compute")
        current = make_attrs(status="active", size="s-1vcpu-2gb")
        desired = make_attrs(size="s-2vcpu-4gb")

        fake_urlopen.script(
            "POST",
            actions_url("123"),
            FakeHTTPResponse(201, {"action": {"id": 1, "status": "in-progress"}}),
            http_error(actions_url("123"), 429, {"message": "too many requests"}),
            FakeHTTPResponse(201, {"action": {"id": 3, "status": "in-progress"}}),
        )
        fake_urlopen.script(
            "GET",
            droplet_url("123"),
            FakeHTTPResponse(200, make_droplet(status="off", size="s-1vcpu-2gb")),
            FakeHTTPResponse(200, make_droplet(status="active", size="s-1vcpu-2gb")),
        )

        with pytest.raises(urllib.error.HTTPError):
            driver.update("123", current, desired, CREDENTIALS)

        record = next(r for r in caplog.records if getattr(r, "http_status", None) is not None)
        assert record.levelno == logging.WARNING
        assert record.http_status == 429
        assert record.do_message == "too many requests"
        assert "falling back to destroy+recreate" not in record.getMessage()

    def test_do_error_message_returns_none_for_non_json_body(self, driver):
        exc = http_error(actions_url("123"), 422, None)
        exc.fp = io.BytesIO(b"not json at all")

        assert driver._do_error_message(exc) is None

    def test_do_error_message_returns_none_when_message_key_absent(self, driver):
        exc = http_error(actions_url("123"), 422, {"id": "unprocessable_entity"})

        assert driver._do_error_message(exc) is None

    def test_do_error_message_extracts_message_field(self, driver):
        exc = http_error(actions_url("123"), 422, {"message": "disk size cannot be decreased"})

        assert driver._do_error_message(exc) == "disk size cannot be decreased"

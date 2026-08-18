import io
import json
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

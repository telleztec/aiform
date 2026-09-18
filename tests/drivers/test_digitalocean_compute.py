# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import io
import json
import logging
import subprocess
import time
import urllib.error
import urllib.request
from email.message import Message

import pytest

from aiform import ssh
from aiform.driver import DriverUpdateNotSupported
from aiform.exceptions import ResourceNotFoundError
from drivers.digitalocean import compute as compute_module
from drivers.digitalocean.compute import Driver

BASE_URL = "https://api.digitalocean.com/v2"
CREDENTIALS = {"DIGITALOCEAN_TOKEN": "dop_v1_test"}
NAME = "telleztec-app-01"

# The DO account key id every test's cached sidecar (see the autouse
# ssh_env fixture below) resolves to, so create()/update() tests exercise
# the zero-extra-HTTP-call path by default. Tests that specifically
# exercise key *registration* delete the sidecar first.
MANAGED_KEY_ID = "999999"

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


def tags_url() -> str:
    return f"{BASE_URL}/tags"


def tag_url(name: str) -> str:
    return f"{BASE_URL}/tags/{name}"


def tag_resources_url(name: str) -> str:
    return f"{BASE_URL}/tags/{name}/resources"


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
                # health()/metrics() must pass a shorter bound than the
                # driver's 30s default, and the only way to assert that
                # is to record what urlopen was actually given.
                "timeout": kwargs.get("timeout", args[0] if args else None),
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


@pytest.fixture(autouse=True)
def ssh_env(tmp_path, monkeypatch):
    """Redirects aiform/ssh.py's implicit .aiform/ssh/ location into a
    tmp dir (so no test ever touches a real checkout) and pre-seeds the DO
    key-id sidecar with MANAGED_KEY_ID, so every create()/update() test
    exercises _ensure_do_key_registered's cached path -- zero extra HTTP
    calls, matching this driver's existing behavior before issue #175.
    Also defaults shutdown_via_ssh to unavailable, so every existing
    resize test keeps exercising the API power_off fallback exactly as
    before; TestSshFirstPowerOff overrides this per test to exercise the
    SSH-success branch."""
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir(parents=True)
    (ssh_dir / "aiform_managed_key.id").write_text(MANAGED_KEY_ID, encoding="utf-8")
    monkeypatch.setattr(ssh, "DEFAULT_SSH_DIR", ssh_dir)
    monkeypatch.setattr(ssh, "shutdown_via_ssh", lambda *args, **kwargs: False)
    return ssh_dir


@pytest.fixture
def driver() -> Driver:
    return Driver()


def public_key_fingerprint(public_key_path) -> str:
    result = subprocess.run(
        ["ssh-keygen", "-E", "md5", "-lf", str(public_key_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.split()[1].removeprefix("MD5:")


def account_keys_url() -> str:
    return f"{BASE_URL}/account/keys"


def account_keys_list_url() -> str:
    # fetch_all_pages injects per_page=200 (drivers/digitalocean/_common.py's
    # DEFAULT_PER_PAGE) on every GET it issues -- the POST upload call
    # below does not go through it, so only the GET url carries this.
    return f"{BASE_URL}/account/keys?per_page=200"


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
        # The aiform-managed key is always appended -- issue #175.
        assert body["ssh_keys"] == [*params["ssh_keys"], MANAGED_KEY_ID]
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


class TestPollBudgets:
    # issue #168: two consecutive live system-test runs both timed out
    # waiting for a power-off, exhausting the (then) 45-attempt/2s-delay
    # (90s) default budget at ~108.4s and ~108.6s -- a tight cluster at a
    # higher number than issue #152's own prior bump (30->45 attempts,
    # 60s->90s, after a ~72-73s cluster), reading as DO's power-off
    # latency having shifted again rather than a one-off flake.
    OBSERVED_WORST_CASE_SECONDS = 108.6

    def test_update_default_budget_clears_the_168_observed_latency_with_margin(self, driver):
        max_attempts, delay_seconds = driver._poll_until.__defaults__

        # The literal pin: this IS the constant issue #168 changed, so a
        # future retune is expected to edit this line, same as it would
        # edit the driver. Kept alongside the margin assertion below (which
        # is implied by this pin once the numbers are fixed) because the
        # margin is the actual invariant being defended -- a reviewer or a
        # future retune should be able to see *why* 150s, not just *that*
        # it's 150s.
        assert (max_attempts, delay_seconds) == (75, 2)

        # #152's own bump landed ~23% over its observed cluster (90s over a
        # ~73.4s worst case) and still needed raising again -- land with
        # more margin than that this time, not just barely above 108.6s.
        budget_seconds = max_attempts * delay_seconds
        assert budget_seconds > self.OBSERVED_WORST_CASE_SECONDS * 1.25

    def test_create_override_budget_is_unchanged_and_still_exceeds_update_default(
        self, driver, fake_urlopen, monkeypatch
    ):
        fake_urlopen.script(
            "POST", droplets_url(), FakeHTTPResponse(202, make_droplet(id=555, status="new"))
        )
        fake_urlopen.script(
            "GET", droplet_url("555"), FakeHTTPResponse(200, make_droplet(id=555, status="active"))
        )

        # Read the true default off the class, not the instance, before
        # wrapping the instance attribute below -- monkeypatch.setattr on
        # `driver` shadows the class method for this instance only.
        update_max_attempts, update_delay_seconds = type(driver)._poll_until.__defaults__

        original_poll_until = driver._poll_until
        captured = {}

        def spy_poll_until(*args, **kwargs):
            captured["max_attempts"] = kwargs.get("max_attempts")
            captured["delay_seconds"] = kwargs.get("delay_seconds")
            return original_poll_until(*args, **kwargs)

        monkeypatch.setattr(driver, "_poll_until", spy_poll_until)

        result = driver.create(NAME, BASE_PARAMS, CREDENTIALS)

        assert result["status"] == "active"
        # create() passes its own explicit budget rather than silently
        # inheriting update()'s default.
        assert (captured["max_attempts"], captured["delay_seconds"]) == (60, 3)
        # ...and that budget stays wider than update()'s, the property the
        # override exists to preserve -- checked against the live default
        # rather than a second literal, so this keeps holding if either
        # constant is retuned again without the other.
        assert 60 * 3 > update_max_attempts * update_delay_seconds


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


class TestUpdateRejectsReplaceForcingDiffs:
    # tags/backups are deliberately absent: DigitalOcean can apply both in
    # place, and treating them as replace-forcing destroyed live droplets
    # on a trivial edit (issue #77). The four below have no DO API surface
    # at all -- see specs/digitalocean_compute.md's capability table.
    @pytest.mark.parametrize(
        "field,value",
        [
            ("region", "nyc3"),
            ("image", "ubuntu-22-04-x64"),
            ("ssh_keys", ["a-different-key"]),
            ("monitoring", False),
        ],
    )
    def test_replace_forcing_field_change_raises_unsupported(
        self, driver, fake_urlopen, field, value
    ):
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

    def test_unsupported_fields_names_only_the_replace_forcing_fields(self, driver, fake_urlopen):
        # size and tags are both applicable in place; region is not. Only
        # region belongs in unsupported_fields -- reporting the whole diff
        # misstates why the replace is happening.
        current = make_attrs()
        desired = make_attrs(size="s-2vcpu-4gb", tags=["aiform", "production"], region="nyc3")

        with pytest.raises(DriverUpdateNotSupported) as excinfo:
            driver.update("123", current, desired, CREDENTIALS)

        assert excinfo.value.unsupported_fields == ["region"]

    def test_replace_forcing_field_mixed_with_in_place_ones_mutates_nothing(
        self, driver, fake_urlopen
    ):
        # The ordering invariant: update() must never raise
        # DriverUpdateNotSupported after mutating anything. The orchestrator
        # answers this exception with a gate #2 review and a "Replace ...?"
        # confirmation the user may decline -- a tag applied before that
        # point would leave the droplet altered while state says otherwise.
        current = make_attrs()
        desired = make_attrs(size="s-2vcpu-4gb", tags=["aiform", "production"], region="nyc3")

        with pytest.raises(DriverUpdateNotSupported):
            driver.update("123", current, desired, CREDENTIALS)

        assert fake_urlopen.calls == []

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


class TestUpdateTagsInPlace:
    def _final_get(self, fake, tags):
        fake.script("GET", droplet_url("123"), FakeHTTPResponse(200, make_droplet(tags=tags)))

    def test_added_tag_absent_from_the_account_is_created_then_assigned(self, driver, fake_urlopen):
        current = make_attrs(tags=["aiform"])
        desired = make_attrs(tags=["aiform", "production"])

        fake_urlopen.script("GET", tag_url("production"), http_error(tag_url("production"), 404))
        fake_urlopen.script(
            "POST", tags_url(), FakeHTTPResponse(201, {"tag": {"name": "production"}})
        )
        fake_urlopen.script("POST", tag_resources_url("production"), FakeHTTPResponse(204, None))
        self._final_get(fake_urlopen, ["aiform", "production"])

        driver.update("123", current, desired, CREDENTIALS)

        assert [(c["method"], c["url"]) for c in fake_urlopen.calls] == [
            ("GET", tag_url("production")),
            ("POST", tags_url()),
            ("POST", tag_resources_url("production")),
            ("GET", droplet_url("123")),
        ]
        create_call = next(c for c in fake_urlopen.calls if c["url"] == tags_url())
        assert create_call["body"] == {"name": "production"}

    def test_added_tag_that_already_exists_is_not_recreated(self, driver, fake_urlopen):
        current = make_attrs(tags=["aiform"])
        desired = make_attrs(tags=["aiform", "production"])

        fake_urlopen.script(
            "GET", tag_url("production"), FakeHTTPResponse(200, {"tag": {"name": "production"}})
        )
        fake_urlopen.script("POST", tag_resources_url("production"), FakeHTTPResponse(204, None))
        self._final_get(fake_urlopen, ["aiform", "production"])

        driver.update("123", current, desired, CREDENTIALS)

        assert tags_url() not in [c["url"] for c in fake_urlopen.calls]

    def test_assignment_body_uses_a_string_resource_id_and_droplet_type(self, driver, fake_urlopen):
        current = make_attrs(tags=["aiform"])
        desired = make_attrs(tags=["aiform", "production"])

        fake_urlopen.script(
            "GET", tag_url("production"), FakeHTTPResponse(200, {"tag": {"name": "production"}})
        )
        fake_urlopen.script("POST", tag_resources_url("production"), FakeHTTPResponse(204, None))
        self._final_get(fake_urlopen, ["aiform", "production"])

        driver.update("123", current, desired, CREDENTIALS)

        assign = next(c for c in fake_urlopen.calls if c["url"] == tag_resources_url("production"))
        # DO's tags_resource.yml types resource_id as a string, even though a
        # droplet id is numeric.
        assert assign["body"] == {"resources": [{"resource_id": "123", "resource_type": "droplet"}]}
        assert assign["content_type"] == "application/json"

    def test_removed_tag_is_unassigned_with_delete(self, driver, fake_urlopen):
        current = make_attrs(tags=["aiform", "retired"])
        desired = make_attrs(tags=["aiform"])

        fake_urlopen.script("DELETE", tag_resources_url("retired"), FakeHTTPResponse(204, None))
        self._final_get(fake_urlopen, ["aiform"])

        driver.update("123", current, desired, CREDENTIALS)

        unassign = next(c for c in fake_urlopen.calls if c["method"] == "DELETE")
        assert unassign["url"] == tag_resources_url("retired")
        assert unassign["body"] == {
            "resources": [{"resource_id": "123", "resource_type": "droplet"}]
        }
        # A removal must not create the tag object it is about to detach.
        assert tags_url() not in [c["url"] for c in fake_urlopen.calls]

    def test_a_tags_only_edit_never_powers_the_droplet_off(self, driver, fake_urlopen):
        # The whole point of issue #77: this used to destroy and recreate.
        current = make_attrs(tags=["aiform"])
        desired = make_attrs(tags=["aiform", "production"])

        fake_urlopen.script(
            "GET", tag_url("production"), FakeHTTPResponse(200, {"tag": {"name": "production"}})
        )
        fake_urlopen.script("POST", tag_resources_url("production"), FakeHTTPResponse(204, None))
        self._final_get(fake_urlopen, ["aiform", "production"])

        result = driver.update("123", current, desired, CREDENTIALS)

        assert action_calls(fake_urlopen, "123") == []
        assert result["id"] == "123"
        assert result["tags"] == ["aiform", "production"]

    def test_tag_name_is_url_quoted_into_the_path(self, driver, fake_urlopen):
        # DO's own pattern would reject this name, so quoting is a no-op for
        # anything valid -- it exists so a malformed name cannot inject an
        # extra path segment into the request URL.
        current = make_attrs(tags=[])
        desired = make_attrs(tags=["we/ird"])

        fake_urlopen.script("GET", tag_url("we%2Fird"), http_error(tag_url("we%2Fird"), 404))
        fake_urlopen.script("POST", tags_url(), FakeHTTPResponse(201, {"tag": {"name": "we/ird"}}))
        fake_urlopen.script("POST", tag_resources_url("we%2Fird"), FakeHTTPResponse(204, None))
        self._final_get(fake_urlopen, ["we/ird"])

        driver.update("123", current, desired, CREDENTIALS)

        assert tag_resources_url("we%2Fird") in [c["url"] for c in fake_urlopen.calls]
        assert tag_resources_url("we/ird") not in [c["url"] for c in fake_urlopen.calls]
        # The body carries the unquoted name; only the path is escaped.
        assert next(c for c in fake_urlopen.calls if c["url"] == tags_url())["body"] == {
            "name": "we/ird"
        }

    def test_transient_error_assigning_a_tag_reraises_and_is_not_unsupported(
        self, driver, fake_urlopen
    ):
        # Converting this into DriverUpdateNotSupported would answer a 5xx
        # with a destroy+recreate -- the same misclassification the resize
        # path already guards against.
        current = make_attrs(tags=["aiform"])
        desired = make_attrs(tags=["aiform", "production"])

        fake_urlopen.script(
            "GET", tag_url("production"), FakeHTTPResponse(200, {"tag": {"name": "production"}})
        )
        fake_urlopen.script(
            "POST",
            tag_resources_url("production"),
            http_error(tag_resources_url("production"), 500),
        )

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            driver.update("123", current, desired, CREDENTIALS)

        assert excinfo.value.code == 500

    def test_error_checking_whether_a_tag_exists_reraises_rather_than_creating(
        self, driver, fake_urlopen
    ):
        # Only a 404 means "absent"; a 500 says nothing about existence, and
        # blindly creating on it would mask a real outage.
        current = make_attrs(tags=["aiform"])
        desired = make_attrs(tags=["aiform", "production"])

        fake_urlopen.script("GET", tag_url("production"), http_error(tag_url("production"), 500))

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            driver.update("123", current, desired, CREDENTIALS)

        assert excinfo.value.code == 500
        assert tags_url() not in [c["url"] for c in fake_urlopen.calls]


class TestUpdateBackupsInPlace:
    def test_enabling_backups_posts_enable_backups_and_polls_features(self, driver, fake_urlopen):
        current = make_attrs(backups=False)
        desired = make_attrs(backups=True)

        fake_urlopen.script(
            "POST", actions_url("123"), FakeHTTPResponse(201, {"action": {"id": 1}})
        )
        fake_urlopen.script(
            "GET",
            droplet_url("123"),
            FakeHTTPResponse(200, make_droplet(backups_enabled=False)),
            FakeHTTPResponse(200, make_droplet(backups_enabled=True)),
        )

        driver.update("123", current, desired, CREDENTIALS)

        types = [c["body"]["type"] for c in action_calls(fake_urlopen, "123")]
        assert types == ["enable_backups"]
        # PARAM_SCHEMA models backups as a bare boolean, so there is no policy
        # to express; DO defaults to daily when the key is omitted.
        assert "backup_policy" not in action_calls(fake_urlopen, "123")[0]["body"]

    def test_disabling_backups_posts_disable_backups(self, driver, fake_urlopen):
        current = make_attrs(backups=True)
        desired = make_attrs(backups=False)

        fake_urlopen.script(
            "POST", actions_url("123"), FakeHTTPResponse(201, {"action": {"id": 1}})
        )
        fake_urlopen.script(
            "GET",
            droplet_url("123"),
            FakeHTTPResponse(200, make_droplet(backups_enabled=True)),
            FakeHTTPResponse(200, make_droplet(backups_enabled=False)),
        )

        driver.update("123", current, desired, CREDENTIALS)

        types = [c["body"]["type"] for c in action_calls(fake_urlopen, "123")]
        assert types == ["disable_backups"]

    def test_backups_change_never_powers_the_droplet_off(self, driver, fake_urlopen):
        current = make_attrs(backups=False)
        desired = make_attrs(backups=True)

        fake_urlopen.script(
            "POST", actions_url("123"), FakeHTTPResponse(201, {"action": {"id": 1}})
        )
        fake_urlopen.script(
            "GET", droplet_url("123"), FakeHTTPResponse(200, make_droplet(backups_enabled=True))
        )

        result = driver.update("123", current, desired, CREDENTIALS)

        types = [c["body"]["type"] for c in action_calls(fake_urlopen, "123")]
        assert "power_off" not in types
        assert result["id"] == "123"
        assert result["backups"] is True

    def test_backups_poll_timeout_raises_timeout_error_naming_id(self, driver, fake_urlopen):
        current = make_attrs(backups=False)
        desired = make_attrs(backups=True)

        fake_urlopen.script(
            "POST", actions_url("123"), FakeHTTPResponse(201, {"action": {"id": 1}})
        )
        # Never converges -- a single-item script repeats forever.
        fake_urlopen.script(
            "GET", droplet_url("123"), FakeHTTPResponse(200, make_droplet(backups_enabled=False))
        )

        with pytest.raises(TimeoutError) as excinfo:
            driver.update("123", current, desired, CREDENTIALS)

        assert "123" in str(excinfo.value)

    def test_transient_error_toggling_backups_reraises_and_is_not_unsupported(
        self, driver, fake_urlopen
    ):
        current = make_attrs(backups=False)
        desired = make_attrs(backups=True)

        fake_urlopen.script("POST", actions_url("123"), http_error(actions_url("123"), 500))

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            driver.update("123", current, desired, CREDENTIALS)

        assert excinfo.value.code == 500


class TestUpdateCombinedInPlaceDiff:
    def test_size_and_tags_together_resize_first_then_tag(self, driver, fake_urlopen):
        # Both halves are individually supported; the old `!= ["size"]` rule
        # rejected the combination outright.
        current = make_attrs(status="active", size="s-1vcpu-2gb", tags=["aiform"])
        desired = make_attrs(size="s-2vcpu-4gb", tags=["aiform", "production"])

        fake_urlopen.script(
            "POST",
            actions_url("123"),
            FakeHTTPResponse(201, {"action": {"id": 1}}),
            FakeHTTPResponse(201, {"action": {"id": 2}}),
            FakeHTTPResponse(201, {"action": {"id": 3}}),
        )
        fake_urlopen.script(
            "GET",
            droplet_url("123"),
            FakeHTTPResponse(200, make_droplet(status="off", size="s-1vcpu-2gb")),
            FakeHTTPResponse(200, make_droplet(status="off", size="s-2vcpu-4gb")),
            FakeHTTPResponse(200, make_droplet(status="active", size="s-2vcpu-4gb")),
            FakeHTTPResponse(
                200,
                make_droplet(status="active", size="s-2vcpu-4gb", tags=["aiform", "production"]),
            ),
        )
        fake_urlopen.script(
            "GET", tag_url("production"), FakeHTTPResponse(200, {"tag": {"name": "production"}})
        )
        fake_urlopen.script("POST", tag_resources_url("production"), FakeHTTPResponse(204, None))

        result = driver.update("123", current, desired, CREDENTIALS)

        urls = [c["url"] for c in fake_urlopen.calls]
        last_action = max(i for i, u in enumerate(urls) if u == actions_url("123"))
        first_tag = min(i for i, u in enumerate(urls) if u.startswith(f"{BASE_URL}/tags"))
        assert last_action < first_tag, "the resize must complete before any tag call"
        assert [c["body"]["type"] for c in action_calls(fake_urlopen, "123")] == [
            "power_off",
            "resize",
            "power_on",
        ]
        # The returned attributes come from a GET taken after the tag work,
        # not from the resize path's own last poll.
        assert result["size"] == "s-2vcpu-4gb"
        assert result["tags"] == ["aiform", "production"]

    def test_a_rejected_resize_raises_before_any_tag_call(self, driver, fake_urlopen):
        current = make_attrs(status="active", size="s-1vcpu-2gb", tags=["aiform"])
        desired = make_attrs(size="s-2vcpu-4gb", tags=["aiform", "production"])

        fake_urlopen.script(
            "POST",
            actions_url("123"),
            FakeHTTPResponse(201, {"action": {"id": 1}}),
            http_error(actions_url("123"), 422, {"message": "disk size cannot be decreased"}),
            FakeHTTPResponse(201, {"action": {"id": 3}}),
        )
        fake_urlopen.script(
            "GET",
            droplet_url("123"),
            FakeHTTPResponse(200, make_droplet(status="off")),
            FakeHTTPResponse(200, make_droplet(status="active")),
        )

        with pytest.raises(DriverUpdateNotSupported) as excinfo:
            driver.update("123", current, desired, CREDENTIALS)

        assert excinfo.value.unsupported_fields == ["size"]
        assert not [c for c in fake_urlopen.calls if c["url"].startswith(f"{BASE_URL}/tags")]


class TestUpdateRejectsMalformedValues:
    """A YAML scalar or a quoted boolean reaches update() exactly as parsed --
    nothing upstream validates params against PARAM_SCHEMA. Both of these
    used to be harmless because a tags/backups diff never reached a local
    mutation path; now they do, so they are rejected before any API call.
    """

    @pytest.mark.parametrize("bad", ["web", None, ["ok", 7], 42, ["ok", ""], {"web"}])
    def test_tags_that_are_not_a_list_of_strings_raise_before_any_call(
        self, driver, fake_urlopen, bad
    ):
        current = make_attrs(tags=["aiform"])
        desired = make_attrs(tags=bad)

        with pytest.raises(ValueError) as excinfo:
            driver.update("123", current, desired, CREDENTIALS)

        assert "tags" in str(excinfo.value)
        assert fake_urlopen.calls == []

    def test_a_scalar_tags_value_is_not_iterated_character_by_character(self, driver, fake_urlopen):
        # `tags: web` in YAML is the string "web", not ["web"]. Iterating it
        # would compute added=["w","e","b"] and removed=["aiform"] -- junk
        # tags created and the real one detached from a live droplet.
        with pytest.raises(ValueError):
            driver.update("123", make_attrs(tags=["aiform"]), make_attrs(tags="web"), CREDENTIALS)

        assert fake_urlopen.calls == []

    # 0 and 1 are deliberately absent: Python has 0 == False and 1 == True,
    # so an int matching the live value produces no diff at all and never
    # reaches this guard, while one that does not match is caught anyway.
    @pytest.mark.parametrize("bad", ["false", "true", 1, None])
    def test_backups_that_is_not_a_bool_raises_rather_than_being_coerced(
        self, driver, fake_urlopen, bad
    ):
        # bool("false") is True: coercing would switch billed backups ON for
        # a user who asked for them off.
        current = make_attrs(backups=False)
        desired = make_attrs(backups=bad)

        with pytest.raises(ValueError) as excinfo:
            driver.update("123", current, desired, CREDENTIALS)

        assert "backups" in str(excinfo.value)
        assert fake_urlopen.calls == []

    def test_an_empty_tag_name_is_rejected_rather_than_probing_the_list_endpoint(
        self, driver, fake_urlopen
    ):
        # An empty name makes the existence check GET /v2/tags/ -- DO's list
        # endpoint, which answers 200 -- so the tag would be reported as
        # already existing and the assignment would then fail at DO.
        with pytest.raises(ValueError) as excinfo:
            driver.update("123", make_attrs(tags=[]), make_attrs(tags=[""]), CREDENTIALS)

        assert "non-empty" in str(excinfo.value)
        assert fake_urlopen.calls == []

    def test_a_malformed_value_is_rejected_even_when_the_diff_forces_a_replace(
        self, driver, fake_urlopen
    ):
        # The guard must run before the replace-forcing partition. Otherwise
        # this raises DriverUpdateNotSupported(["region"]) first, the user
        # approves the replace, delete() succeeds -- and create() hands
        # "web" to DO, which rejects it. Droplet destroyed, nothing rebuilt.
        current = make_attrs(tags=["aiform"], region="sfo3")
        desired = make_attrs(tags="web", region="nyc3")

        with pytest.raises(ValueError) as excinfo:
            driver.update("123", current, desired, CREDENTIALS)

        assert "tags" in str(excinfo.value)
        assert fake_urlopen.calls == []

    def test_an_int_equal_to_the_live_bool_is_simply_no_diff(self, driver, fake_urlopen):
        # Not an endorsement of `backups: 0`, just the honest consequence of
        # 0 == False: there is nothing to apply, so nothing happens.
        result = driver.update("123", make_attrs(backups=False), make_attrs(backups=0), CREDENTIALS)

        assert fake_urlopen.calls == []
        assert result["backups"] is False

    def test_a_malformed_value_is_not_reported_as_an_unsupported_diff(self, driver, fake_urlopen):
        # DriverUpdateNotSupported would send the orchestrator into a
        # destroy+recreate, which feeds the same bad value to create().
        with pytest.raises(ValueError):
            driver.update("123", make_attrs(), make_attrs(tags="web"), CREDENTIALS)

        with pytest.raises(ValueError):
            driver.update("123", make_attrs(), make_attrs(backups="false"), CREDENTIALS)


class TestInPlaceFieldSetIsConsistent:
    def test_likely_replace_fields_is_the_complement_of_the_in_place_set(self, driver):
        # These encode one fact -- which PARAM_SCHEMA fields DO can change on
        # a live droplet -- in two hand-written places, and the literal is
        # copied into PLAN.md, specs/driver.md and specs/digitalocean_compute.md
        # as well. Left inconsistent, `plan` mispredicts in whichever
        # direction was forgotten. LIKELY_REPLACE_FIELDS stays a literal
        # (PLAN.md section 4 quotes it verbatim and CLAUDE.md treats that as
        # authoritative); this test is what keeps the two in step.
        from drivers.digitalocean.compute import _IN_PLACE_UPDATABLE_FIELDS

        expected = [
            f for f in driver.PARAM_SCHEMA["properties"] if f not in _IN_PLACE_UPDATABLE_FIELDS
        ]
        assert sorted(driver.LIKELY_REPLACE_FIELDS) == sorted(expected)

    def test_every_in_place_field_is_declared_in_param_schema(self, driver):
        from drivers.digitalocean.compute import _IN_PLACE_UPDATABLE_FIELDS

        # update()'s diff loop iterates PARAM_SCHEMA, so a name here that is
        # absent there is silently dead code.
        assert set(_IN_PLACE_UPDATABLE_FIELDS) <= set(driver.PARAM_SCHEMA["properties"])


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
        # Pinned to the live default rather than a literal: this is
        # exactly the number that drifted from 30 to 45 (issue #152), and
        # a literal here would need editing every time that budget is
        # re-tuned rather than catching a caller who forgot to update it.
        assert record.attempts_used == driver._poll_until.__defaults__[0]
        assert record.levelno == logging.ERROR

    def test_tags_step_logs_what_it_set_out_to_change(self, driver, fake_urlopen, caplog):
        # specs/digitalocean_compute.md calls this the sole record of the
        # tags step's intent when a later call in the loop fails partway
        # through -- the step makes several requests and polls none of them.
        caplog.set_level("INFO", logger="aiform.driver.digitalocean.compute")
        current = make_attrs(tags=["aiform", "retired"])
        desired = make_attrs(tags=["aiform", "production"])

        fake_urlopen.script(
            "GET", tag_url("production"), FakeHTTPResponse(200, {"tag": {"name": "production"}})
        )
        fake_urlopen.script("POST", tag_resources_url("production"), FakeHTTPResponse(204, None))
        fake_urlopen.script("DELETE", tag_resources_url("retired"), FakeHTTPResponse(204, None))
        fake_urlopen.script(
            "GET", droplet_url("123"), FakeHTTPResponse(200, make_droplet(tags=["aiform"]))
        )

        driver.update("123", current, desired, CREDENTIALS)

        record = next(r for r in caplog.records if getattr(r, "tags_added", None) is not None)
        assert record.id == "123"
        assert record.tags_added == ["production"]
        assert record.tags_removed == ["retired"]

    @pytest.mark.parametrize(
        "enabled,expected_step", [(True, "enable_backups"), (False, "disable_backups")]
    )
    def test_backups_step_logs_which_action_it_is_taking(
        self, driver, fake_urlopen, caplog, enabled, expected_step
    ):
        caplog.set_level("INFO", logger="aiform.driver.digitalocean.compute")
        current = make_attrs(backups=not enabled)
        desired = make_attrs(backups=enabled)

        fake_urlopen.script(
            "POST", actions_url("123"), FakeHTTPResponse(201, {"action": {"id": 1}})
        )
        fake_urlopen.script(
            "GET", droplet_url("123"), FakeHTTPResponse(200, make_droplet(backups_enabled=enabled))
        )

        driver.update("123", current, desired, CREDENTIALS)

        steps = [getattr(r, "step", None) for r in caplog.records]
        assert expected_step in steps
        # _poll_until's own line uses the hyphenated form for the same step.
        assert expected_step.replace("_", "-") in steps

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


class TestCreateInjectsManagedKey:
    def test_appends_managed_key_to_user_supplied_ssh_keys(self, driver, fake_urlopen):
        params = {**BASE_PARAMS, "ssh_keys": ["key-1"]}
        fake_urlopen.script(
            "POST", droplets_url(), FakeHTTPResponse(202, make_droplet(id=555, status="new"))
        )
        fake_urlopen.script("GET", droplet_url("555"), FakeHTTPResponse(200, make_droplet(id=555)))

        driver.create(NAME, params, CREDENTIALS)

        body = fake_urlopen.calls[0]["body"]
        assert body["ssh_keys"] == ["key-1", MANAGED_KEY_ID]

    def test_sets_ssh_keys_to_managed_key_alone_when_none_given(self, driver, fake_urlopen):
        fake_urlopen.script(
            "POST", droplets_url(), FakeHTTPResponse(202, make_droplet(id=555, status="new"))
        )
        fake_urlopen.script("GET", droplet_url("555"), FakeHTTPResponse(200, make_droplet(id=555)))

        driver.create(NAME, BASE_PARAMS, CREDENTIALS)

        body = fake_urlopen.calls[0]["body"]
        assert body["ssh_keys"] == [MANAGED_KEY_ID]

    def test_does_not_duplicate_the_managed_key(self, driver, fake_urlopen):
        params = {**BASE_PARAMS, "ssh_keys": [MANAGED_KEY_ID]}
        fake_urlopen.script(
            "POST", droplets_url(), FakeHTTPResponse(202, make_droplet(id=555, status="new"))
        )
        fake_urlopen.script("GET", droplet_url("555"), FakeHTTPResponse(200, make_droplet(id=555)))

        driver.create(NAME, params, CREDENTIALS)

        body = fake_urlopen.calls[0]["body"]
        assert body["ssh_keys"] == [MANAGED_KEY_ID]

    def test_returned_attrs_echo_only_the_user_declared_keys(self, driver, fake_urlopen):
        # The managed key is API-body plumbing only -- it must never show
        # up in the stored ssh_keys attribute, or NON_DIFFABLE_FIELDS'
        # carry-forward would compare a user's aiform.md-declared list
        # against a state value the user never wrote, producing a
        # perpetual spurious diff and defeating the zero-LLM-call
        # unchanged-run guarantee.
        params = {**BASE_PARAMS, "ssh_keys": ["key-1"]}
        fake_urlopen.script(
            "POST", droplets_url(), FakeHTTPResponse(202, make_droplet(id=555, status="new"))
        )
        fake_urlopen.script("GET", droplet_url("555"), FakeHTTPResponse(200, make_droplet(id=555)))

        result = driver.create(NAME, params, CREDENTIALS)

        assert result["ssh_keys"] == ["key-1"]

    def test_registers_the_key_before_posting_the_droplet(self, driver, fake_urlopen, ssh_env):
        (ssh_env / "aiform_managed_key.id").unlink()
        fake_urlopen.script(
            "GET", account_keys_list_url(), FakeHTTPResponse(200, {"ssh_keys": [], "links": {}})
        )
        fake_urlopen.script(
            "POST",
            account_keys_url(),
            FakeHTTPResponse(201, {"ssh_key": {"id": 42424242, "fingerprint": "aa:bb"}}),
        )
        fake_urlopen.script(
            "POST", droplets_url(), FakeHTTPResponse(202, make_droplet(id=555, status="new"))
        )
        fake_urlopen.script("GET", droplet_url("555"), FakeHTTPResponse(200, make_droplet(id=555)))

        driver.create(NAME, BASE_PARAMS, CREDENTIALS)

        post_urls = [c["url"] for c in fake_urlopen.calls if c["method"] == "POST"]
        assert post_urls.index(account_keys_url()) < post_urls.index(droplets_url())
        droplet_body = next(c for c in fake_urlopen.calls if c["url"] == droplets_url())["body"]
        assert droplet_body["ssh_keys"] == ["42424242"]


class TestEnsureDoKeyRegistered:
    def test_uses_the_cached_sidecar_with_no_http_calls(self, driver, fake_urlopen, ssh_env):
        key_id, private_key_path = driver._ensure_do_key_registered(CREDENTIALS)

        assert key_id == MANAGED_KEY_ID
        assert private_key_path == ssh_env / "aiform_managed_key"
        assert fake_urlopen.calls == []

    def test_no_sidecar_and_not_registered_uploads_a_new_key(self, driver, fake_urlopen, ssh_env):
        (ssh_env / "aiform_managed_key.id").unlink()
        fake_urlopen.script(
            "GET", account_keys_list_url(), FakeHTTPResponse(200, {"ssh_keys": [], "links": {}})
        )
        fake_urlopen.script(
            "POST",
            account_keys_url(),
            FakeHTTPResponse(201, {"ssh_key": {"id": 42424242, "fingerprint": "aa:bb"}}),
        )

        key_id, _ = driver._ensure_do_key_registered(CREDENTIALS)

        assert key_id == "42424242"
        assert (ssh_env / "aiform_managed_key.id").read_text().strip() == "42424242"
        upload_body = fake_urlopen.calls[-1]["body"]
        assert upload_body["name"] == "aiform-managed-key"
        assert upload_body["public_key"] == (ssh_env / "aiform_managed_key.pub").read_text().strip()

    def test_no_sidecar_but_key_already_registered_matches_by_fingerprint(
        self, driver, fake_urlopen, ssh_env
    ):
        private_key_path, public_key_path = ssh.ensure_managed_key(ssh_env)
        (ssh_env / "aiform_managed_key.id").unlink()
        fingerprint = public_key_fingerprint(public_key_path)
        fake_urlopen.script(
            "GET",
            account_keys_list_url(),
            FakeHTTPResponse(
                200, {"ssh_keys": [{"id": 777, "fingerprint": fingerprint}], "links": {}}
            ),
        )

        key_id, _ = driver._ensure_do_key_registered(CREDENTIALS)

        assert key_id == "777"
        assert (ssh_env / "aiform_managed_key.id").read_text().strip() == "777"
        assert [c for c in fake_urlopen.calls if c["method"] == "POST"] == []

    def test_upload_422_recovers_the_id_via_fingerprint_match(self, driver, fake_urlopen, ssh_env):
        private_key_path, public_key_path = ssh.ensure_managed_key(ssh_env)
        (ssh_env / "aiform_managed_key.id").unlink()
        fingerprint = public_key_fingerprint(public_key_path)
        fake_urlopen.script(
            "GET",
            account_keys_list_url(),
            FakeHTTPResponse(200, {"ssh_keys": [], "links": {}}),
            FakeHTTPResponse(
                200, {"ssh_keys": [{"id": 555, "fingerprint": fingerprint}], "links": {}}
            ),
        )
        fake_urlopen.script(
            "POST",
            account_keys_url(),
            http_error(
                account_keys_url(), 422, {"message": "SSH Key is already in use on your account"}
            ),
        )

        key_id, _ = driver._ensure_do_key_registered(CREDENTIALS)

        assert key_id == "555"
        assert (ssh_env / "aiform_managed_key.id").read_text().strip() == "555"
        get_calls = [c for c in fake_urlopen.calls if c["method"] == "GET"]
        assert len(get_calls) == 2

    def test_upload_422_with_no_fingerprint_match_reraises(self, driver, fake_urlopen, ssh_env):
        (ssh_env / "aiform_managed_key.id").unlink()
        fake_urlopen.script(
            "GET", account_keys_list_url(), FakeHTTPResponse(200, {"ssh_keys": [], "links": {}})
        )
        fake_urlopen.script(
            "POST",
            account_keys_url(),
            http_error(account_keys_url(), 422, {"message": "something else entirely"}),
        )

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            driver._ensure_do_key_registered(CREDENTIALS)

        assert excinfo.value.code == 422
        assert not (ssh_env / "aiform_managed_key.id").exists()

    def test_non_422_upload_failure_propagates_unmodified(self, driver, fake_urlopen, ssh_env):
        (ssh_env / "aiform_managed_key.id").unlink()
        fake_urlopen.script(
            "GET", account_keys_list_url(), FakeHTTPResponse(200, {"ssh_keys": [], "links": {}})
        )
        fake_urlopen.script(
            "POST", account_keys_url(), http_error(account_keys_url(), 500, {"message": "oops"})
        )

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            driver._ensure_do_key_registered(CREDENTIALS)

        assert excinfo.value.code == 500


class TestSshFirstPowerOff:
    def test_ssh_success_skips_the_power_off_api_action(self, driver, fake_urlopen, monkeypatch):
        current = make_attrs(status="active", size="s-1vcpu-2gb")
        desired = make_attrs(size="s-2vcpu-4gb")
        monkeypatch.setattr(ssh, "shutdown_via_ssh", lambda *a, **kw: True)

        fake_urlopen.script(
            "POST",
            actions_url("123"),
            FakeHTTPResponse(201, {"action": {"id": 2, "status": "in-progress"}}),  # resize
            FakeHTTPResponse(201, {"action": {"id": 3, "status": "in-progress"}}),  # power_on
        )
        fake_urlopen.script(
            "GET",
            droplet_url("123"),
            FakeHTTPResponse(200, make_droplet(status="off", size="s-1vcpu-2gb")),
            FakeHTTPResponse(200, make_droplet(status="off", size="s-2vcpu-4gb")),
            FakeHTTPResponse(200, make_droplet(status="active", size="s-2vcpu-4gb")),
        )

        driver.update("123", current, desired, CREDENTIALS)

        types = [c["body"]["type"] for c in action_calls(fake_urlopen, "123")]
        assert "power_off" not in types
        assert types == ["resize", "power_on"]

    def test_ssh_success_calls_shutdown_via_ssh_with_the_right_arguments(
        self, driver, fake_urlopen, monkeypatch, ssh_env
    ):
        current = make_attrs(status="active", size="s-1vcpu-2gb", ipv4_address="198.51.100.7")
        desired = make_attrs(size="s-2vcpu-4gb")
        calls = []

        def _fake_shutdown(ip, private_key_path, known_hosts_path, *, connect_timeout_budget):
            calls.append(
                {
                    "ip": ip,
                    "private_key_path": private_key_path,
                    "known_hosts_path": known_hosts_path,
                    "connect_timeout_budget": connect_timeout_budget,
                }
            )
            return True

        monkeypatch.setattr(ssh, "shutdown_via_ssh", _fake_shutdown)

        fake_urlopen.script(
            "POST",
            actions_url("123"),
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

        assert len(calls) == 1
        assert calls[0]["ip"] == "198.51.100.7"
        assert calls[0]["private_key_path"] == ssh_env / "aiform_managed_key"
        assert calls[0]["known_hosts_path"] == ssh_env / "known_hosts"
        assert calls[0]["connect_timeout_budget"] == 45.0

    def test_ssh_success_logs_ssh_success_path(self, driver, fake_urlopen, monkeypatch, caplog):
        caplog.set_level("INFO", logger="aiform.driver.digitalocean.compute")
        current = make_attrs(status="active", size="s-1vcpu-2gb")
        desired = make_attrs(size="s-2vcpu-4gb")
        monkeypatch.setattr(ssh, "shutdown_via_ssh", lambda *a, **kw: True)

        fake_urlopen.script(
            "POST",
            actions_url("123"),
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

        record = next(r for r in caplog.records if getattr(r, "power_off_path", None) is not None)
        assert record.power_off_path == "ssh-success"

    def test_ssh_success_but_poll_timeout_falls_back_to_api_power_off(
        self, driver, fake_urlopen, monkeypatch, caplog
    ):
        caplog.set_level("INFO", logger="aiform.driver.digitalocean.compute")
        current = make_attrs(status="active", size="s-1vcpu-2gb")
        desired = make_attrs(size="s-2vcpu-4gb")
        monkeypatch.setattr(ssh, "shutdown_via_ssh", lambda *a, **kw: True)

        fake_urlopen.script(
            "POST",
            actions_url("123"),
            FakeHTTPResponse(201, {"action": {"id": 1, "status": "in-progress"}}),  # power_off
            FakeHTTPResponse(201, {"action": {"id": 2, "status": "in-progress"}}),  # resize
            FakeHTTPResponse(201, {"action": {"id": 3, "status": "in-progress"}}),  # power_on
        )
        # Stays "active" through both (capped) SSH-poll attempts, so that
        # poll exhausts and falls back to the real power_off action --
        # which then converges normally through the remaining entries.
        fake_urlopen.script(
            "GET",
            droplet_url("123"),
            FakeHTTPResponse(200, make_droplet(status="active", size="s-1vcpu-2gb")),
            FakeHTTPResponse(200, make_droplet(status="active", size="s-1vcpu-2gb")),
            FakeHTTPResponse(200, make_droplet(status="off", size="s-1vcpu-2gb")),
            FakeHTTPResponse(200, make_droplet(status="off", size="s-2vcpu-4gb")),
            FakeHTTPResponse(200, make_droplet(status="active", size="s-2vcpu-4gb")),
        )

        # Swap in a tiny SSH poll budget so this test doesn't spend 30
        # fake-but-looped iterations to prove the fallback fires.
        monkeypatch.setattr(compute_module, "_SSH_POWER_OFF_POLL_MAX_ATTEMPTS", 2)

        driver.update("123", current, desired, CREDENTIALS)

        types = [c["body"]["type"] for c in action_calls(fake_urlopen, "123")]
        assert types == ["power_off", "resize", "power_on"]
        record = next(r for r in caplog.records if getattr(r, "power_off_path", None) is not None)
        assert record.power_off_path == "ssh-attempted-fallback"

    def test_ssh_unavailable_falls_back_to_api_power_off(self, driver, fake_urlopen, caplog):
        # The default ssh_env fixture already mocks shutdown_via_ssh to
        # return False -- this test makes that fallback explicit and
        # checks the structured log field for it.
        caplog.set_level("INFO", logger="aiform.driver.digitalocean.compute")
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

        types = [c["body"]["type"] for c in action_calls(fake_urlopen, "123")]
        assert types == ["power_off", "resize", "power_on"]
        record = next(r for r in caplog.records if getattr(r, "power_off_path", None) is not None)
        assert record.power_off_path == "ssh-attempted-fallback"

    def test_no_public_ip_skips_ssh_entirely(self, driver, fake_urlopen, monkeypatch, caplog):
        caplog.set_level("INFO", logger="aiform.driver.digitalocean.compute")
        current = make_attrs(status="active", size="s-1vcpu-2gb", ipv4_address=None)
        desired = make_attrs(size="s-2vcpu-4gb")
        ssh_calls = []
        monkeypatch.setattr(
            ssh, "shutdown_via_ssh", lambda *a, **kw: ssh_calls.append((a, kw)) or False
        )

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

        assert ssh_calls == []
        types = [c["body"]["type"] for c in action_calls(fake_urlopen, "123")]
        assert types == ["power_off", "resize", "power_on"]
        record = next(r for r in caplog.records if getattr(r, "power_off_path", None) is not None)
        assert record.power_off_path == "no-ip-fallback"

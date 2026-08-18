import json
import time
import urllib.error
import urllib.request

from aiform.driver import DriverUpdateNotSupported, ResourceDriver
from aiform.exceptions import ResourceNotFoundError

BASE_URL = "https://api.digitalocean.com/v2"
REQUEST_TIMEOUT_SECONDS = 30


class Driver(ResourceDriver):
    PARAM_SCHEMA = {
        "type": "object",
        "properties": {
            "region": {"type": "string"},
            "size": {"type": "string"},
            "image": {"type": "string"},
            "ssh_keys": {"type": "array", "items": {"type": "string"}},
            "backups": {"type": "boolean"},
            "monitoring": {"type": "boolean"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["region", "size", "image"],
        "additionalProperties": True,
    }
    LIKELY_REPLACE_FIELDS = ["image", "region"]
    NON_DIFFABLE_FIELDS = ["ssh_keys"]

    def _request(self, method, url, credentials, body=None):
        data = None
        headers = {"Authorization": f"Bearer {credentials['DIGITALOCEAN_TOKEN']}"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read()
        if not raw:
            return None
        return json.loads(raw)

    def _flatten(self, droplet):
        ipv4_address = None
        for network in droplet.get("networks", {}).get("v4", []):
            if network.get("type") == "public":
                ipv4_address = network.get("ip_address")
                break
        return {
            "id": str(droplet["id"]),
            "region": droplet["region"]["slug"],
            "size": droplet["size_slug"],
            "image": droplet["image"]["slug"],
            "status": droplet["status"],
            "tags": droplet.get("tags", []),
            "ipv4_address": ipv4_address,
        }

    def _get_droplet(self, id, credentials):
        payload = self._request("GET", f"{BASE_URL}/droplets/{id}", credentials)
        return payload["droplet"]

    def _action(self, id, credentials, body):
        self._request("POST", f"{BASE_URL}/droplets/{id}/actions", credentials, body=body)

    def _poll_until(self, id, credentials, predicate, step, max_attempts=20, delay_seconds=2):
        for attempt in range(max_attempts):
            droplet = self._get_droplet(id, credentials)
            if predicate(droplet):
                return droplet
            if attempt < max_attempts - 1:
                time.sleep(delay_seconds)
        raise TimeoutError(f"timed out waiting for droplet {id} during the {step} step")

    def _do_action_and_wait(self, id, credentials, body, predicate, step):
        self._action(id, credentials, body)
        return self._poll_until(id, credentials, predicate, step)

    def create(self, name, params, credentials):
        body = {
            "name": name,
            "region": params["region"],
            "size": params["size"],
            "image": params["image"],
        }
        for key in ("ssh_keys", "backups", "monitoring", "tags"):
            if key in params:
                body[key] = params[key]

        payload = self._request("POST", f"{BASE_URL}/droplets", credentials, body=body)
        new_id = payload["droplet"]["id"]
        # _poll_until's default budget (20 attempts * 2s = 40s) is tuned for
        # update()'s power-off/resize/power-on actions against an already-
        # existing droplet -- full provisioning from scratch commonly takes
        # longer than that per DO's own docs, so this uses a wider budget
        # (60 * 3s = 180s) to avoid spuriously timing out a create that
        # would have converged moments later.
        droplet = self._poll_until(
            new_id,
            credentials,
            lambda d: d["status"] == "active",
            "create",
            max_attempts=60,
            delay_seconds=3,
        )
        attrs = self._flatten(droplet)
        attrs["ssh_keys"] = params.get("ssh_keys", [])
        attrs["backups"] = params.get("backups", False)
        attrs["monitoring"] = params.get("monitoring", False)
        return attrs

    def read(self, id, credentials):
        try:
            droplet = self._get_droplet(id, credentials)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise ResourceNotFoundError(f"DigitalOcean droplet {id} not found") from exc
            raise
        attrs = self._flatten(droplet)
        attrs["monitoring"] = "monitoring" in droplet.get("features", [])
        attrs["backups"] = "backups" in droplet.get("features", [])
        return attrs

    def update(self, id, current, desired, credentials):
        diff_fields = [
            key
            for key in self.PARAM_SCHEMA["properties"]
            if key in desired
            and key not in self.NON_DIFFABLE_FIELDS
            and current.get(key) != desired.get(key)
        ]
        if not diff_fields:
            return dict(current)

        if diff_fields != ["size"]:
            raise DriverUpdateNotSupported(
                f"DigitalOcean droplets cannot update {diff_fields} in place; "
                "a replace is required for this diff.",
                unsupported_fields=diff_fields,
            )

        status = current.get("status")
        if status not in ("active", "off"):
            raise DriverUpdateNotSupported(
                f"cannot resize droplet {id} in place from unmodeled status {status!r}",
                unsupported_fields=["size"],
            )

        target_size = desired.get("size")
        if not target_size:
            raise DriverUpdateNotSupported(
                f"cannot resize droplet {id}: desired params has no size value",
                unsupported_fields=["size"],
            )

        we_powered_off = status == "active"
        if we_powered_off:
            self._do_action_and_wait(
                id, credentials, {"type": "power_off"}, lambda d: d["status"] == "off", "power-off"
            )

        try:
            self._action(id, credentials, {"type": "resize", "disk": False, "size": target_size})
        except urllib.error.HTTPError as exc:
            # Only restore power state we ourselves changed -- a droplet that
            # started "off" (the user's own choice) stays off on a rejected
            # resize, it doesn't get turned on as a side effect of the failure.
            if we_powered_off:
                self._do_action_and_wait(
                    id,
                    credentials,
                    {"type": "power_on"},
                    lambda d: d["status"] == "active",
                    "power-on",
                )
            raise DriverUpdateNotSupported(
                f"DigitalOcean rejected an in-place resize of droplet {id} to {target_size!r}",
                unsupported_fields=["size"],
            ) from exc

        # Unlike the rejection path above, a *successful* resize always powers
        # the droplet back on, even if it started "off" -- this driver has now
        # changed its size, so it's expected to come back up, not stay down.
        self._poll_until(id, credentials, lambda d: d["size_slug"] == target_size, "resize")
        final_droplet = self._do_action_and_wait(
            id, credentials, {"type": "power_on"}, lambda d: d["status"] == "active", "power-on"
        )

        attrs = self._flatten(final_droplet)
        # desired.get(key, current.get(key, ...)) -- prefer desired's value
        # when the field is actually managed, else preserve current's rather
        # than resetting to a bare default: desired omitting an optional
        # field means it isn't part of this diff at all (see diff_fields
        # above), not that it should revert to [].
        attrs["ssh_keys"] = desired.get("ssh_keys", current.get("ssh_keys", []))
        attrs["backups"] = desired.get("backups", current.get("backups", False))
        attrs["monitoring"] = desired.get("monitoring", current.get("monitoring", False))
        return attrs

    def delete(self, id, credentials):
        try:
            self._request("DELETE", f"{BASE_URL}/droplets/{id}", credentials)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        return None

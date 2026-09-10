# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from aiform.driver import ResourceDriver
from aiform.exceptions import ResourceNotFoundError

BASE_URL = "https://api.digitalocean.com/v2"
REQUEST_TIMEOUT_SECONDS = 30

# Named explicitly rather than via logging.getLogger(__name__) -- see
# drivers/digitalocean/compute.py's and domain.py's identical comment.
# load_driver() execs this file under a synthetic module name that isn't
# a dotted descendant of the "aiform" logger aiform/log.py attaches
# handlers to, so __name__ here would produce a logger with no handler
# and no propagation to either sink.
logger = logging.getLogger("aiform.driver.digitalocean.firewall")

_PROTOCOLS = ("tcp", "udp", "icmp")
_ACTIONS = ("allow", "deny")
# The keys DigitalOcean accepts inside a rule's sources/destinations
# object. Every one of them is a reference to another resource kind --
# see specs/digitalocean_firewall.md's "Resource graph".
_TARGET_KEYS = ("addresses", "droplet_ids", "tags", "load_balancer_uids", "kubernetes_ids")
# Server-set and excluded from read(): they churn on every edit, so
# storing them would rewrite state.json on every refresh. Same reasoning
# as domain.py's zone_file exclusion.
_SERVER_SET_FIELDS = ("status", "created_at", "pending_changes")
_MANAGED_FIELDS = ("inbound_rules", "outbound_rules", "droplet_ids", "tags")

_TARGET_KEY_FOR = {"inbound_rules": "sources", "outbound_rules": "destinations"}

_RULE_TARGET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "addresses": {"type": "array", "items": {"type": "string"}},
        "droplet_ids": {"type": "array", "items": {"type": "integer"}},
        "tags": {"type": "array", "items": {"type": "string"}},
        "load_balancer_uids": {"type": "array", "items": {"type": "string"}},
        "kubernetes_ids": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}


def _rule_schema(target_key: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "protocol": {"type": "string", "enum": list(_PROTOCOLS)},
            # A string, not an integer: DigitalOcean stores "22", and a
            # written 22 comes back as "22" and diffs forever.
            "ports": {"type": "string"},
            "action": {"type": "string", "enum": list(_ACTIONS)},
            target_key: _RULE_TARGET_SCHEMA,
        },
        "required": ["protocol", "ports", "action", target_key],
        "additionalProperties": False,
    }


class Driver(ResourceDriver):
    PARAM_SCHEMA: dict[str, Any] = {
        "type": "object",
        "properties": {
            "inbound_rules": {"type": "array", "items": _rule_schema("sources")},
            "outbound_rules": {"type": "array", "items": _rule_schema("destinations")},
            "droplet_ids": {"type": "array", "items": {"type": "integer"}},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        # All four are required. An omitted key is invisible to
        # planner.diff_attributes() (it iterates `desired` only), yet the
        # whole-object PUT still sends [] for it -- so omitting 'tags' or
        # 'droplet_ids' would silently clear them on the next unrelated
        # edit, with no plan line naming the change. Requiring them makes
        # "clear this" something a user writes on purpose.
        "required": ["inbound_rules", "outbound_rules", "droplet_ids", "tags"],
        "additionalProperties": False,
    }
    # A firewall is replaced wholesale by a single PUT, and its name --
    # aiform's state key -- is never updatable through update(), so
    # nothing about this resource forces a replace.
    LIKELY_REPLACE_FIELDS: list[str] = []
    # read() recovers every managed field from the API.
    NON_DIFFABLE_FIELDS: list[str] = []
    # Rule lists, droplet ids and tags are all sets in practice:
    # DigitalOcean is free to return them in an order the user did not
    # write, and plain != would then diff forever. Declared as an
    # assumption with its reasoning rather than measured -- a single live
    # observation cannot establish order-stability
    # (specs/unordered_fields.md).
    UNORDERED_FIELDS: list[str] = ["inbound_rules", "outbound_rules", "droplet_ids", "tags"]

    # --- HTTP -------------------------------------------------------

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

    def _do_error_message(self, exc: urllib.error.HTTPError) -> str | None:
        # Best-effort, mirrors compute.py/domain.py: never let extraction
        # itself raise, and a malformed or already-consumed body just
        # means no message gets folded in, not a crash mid-error-handling.
        try:
            body = exc.read()
        except Exception:
            return None
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return data.get("message") if isinstance(data, dict) else None

    def _fold_do_error_into_exc(self, exc: urllib.error.HTTPError) -> str | None:
        message = self._do_error_message(exc)
        if message:
            exc.msg = f"{exc.msg}: {message}"
        return message

    # --- validation -------------------------------------------------

    def _validate_params(self, params: Any) -> None:
        """Reject anything DigitalOcean would silently rewrite on store.

        aiform/planner.py's diff_attributes() compares read()'s output
        against the user's raw params with no hook for this driver to
        normalize either side, so a value the API rewrites is a
        permanent phantom diff. Each rejection below names the storable
        spelling, following domain.py's trailing-dot precedent: a user
        is never left holding a value that can never converge.
        """
        if not isinstance(params, dict):
            raise ValueError(f"params must be a dict, got {params!r}")

        unexpected = set(params) - set(self.PARAM_SCHEMA["properties"])
        if unexpected:
            raise ValueError(
                f"unsupported params key(s) {sorted(unexpected)}; this driver's "
                f"PARAM_SCHEMA accepts {sorted(self.PARAM_SCHEMA['properties'])}"
            )
        for key in self.PARAM_SCHEMA["required"]:
            if key not in params:
                raise ValueError(f"params is missing required key {key!r}")

        total_rules = 0
        for list_key, target_key in _TARGET_KEY_FOR.items():
            rules = params[list_key]
            if not isinstance(rules, list):
                raise ValueError(f"{list_key!r} must be a list, got {type(rules).__name__}")
            total_rules += len(rules)
            for index, rule in enumerate(rules):
                if not isinstance(rule, dict):
                    raise ValueError(
                        f"{list_key}[{index}] must be a dict, got {type(rule).__name__}"
                    )
                self._validate_rule(list_key, index, rule, target_key)

        if total_rules == 0:
            # Verified live: DigitalOcean 422s with "must have at least
            # one rule". Caught here so it is a ValueError before any
            # call rather than a 422 after one.
            raise ValueError(
                "a firewall must have at least one rule; 'inbound_rules' and "
                "'outbound_rules' are both empty"
            )

        self._validate_scalar_list(params, "droplet_ids", int)
        self._validate_scalar_list(params, "tags", str)

    def _validate_scalar_list(self, params: dict[str, Any], key: str, expected: type) -> None:
        # No "if key not in params" guard: every managed field is in
        # PARAM_SCHEMA["required"] and checked above, so a missing one has
        # already raised.
        value = params[key]
        if not isinstance(value, list):
            raise ValueError(f"{key!r} must be a list, got {type(value).__name__}")
        self._reject_wrong_scalars(key, value, expected)

    def _reject_wrong_scalars(self, label: str, value: list[Any], expected: type) -> None:
        for index, item in enumerate(value):
            # bool is an int subclass, so `droplet_ids: [true]` would
            # otherwise sail through isinstance(item, int).
            wrong_type = not isinstance(item, expected)
            sneaky_bool = expected is int and isinstance(item, bool)
            if wrong_type or sneaky_bool:
                raise ValueError(f"{label}[{index}] must be a {expected.__name__}, got {item!r}")

    def _validate_rule(
        self, list_key: str, index: int, rule: dict[str, Any], target_key: str
    ) -> None:
        where = f"{list_key}[{index}]"
        allowed = {"protocol", "ports", "action", target_key}
        # Checked before the missing-field check: an outbound rule written
        # with 'sources' is BOTH missing 'destinations' and carrying an
        # unexpected key, and the generic "missing destinations" message
        # would not name what the user actually did wrong.
        other = _TARGET_KEY_FOR["outbound_rules" if target_key == "sources" else "inbound_rules"]
        if other in rule:
            raise ValueError(
                f"{where} has {other!r}; an entry in {list_key!r} must use {target_key!r} instead"
            )
        missing = allowed - set(rule)
        if missing:
            raise ValueError(f"{where} is missing required field(s) {sorted(missing)}")
        unexpected = set(rule) - allowed
        if unexpected:
            raise ValueError(f"{where} has unsupported field(s) {sorted(unexpected)}")

        protocol = rule["protocol"]
        if not isinstance(protocol, str):
            raise ValueError(f"{where}: 'protocol' must be a str, got {protocol!r}")
        if protocol not in _PROTOCOLS:
            if protocol.lower() in _PROTOCOLS:
                # Verified live: DigitalOcean accepts "TCP" and stores
                # "tcp", which would diff forever.
                raise ValueError(
                    f"{where}: 'protocol' must be lowercase -- DigitalOcean stores "
                    f"it that way; write {protocol.lower()!r} instead of {protocol!r}"
                )
            raise ValueError(
                f"{where}: unsupported protocol {protocol!r}; supported are {list(_PROTOCOLS)}"
            )

        ports = rule["ports"]
        if not isinstance(ports, str):
            # Verified live: DigitalOcean accepts the int 22 and stores
            # the string "22".
            raise ValueError(
                f"{where}: 'ports' must be a str -- DigitalOcean stores it that "
                f"way; write {str(ports)!r} instead of {ports!r}"
            )
        if ports.lower() == "all":
            # Verified live: "all" is stored as "0".
            raise ValueError(
                f"{where}: 'ports' {ports!r} is stored by DigitalOcean as '0'; write '0' instead"
            )

        action = rule["action"]
        if action not in _ACTIONS:
            raise ValueError(f"{where}: 'action' must be one of {list(_ACTIONS)}, got {action!r}")

        self._validate_target(where, target_key, rule[target_key])

    def _validate_target(self, where: str, target_key: str, target: Any) -> None:
        if not isinstance(target, dict):
            raise ValueError(f"{where}: {target_key!r} must be a dict, got {target!r}")
        if not target:
            # DigitalOcean accepts an empty sources object (verified
            # live, 202) and the resulting rule matches nothing. In a
            # firewall a rule that silently does nothing is a
            # security-relevant footgun, so this driver refuses it.
            raise ValueError(
                f"{where}: {target_key!r} is empty, which matches no traffic; name "
                f"at least one of {list(_TARGET_KEYS)}"
            )
        unexpected = set(target) - set(_TARGET_KEYS)
        if unexpected:
            raise ValueError(
                f"{where}: {target_key!r} has unsupported key(s) {sorted(unexpected)}; "
                f"supported are {list(_TARGET_KEYS)}"
            )
        for key, value in target.items():
            if not isinstance(value, list):
                raise ValueError(
                    f"{where}: {target_key}.{key} must be a list, got {type(value).__name__}"
                )
            if not value:
                # An empty sub-list matches no traffic just as an empty
                # target object does, and what DigitalOcean returns for one
                # is unprobed -- an omitempty-style API would return {} and
                # then diff forever against {"addresses": []}.
                raise ValueError(
                    f"{where}: {target_key}.{key} is empty, which matches no traffic; "
                    "list at least one entry or drop the key"
                )
            self._validate_target_items(where, target_key, key, value)

    def _validate_target_items(
        self, where: str, target_key: str, key: str, value: list[Any]
    ) -> None:
        # Nothing upstream enforces PARAM_SCHEMA -- nothing in aiform/ runs
        # a JSON-schema validator, it is grounding shown to the LLM -- so
        # this is the only guard. Without it a string droplet id sails
        # through, and DigitalOcean coerces scalars on store (verified live
        # for ports), so it would read back as an int and diff forever:
        # the same bug the top-level scalar checks prevent, one level down.
        expected = int if key == "droplet_ids" else str
        self._reject_wrong_scalars(f"{where}: {target_key}.{key}", value, expected)

    # --- projection -------------------------------------------------

    def _project_rule(self, raw_rule: dict[str, Any], target_key: str) -> dict[str, Any]:
        # 'action' is kept deliberately: DigitalOcean adds it to every
        # rule it returns even though it is absent from the published
        # firewall_rule_base schema, so dropping it would make read()'s
        # rules permanently unequal to the user's params.
        return {
            "protocol": raw_rule["protocol"],
            "ports": raw_rule["ports"],
            "action": raw_rule["action"],
            target_key: raw_rule.get(target_key, {}),
        }

    def _project(self, firewall: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": firewall["id"],
            # Not a PARAM_SCHEMA key. Carried because update()'s
            # whole-object PUT needs it and update() is handed only id,
            # current and desired. Safe: diff_attributes() iterates
            # `desired` only, so an extra key here can never diff.
            "name": firewall["name"],
            "inbound_rules": [
                self._project_rule(r, "sources") for r in firewall.get("inbound_rules") or []
            ],
            "outbound_rules": [
                self._project_rule(r, "destinations") for r in firewall.get("outbound_rules") or []
            ],
            "droplet_ids": list(firewall.get("droplet_ids") or []),
            "tags": list(firewall.get("tags") or []),
        }

    def _wire_body(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        # Every managed field is required by PARAM_SCHEMA and validated
        # before this runs, so each is present and a list -- no defaulting
        # here, which would only paper over an omission the driver
        # refuses. All four go out on every write because an omitted key
        # is RESET rather than preserved, so a partial body would silently
        # discard state. That is verified live for tags (transcripts
        # 24-26) and inferred for droplet_ids, which no probe attaches.
        body: dict[str, Any] = {"name": name}
        for key in _MANAGED_FIELDS:
            body[key] = params[key]
        return body

    # --- ResourceDriver ---------------------------------------------

    def create(self, name: str, params: dict[str, Any], credentials: dict[str, str]):
        self._validate_params(params)
        try:
            payload = self._request(
                "POST", f"{BASE_URL}/firewalls", credentials, body=self._wire_body(name, params)
            )
        except urllib.error.HTTPError as exc:
            self._fold_do_error_into_exc(exc)
            raise
        firewall_id = payload["firewall"]["id"]
        # Everything after the POST runs under a rollback, mirroring
        # domain.py's zone rollback and for the same reason: the resource
        # is live from here on, but nothing has recorded its id yet, so
        # anything that raises leaks a firewall the state file has never
        # heard of and no teardown can find.
        #
        # This is not hypothetical. The live system test's first run
        # raised here on a reserved-LogRecord-attribute collision, after
        # the POST had succeeded, and left exactly such an orphan.
        try:
            # firewall_name, not name: 'name' is a reserved LogRecord
            # attribute (the logger's own name), and stdlib logging
            # raises "Attempt to overwrite 'name' in LogRecord" from
            # makeRecord when an extra collides with one. It only fires
            # once a handler has the level enabled, so unit tests --
            # which never call log.configure() -- cannot reach it.
            logger.info("", extra={"id": firewall_id, "firewall_name": name})
            # No polling: an unattached firewall comes back "succeeded"
            # in the create response itself (verified live), so unlike
            # compute.py there is nothing to converge.
            return self.read(firewall_id, credentials)
        except Exception as exc:
            logger.warning(
                "create failed after the firewall was created; rolling back",
                extra={"id": firewall_id, "error": str(exc)},
            )
            try:
                self.delete(firewall_id, credentials)
            except Exception as delete_exc:
                if isinstance(delete_exc, urllib.error.HTTPError):
                    # Same as domain.py: without this the orphan message
                    # carries a bare "HTTP Error 4xx" and drops the one
                    # sentence saying why the rollback failed.
                    self._fold_do_error_into_exc(delete_exc)
                raise RuntimeError(
                    f"firewall {name}: create failed ({exc}) and the rollback delete also "
                    f"failed ({delete_exc}) -- firewall {firewall_id} may be orphaned, live "
                    "and untracked"
                ) from exc
            raise

    def read(self, id: str, credentials: dict[str, str]) -> dict[str, Any]:
        try:
            payload = self._request("GET", f"{BASE_URL}/firewalls/{id}", credentials)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                # Verified live: a malformed id 404s exactly as an absent
                # one does, so no separate 422 branch is needed.
                raise ResourceNotFoundError(f"DigitalOcean firewall {id} not found") from exc
            raise
        return self._project(payload["firewall"])

    def update(
        self,
        id: str,
        current: dict[str, Any],
        desired: dict[str, Any],
        credentials: dict[str, str],
    ) -> dict[str, Any]:
        # Validated before any mutation, so a malformed value cannot
        # leave a half-applied firewall. A bad value is a ValueError,
        # never DriverUpdateNotSupported -- the orchestrator answers
        # that exception by destroying and recreating the resource.
        self._validate_params(desired)

        # One PUT replaces the whole object, which makes driver.py's
        # ordering invariant trivially true: validation and any refusal
        # happen strictly before the single mutation, so this driver can
        # never leave a firewall partly updated. DriverUpdateNotSupported
        # is therefore unreachable here -- every PARAM_SCHEMA field is
        # expressible in the PUT body, and 'name' is aiform's state key
        # so it never arrives as a diff.
        # Only `current` can supply it: read() returns 'name', and
        # _validate_params rejects a 'name' key in `desired`, so there is
        # no second source to fall back to. Reachable only from a state
        # entry hand-edited or written before read() carried 'name'.
        name = current.get("name")
        if not name:
            raise ValueError(
                f"firewall {id}: state entry has no 'name', which the whole-object "
                "PUT requires; the entry was hand-edited or predates this driver -- "
                "add the firewall's current name to it, or delete the entry and "
                "recreate the resource"
            )
        try:
            self._request(
                "PUT",
                f"{BASE_URL}/firewalls/{id}",
                credentials,
                body=self._wire_body(name, desired),
            )
        except urllib.error.HTTPError as exc:
            self._fold_do_error_into_exc(exc)
            raise
        return self.read(id, credentials)

    def delete(self, id: str, credentials: dict[str, str]) -> None:
        try:
            self._request("DELETE", f"{BASE_URL}/firewalls/{id}", credentials)
        except urllib.error.HTTPError as exc:
            # Verified live: a second delete 404s. Idempotent by
            # contract, so that is success.
            if exc.code == 404:
                return None
            raise
        return None

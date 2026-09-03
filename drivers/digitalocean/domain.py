# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import json
import logging
import urllib.error
import urllib.request
from collections import defaultdict
from typing import Any

from aiform.driver import ResourceDriver
from aiform.exceptions import ResourceNotFoundError
from drivers.digitalocean._common import fetch_all_pages

BASE_URL = "https://api.digitalocean.com/v2"
REQUEST_TIMEOUT_SECONDS = 30

# Named explicitly rather than via logging.getLogger(__name__) -- see
# drivers/digitalocean/compute.py's identical comment. load_driver()
# execs this file under a synthetic module name that isn't a dotted
# descendant of the "aiform" logger aiform/log.py attaches handlers to,
# so __name__ here would produce a logger with no handler and no
# propagation to either sink.
logger = logging.getLogger("aiform.driver.digitalocean.domain")

_RECORD_TYPES = ["A", "AAAA", "CAA", "CNAME", "MX", "NS", "SRV", "TXT"]
_BASE_FIELDS = frozenset({"type", "name", "data", "ttl"})
# The fields each type additionally requires, beyond the base four --
# specs/digitalocean_domain.md's per-type field table. A field not
# listed here for a type is rejected: accepting it would guarantee a
# permanent diff, since read() only ever returns a type's own fields.
_TYPE_EXTRA_FIELDS: dict[str, frozenset[str]] = {
    "A": frozenset(),
    "AAAA": frozenset(),
    "CNAME": frozenset(),
    "NS": frozenset(),
    "TXT": frozenset(),
    "MX": frozenset({"priority"}),
    "SRV": frozenset({"priority", "port", "weight"}),
    "CAA": frozenset({"flags", "tag"}),
}
# DigitalOcean expands `data` on these types to a fully-qualified name
# with a trailing dot -- see the spec's "Why read() does not
# un-normalize". "@"/"www"/"www.example.com." all round-trip to the
# same stored value, so no reverse mapping could recover which one the
# user typed; the canonical form is instead whatever DO stores, and the
# user must write that.
_FQDN_TYPES = frozenset({"CNAME", "MX", "NS", "SRV"})
# Types where a (type, name) group is expected to hold at most one
# record, so a changed value is an in-place PUT rather than a
# delete/create pair -- specs/digitalocean_domain.md's "Identity" names
# A and CNAME as "the common single-valued cases"; AAAA is the same
# address-record shape as A. Every other type (MX, NS, SRV, TXT, CAA)
# is reconciled by (type, name, data) identity instead, per the same
# section's own MX/TXT/NS example -- notably this holds even when only
# one record of that type currently exists for a name: a lone TXT
# record whose data changes is a delete+create, not a PUT, because nothing
# about TXT's semantics changes just because there's one of it today.
_SINGLE_VALUED_TYPES = frozenset({"A", "AAAA", "CNAME"})


class Driver(ResourceDriver):
    PARAM_SCHEMA: dict[str, Any] = {
        "type": "object",
        "properties": {
            "records": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": [
                                "A",
                                "AAAA",
                                "CAA",
                                "CNAME",
                                "MX",
                                "NS",
                                "SRV",
                                "TXT",
                            ],
                        },
                        "name": {"type": "string"},
                        "data": {"type": "string"},
                        "ttl": {"type": "integer"},
                        "priority": {"type": "integer"},
                        "port": {"type": "integer"},
                        "weight": {"type": "integer"},
                        "flags": {"type": "integer"},
                        "tag": {"type": "string"},
                    },
                    "required": ["type", "name", "data", "ttl"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["records"],
        "additionalProperties": False,
    }
    # Every records change is applicable in place through the record
    # endpoints, and a zone's name is the state key so it can't change
    # via update() at all -- nothing about this resource forces a
    # replace.
    LIKELY_REPLACE_FIELDS: list[str] = []
    # read() recovers every managed field from the API, so nothing here
    # is structurally unrecoverable.
    NON_DIFFABLE_FIELDS: list[str] = []
    UNORDERED_FIELDS: list[str] = ["records"]

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
        # Best-effort, mirrors compute.py's identical helper: never let
        # extraction itself raise, and a malformed/already-consumed body
        # just means no message gets folded in, not a crash mid-error-handling.
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

    def _validate_params(self, params: Any) -> None:
        if not isinstance(params, dict):
            raise ValueError(f"params must be a dict, got {params!r}")
        if "ip_address" in params:
            # Write-only on DO's side, so read() could never recover it
            # -- supporting it would mean a permanently non-diffable
            # field and two different ways to express the same apex
            # address. See "ip_address is not a supported param" in the
            # spec's Edge cases.
            raise ValueError(
                "'ip_address' is not a supported param: it is write-only on "
                "DigitalOcean's side and read() could never recover it. Write "
                "an explicit apex A record in 'records' instead."
            )
        unexpected = set(params) - {"records"}
        if unexpected:
            raise ValueError(
                f"unsupported params key(s) {sorted(unexpected)}; this driver's "
                "PARAM_SCHEMA only accepts 'records'"
            )
        if "records" not in params:
            raise ValueError("params is missing required key 'records'")

        records = params["records"]
        if not isinstance(records, list):
            raise ValueError(f"'records' must be a list, got {type(records).__name__}")
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                raise ValueError(f"records[{index}] must be a dict, got {type(record).__name__}")
            self._validate_record(index, record)

        seen = set()
        for record in records:
            key = tuple(sorted(record.items()))
            if key in seen:
                raise ValueError(f"duplicate record in 'records': {record}")
            seen.add(key)

    def _validate_record(self, index: int, record: dict[str, Any]) -> None:
        if "type" not in record:
            raise ValueError(f"records[{index}] is missing required field 'type'")
        record_type = record["type"]
        if record_type not in _RECORD_TYPES:
            raise ValueError(
                f"records[{index}]: unsupported record type {record_type!r}; "
                f"supported types are {_RECORD_TYPES}"
            )

        allowed = _BASE_FIELDS | _TYPE_EXTRA_FIELDS[record_type]
        missing = allowed - set(record)
        if missing:
            raise ValueError(
                f"records[{index}] (type {record_type}) is missing required "
                f"field(s) {sorted(missing)}"
            )
        unexpected = set(record) - allowed
        if unexpected:
            raise ValueError(
                f"records[{index}] (type {record_type}) has field(s) not valid "
                f"for this type: {sorted(unexpected)}"
            )

        if record_type in _FQDN_TYPES:
            data = record["data"]
            if not isinstance(data, str) or not data.endswith("."):
                raise ValueError(
                    f"records[{index}] (type {record_type}): 'data' must be "
                    f"fully qualified with a trailing dot (DigitalOcean stores "
                    f"it that way), got {data!r}"
                )

    def _project_record(self, raw_record: dict[str, Any]) -> dict[str, Any]:
        record_type = raw_record["type"]
        fields = _BASE_FIELDS | _TYPE_EXTRA_FIELDS[record_type]
        return {key: raw_record[key] for key in fields}

    def _is_do_managed_ns(self, raw_record: dict[str, Any]) -> bool:
        # Both conditions are required: a user's own delegated-subdomain
        # NS record (name != "@") stays managed, and so would an apex NS
        # pointing somewhere other than DO's own nameservers.
        return (
            raw_record["type"] == "NS"
            and raw_record["name"] == "@"
            and str(raw_record.get("data") or "").endswith(".digitalocean.com.")
        )

    def _filter_managed(self, raw_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [r for r in raw_records if r["type"] != "SOA" and not self._is_do_managed_ns(r)]

    def _sort_key(self, record: dict[str, Any]) -> tuple:
        return (
            record["type"],
            record["name"],
            record["data"],
            str(record.get("priority", "")),
            str(record.get("port", "")),
            str(record.get("weight", "")),
            str(record.get("flags", "")),
            str(record.get("tag", "")),
        )

    def _fetch_managed_raw_records(self, id: str, credentials) -> list[dict[str, Any]]:
        url = f"{BASE_URL}/domains/{id}/records"
        raw_records = fetch_all_pages(
            lambda u: self._request("GET", u, credentials), url, "domain_records"
        )
        return self._filter_managed(raw_records)

    def create(self, name: str, params: dict[str, Any], credentials: dict[str, str]):
        self._validate_params(params)
        records = params["records"]

        self._request("POST", f"{BASE_URL}/domains", credentials, body={"name": name})

        for record in records:
            try:
                self._post_record(name, record, credentials)
            except Exception as exc:
                if isinstance(exc, urllib.error.HTTPError):
                    self._fold_do_error_into_exc(exc)
                logger.warning(
                    "record creation failed after the zone was created; rolling back",
                    extra={"id": name, "error": str(exc)},
                )
                try:
                    self.delete(name, credentials)
                except Exception as delete_exc:
                    if isinstance(delete_exc, urllib.error.HTTPError):
                        self._fold_do_error_into_exc(delete_exc)
                    raise RuntimeError(
                        f"domain {name}: create failed ({exc}) and the rollback "
                        f"delete also failed ({delete_exc}) -- zone may be "
                        "orphaned, live and untracked"
                    ) from exc
                raise

        return self.read(name, credentials)

    def _post_record(self, name: str, record: dict[str, Any], credentials) -> None:
        self._request("POST", f"{BASE_URL}/domains/{name}/records", credentials, body=dict(record))

    def _put_record(self, name: str, record_id, record: dict[str, Any], credentials) -> None:
        self._request(
            "PUT",
            f"{BASE_URL}/domains/{name}/records/{record_id}",
            credentials,
            body=dict(record),
        )

    def _delete_record(self, name: str, record_id, credentials) -> None:
        self._request("DELETE", f"{BASE_URL}/domains/{name}/records/{record_id}", credentials)

    def read(self, id: str, credentials: dict[str, str]) -> dict[str, Any]:
        try:
            payload = self._request("GET", f"{BASE_URL}/domains/{id}", credentials)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise ResourceNotFoundError(f"DigitalOcean domain {id} not found") from exc
            raise
        domain = payload["domain"]

        managed = self._fetch_managed_raw_records(id, credentials)
        records = sorted((self._project_record(r) for r in managed), key=self._sort_key)

        # zone_file is deliberately excluded: it embeds the SOA serial,
        # which changes on every zone edit, so storing it would churn
        # state.json on every apply.
        return {"id": id, "ttl": domain["ttl"], "records": records}

    def _reconcile(
        self, raw_current: list[dict[str, Any]], desired_records: list[dict[str, Any]]
    ) -> list[tuple[str, Any, dict[str, Any] | None]]:
        current_by_type_name: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for raw in raw_current:
            current_by_type_name[(raw["type"], raw["name"])].append(raw)

        desired_by_type_name: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for record in desired_records:
            desired_by_type_name[(record["type"], record["name"])].append(record)

        actions: list[tuple[str, Any, dict[str, Any] | None]] = []
        for key in set(current_by_type_name) | set(desired_by_type_name):
            record_type, _name = key
            current_list = current_by_type_name.get(key, [])
            desired_list = desired_by_type_name.get(key, [])

            if (
                record_type in _SINGLE_VALUED_TYPES
                and len(current_list) <= 1
                and len(desired_list) <= 1
            ):
                actions.extend(self._reconcile_single_valued(current_list, desired_list))
            else:
                actions.extend(self._reconcile_multi_valued(current_list, desired_list))

        return actions

    def _reconcile_single_valued(self, current_list, desired_list):
        curr_raw = current_list[0] if current_list else None
        des_record = desired_list[0] if desired_list else None
        if curr_raw is not None and des_record is not None:
            if self._project_record(curr_raw) != des_record:
                return [("PUT", curr_raw["id"], des_record)]
            return []
        if des_record is not None:
            return [("POST", None, des_record)]
        return [("DELETE", curr_raw["id"], None)]

    def _reconcile_multi_valued(self, current_list, desired_list):
        current_by_data: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for raw in current_list:
            current_by_data[raw["data"]].append(raw)
        desired_by_data: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in desired_list:
            desired_by_data[record["data"]].append(record)

        actions: list[tuple[str, Any, dict[str, Any] | None]] = []
        for data_key in set(current_by_data) | set(desired_by_data):
            curr_matches = current_by_data.get(data_key, [])
            des_matches = desired_by_data.get(data_key, [])
            if curr_matches and des_matches:
                curr_raw = curr_matches[0]
                des_record = des_matches[0]
                if self._project_record(curr_raw) != des_record:
                    actions.append(("PUT", curr_raw["id"], des_record))
            elif des_matches:
                actions.extend(("POST", None, des_record) for des_record in des_matches)
            elif curr_matches:
                actions.extend(("DELETE", curr_raw["id"], None) for curr_raw in curr_matches)
        return actions

    def update(
        self,
        id: str,
        current: dict[str, Any],
        desired: dict[str, Any],
        credentials: dict[str, str],
    ) -> dict[str, Any]:
        # Validated first, before any mutation, so a malformed value
        # can't leave the zone half-edited. An unknown top-level key
        # (e.g. a stray `ttl:`) is a ValueError here, never
        # DriverUpdateNotSupported -- the orchestrator answers that
        # exception by destroying and recreating the resource, which
        # for a DNS zone means deleting it and every record inside it
        # over what is very likely a typo.
        self._validate_params(desired)

        raw_current = self._fetch_managed_raw_records(id, credentials)
        actions = self._reconcile(raw_current, desired["records"])

        puts = [a for a in actions if a[0] == "PUT"]
        posts = [a for a in actions if a[0] == "POST"]
        deletes = [a for a in actions if a[0] == "DELETE"]
        logger.info(
            "",
            extra={"id": id, "puts": len(puts), "posts": len(posts), "deletes": len(deletes)},
        )

        # Order matters: updates and additions land before removals so
        # the zone is never missing a record it will have again.
        for _, record_id, record in puts:
            self._put_record(id, record_id, record, credentials)
        for _, _, record in posts:
            self._post_record(id, record, credentials)
        for _, record_id, _ in deletes:
            self._delete_record(id, record_id, credentials)

        return self.read(id, credentials)

    def delete(self, id: str, credentials: dict[str, str]) -> None:
        try:
            self._request("DELETE", f"{BASE_URL}/domains/{id}", credentials)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        return None

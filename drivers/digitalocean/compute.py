# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import http.client
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from aiform import log
from aiform.driver import DriverUpdateNotSupported, ResourceDriver
from aiform.exceptions import ResourceNotFoundError
from aiform.models import HealthReport, HealthStatus, MetricKind, Sample

BASE_URL = "https://api.digitalocean.com/v2"
REQUEST_TIMEOUT_SECONDS = 30
# DO's conventional statuses for "your request is well-formed but this
# particular resize is invalid" -- anything else (429, 5xx, 401/403) is a
# transient or unrelated CSP failure, not evidence the resize itself is
# unsupported. Named as a constant, not an inline tuple, so there's one
# place in the code to update -- flagged during /code-review.
# Deliberately NOT expanded to 403/409/423 despite a later /code-review
# suggestion: 403 reads as a permission/credential problem, not "this
# resize is invalid" -- misclassifying that into a destroy+recreate is
# exactly the dangerous pattern this fix exists to prevent. 409/423 read
# as "droplet locked/busy," which is transient (retry later), not a
# permanent rejection either. Left as-is unless a concrete, observed DO
# status code demonstrates otherwise.
_RESIZE_REJECTED_STATUSES = (400, 422)

# Backoff schedule for _poll_until, replacing the previous fixed 2s
# interval (issue #171). DigitalOcean's own account-wide action log
# (GET /v2/actions) showed most power_off actions completing in ~11-14s
# but one taking 303s -- a genuine ~20-25x outlier, not a bug in the poll
# loop itself (a plain, correct poll-predicate-sleep loop). A flat
# interval forced picking one cadence for both regimes: fast enough not
# to waste requests, slow enough to survive the outlier -- which is why
# the flat total budget needed raising three separate times (an
# unnumbered 20->30 attempts/40s->60s raise, then #152's 30->45
# attempts/60s->90s, then #168's 45->75 attempts/90s->150s) while
# polling at that same rate the entire time regardless of how close to
# done the wait actually was. Starting fast and doubling keeps the first
# retry delay exactly at the old fixed interval (still 2s, so the second
# check lands at the same t=2s the flat interval always used) and tracks
# it closely through the ~11-14s range this issue's own data was drawn
# from; past that the gap between checks necessarily widens as
# delay grows toward the cap (up to _POLL_MAX_DELAY_SECONDS between
# checks), trading a longer worst-case detection lag for collapsing how
# many requests it takes to survive a rare long wait. Bounding total
# elapsed time rather than attempt count makes "how long are we willing
# to wait, total" an explicit number instead of an emergent side effect
# of attempts * delay -- 420s is ~38% margin over the observed 303s
# outlier, the same margin #170 used over its own observed worst case
# (150s over a 108.6s worst case).
#
# That ceiling is per _poll_until call, not per operation: update()'s
# size+backups path chains up to four separate polls (power-off, resize,
# power-on, backups), so a pathological worst case across all of them is
# ~4*420s =~ 28 minutes, versus the old design's ~4*150s =~ 10 minutes --
# larger, because 420s is sized off power_off's own observed outlier and
# applied uniformly to every step, rather than giving each step its own
# narrower, separately-hand-tuned budget, which would reintroduce
# exactly the per-site magic numbers this issue removes. Every poll here
# hits the same DigitalOcean infrastructure the outlier was observed
# against, so there is no evidence a shorter ceiling would be safe for
# the other steps either -- flagged during /code-review, deliberately
# left as a documented trade-off rather than a per-step override.
_POLL_INITIAL_DELAY_SECONDS = 2
_POLL_BACKOFF_MULTIPLIER = 2
_POLL_MAX_DELAY_SECONDS = 20
_POLL_TIMEOUT_SECONDS = 420

# health()/metrics() are run in a loop by a script and a human waits on
# them, so they must not inherit REQUEST_TIMEOUT_SECONDS -- six times
# specs/driver_observability.md's per-resource target, per resource, per
# request. Be honest about what this bounds: metrics() issues one request
# per family below, so a pathological worst case is still that many
# multiples of this, which is past the target. The spec says plainly that
# no whole-sweep deadline exists yet and owns that gap under PLAN.md
# §10's timeout/retry entry.
OBSERVE_TIMEOUT_SECONDS = 5

# DigitalOcean's monitoring series steps every 120 seconds and its newest
# point was observed up to ~100s old (probe 12), so a window narrower
# than about four minutes can legitimately return nothing for a droplet
# that is reporting normally.
METRIC_WINDOW_SECONDS = 600

# Statuses this driver models. Anything else is DEGRADED rather than OK:
# a verdict of OK on a status the driver has never seen asserts health it
# has no basis for.
_FAILING_STATUSES = ("off", "archive")

# (endpoint, sample name, kind). Bare names, unit in the name: a future
# exporter applies any prefix and the identity labels, and needs them
# unqualified to do it.
#
# cpu is the one COUNTER. Its per-mode seconds reset when the droplet
# reboots, which an earlier version of the counter-honesty rule would
# have disqualified -- see that rule's amendment in
# specs/driver_observability.md: a reset is a documented, handled
# condition in every consumer that types series this way, and shipping
# the canonical counter as a gauge makes rate() meaningless.
#
# bandwidth is deliberately absent: it is the only metric in the family
# that needs interface and direction parameters (probe 11 got a 400
# without them), so it costs four more requests for four more series, and
# it reports a rate rather than a state a human reads off a droplet.
_METRIC_FAMILIES = (
    ("memory_total", "memory_total_bytes", MetricKind.GAUGE),
    ("memory_available", "memory_available_bytes", MetricKind.GAUGE),
    ("filesystem_free", "filesystem_free_bytes", MetricKind.GAUGE),
    ("filesystem_size", "filesystem_size_bytes", MetricKind.GAUGE),
    ("load_1", "load1", MetricKind.GAUGE),
    ("load_5", "load5", MetricKind.GAUGE),
    ("load_15", "load15", MetricKind.GAUGE),
    ("cpu", "cpu_seconds_total", MetricKind.COUNTER),
)

# Identity DigitalOcean stamps on every series. The output already prints
# the resource beside its samples, and a future exporter must be able to
# stamp its own without colliding with the driver's.
_IDENTITY_LABEL = "host_id"

# The PARAM_SCHEMA fields DigitalOcean can change on a live droplet.
# Everything else forces a replace: region needs a snapshot+recreate,
# image a destructive rebuild, monitoring has no API action at all, and
# ssh_keys is guest-OS state cloud-init writes once at first boot --
# see specs/digitalocean_compute.md's capability table.
_IN_PLACE_UPDATABLE_FIELDS = ("size", "tags", "backups")

# Named explicitly rather than via logging.getLogger(__name__).
# orchestrator.py's load_driver() execs this file as a module with a
# synthetic name ("aiform_driver_digitalocean_compute", via
# importlib.util.spec_from_file_location), not "drivers.digitalocean.compute"
# -- that name is not a dotted descendant of the "aiform" logger
# aiform/log.py's configure() attaches handlers to, so __name__ here
# would silently produce a logger with no handler and no propagation to
# either sink. Not an error, just missing output -- confirmed empirically
# before writing this, not assumed.
logger = logging.getLogger("aiform.driver.digitalocean.compute")


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
    # Advisory only -- update() is the arbiter. Mirrors the fields that
    # genuinely force a replace, so `plan` can warn up front instead of
    # the user meeting the mid-apply "Replace ...?" prompt.
    LIKELY_REPLACE_FIELDS = ["image", "region", "ssh_keys", "monitoring"]
    NON_DIFFABLE_FIELDS = ["ssh_keys"]
    UNORDERED_FIELDS = ["tags"]

    def _request(self, method, url, credentials, body=None, timeout=REQUEST_TIMEOUT_SECONDS):
        data = None
        headers = {"Authorization": f"Bearer {credentials['DIGITALOCEAN_TOKEN']}"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=timeout) as response:
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

    def _get_droplet(self, id, credentials, timeout=REQUEST_TIMEOUT_SECONDS):
        payload = self._request("GET", f"{BASE_URL}/droplets/{id}", credentials, timeout=timeout)
        return payload["droplet"]

    def _action(self, id, credentials, body):
        self._request("POST", f"{BASE_URL}/droplets/{id}/actions", credentials, body=body)

    def _do_error_message(self, exc: urllib.error.HTTPError) -> str | None:
        # Best-effort: DO's error responses are typically {"id": "...",
        # "message": "..."} JSON, and that message is usually the single
        # most useful diagnostic string available -- it names the actual
        # reason (e.g. a disk-size-class mismatch) that a bare HTTP 422
        # doesn't. Never let extraction itself raise; a malformed or
        # already-consumed body just means no do_message field gets
        # logged, not a crash in the middle of error handling.
        try:
            body = exc.read()
        except Exception:
            return None
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return data.get("message") if isinstance(data, dict) else None

    def _poll_until(self, id, credentials, predicate, step):
        # No timeout/delay override parameters -- every call site shares
        # this one schedule (issue #171 removed create()'s previous
        # separate override), and CLAUDE.md's "don't add config knobs for
        # scenarios that can't happen yet" argues against re-adding a seam
        # nothing currently needs.
        start = time.monotonic()
        delay = _POLL_INITIAL_DELAY_SECONDS
        attempt = 0
        while True:
            attempt += 1
            droplet = self._get_droplet(id, credentials)
            if predicate(droplet):
                logger.info(
                    "",
                    extra={
                        "id": id,
                        "step": step,
                        "attempts_used": attempt,
                        "duration_ms": log.elapsed_ms(start),
                        "outcome": "success",
                    },
                )
                return droplet
            elapsed = time.monotonic() - start
            if elapsed >= _POLL_TIMEOUT_SECONDS:
                logger.error(
                    "",
                    extra={
                        "id": id,
                        "step": step,
                        "attempts_used": attempt,
                        "duration_ms": log.elapsed_ms(start),
                        "outcome": "timeout",
                    },
                )
                raise TimeoutError(f"timed out waiting for droplet {id} during the {step} step")
            time.sleep(min(delay, _POLL_TIMEOUT_SECONDS - elapsed))
            delay = min(delay * _POLL_BACKOFF_MULTIPLIER, _POLL_MAX_DELAY_SECONDS)

    def _do_action_and_wait(self, id, credentials, body, predicate, step):
        self._action(id, credentials, body)
        return self._poll_until(id, credentials, predicate, step)

    def _fold_do_error_into_exc(self, exc: urllib.error.HTTPError) -> str | None:
        # Shared by both the resize exc and the power-on-restore
        # restore_exc in update() -- was copy-pasted separately for each,
        # flagged during /code-review. Returns the extracted DO message
        # (or None) rather than a bare bool -- a caller needs the actual
        # text for its own structured logging (e.g. a "do_message" extra
        # field distinct from the now-prefixed exc.msg) as well as the
        # true/false signal, and re-deriving it via a second
        # _do_error_message(exc) call would read exc's already-consumed
        # body a second time and silently get None back -- found merging
        # this driver's structured-logging work in.
        do_message = self._do_error_message(exc)
        if do_message:
            exc.msg = f"{exc.msg}: {do_message}"
        return do_message

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
        # Prior to issue #171, create() passed its own wider fixed-interval
        # budget here (max_attempts=60, delay_seconds=3 -- 180s) because the
        # old shared default was tuned for update()'s shorter power-off/
        # resize profile and would otherwise time out a still-converging
        # create. #171's backoff schedule already gives every _poll_until
        # caller a 420s total-elapsed ceiling (see _POLL_TIMEOUT_SECONDS)
        # -- comfortably above that old 180s override -- and create()'s
        # override was never once exercised across either of update()'s own
        # budget raises (#152, #168), so there's no observed evidence it
        # needs a longer or differently-shaped wait than update() gets.
        # Sharing one schedule here removes one of the two hardcoded
        # _poll_until budgets PLAN.md §10 named as needing a real backoff
        # policy instead of magic numbers, rather than just retuning both
        # again.
        droplet = self._poll_until(new_id, credentials, lambda d: d["status"] == "active", "create")
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
        # Deliberately does NOT special-case NON_DIFFABLE_FIELDS
        # (ssh_keys) out of this comparison. read() can never repopulate
        # ssh_keys (DO's droplet GET has no such field), but the caller
        # (aiform/orchestrator.py's refresh_resource()) carries the prior
        # state's ssh_keys value forward into `current` before update()
        # is ever invoked -- by the time this runs, an unchanged ssh_keys
        # already matches desired, and a genuinely changed one already
        # differs, exactly like any other field. Excluding it here
        # instead would silently drop a real ssh_keys edit (this was
        # tried and reverted -- see specs/digitalocean_compute.md's Edge
        # cases and aiform/driver.py's NON_DIFFABLE_FIELDS docstring).
        diff_fields = [
            key
            for key in self.PARAM_SCHEMA["properties"]
            if key in desired and current.get(key) != desired.get(key)
        ]
        if not diff_fields:
            return dict(current)

        # Before the partition, not after: a diff that also touches a
        # replace-forcing field would otherwise raise
        # DriverUpdateNotSupported first, and the destroy+recreate the user
        # then approves hands the same malformed value to create() -- which
        # DO rejects, leaving the droplet deleted and nothing rebuilt.
        self._reject_malformed_values(id, diff_fields, desired)

        replace_forcing = [f for f in diff_fields if f not in _IN_PLACE_UPDATABLE_FIELDS]
        if replace_forcing:
            # Only the genuinely replace-forcing fields, not the whole diff:
            # a size+region change is a replace because of region alone.
            raise DriverUpdateNotSupported(
                f"DigitalOcean droplets cannot update {replace_forcing} in place; "
                "a replace is required for this diff.",
                unsupported_fields=replace_forcing,
            )

        # Order matters. The resize is the only step that can raise
        # DriverUpdateNotSupported once running, and the orchestrator answers
        # that with a gate #2 review and a "Replace ...?" prompt the user may
        # decline -- so it runs before anything else is mutated. The
        # invariant: update() never raises DriverUpdateNotSupported after
        # changing anything but a power state it restores.
        if "size" in diff_fields:
            self._resize_in_place(id, current, desired, credentials)
        if "tags" in diff_fields:
            self._apply_tag_changes(id, current.get("tags") or [], desired["tags"], credentials)
        if "backups" in diff_fields:
            self._set_backups(id, desired["backups"], credentials)

        # One GET after every mutation, rather than reusing the resize path's
        # last poll -- otherwise a size+tags update returns the pre-tag tags.
        attrs = self._flatten(self._get_droplet(id, credentials))
        # desired.get(key, current.get(key, ...)) -- prefer desired's value
        # when the field is actually managed, else preserve current's rather
        # than resetting to a bare default: desired omitting an optional
        # field means it isn't part of this diff at all (see diff_fields
        # above), not that it should revert to [].
        attrs["ssh_keys"] = desired.get("ssh_keys", current.get("ssh_keys", []))
        attrs["backups"] = desired.get("backups", current.get("backups", False))
        attrs["monitoring"] = desired.get("monitoring", current.get("monitoring", False))
        return attrs

    def _reject_malformed_values(self, id, diff_fields, desired):
        # Nothing upstream checks params against PARAM_SCHEMA -- driver.py's
        # own docstring claims the orchestrator does, but it does not -- so
        # these values arrive exactly as YAML parsed them. Both checks guard
        # a step that acts on the value locally before any API call could
        # reject it: `tags: web` (a scalar, not a one-item list) would
        # otherwise be iterated character by character, creating tags "w",
        # "e" and "b" and unassigning every real one; `backups: "false"` is
        # a non-empty string, so a bool() coercion would enable billed
        # backups for a user who asked to switch them off.
        #
        # ValueError, not DriverUpdateNotSupported: a malformed value is not
        # a diff the CSP declined, and a destroy+recreate would only feed
        # the same value to create(). Raised before any mutation, so the
        # ordering invariant above still holds.
        if "tags" in diff_fields:
            tags = desired["tags"]
            if not isinstance(tags, list) or not all(isinstance(t, str) and t for t in tags):
                # Non-empty specifically: an empty name makes the existence
                # check GET /v2/tags/, which is DO's *list* endpoint and
                # answers 200, so the tag would be reported as already
                # existing and the assignment would then fail at DO.
                raise ValueError(
                    f"droplet {id}: params 'tags' must be a list of non-empty strings, got {tags!r}"
                )
        if "backups" in diff_fields and not isinstance(desired["backups"], bool):
            raise ValueError(
                f"droplet {id}: params 'backups' must be true or false, got {desired['backups']!r}"
            )

    def _tag_url(self, tag: str, suffix: str = "") -> str:
        # The tag goes into the request path, so a malformed name must not
        # be able to inject an extra path segment. ':' stays unescaped
        # because DO's own pattern (^[a-zA-Z0-9_\-\:]+$) allows it and DOKS
        # really uses it (k8s:<cluster-id>) -- percent-encoding it would
        # risk a 404 on a name create() accepts. Both callers route through
        # here so the escaping cannot drift between them.
        return f"{BASE_URL}/tags/{urllib.parse.quote(tag, safe=':')}{suffix}"

    def _ensure_tag_exists(self, tag, credentials):
        # Creating a droplet with tags auto-creates them, but assigning to an
        # existing droplet does not -- POST /v2/tags/{name}/resources 404s on
        # an unknown tag. Checking first rather than creating on that 404
        # keeps the two "not found" cases (tag vs droplet) from having to be
        # told apart from one status code.
        try:
            self._request("GET", self._tag_url(tag), credentials)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            self._request("POST", f"{BASE_URL}/tags", credentials, body={"name": tag})

    def _apply_tag_changes(self, id, current_tags, desired_tags, credentials):
        added = [t for t in desired_tags if t not in current_tags]
        removed = [t for t in current_tags if t not in desired_tags]
        logger.info("", extra={"id": id, "tags_added": added, "tags_removed": removed})

        # An HTTPError here propagates as a real driver error and is never
        # converted to DriverUpdateNotSupported: a tag DigitalOcean rejects
        # would be rejected at create() too, so a destroy+recreate is no
        # remedy for it.
        body = {"resources": [{"resource_id": str(id), "resource_type": "droplet"}]}
        for tag in added:
            self._ensure_tag_exists(tag, credentials)
            self._request("POST", self._tag_url(tag, "/resources"), credentials, body=body)
        for tag in removed:
            self._request("DELETE", self._tag_url(tag, "/resources"), credentials, body=body)

    def _set_backups(self, id, enabled, credentials):
        # No backup_policy is sent: PARAM_SCHEMA models backups as a bare
        # boolean, and DO defaults to daily when the key is omitted.
        # `enabled` is a real bool -- _reject_malformed_values guarantees it
        # rather than coercing, so a truthy string cannot turn backups on.
        action = "enable_backups" if enabled else "disable_backups"
        logger.info("", extra={"id": id, "step": action})
        self._do_action_and_wait(
            id,
            credentials,
            {"type": action},
            lambda d: ("backups" in d.get("features", [])) == enabled,
            action.replace("_", "-"),
        )

    def _resize_in_place(self, id, current, desired, credentials):
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

        logger.info(
            "",
            extra={
                "id": id,
                "status": status,
                "current_size": current.get("size"),
                "target_size": target_size,
            },
        )

        we_powered_off = status == "active"
        if we_powered_off:
            self._do_action_and_wait(
                id, credentials, {"type": "power_off"}, lambda d: d["status"] == "off", "power-off"
            )

        try:
            self._action(id, credentials, {"type": "resize", "disk": False, "size": target_size})
        except urllib.error.HTTPError as exc:
            # DO's own diagnostic text (e.g. "rate limit exceeded, retry
            # after 30s") folded into exc.msg in place -- keeps
            # exc.code/isinstance intact for the re-raise path below,
            # while still enriching what str(exc) shows. Previously
            # extracted only for the DriverUpdateNotSupported branch,
            # silently dropping it for exactly the transient failures a
            # human would most want it for -- flagged during /code-review.
            resize_do_message = self._fold_do_error_into_exc(exc)

            # Only restore power state we ourselves changed -- a droplet that
            # started "off" (the user's own choice) stays off on a rejected
            # resize, it doesn't get turned on as a side effect of the failure.
            # This restoration happens regardless of *why* the resize failed.
            if we_powered_off:
                try:
                    self._do_action_and_wait(
                        id,
                        credentials,
                        {"type": "power_on"},
                        lambda d: d["status"] == "active",
                        "power-on",
                    )
                except (
                    urllib.error.URLError,
                    TimeoutError,
                    http.client.HTTPException,
                    OSError,
                    json.JSONDecodeError,
                ) as restore_exc:
                    # Matches tests/system/conftest.py's
                    # wait_until_droplet_gone() exactly, for the identical
                    # urlopen/read/json.loads shape against the same DO
                    # API (established in fc2dd1d). An earlier version of
                    # this clause narrowed to just (HTTPError, TimeoutError)
                    # on the mistaken belief those were the only exceptions
                    # _do_action_and_wait/_poll_until can raise -- flagged
                    # during a second /code-review pass: an uncaught
                    # URLError/OSError/JSONDecodeError here would silently
                    # lose the original resize failure's context, the same
                    # way the bare except Exception this replaced could.
                    # HTTPError is a URLError subclass, so it's still
                    # covered without listing it separately. A genuinely
                    # unexpected type (never observed against this API)
                    # still propagates immediately rather than being folded
                    # into a generic "restore also failed" message that
                    # would make an unrelated bug harder to distinguish
                    # from a real DO-API restore failure.
                    #
                    # If restoring power *also* fails, that failure must not
                    # silently replace the original resize error -- flagged
                    # during /code-review. A compounding failure like this
                    # is unusual enough to warrant a loud, undisguised error
                    # rather than a guess at classification either way.
                    if isinstance(restore_exc, urllib.error.HTTPError):
                        self._fold_do_error_into_exc(restore_exc)
                    raise RuntimeError(
                        f"droplet {id}: resize to {target_size!r} failed ({exc}) and "
                        f"the power-on restore that followed also failed ({restore_exc}) "
                        "-- droplet may still be powered off"
                    ) from exc

            if exc.code not in _RESIZE_REJECTED_STATUSES:
                # Not DO telling us this specific resize is invalid -- a
                # transient failure (429, 5xx) or an auth problem (401/403).
                # Converting this into DriverUpdateNotSupported would
                # misclassify a retriable/unrelated failure as "this
                # resize is unsupported" and trigger a destructive
                # destroy+recreate for a resize that might have succeeded
                # on retry. Let it propagate as a real error instead.
                #
                # Logged (not just raised) specifically because this is a
                # real driver-execution failure with no further handling
                # above it -- the log file is the only durable record of
                # what happened once this propagates. Doesn't claim
                # "falling back to destroy+recreate" the way the rejection
                # branch below does -- that would be wrong here, no replace
                # is triggered on this path. Split into two log calls (was
                # one unconditional call before this branch existed) when
                # merging the resize-classification fix in.
                logger.warning(
                    "DigitalOcean's resize action failed with an unrecognized "
                    "status; propagating as a genuine driver error, no replace "
                    "triggered",
                    extra={
                        "id": id,
                        "target_size": target_size,
                        "http_status": exc.code,
                        "do_message": resize_do_message,
                    },
                )
                raise
            logger.warning(
                "DigitalOcean rejected the in-place resize; falling back to destroy+recreate",
                extra={
                    "id": id,
                    "target_size": target_size,
                    "http_status": exc.code,
                    "do_message": resize_do_message,
                },
            )
            # Built from the already-enriched exc.msg when there's
            # something to add (see _fold_do_error_into_exc above),
            # rather than re-appending the DO message a second time
            # through an independent check -- flagged during /code-review:
            # the same diagnostic text was threaded through two
            # unsynchronized `if do_message:` blocks. Not unconditional,
            # though -- flagged on a second pass: always appending exc.msg
            # would add urllib's bare HTTP reason phrase even with no DO
            # message to add, changing output for the no-body case.
            reason = f"DigitalOcean rejected an in-place resize of droplet {id} to {target_size!r}"
            if resize_do_message:
                reason += f": {exc.msg}"
            raise DriverUpdateNotSupported(reason, unsupported_fields=["size"]) from exc

        # Unlike the rejection path above, a *successful* resize always powers
        # the droplet back on, even if it started "off" -- this driver has now
        # changed its size, so it's expected to come back up, not stay down.
        self._poll_until(id, credentials, lambda d: d["size_slug"] == target_size, "resize")
        self._do_action_and_wait(
            id, credentials, {"type": "power_on"}, lambda d: d["status"] == "active", "power-on"
        )

    def delete(self, id, credentials):
        try:
            self._request("DELETE", f"{BASE_URL}/droplets/{id}", credentials)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        return None

    def health(self, id, credentials):
        # Deliberately not self.read(): read() projects the droplet down
        # to what is worth storing, and `locked` -- an action in flight,
        # which is exactly a DEGRADED signal -- is not in it. Widening
        # read() to carry it would put a field that churns into state,
        # which is the thing specs/driver_observability.md forbids.
        try:
            droplet = self._get_droplet(id, credentials, timeout=OBSERVE_TIMEOUT_SECONDS)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise ResourceNotFoundError(f"DigitalOcean droplet {id} not found") from exc
            # Everything else propagates. A driver does not classify its
            # own failures as UNKNOWN -- observability.collect() does
            # that, and keeps the error text a swallowed exception loses.
            raise

        status = droplet["status"]
        ipv4 = self._flatten(droplet)["ipv4_address"]
        observations = {
            "status": status,
            "locked": str(bool(droplet.get("locked"))).lower(),
            "region": droplet["region"]["slug"],
            "size": droplet["size_slug"],
            "ipv4_address": ipv4 or "none",
            "features": ",".join(droplet.get("features") or []) or "none",
        }

        if status in _FAILING_STATUSES:
            return HealthReport(
                status=HealthStatus.FAILING,
                summary=f'status is "{status}"',
                observations=observations,
            )
        if status == "new":
            return HealthReport(
                status=HealthStatus.DEGRADED,
                summary="still provisioning",
                observations=observations,
            )
        if status != "active":
            return HealthReport(
                status=HealthStatus.DEGRADED,
                summary=f'unmodelled status "{status}"',
                observations=observations,
            )
        if droplet.get("locked"):
            return HealthReport(
                status=HealthStatus.DEGRADED,
                summary="active but locked: an action is in flight",
                observations=observations,
            )
        if not ipv4:
            return HealthReport(
                status=HealthStatus.DEGRADED,
                summary="active with no public v4 address",
                observations=observations,
            )
        return HealthReport(
            status=HealthStatus.OK,
            summary=f"active, public v4 {ipv4}",
            observations=observations,
        )

    def metrics(self, id, credentials):
        end = int(time.time())
        start = end - METRIC_WINDOW_SECONDS
        samples = []
        for endpoint, name, kind in _METRIC_FAMILIES:
            # No try/except: a 401 here means either the droplet is gone
            # or the token is wrong, and DigitalOcean answers 401 for
            # both (probes 14 and 15 -- it never 404s on this endpoint).
            # Guessing ResourceNotFoundError would report a live droplet
            # as deleted every time a token was wrong, so the error
            # propagates and collect() records it as what it is.
            payload = self._request(
                "GET",
                f"{BASE_URL}/monitoring/metrics/droplet/{endpoint}"
                f"?host_id={id}&start={start}&end={end}",
                credentials,
                timeout=OBSERVE_TIMEOUT_SECONDS,
            )
            samples.extend(self._samples_from_series(payload, name, kind))
        return samples

    def _samples_from_series(self, payload, name, kind):
        # An empty result is the normal answer for a droplet whose agent
        # has not reported -- one created with monitoring off, or simply
        # younger than the agent's first push (probes 23 and 24). DO
        # answers 200 with no series, not an error, and so does this.
        for series in ((payload or {}).get("data") or {}).get("result") or []:
            values = series.get("values") or []
            if not values:
                continue
            # The newest point, not an average of the window: the value
            # is already pre-averaged by DigitalOcean over its own 120s
            # step, and resampling it here would paper over a staleness
            # the operator should see rather than fix.
            yield Sample(
                name=name,
                kind=kind,
                value=float(values[-1][1]),
                labels={
                    key: value
                    for key, value in (series.get("metric") or {}).items()
                    if key != _IDENTITY_LABEL
                },
            )

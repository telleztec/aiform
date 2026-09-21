# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Runtime health and metrics over tracked resources -- the day-2
questions read() cannot answer, for `aiform resource check/metrics/status`
(specs/driver_observability.md).

Nothing here writes state, and nothing here calls a model. Both are
contract, not incidental: an inspection command that mutates the record
makes the next `plan` mean something different because you looked, and
these commands are built to be run in a loop by a script.
"""

import json
import logging
import math
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from aiform import config, orchestrator, parser, planner, state
from aiform.driver import CapabilityNotSupported, ResourceDriver
from aiform.exceptions import ResourceNotFoundError
from aiform.models import HealthReport, HealthStatus, MetricKind, Sample, StateEntry
from aiform.state import State

logger = logging.getLogger(__name__)

# The character sets a metrics system will require of these later. A
# driver's mistake is caught here rather than on the Pydantic model so it
# lands as a dropped sample naming the driver, not a crash -- see
# specs/models.md.
# fullmatch, not match: `$` also matches just before a trailing newline,
# so "cpu_percent\n" validated and then rendered as one sample split
# across two lines with every other row padded to the inflated width.
_METRIC_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")

# A future exporter must stamp these to make a series addressable, and
# cannot do so safely if a driver has already put its own values there.
_IDENTITY_LABELS = frozenset({"provider", "resource_type", "name", "id"})

# ok < degraded < unknown < failing. UNKNOWN sits below FAILING because
# "aiform could not find out" is a weaker claim than "the CSP says it is
# broken" -- but above DEGRADED, since a resource that cannot be reached
# is less trustworthy than one reporting a partial problem about itself.
_SEVERITY = {
    HealthStatus.OK: 0,
    HealthStatus.DEGRADED: 1,
    HealthStatus.UNKNOWN: 2,
    HealthStatus.FAILING: 3,
}

TEXT = "text"
JSON = "json"
_FORMATS = (TEXT, JSON)

_COLUMN_GAP = "  "
_ROW_INDENT = "  "
_OBSERVATION_INDENT = "    "


@dataclass
class ResourceReading:
    """One tracked resource's answers. `health` and `samples` are
    independent: one method declining or raising never skips the other,
    so a single broken driver cannot blank the report."""

    resource_key: str
    provider: str
    resource_type: str
    name: str
    id: str
    health: HealthReport | None
    health_unsupported: str | None
    samples: list[Sample]
    samples_unsupported: str | None
    errors: list[str] = field(default_factory=list)


@dataclass
class Collection:
    readings: list[ResourceReading]
    elapsed_seconds: float
    # Family-level only. A per-sample rejection has a resource that
    # produced it and goes in that resource's own errors; a family
    # rejected because two drivers gave one name different kinds belongs
    # to neither driver alone.
    errors: list[str] = field(default_factory=list)


@dataclass
class ConfigStatus:
    """`status`' config answer, structured the way `health` beside it
    already is: the verdict is a value, and prose appears only where the
    reason genuinely is free text. `in_sync` is None when aiform could
    not determine it at all -- gone, unreadable, no source file -- and
    `detail` then says which of those it was."""

    in_sync: bool | None
    spec_file: str
    drifted_fields: list[str]
    detail: str | None


@dataclass
class StatusReport:
    """`aiform resource status`. Four independent answers -- deployed,
    live, config, health -- over the resource's identity; any one of the
    four can be the surprising one, so none is folded into another.

    `deployed_at`/`id` and `config` are stored as values rather than as
    the sentences the text form prints: a consumer of `--format json`
    wanting just the deploy timestamp, just the id, or a plain yes/no "is
    it in sync" should not have to parse them back out of a string built
    for a terminal. _status_value() composes those sentences at render
    time, the way it already did for `health`. `live` is still a string:
    its error arm is free-form driver text, and #161 did not ask for it."""

    resource_key: str
    provider: str
    resource_type: str
    name: str
    id: str
    # Not `| None`: status_for() raises for an untracked key, so every
    # report has a state entry and nothing can produce None. A field
    # whose None arm no caller can reach is error handling for a scenario
    # that cannot happen.
    deployed_at: datetime
    live: str
    config: ConfigStatus
    health: HealthReport | None
    health_unsupported: str | None


def resolve_name(name: str, st: State) -> str:
    """A `name:` frontmatter value -> the one matching state key."""
    matches = [key for key, entry in st.resources.items() if entry.name == name]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        tracked = ", ".join(sorted(entry.name for entry in st.resources.values())) or "nothing"
        raise ValueError(f"no tracked resource is named {name!r}; tracked: {tracked}")
    # Never a guess: the two could be a droplet and the firewall in front
    # of it, and checking the wrong one answers confidently about the
    # thing you did not ask about.
    raise ValueError(
        f"{name!r} matches more than one tracked resource: {', '.join(sorted(matches))} "
        "-- name one of them"
    )


def _oneline(text: str) -> str:
    """Collapse every whitespace run to one space. `check` renders one
    line per resource, and a continuation line at column zero reads as
    another resource's row -- so any free-form text that reaches a
    rendered line goes through here, whether it came from an exception or
    from the driver itself. Stricter than log.py, which only replaces
    newlines."""
    return " ".join(text.split())


def _warn(message: str, **fields) -> str:
    logger.warning(message, extra=fields)
    return message


def _health_for(
    driver: ResourceDriver, entry: StateEntry, credentials: dict[str, str], errors: list[str]
) -> tuple[HealthReport | None, str | None]:
    try:
        report = driver.health(entry.id, credentials)
    except CapabilityNotSupported as exc:
        # Declining a capability is not an error anywhere in this spec.
        return None, exc.reason
    except ResourceNotFoundError:
        # The resource being gone is the finding, not a failure to
        # observe, so it is the verdict and is not repeated in errors.
        return HealthReport(status=HealthStatus.FAILING, summary="resource not found"), None
    except Exception as exc:  # noqa: BLE001 - one bad driver must not blank the report
        return _unknown(entry, f"health() failed: {exc}", errors), None

    if not isinstance(report, HealthReport):
        # Caught here rather than in the renderer: a driver returning a
        # dict would otherwise crash render_check partway through the
        # sweep, taking every other resource's line with it.
        return _unknown(
            entry, f"health() returned {type(report).__name__}, not HealthReport", errors
        ), None
    if report.status is HealthStatus.UNKNOWN:
        # driver.py's docstring forbids it: UNKNOWN means aiform could
        # not find out, and only collect() knows that. A driver returning
        # it has swallowed the error text that would say what went wrong,
        # so the verdict stands but the driver bug is recorded.
        errors.append(
            _warn(
                f"{entry.provider}.{entry.resource_type} health() returned UNKNOWN; a driver "
                "must let its own failures propagate instead",
                resource_key=_key_of(entry),
                operation="health",
            )
        )
    return report, None


def _unknown(entry: StateEntry, detail: str, errors: list[str]) -> HealthReport:
    message = _warn(
        _oneline(f"{entry.provider}.{entry.resource_type} {detail}"),
        resource_key=_key_of(entry),
        operation="health",
    )
    errors.append(message)
    return HealthReport(status=HealthStatus.UNKNOWN, summary=message)


def _samples_for(
    driver: ResourceDriver, entry: StateEntry, credentials: dict[str, str], errors: list[str]
) -> tuple[list[Sample], str | None]:
    try:
        raw = driver.metrics(entry.id, credentials)
    except CapabilityNotSupported as exc:
        return [], exc.reason
    except ResourceNotFoundError:
        # metrics never calls health(), so without this a vanished
        # resource prints an empty block and exits 0 with nothing said.
        errors.append(
            _warn(
                "resource not found",
                resource_key=_key_of(entry),
                operation="metrics",
            )
        )
        return [], None
    except Exception as exc:  # noqa: BLE001 - same reason as _health_for
        errors.append(
            _warn(
                _oneline(f"{entry.provider}.{entry.resource_type} metrics() failed: {exc}"),
                resource_key=_key_of(entry),
                operation="metrics",
            )
        )
        return [], None
    if not isinstance(raw, list) or not all(isinstance(s, Sample) for s in raw):
        # Same reasoning as health()'s type check: without this a driver
        # returning dicts raises AttributeError out of collect() and
        # blanks every other resource's reading too.
        errors.append(
            _warn(
                f"{entry.provider}.{entry.resource_type} metrics() did not return list[Sample]",
                resource_key=_key_of(entry),
                operation="metrics",
            )
        )
        return [], None
    return _validated(raw, entry, errors), None


def _rejection(sample: Sample, reason: str) -> str:
    return f"dropped sample {sample.name!r}: {reason}"


def _validated(samples: list[Sample], entry: StateEntry, errors: list[str]) -> list[Sample]:
    kept: list[Sample] = []
    for sample in samples:
        reason = _rejection_reason(sample)
        if reason is None:
            kept.append(sample)
            continue
        errors.append(
            _warn(
                _rejection(sample, reason),
                resource_key=_key_of(entry),
                operation="metrics",
            )
        )
    return kept


def _rejection_reason(sample: Sample) -> str | None:
    if not _METRIC_NAME.fullmatch(sample.name):
        return "name is not a valid metric name"
    if sample.kind is MetricKind.COUNTER and not sample.name.endswith("_total"):
        return "a counter's name must end in '_total'"
    if not math.isfinite(sample.value):
        # The format has spellings for these, but a driver producing one
        # has almost always divided by an unchecked zero.
        return f"value is not finite ({sample.value})"
    for label in sample.labels:
        if label in _IDENTITY_LABELS:
            return f"label {label!r} collides with an identity label the output already carries"
        if not _LABEL_NAME.fullmatch(label):
            return f"label name {label!r} is not a valid label name"
    return None


def _key_of(entry: StateEntry) -> str:
    return orchestrator.resource_key(entry.provider, entry.resource_type, entry.name)


def _drop_colliding_families(readings: list[ResourceReading]) -> list[str]:
    """A consumer types a series by name, so two drivers giving one name
    different kinds cannot both be represented; silently picking one
    would mistype the other's samples."""
    kinds: dict[str, dict[MetricKind, set[str]]] = {}
    for reading in readings:
        driver = f"{reading.provider}.{reading.resource_type}"
        for sample in reading.samples:
            kinds.setdefault(sample.name, {}).setdefault(sample.kind, set()).add(driver)

    errors: list[str] = []
    for name, by_kind in sorted(kinds.items()):
        if len(by_kind) < 2:
            continue
        claims = ", ".join(
            f"{d} says {kind.value}"
            for kind, drivers in sorted(by_kind.items())
            for d in sorted(drivers)
        )
        errors.append(_warn(f"dropped family {name!r}: {claims}", metric_name=name))
        for reading in readings:
            reading.samples = [s for s in reading.samples if s.name != name]
    return errors


def collect(
    *,
    keys: list[str] | None = None,
    want_health: bool = True,
    want_metrics: bool = True,
    state_path: Path = state.DEFAULT_STATE_PATH,
) -> Collection:
    """Read tracked resources: exactly those in `keys`, or every one when
    `keys` is None. Reads state; never writes it. Makes zero Anthropic
    API calls.

    An unknown key is silently skipped rather than raised, unlike
    status_reports()'s per-key ValueError -- not reachable from cli.py
    today (resolve_name() raises first), but a caller synthesizing keys
    of its own should not assume the two agree."""
    started = time.monotonic()
    st = state.load(state_path)
    entries = (
        list(st.resources.values())
        if keys is None
        else [st.resources[key] for key in keys if key in st.resources]
    )

    driver_cache: dict[tuple[str, str], ResourceDriver] = {}
    credentials_cache: dict[str, dict[str, str]] = {}
    readings: list[ResourceReading] = []

    for entry in entries:
        errors: list[str] = []
        reading = ResourceReading(
            resource_key=_key_of(entry),
            provider=entry.provider,
            resource_type=entry.resource_type,
            name=entry.name,
            id=entry.id,
            health=None,
            health_unsupported=None,
            samples=[],
            samples_unsupported=None,
            errors=errors,
        )
        readings.append(reading)

        driver, credentials = _resources_for(entry, driver_cache, credentials_cache, errors)
        if driver is None or credentials is None:
            continue

        if want_health:
            reading.health, reading.health_unsupported = _health_for(
                driver, entry, credentials, errors
            )
        if want_metrics:
            reading.samples, reading.samples_unsupported = _samples_for(
                driver, entry, credentials, errors
            )

    return Collection(
        readings=readings,
        elapsed_seconds=time.monotonic() - started,
        errors=_drop_colliding_families(readings),
    )


def _resources_for(
    entry: StateEntry,
    driver_cache: dict[tuple[str, str], ResourceDriver],
    credentials_cache: dict[str, dict[str, str]],
    errors: list[str],
) -> tuple[ResourceDriver | None, dict[str, str] | None]:
    """Driver and credentials for one entry, both cached. Uses
    orchestrator.load_driver() rather than a private copy so these
    commands and `plan` can never disagree about which driver file they
    loaded. Unlike refresh_state(), a failure here is recorded against
    this one resource instead of aborting the sweep."""
    driver_key = (entry.provider, entry.resource_type)
    if driver_key not in driver_cache:
        try:
            driver_cache[driver_key] = orchestrator.load_driver(*driver_key)
        except Exception as exc:  # noqa: BLE001 - see below
            # Not just PlanBlockedError: load_driver() converts only
            # FileNotFoundError, so a driver file with a SyntaxError, a
            # bad import, or no Driver class propagates as-is and would
            # blank the whole report.
            errors.append(
                _warn(_oneline(str(exc)), resource_key=_key_of(entry), operation="load_driver")
            )
            return None, None
    if entry.provider not in credentials_cache:
        try:
            credentials_cache[entry.provider] = config.resolve_credentials(entry.provider)
        except RuntimeError as exc:
            # Deliberately not cached as a failure. A review pass noted a
            # fleet sharing one missing token re-reads credentials.env
            # once per resource -- true, and it costs a stat on a cold
            # error path, against a union-typed cache every reader would
            # then have to decode. Left simple.
            errors.append(
                _warn(
                    _oneline(str(exc)),
                    resource_key=_key_of(entry),
                    operation="resolve_credentials",
                )
            )
            return None, None
    return driver_cache[driver_key], credentials_cache[entry.provider]


def status_reports(
    keys: list[str] | None = None, *, state_path: Path = state.DEFAULT_STATE_PATH
) -> list[StatusReport]:
    """`status` for exactly `keys`, or every tracked resource when None.

    Exists because the fleet form is N resources, not one: calling
    status_for() in a loop loads state once per resource and hands each
    call throwaway caches, so twenty droplets meant twenty
    exec_module()s, twenty credential resolutions and twenty-one state
    reads -- and a missing token reported twenty times. One State and one
    pair of caches here, the shape collect() already uses."""
    st = state.load(state_path)
    if keys is None:
        keys = list(st.resources)
    driver_cache: dict[tuple[str, str], ResourceDriver] = {}
    credentials_cache: dict[str, dict[str, str]] = {}
    return [_status_for_entry(st, key, driver_cache, credentials_cache) for key in keys]


def status_for(key: str, *, state_path: Path = state.DEFAULT_STATE_PATH) -> StatusReport:
    """The four answers for one resource. Composes a state lookup, a live
    read(), diff_attributes() against the discovered .aiform.md, and
    health(). Adds no driver method of its own. Writes no state.

    For more than one resource use status_reports(), which shares the
    driver and credential caches across them."""
    return _status_for_entry(state.load(state_path), key, {}, {})


def _status_for_entry(
    st: State,
    key: str,
    driver_cache: dict[tuple[str, str], ResourceDriver],
    credentials_cache: dict[str, dict[str, str]],
) -> StatusReport:
    """Loads the driver once and threads it through all three live
    answers, rather than going via collect(): orchestrator.load_driver()
    execs the driver file on every call, so composing the three steps
    independently would exec it three times for one resource."""
    entry = st.resources.get(key)
    if entry is None:
        tracked = ", ".join(sorted(st.resources)) or "nothing"
        raise ValueError(f"{key!r} is not tracked; tracked: {tracked}")

    errors: list[str] = []
    driver, credentials = _resources_for(entry, driver_cache, credentials_cache, errors)
    if driver is None or credentials is None:
        # The four lines are independent, but all three live answers rest
        # on the same driver and credentials, so one failure is the
        # answer to all of them rather than three restatements of it.
        return _report_for(
            key,
            entry,
            live="; ".join(errors),
            config=_undetermined(entry, "not applicable: the resource could not be read"),
            health=None,
            health_unsupported=None,
        )

    health, health_unsupported = _health_for(driver, entry, credentials, errors)
    live, attributes, liveness = _live_for(driver, entry, credentials)
    return _report_for(
        key,
        entry,
        live=live,
        config=_config_for(driver, entry, attributes, liveness),
        health=health,
        health_unsupported=health_unsupported,
    )


def _report_for(
    key: str,
    entry: StateEntry,
    *,
    live: str,
    config: ConfigStatus,
    health: HealthReport | None,
    health_unsupported: str | None,
) -> StatusReport:
    return StatusReport(
        resource_key=key,
        provider=entry.provider,
        resource_type=entry.resource_type,
        name=entry.name,
        id=entry.id,
        deployed_at=entry.last_applied_at,
        live=live,
        config=config,
        health=health,
        health_unsupported=health_unsupported,
    )


def _stamp(moment: datetime) -> str:
    # astimezone first: state.json can carry an offset other than +00:00,
    # and strftime would then stamp a Z onto a time that is not UTC.
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# _live_for's third value: whether `attributes is None` means the
# resource is gone, or only that aiform could not read it. Without it
# _config_for reported "resource is gone" beside a `live` line saying
# HTTP 503 -- the spec reserves that wording for ResourceNotFoundError.
GONE = "gone"
UNREADABLE = "unreadable"
PRESENT = "present"


def _live_for(
    driver: ResourceDriver, entry: StateEntry, credentials: dict[str, str]
) -> tuple[str, dict | None, str]:
    """Whether the record is still true, plus the live attributes the
    config line diffs against. Goes through refresh_resource() rather
    than driver.read() directly, so a NON_DIFFABLE_FIELDS value is
    carried forward here exactly as it is on the plan path -- without it
    a write-only field like ssh_keys would report permanent drift.
    refresh_resource() itself writes nothing; every state.save() is in
    its callers."""
    try:
        attributes, drifted_missing = orchestrator.refresh_resource(driver, entry, credentials)
    except Exception as exc:  # noqa: BLE001 - a read failure is an answer, not a crash
        # Not just DriverExecutionError: refresh_resource() only wraps
        # what driver.read() raises, and _pop_id() can raise on its own.
        return _oneline(str(exc)), None, UNREADABLE
    if drifted_missing:
        return "missing on the provider", None, GONE
    return "present", attributes, PRESENT


def _undetermined(entry: StateEntry, detail: str) -> ConfigStatus:
    """No yes/no answer to give. `spec_file` is still the path the entry
    records, which is a fact about the resource rather than about this
    attempt to diff it."""
    return ConfigStatus(
        in_sync=None, spec_file=entry.aiform_md_path, drifted_fields=[], detail=detail
    )


def _config_for(
    driver: ResourceDriver, entry: StateEntry, attributes: dict | None, liveness: str
) -> ConfigStatus:
    # Nothing to diff against. Reported rather than left silent: silence
    # on a drift question reads as "no drift", which is the opposite of
    # what is known -- but which of the two it is matters, since only one
    # of them means the resource is actually gone.
    if liveness == GONE:
        return _undetermined(entry, "not applicable: resource is gone")
    if attributes is None:
        return _undetermined(entry, "not applicable: the resource could not be read")

    source = Path(entry.aiform_md_path)
    try:
        content = source.read_text(encoding="utf-8-sig")
        spec = parser.parse_frontmatter(content)
    except OSError:
        return _undetermined(entry, "no source file found")
    except ValueError as exc:
        # Catches both a malformed-frontmatter ValueError and the
        # UnicodeDecodeError read_text() raises on undecodable bytes --
        # itself a ValueError subclass, so it belongs in this branch, not
        # OSError's. Distinct from a missing file: `plan` would say
        # "malformed frontmatter" here, and reporting it as absent sends
        # a reader looking for a file that is sitting right there.
        return _undetermined(entry, _oneline(f"source file is malformed: {exc}"))
    if (spec.provider, spec.resource, spec.name) != (
        entry.provider,
        entry.resource_type,
        entry.name,
    ):
        # The plan path matches by frontmatter, not by the recorded path
        # (orchestrator.build_create_plan), so a file since repurposed to
        # another resource would have `status` and `plan` disagreeing --
        # `status` diffing this droplet against, say, a firewall's params.
        return _undetermined(
            entry,
            f"{entry.aiform_md_path} now declares "
            f"{spec.provider}.{spec.resource}.{spec.name}, not this resource",
        )
    drifted = planner.diff_attributes(
        attributes, spec.params, unordered_fields=driver.UNORDERED_FIELDS
    )
    return ConfigStatus(
        in_sync=not drifted,
        spec_file=entry.aiform_md_path,
        drifted_fields=sorted(drifted),
        detail=None,
    )


def _require_format(fmt: str) -> None:
    if fmt not in _FORMATS:
        raise ValueError(f"unknown format {fmt!r}; expected one of {', '.join(_FORMATS)}")


def _widths(rows: list[list[str]]) -> list[int]:
    """One alignment for the whole output, not per block -- which is what
    makes the numbers scannable down the page. The last column is never
    padded, so no line carries trailing whitespace."""
    if not rows:
        return []
    return [max(len(row[i]) for row in rows) for i in range(len(rows[0]) - 1)]


def _row(cells: list[str], widths: list[int], indent: str = "") -> str:
    padded = [cell.ljust(widths[i]) for i, cell in enumerate(cells[:-1])]
    return (indent + _COLUMN_GAP.join([*padded, cells[-1]])).rstrip()


def _is_fleet(items: list, fleet: bool | None) -> bool:
    # A caller that knows says so; the default reads it off the list,
    # which is right except for a fleet that happens to hold exactly one
    # resource. cli.py passes the flag explicitly for that reason.
    return len(items) != 1 if fleet is None else fleet


def render_check(
    readings: list[ResourceReading], fmt: str, *, fleet: bool | None = None
) -> tuple[str, int]:
    """The rendered report and the exit code, together: the mapping from
    verdicts to an exit code is the one place in this surface where an
    exit code carries a verdict, and deriving it twice is how the two
    copies drift."""
    _require_format(fmt)
    verdicts = [r.health.status for r in readings if r.health is not None]
    worst = max(verdicts, key=_SEVERITY.__getitem__, default=None)
    code = 2 if worst is None else (0 if worst is HealthStatus.OK else 1)

    coverage = {
        "reporting": len(verdicts),
        "total": len(readings),
        "unsupported": sum(1 for r in readings if r.health_unsupported is not None),
    }
    if fmt == JSON:
        return (
            json.dumps(
                {
                    "resources": [_check_json(r) for r in readings],
                    "coverage": coverage,
                    "worst_status": worst.value if worst is not None else None,
                },
                indent=2,
            ),
            code,
        )

    # The type gets a column of its own: as the middle segment of the
    # dot-joined key it is only recoverable by a reader who already knows
    # that convention and parses it out by hand.
    rows = [[_check_label(r), r.resource_type, r.resource_key, _check_summary(r)] for r in readings]
    widths = _widths(rows)
    lines: list[str] = []
    for reading, row in zip(readings, rows, strict=True):
        lines.append(_row(row, widths))
        lines.extend(_observation_lines(reading))
    if _is_fleet(readings, fleet):
        lines.append(
            f"{coverage['reporting']} of {coverage['total']} resources report health; "
            f"{coverage['unsupported']} unsupported"
        )
    return "\n".join(lines), code


def _check_label(reading: ResourceReading) -> str:
    if reading.health is not None:
        return reading.health.status.value
    if reading.health_unsupported is not None:
        return "unsupported"
    # Neither a verdict nor a decline: the driver file or the credentials
    # failed. Labelling it "unsupported" contradicted the coverage line
    # printed directly beneath it, which counts only real declines.
    return "error"


def _check_summary(reading: ResourceReading) -> str:
    # Collapsed here, not only for exception text: a driver's own
    # summary and its decline reason are free-form strings too, and a
    # newline in either breaks check's one-line-per-resource rule
    # identically.
    if reading.health is not None:
        return _oneline(reading.health.summary)
    if reading.health_unsupported is not None:
        return _oneline(reading.health_unsupported)
    # No verdict and no decline: the driver file or the credentials
    # failed, and the reason is the only thing worth printing here.
    return _oneline("; ".join(reading.errors)) or "no verdict"


def _observation_lines(reading: ResourceReading) -> list[str]:
    # Only when the verdict is not ok. The gate use case wants one line
    # per resource and silence when everything is fine; the diagnosis use
    # case is by definition the one where the verdict is bad. No flag is
    # needed because the two cases never overlap.
    if reading.health is None or reading.health.status is HealthStatus.OK:
        return []
    observations = reading.health.observations
    if not observations:
        return []
    observations = {_oneline(k): _oneline(v) for k, v in observations.items()}
    # A newline in a key would also inflate this width and pad every
    # other row against a value nothing in the output is that wide.
    width = max(len(key) for key in observations)
    # Four spaces rather than two so an observation cannot be mistaken
    # for a second resource's row in the fleet form.
    return [
        f"{_OBSERVATION_INDENT}{key.ljust(width)}{_COLUMN_GAP}{value}".rstrip()
        for key, value in observations.items()
    ]


def _check_json(reading: ResourceReading) -> dict:
    return {
        "resource_key": reading.resource_key,
        "provider": reading.provider,
        "resource_type": reading.resource_type,
        "name": reading.name,
        "id": reading.id,
        "status": reading.health.status.value if reading.health is not None else None,
        "summary": reading.health.summary if reading.health is not None else None,
        "observations": reading.health.observations if reading.health is not None else {},
        "unsupported": reading.health_unsupported,
        "errors": reading.errors,
    }


def render_metrics(
    readings: list[ResourceReading],
    fmt: str,
    *,
    fleet: bool | None = None,
    elapsed_seconds: float = 0.0,
    errors: Sequence[str] = (),
) -> str:
    """`elapsed_seconds` and `errors` are keyword-only because they come
    off the Collection rather than off any one reading, and the JSON
    document's fixed shape carries both. Passing the whole Collection
    instead would make this the only renderer that does not take a
    list."""
    _require_format(fmt)
    if fmt == JSON:
        return json.dumps(
            {
                "elapsed_seconds": elapsed_seconds,
                "errors": list(errors),
                "resources": [_metrics_json(r) for r in readings],
            },
            indent=2,
        )

    per_resource = [
        [[_sample_name_cell(s), _number(s.value)] for s in reading.samples] for reading in readings
    ]
    widths = _widths([row for rows in per_resource for row in rows])
    is_fleet = _is_fleet(readings, fleet)
    indent = _ROW_INDENT if is_fleet else ""
    lines: list[str] = []
    for reading, rows in zip(readings, per_resource, strict=True):
        if is_fleet:
            lines.append(reading.resource_key)
        lines.extend(_row(row, widths, indent) for row in rows)
        if not rows:
            lines.append(indent + _placeholder(reading))
        lines.extend(indent + error for error in reading.errors)
    lines.extend(errors)
    return "\n".join(lines)


def _placeholder(reading: ResourceReading) -> str:
    if reading.samples_unsupported is not None:
        return _oneline(f"unsupported: {reading.samples_unsupported}")
    return "no samples"


def _sample_name_cell(sample: Sample) -> str:
    """A label's value, not its key: for the families that carry one today
    (`cpu_seconds_total`'s `mode`) the value alone -- `idle`, `iowait`, ...
    -- already tells a human what the row is, and the key would only repeat
    what the bracket position already says."""
    if not sample.labels:
        return sample.name
    values = ",".join(v for _, v in sorted(sample.labels.items()))
    return f"{sample.name}[{values}]"


def _number(value: float) -> str:
    """No thousands separators, no unit scaling: the name carries the
    unit, and a reader comparing two runs needs the digits to line up
    rather than be re-scaled between them."""
    if abs(value - round(value)) < 1e-6:
        return str(round(value))
    return repr(value)


def _metrics_json(reading: ResourceReading) -> dict:
    return {
        "resource_key": reading.resource_key,
        "provider": reading.provider,
        "resource_type": reading.resource_type,
        "name": reading.name,
        "id": reading.id,
        "samples": [
            {"name": s.name, "kind": s.kind.value, "value": s.value, "labels": s.labels}
            for s in reading.samples
        ],
        "samples_unsupported": reading.samples_unsupported,
        "errors": reading.errors,
    }


# `type` leads: it is identity rather than a fifth answer, and the four
# answers below it read the same as they always have.
_STATUS_LABELS = ("type", "deployed", "live", "config", "health")


def render_status(reports: list[StatusReport], fmt: str, *, fleet: bool | None = None) -> str:
    _require_format(fmt)
    if fmt == JSON:
        return json.dumps({"resources": [_status_json(r) for r in reports]}, indent=2)

    per_resource = [
        [[label, _status_value(report, label)] for label in _STATUS_LABELS] for report in reports
    ]
    widths = _widths([row for rows in per_resource for row in rows])
    is_fleet = _is_fleet(reports, fleet)
    indent = _ROW_INDENT if is_fleet else ""
    lines: list[str] = []
    for report, rows in zip(reports, per_resource, strict=True):
        if is_fleet:
            lines.append(report.resource_key)
        lines.extend(_row(row, widths, indent) for row in rows)
    return "\n".join(lines)


def _status_value(report: StatusReport, label: str) -> str:
    """The text form's sentences, composed from the structured fields at
    render time rather than stored as prose -- which is what `health` has
    always done and what `deployed`/`config` now do too."""
    if label == "type":
        return report.resource_type
    if label == "deployed":
        return f"{_stamp(report.deployed_at)}, id {report.id}"
    if label == "live":
        return report.live
    if label == "config":
        return _config_value(report.config)
    if report.health is not None:
        return _oneline(f"{report.health.status.value} — {report.health.summary}")
    if report.health_unsupported is not None:
        return _oneline(f"unsupported: {report.health_unsupported}")
    return "no verdict"


def _config_value(config: ConfigStatus) -> str:
    if config.in_sync is None:
        return config.detail
    if config.in_sync:
        return f"in sync with {config.spec_file}"
    noun = "field" if len(config.drifted_fields) == 1 else "fields"
    return f"{len(config.drifted_fields)} {noun} drifted: {', '.join(config.drifted_fields)}"


def _status_json(report: StatusReport) -> dict:
    return {
        "resource_key": report.resource_key,
        "provider": report.provider,
        "resource_type": report.resource_type,
        "name": report.name,
        "id": report.id,
        "deployed_at": _stamp(report.deployed_at),
        "live": report.live,
        "config": {
            "in_sync": report.config.in_sync,
            "spec_file": report.config.spec_file,
            "drifted_fields": report.config.drifted_fields,
            "detail": report.config.detail,
        },
        "health": (
            None
            if report.health is None
            else {
                "status": report.health.status.value,
                "summary": report.health.summary,
                "observations": report.health.observations,
            }
        ),
        "health_unsupported": report.health_unsupported,
    }

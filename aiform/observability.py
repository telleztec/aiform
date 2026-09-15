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
from dataclasses import dataclass, field
from pathlib import Path

from aiform import config, orchestrator, parser, planner, state
from aiform.driver import CapabilityNotSupported, ResourceDriver
from aiform.exceptions import DriverExecutionError, PlanBlockedError, ResourceNotFoundError
from aiform.models import HealthReport, HealthStatus, MetricKind, Sample, StateEntry
from aiform.state import State

logger = logging.getLogger(__name__)

# The character sets a metrics system will require of these later. A
# driver's mistake is caught here rather than on the Pydantic model so it
# lands as a dropped sample naming the driver, not a crash -- see
# specs/models.md.
_METRIC_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_LABEL_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

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
class StatusReport:
    """`aiform resource status`. Four independent answers; any one can be
    the surprising one, so none is folded into another."""

    resource_key: str
    name: str
    deployed: str | None
    live: str
    config: str
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


def _warn(message: str, **fields) -> str:
    logger.warning(message, extra=fields)
    return message


def _health_for(
    driver: ResourceDriver, entry: StateEntry, credentials: dict[str, str], errors: list[str]
) -> tuple[HealthReport | None, str | None]:
    try:
        return driver.health(entry.id, credentials), None
    except CapabilityNotSupported as exc:
        # Declining a capability is not an error anywhere in this spec.
        return None, exc.reason
    except ResourceNotFoundError:
        # The resource being gone is the finding, not a failure to
        # observe, so it is the verdict and is not repeated in errors.
        return HealthReport(status=HealthStatus.FAILING, summary="resource not found"), None
    except Exception as exc:  # noqa: BLE001 - one bad driver must not blank the report
        message = _warn(
            f"{entry.provider}.{entry.resource_type} health() failed: {exc}",
            resource_key=_key_of(entry),
            operation="health",
        )
        errors.append(message)
        return HealthReport(status=HealthStatus.UNKNOWN, summary=str(exc)), None


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
                f"{entry.provider}.{entry.resource_type} metrics() failed: {exc}",
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
    if not _METRIC_NAME.match(sample.name):
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
        if not _LABEL_NAME.match(label):
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
    API calls."""
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
        except PlanBlockedError as exc:
            errors.append(_warn(str(exc), resource_key=_key_of(entry), operation="load_driver"))
            return None, None
    if entry.provider not in credentials_cache:
        try:
            credentials_cache[entry.provider] = config.resolve_credentials(entry.provider)
        except RuntimeError as exc:
            errors.append(
                _warn(str(exc), resource_key=_key_of(entry), operation="resolve_credentials")
            )
            return None, None
    return driver_cache[driver_key], credentials_cache[entry.provider]


def status_for(key: str, *, state_path: Path = state.DEFAULT_STATE_PATH) -> StatusReport:
    """The four answers for one resource. Composes a state lookup, a live
    read(), diff_attributes() against the discovered .aiform.md, and
    health(). Adds no driver method of its own. Writes no state.

    Loads the driver once and threads it through all three, rather than
    going via collect(): orchestrator.load_driver() execs the driver file
    on every call, so composing the three steps independently would exec
    it three times for one resource."""
    st = state.load(state_path)
    entry = st.resources.get(key)
    if entry is None:
        tracked = ", ".join(sorted(st.resources)) or "nothing"
        raise ValueError(f"{key!r} is not tracked; tracked: {tracked}")

    errors: list[str] = []
    driver, credentials = _resources_for(entry, {}, {}, errors)
    if driver is None or credentials is None:
        # The four lines are independent, but all three live answers rest
        # on the same driver and credentials, so one failure is the
        # answer to all of them rather than three restatements of it.
        return StatusReport(
            resource_key=key,
            name=entry.name,
            deployed=f"{_stamp(entry.last_applied_at)}, id {entry.id}",
            live="; ".join(errors),
            config="not applicable: the resource could not be read",
            health=None,
            health_unsupported=None,
        )

    health, health_unsupported = _health_for(driver, entry, credentials, errors)
    live, attributes = _live_for(driver, entry, credentials)
    return StatusReport(
        resource_key=key,
        name=entry.name,
        deployed=f"{_stamp(entry.last_applied_at)}, id {entry.id}",
        live=live,
        config=_config_for(driver, entry, attributes),
        health=health,
        health_unsupported=health_unsupported,
    )


def _stamp(moment) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _live_for(
    driver: ResourceDriver, entry: StateEntry, credentials: dict[str, str]
) -> tuple[str, dict | None]:
    """Whether the record is still true, plus the live attributes the
    config line diffs against. Goes through refresh_resource() rather
    than driver.read() directly, so a NON_DIFFABLE_FIELDS value is
    carried forward here exactly as it is on the plan path -- without it
    a write-only field like ssh_keys would report permanent drift.
    refresh_resource() itself writes nothing; every state.save() is in
    its callers."""
    try:
        attributes, drifted_missing = orchestrator.refresh_resource(driver, entry, credentials)
    except DriverExecutionError as exc:
        return str(exc), None
    if drifted_missing:
        return "missing on the provider", None
    return "present", attributes


def _config_for(driver: ResourceDriver, entry: StateEntry, attributes: dict | None) -> str:
    if attributes is None:
        # Nothing to diff against. Reported rather than left silent:
        # silence on a drift question reads as "no drift", which is the
        # opposite of what is known.
        return "not applicable: resource is gone"
    try:
        spec = parser.parse_frontmatter(Path(entry.aiform_md_path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return "no source file found"
    drifted = planner.diff_attributes(
        attributes, spec.params, unordered_fields=driver.UNORDERED_FIELDS
    )
    if not drifted:
        return f"in sync with {entry.aiform_md_path}"
    noun = "field" if len(drifted) == 1 else "fields"
    return f"{len(drifted)} {noun} drifted: {', '.join(sorted(drifted))}"


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

    rows = [
        [
            r.health.status.value if r.health is not None else "unsupported",
            r.resource_key,
            _check_summary(r),
        ]
        for r in readings
    ]
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


def _check_summary(reading: ResourceReading) -> str:
    if reading.health is not None:
        return reading.health.summary
    if reading.health_unsupported is not None:
        return reading.health_unsupported
    # No verdict and no decline: the driver file or the credentials
    # failed, and the reason is the only thing worth printing here.
    return "; ".join(reading.errors) or "no verdict"


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
        "name": reading.name,
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
    errors: list[str] = (),
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

    rows = [
        [sample.kind.value, sample.name, _number(sample.value)]
        for reading in readings
        for sample in reading.samples
    ]
    widths = _widths(rows)
    indent = _ROW_INDENT if _is_fleet(readings, fleet) else ""
    lines: list[str] = []
    for reading in readings:
        if _is_fleet(readings, fleet):
            lines.append(reading.resource_key)
        if reading.samples:
            lines.extend(
                _row([s.kind.value, s.name, _number(s.value)], widths, indent)
                for s in reading.samples
            )
        else:
            lines.append(indent + _placeholder(reading))
        lines.extend(indent + error for error in reading.errors)
    lines.extend(errors)
    return "\n".join(lines)


def _placeholder(reading: ResourceReading) -> str:
    if reading.samples_unsupported is not None:
        return f"unsupported: {reading.samples_unsupported}"
    return "no samples"


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


_STATUS_LABELS = ("deployed", "live", "config", "health")


def render_status(reports: list[StatusReport], fmt: str, *, fleet: bool | None = None) -> str:
    _require_format(fmt)
    if fmt == JSON:
        return json.dumps({"resources": [_status_json(r) for r in reports]}, indent=2)

    rows = [[label, _status_value(report, label)] for report in reports for label in _STATUS_LABELS]
    widths = _widths(rows)
    indent = _ROW_INDENT if _is_fleet(reports, fleet) else ""
    lines: list[str] = []
    for report in reports:
        if _is_fleet(reports, fleet):
            lines.append(report.resource_key)
        lines.extend(
            _row([label, _status_value(report, label)], widths, indent) for label in _STATUS_LABELS
        )
    return "\n".join(lines)


def _status_value(report: StatusReport, label: str) -> str:
    if label == "health":
        if report.health is not None:
            return f"{report.health.status.value} — {report.health.summary}"
        if report.health_unsupported is not None:
            return f"unsupported: {report.health_unsupported}"
        return "no verdict"
    if label == "deployed":
        return report.deployed if report.deployed is not None else "never deployed by aiform"
    return getattr(report, label)


def _status_json(report: StatusReport) -> dict:
    return {
        "resource_key": report.resource_key,
        "name": report.name,
        "deployed": report.deployed,
        "live": report.live,
        "config": report.config,
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

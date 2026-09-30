# specs/exceptions.md — `aiform/exceptions.py`

## Purpose

Shared exception types referenced by name from other already-merged
modules and specs (`aiform/driver.py`'s `read()` docstring, `PLAN.md`
§4/§5) before this module itself existed. Built now, out of the
suggested implementation order, because `drivers/digitalocean/compute.py`
(the first curated driver) cannot correctly satisfy `read()`'s
contract without `ResourceNotFoundError` actually existing — not a
speculative addition, a genuinely load-bearing gap.

**No longer partial.** `PLAN.md` §1's repo-layout comment lists this
file's eventual contents as `DriverUpdateNotSupported, ResourceNotFoundError,
DriverExecutionError, PlanBlockedError`. `DriverUpdateNotSupported` was
already relocated to `aiform/driver.py` (see `specs/driver.md`'s flagged
discrepancy — that resolution stands). `DriverExecutionError` and
`PlanBlockedError` were deferred until `orchestrator.py` — their only
caller — actually existed; `specs/orchestrator.md` now specifies both,
and this file defines them alongside `ResourceNotFoundError`.

## Interface

```python
class ResourceNotFoundError(Exception):
    """Raised by a ResourceDriver's read() when the resource no longer
    exists on the provider's side (PLAN.md §4/§5) — the orchestrator's
    refresh step catches this by name to mark drifted_missing rather
    than treating a deleted resource as an unhandled error."""


class DriverExecutionError(Exception):
    """Raised by orchestrator.py when a driver call raises anything other
    than the exception types the driver contract documents (PLAN.md §4's
    "Orchestrator invocation contract") — a raw CSP API failure, wrapped
    for uniform CLI error formatting."""

    def __init__(self, provider: str, resource_type: str, operation: str, original: Exception):
        self.provider = provider
        self.resource_type = resource_type
        self.operation = operation
        self.original = original
        super().__init__(f"{provider}.{resource_type} driver failed during {operation}: {original}")


class PlanBlockedError(Exception):
    """Raised by orchestrator.py whenever a plan cannot proceed for a
    policy reason -- a missing driver, a missing credential, or a gate #2
    review that didn't approve (PLAN.md §5)."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class DeploymentMismatchError(Exception):
    """Raised by state.load() when the state file at `path` belongs to a
    different deployment than the one the caller asked to act on (#201)."""

    def __init__(self, requested: str, found: str, path: Path):
        self.requested = requested
        self.found = found
        self.path = path
        super().__init__(
            f"this state file belongs to deployment {found!r}, not {requested!r}.\n"
            f"  state file: {path}\n"
            "  Nothing was read from the provider and nothing was changed."
        )


class StateMissingDeploymentError(Exception):
    """Raised by state.load() when the state file at `path` has no top-level
    `deployment` key, i.e. it was written before #201."""

    def __init__(self, path: Path):
        self.path = path
        super().__init__(
            "this state file has no 'deployment' field: it was written before "
            "deployments were named.\n"
            f"  state file: {path}\n"
            "  Either delete it (aiform then forgets every resource it tracked) or add "
            '"deployment": "default" as a top-level key by hand.\n'
            "  Nothing was read from the provider and nothing was changed."
        )
```

`ResourceNotFoundError` has no constructor beyond `Exception`'s own — no
structured fields, unlike `DriverUpdateNotSupported`'s
`reason`/`unsupported_fields`. `PLAN.md` §4/§5 never describe this
exception carrying any data beyond being raised; a driver that wants to
include the id in its message can do so via the plain
`Exception.__init__(message)` args, same as any exception.

`DriverExecutionError` and `PlanBlockedError` mirror
`DriverUpdateNotSupported`'s shape (structured fields, a formatted
message passed to `Exception.__init__`) — see `specs/orchestrator.md`
for exactly which call sites raise each and why.

`DeploymentMismatchError` is the same shape: structured fields and a
formatted message. `path` is stored **absolute** — `state.load()` passes
`path.absolute()` — so the message names the file the user would have
touched, not a relative `.aiform/state.json` that means nothing out of
context. `str(exc)` is three lines:

```
this state file belongs to deployment 'prod', not 'scratch'.
  state file: /abs/path/.aiform/state.json
  Nothing was read from the provider and nothing was changed.
```

The last line is a promise about the raise site, not about the process:
`state.load()` is the first thing every state-reading command does, before any
driver load, credential resolution, provider call or LLM call, so it is true
of every command that can raise this. `cli.py` maps it to `Error: <message>`
on stderr and exit 2 (`specs/cli.md`).

`StateMissingDeploymentError` is the same shape, with `path` absolute for the
same reason. `str(exc)` is four lines and never contains the file's contents:

```
this state file has no 'deployment' field: it was written before deployments were named.
  state file: /abs/path/.aiform/state.json
  Either delete it (aiform then forgets every resource it tracked) or add "deployment": "default" as a top-level key by hand.
  Nothing was read from the provider and nothing was changed.
```

It exists so the one predictable failure of not migrating (`specs/state.md`)
does not surface as Pydantic's `ValidationError`, whose text is the whole state
file. `cli.py` maps it to `Error: <message>` on stderr and exit 2, like
`DeploymentMismatchError`.

## Behavior

- `ResourceNotFoundError` is a plain subclass of `Exception` — no custom
  `__init__`, no special attributes.
- Constructible and raisable exactly like any built-in exception:
  `raise ResourceNotFoundError(f"droplet {id} not found")`.
- `DriverExecutionError(provider, resource_type, operation, original)`
  stores all four constructor arguments verbatim as same-named
  attributes; `str(exc)` is
  `f"{provider}.{resource_type} driver failed during {operation}: {original}"`.
- `PlanBlockedError(reason)` stores `reason` verbatim; `str(exc) ==
  reason` (inherited from `Exception.__init__(reason)`, same as
  `DriverUpdateNotSupported`'s `.reason`/`str()` relationship).
- `DeploymentMismatchError(requested, found, path)` stores all three verbatim
  as same-named attributes and `str(exc)` is the three-line text above.
- `StateMissingDeploymentError(path)` stores `path` verbatim and `str(exc)` is
  the four-line text above.

## Edge cases / errors

- Not a subclass of any built-in exception type with pre-existing
  semantics (e.g. not `LookupError`) — deliberately its own type, so
  catching it can never accidentally also catch an unrelated `KeyError`/
  `IndexError` a driver's own response-parsing code might raise. Same
  reasoning applies to `DriverExecutionError`/`PlanBlockedError`/
  `DeploymentMismatchError`/`StateMissingDeploymentError` — plain `Exception` subclasses, not tied to any
  built-in hierarchy.

## Out of scope

- `DriverUpdateNotSupported` — lives in `aiform/driver.py`, per
  `specs/driver.md`'s already-resolved discrepancy with `PLAN.md` §1.
- Any exception types `driver_gen.py`'s retry-exhaustion path might
  eventually want — that module currently raises its own
  `DriverGenerationFailed`, per `specs/driver_gen.md`'s own stance on not
  anticipating `exceptions.py` types ahead of a real need; unrelated to
  the two types added here, which exist for `orchestrator.py` alone.

## Addendum: `CapabilityNotSupported` does not live here

`specs/driver_observability.md` adds `CapabilityNotSupported`, raised by
`ResourceDriver.health()`/`metrics()` when a driver cannot answer for its
resource kind. It is defined in `aiform/driver.py`, alongside
`DriverUpdateNotSupported`, **not** in this module — the base class itself
raises it, so it is part of the driver contract rather than a general-purpose
error type. Recorded here so the next reader looking for it doesn't conclude it
was forgotten, and doesn't "fix" it by moving it and re-creating the `PLAN.md`
§1 discrepancy `specs/driver.md` has flagged since it was written.

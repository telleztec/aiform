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

## Edge cases / errors

- Not a subclass of any built-in exception type with pre-existing
  semantics (e.g. not `LookupError`) — deliberately its own type, so
  catching it can never accidentally also catch an unrelated `KeyError`/
  `IndexError` a driver's own response-parsing code might raise. Same
  reasoning applies to `DriverExecutionError`/`PlanBlockedError` — plain
  `Exception` subclasses, not tied to any built-in hierarchy.

## Out of scope

- `DriverUpdateNotSupported` — lives in `aiform/driver.py`, per
  `specs/driver.md`'s already-resolved discrepancy with `PLAN.md` §1.
- Any exception types `driver_gen.py`'s retry-exhaustion path might
  eventually want — that module currently raises its own
  `DriverGenerationFailed`, per `specs/driver_gen.md`'s own stance on not
  anticipating `exceptions.py` types ahead of a real need; unrelated to
  the two types added here, which exist for `orchestrator.py` alone.

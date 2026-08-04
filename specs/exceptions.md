# specs/exceptions.md — `aiform/exceptions.py`

## Purpose

Shared exception types referenced by name from other already-merged
modules and specs (`aiform/driver.py`'s `read()` docstring, `PLAN.md`
§4/§5) before this module itself existed. Built now, out of the
suggested implementation order, because `drivers/digitalocean/compute.py`
(the first generated driver) cannot correctly satisfy `read()`'s
contract without `ResourceNotFoundError` actually existing — not a
speculative addition, a genuinely load-bearing gap.

**Deliberately partial.** `PLAN.md` §1's repo-layout comment lists this
file's eventual contents as `DriverUpdateNotSupported, ResourceNotFoundError,
DriverExecutionError, PlanBlockedError`. `DriverUpdateNotSupported` was
already relocated to `aiform/driver.py` (see `specs/driver.md`'s flagged
discrepancy — that resolution stands). `DriverExecutionError` and
`PlanBlockedError` are `orchestrator.py`/`driver_gen.py`-retry-path
concerns with no current caller — not built here; adding them
speculatively ahead of a real need would be exactly the kind of
premature abstraction `CLAUDE.md` warns against. This file currently
defines `ResourceNotFoundError` alone.

## Interface

```python
class ResourceNotFoundError(Exception):
    """Raised by a ResourceDriver's read() when the resource no longer
    exists on the provider's side (PLAN.md §4/§5) — the orchestrator's
    refresh step catches this by name to mark drifted_missing rather
    than treating a deleted resource as an unhandled error."""
```

No constructor beyond `Exception`'s own — no structured fields, unlike
`DriverUpdateNotSupported`'s `reason`/`unsupported_fields`. `PLAN.md`
§4/§5 never describe this exception carrying any data beyond being
raised; a driver that wants to include the id in its message can do so
via the plain `Exception.__init__(message)` args, same as any exception.

## Behavior

- `ResourceNotFoundError` is a plain subclass of `Exception` — no custom
  `__init__`, no special attributes.
- Constructible and raisable exactly like any built-in exception:
  `raise ResourceNotFoundError(f"droplet {id} not found")`.

## Edge cases / errors

- Not a subclass of any built-in exception type with pre-existing
  semantics (e.g. not `LookupError`) — deliberately its own type, so
  catching it can never accidentally also catch an unrelated `KeyError`/
  `IndexError` a driver's own response-parsing code might raise. This
  was a real bug in an earlier draft of `drivers/digitalocean/compute.py`'s
  spec, caught by `/code-review`: it substituted a plain `LookupError`
  to avoid depending on this not-yet-built module, which both broke the
  already-merged `driver.py`/`PLAN.md` contract by name and created a
  real collision risk with genuine `KeyError`s from response parsing.

## Out of scope

- `DriverExecutionError`, `PlanBlockedError` — real future work, added
  when `orchestrator.py` or `driver_gen.py`'s retry-exhaustion path
  actually needs them, not speculatively now.
- `DriverUpdateNotSupported` — lives in `aiform/driver.py`, per
  `specs/driver.md`'s already-resolved discrepancy with `PLAN.md` §1.

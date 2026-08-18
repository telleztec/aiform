# specs/driver.md — `aiform/driver.py`

## Purpose

The hand-written contract every generated `(provider, resource)` driver
implements (`PLAN.md` §4). This is the seam that lets the orchestrator
call any provider/resource combination identically — it never inspects a
driver's internals, only the four methods below. Pure interface + one
exception type — no file I/O, no LLM calls, no CSP API calls, no dynamic
import logic.

**Flagged discrepancy**: `PLAN.md` §1's repo-layout comment lists
`DriverUpdateNotSupported` as living in `exceptions.py`, but §4's actual
code defines it inside `driver.py`, alongside `ResourceDriver`. Per
`CLAUDE.md`'s "follow the `ResourceDriver` interface in `PLAN.md` §4
exactly," this spec treats §4 as authoritative: `DriverUpdateNotSupported`
is defined here, in `driver.py`. `exceptions.py` (not yet built) can
re-export it later if convenient; §1's comment is stale and should be
corrected whenever `exceptions.py` is actually written, not silently
worked around.

**Second flagged discrepancy, now resolved**: `create()`'s original
two-argument signature (`params`, `credentials`) had no way for a driver
to receive the resource's `name:` frontmatter field, which is a separate
top-level field never nested inside `params:` (`PLAN.md` §2). The
curated `drivers/digitalocean/compute.py` driver — and its own test
suite — were both written under the mistaken assumption that `params`
would include `"name"`, which it never does; every real `create()` call
against the live DigitalOcean API would have raised `KeyError`. Fixed by
adding `name: str` as `create()`'s new first parameter, both here and in
`PLAN.md` §4 (this spec restates §4, so both had the same gap and both
are fixed together).

## Interface

Exactly `PLAN.md` §4's code block, restated here for test-writing:

```python
from abc import ABC, abstractmethod
from typing import Any


class DriverUpdateNotSupported(Exception):
    def __init__(self, reason: str, unsupported_fields: list[str] | None = None):
        self.reason = reason
        self.unsupported_fields = unsupported_fields or []
        super().__init__(reason)


class ResourceDriver(ABC):
    PARAM_SCHEMA: dict[str, Any]
    LIKELY_REPLACE_FIELDS: list[str] = []
    NON_DIFFABLE_FIELDS: list[str] = []

    @abstractmethod
    def create(
        self, name: str, params: dict[str, Any], credentials: dict[str, str]
    ) -> dict[str, Any]: ...

    @abstractmethod
    def read(self, id: str, credentials: dict[str, str]) -> dict[str, Any]: ...

    @abstractmethod
    def update(
        self, id: str, current: dict[str, Any], desired: dict[str, Any], credentials: dict[str, str]
    ) -> dict[str, Any]: ...

    @abstractmethod
    def delete(self, id: str, credentials: dict[str, str]) -> None: ...
```

See `PLAN.md` §4 for each method's full docstring (params/return/raises
semantics) — not repeated here since it's not test-relevant beyond what's
in Behavior below.

## Behavior

- `ResourceDriver` cannot be instantiated directly — it has four
  `@abstractmethod`s and no concrete implementations, so Python's `abc`
  machinery raises `TypeError` on `ResourceDriver()`.
- A subclass that implements all four methods (`create`, `read`, `update`,
  `delete`) can be instantiated normally.
- A subclass missing one or more of the four methods cannot be
  instantiated — `TypeError`, same mechanism as the base class, naming the
  still-abstract method(s).
- `LIKELY_REPLACE_FIELDS` defaults to `[]` on the base class. A subclass
  that doesn't override it inherits that empty list; one that does
  (`LIKELY_REPLACE_FIELDS = ["image", "region"]`, per §4's example driver)
  shadows it with its own class attribute. This default is a **shared**
  class attribute — a driver (or `driver_gen.py`, once built) must
  reassign it, never mutate it in place (`.append(...)`), or it corrupts
  the inherited empty list for every other driver that hasn't overridden
  it yet.
- `NON_DIFFABLE_FIELDS` — same `[]` default, same shared-class-attribute
  reassign-don't-mutate rule as `LIKELY_REPLACE_FIELDS`. Declares
  `PARAM_SCHEMA` keys a driver's `read()` structurally cannot ever
  populate (write-only at the CSP level, only accepted at creation
  time) — `driver.py` itself does nothing with this list; it's read by
  `orchestrator.py`'s `refresh_resource()` (`specs/orchestrator.md`),
  which carries a prior state entry's value for such a key forward
  across a `read()` refresh instead of letting the fresh (necessarily
  incomplete) response blank it out. **This is not a diff-exclusion
  mechanism** — an earlier version of this fix had `planner.py`'s
  `diff_attributes()` skip these keys entirely, which silently dropped
  a genuine, intended change to the field (a user editing
  `ssh_keys` in `.aiform.md` got a `no-op` plan and no error, because
  the diff never contained the changed key at all) — caught by a
  `/code-review` pass and reverted before merge. Carrying the value
  forward instead means the diff itself is untouched: an unchanged
  desired value correctly diffs as unchanged (the field it was added to
  fix), and a genuinely changed desired value still diffs against the
  last-known one and correctly surfaces as a real change. Added after a
  live system-test run showed `drivers/digitalocean/compute.py`'s
  `ssh_keys` field — accepted by `PARAM_SCHEMA` but never returned by
  DigitalOcean's `GET /v2/droplets/{id}` at all (verified against DO's
  public OpenAPI schema, not merely asserted) — produced a permanent,
  spurious diff on every `plan` after the first `read()` refresh. See
  `specs/digitalocean_compute.md` for the full worked case, including
  why `backups` — also affected, but for a different reason — is fixed
  differently (in `read()` itself, not via carry-forward).
- `PARAM_SCHEMA` is a bare type annotation with no default — **not** an
  `@abstractmethod`, so Python's `abc` machinery does not enforce its
  presence at instantiation time. A subclass that omits it can still be
  instantiated; only actually accessing `SomeDriver.PARAM_SCHEMA` (or
  `instance.PARAM_SCHEMA`) without it having been set raises a plain
  `AttributeError`, at access time, not at class-definition or
  instantiation time. Enforcing "every driver declares a schema" is
  `driver_gen.py`'s static-validation job (not built yet), not this
  module's — matching §4's own framing ("used by the orchestrator to
  validate... shown to the code-review-model... as ground truth").
- `DriverUpdateNotSupported(reason, unsupported_fields=None)`:
  - `.reason` is exactly the string passed in.
  - `.unsupported_fields` is `[]` when the argument is omitted (or passed
    as `None`), otherwise exactly the list passed in.
  - `str(exc) == reason` — inherited from `Exception.__init__(reason)`.
  - It's a plain `Exception` subclass, not tied to `ResourceDriver` in any
    way (no shared base, no special handling elsewhere in this module).

## Edge cases / errors

- Instantiating `ResourceDriver()` directly → `TypeError` (standard
  `abc.ABC` behavior, not custom-raised).
- A concrete subclass missing only one method (e.g. `delete`) → `TypeError`
  at instantiation, same as missing all four — `abc` doesn't distinguish
  "missing one" from "missing all."
- `PARAM_SCHEMA`/`LIKELY_REPLACE_FIELDS` are never validated for shape by
  this module (e.g. nothing here checks `PARAM_SCHEMA` is a well-formed
  JSON Schema) — that's `driver_gen.py`'s and the orchestrator's
  responsibility, once built, not `driver.py`'s.
- `DriverUpdateNotSupported(reason, unsupported_fields=[])` (explicit empty
  list, not omitted) behaves identically to the omitted case — both end up
  as `.unsupported_fields == []`; the module doesn't distinguish "caller
  passed an empty list" from "caller passed nothing."

## Addendum: marker-tag helpers (`specs/resource_tagging.md`, not yet implemented)

`specs/resource_tagging.md` adds a module-level constant,
`AIFORM_MANAGED_TAG = "aiform-managed"`, and two concrete (non-abstract)
methods to `ResourceDriver`:

```python
def _tags_for_create(self, requested_tags: list[str]) -> list[str]: ...
def _tags_for_attributes(self, live_tags: list[str]) -> list[str]: ...
```

Per `CLAUDE.md`'s "follow the `ResourceDriver` interface in `PLAN.md`
§4 exactly," `PLAN.md` §4's code block has been updated to include
them, and this file's Interface code block above should be updated to
match at implementation time. Not done as part of that spec or this one
— this addendum exists so a reader of this file doesn't miss that the
contract is about to grow, and isn't misled into thinking the four
abstract methods above are still the whole story. See
`specs/resource_tagging.md`'s Behavior/Edge cases sections for what
these two methods do, when a driver should call them, and why they're
concrete rather than abstract (no flag gates them — a driver opts in by
calling them from its own methods, or doesn't).

## Out of scope

- `ResourceNotFoundError`, `DriverExecutionError`, `PlanBlockedError` —
  these belong to `aiform/exceptions.py` (`PLAN.md` §1), not yet built.
  `driver.py` only defines `DriverUpdateNotSupported`, per the flagged
  discrepancy above.
- Dynamic driver import (`importlib.util.spec_from_file_location`),
  instantiating `module.Driver()`, and credential wiring — all
  `orchestrator.py`'s "Orchestrator invocation contract" (`PLAN.md` §4),
  not built yet.
- Static AST validation of a generated driver's source (no `anthropic`
  import, no `ANTHROPIC_API_KEY` read, etc.) — `driver_gen.py`, not built
  yet.
- Any actual CSP API calls, or a concrete `Driver` subclass —
  `drivers/digitalocean/compute.py` is a separate, later step that
  *implements* this contract; this module only *defines* it.

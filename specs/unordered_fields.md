# specs/unordered_fields.md — order-insensitive attribute comparison (`aiform/compare.py`, + `driver.py`/`planner.py`/`orchestrator.py`)

**Naming note**: like `specs/resource_tagging.md`, this filename deliberately
doesn't follow `specs/README.md`'s per-module mirroring rule. The change is one
small shared capability spread across four already-specced modules
(`specs/driver.md`, `specs/planner.md`, `specs/orchestrator.md`,
`specs/digitalocean_compute.md`) plus one tiny new module. Named for the
feature so it's discoverable from any of them, with each cross-referencing it.

Closes #110.

## Purpose

Let a driver declare which of its `PARAM_SCHEMA` fields are collections whose
**order carries no meaning**, so the planner compares them as multisets instead
of as ordered sequences.

## The bug this fixes

`planner.diff_attributes()` compares with `!=`, which on two lists is ordered
comparison. For a field whose real semantics are a set — DigitalOcean `tags`,
and a DNS zone's `records` (#113) — a CSP that returns the same elements in a
different order than the user declared them produces a **permanent, non-empty
diff**.

That is not a cosmetic misrendering. `plan_resource()`'s zero-LLM-call
short-circuit requires `not diff`, so such a field permanently defeats
`CLAUDE.md`'s "on repeat runs against unchanged input, zero Anthropic API
calls" guarantee for that resource — forever, on whichever account happens to
trigger it.

**Scope of the cure.** This fixes *the CSP returning a different order than you
declared*. It deliberately does **not** make reordering your own `.aiform.md`
free: that changes the file's sha256, and `plan_resource()` requires an
unchanged sha to short-circuit. One categorization call after a real file edit
is correct behavior, and `apply` then persists the new sha.

## Interface

### New module

```python
# aiform/compare.py


def canonical_key(value: Any) -> str:
    """Total, deterministic ordering key for any YAML/JSON-derived value."""


def unordered_equal(a: Any, b: Any) -> bool:
    """Multiset equality for two lists; falls back to == otherwise."""
```

### `aiform/driver.py`

A third per-field declaration on `ResourceDriver`, alongside the existing two:

```python
UNORDERED_FIELDS: list[str] = []
```

Defaults to empty, so **no existing driver's behavior changes unless it opts
in** — the same opt-in shape as `LIKELY_REPLACE_FIELDS` and
`NON_DIFFABLE_FIELDS`, and the direct answer to #110's concern that a future
driver whose list field *is* order-sensitive (a boot-script sequence, a
priority list) must not be forced into set semantics. Same shared-class-attribute
rule as the other two: a subclass **reassigns** it, never mutates it in place,
or it corrupts every other driver inheriting the base's list.

### `aiform/planner.py`

```python
def diff_attributes(
    current: dict[str, Any],
    desired: dict[str, Any],
    *,
    unordered_fields: Sequence[str] = (),
) -> dict[str, dict[str, Any]]: ...


def plan_resource(..., unordered_fields: Sequence[str] = (), ...) -> PlanEntry: ...
```

Keyword-only with a default, so every existing caller and test keeps working
unchanged and the new behavior is strictly opt-in.

### `aiform/orchestrator.py`

One added keyword argument in `build_create_plan()`'s existing
`planner.plan_resource(...)` call, beside the
`likely_replace_fields=driver.LIKELY_REPLACE_FIELDS` already there:

```python
entry = planner.plan_resource(
    key,
    current_attributes,
    resource_spec.params,
    param_schema=driver.PARAM_SCHEMA,
    likely_replace_fields=driver.LIKELY_REPLACE_FIELDS,
    unordered_fields=driver.UNORDERED_FIELDS,  # <-- the only change
    # ... remaining arguments unchanged
)
```

### `drivers/digitalocean/compute.py`

```python
UNORDERED_FIELDS = ["tags"]
```

## Behavior

- **`canonical_key(value)`** JSON-serializes a canonicalized form of `value`.
  It is **total**: it never raises, for any input. Callers depend on that —
  this runs inside the planner's diff, where an exception surfaces as an opaque
  failure on a plain `plan`.
  - **Dict keys are stringified and sorted by `str(key)`.** This makes key
    order *within* a dict irrelevant, which is necessary because one side's
    dicts come from the user's YAML and the other's from the CSP's JSON, and
    neither guarantees key order. Sorting by `str(key)` rather than using
    `json.dumps`' own `sort_keys=True` is what makes it total: that flag sorts
    the raw keys and so raises `TypeError` on a dict with mixed-type keys,
    which YAML produces readily (`1:` gives an int key, `~:` a `None` key).
    Stringifying means `{1: "x"}` and `{"1": "x"}` collapse — matching JSON's
    own object-key semantics rather than inventing new ones.
  - **A value `json` cannot represent natively is tagged with its type name**,
    not stringified. Bare stringification (`default=str`) made
    `datetime.date(2026, 1, 1)` compare **equal** to the string
    `"2026-01-01"` — and YAML parses an unquoted `2026-01-01` as a `date`, so
    that collision was reachable. Hiding a real difference is the one direction
    this module must never fail in.
  - Distinct values stay distinct: `True` vs `1` serialize as `true` vs `1`,
    and `1` vs `1.0` as `1` vs `1.0`. The key must never merge values the CSP
    would treat differently.
- **`unordered_equal(a, b)`** returns `a == b` unless **both** are lists;
  otherwise `sorted(map(canonical_key, a)) == sorted(map(canonical_key, b))`.
- **Multiset, not set** — the deliberate answer to #110's open question. `set()`
  would report `["x", "x"]` equal to `["x"]`, silently swallowing a duplicated
  tag or record. Multiset never reports genuinely different inputs as equal, so
  it errs toward showing a diff rather than hiding one, and keeps a real user
  error visible. DigitalOcean's tags cannot duplicate, so this is decided on
  general grounds rather than by the case in hand.
- **`diff_attributes()`** uses `unordered_equal()` for a key in
  `unordered_fields` and plain `!=` for every other key. Unchanged otherwise:
  it still iterates `desired.items()` only, and a differing key still reports
  `{"current": <verbatim>, "desired": <verbatim>}`. **The recorded values are
  never sorted or canonicalized** — only the equality test changes, so the
  `intent-orchestration-model` and the user's plan output keep seeing real
  values in their real order.
- **Sorting is never applied to stored state.** `read()`'s return is written to
  `state.json` as the driver produced it. This mechanism is a comparison rule,
  not a normalization pass.

### Why a shared module rather than inline in `planner.py`

#110 scopes the driver's *own* local diff out — `compute.py`'s `update()` has
the identical `!=` bug — noting it belongs to "that driver's call once the
generic layer exists to call into". `aiform/compare.py` **is** that call-in
point: both layers apply one identical rule instead of two implementations that
drift apart. Fixing `compute.py`'s local diff is still not done here (see Out of
scope), but the function it will call now exists.

### Why not infer this from `PARAM_SCHEMA`

`type: array` does not mean "unordered" — plenty of legitimately ordered fields
are arrays. #110 rules this out explicitly; restated here so a later reader
doesn't "simplify" the explicit declaration away.

## Edge cases / errors

- **A declared field that isn't a list on one or both sides** (e.g. `tags: web`,
  a scalar): falls back to `==`, so the planner never raises. The clear error
  belongs to the driver — `compute.py`'s `_reject_malformed_values()` already
  produces it — and must not be pre-empted by a `TypeError` from the diff.
- **A declared field absent from `desired`**: never compared, because
  `diff_attributes()` iterates `desired.items()`. Unchanged.
- **A declared field absent from `current`**: `current.get(key)` is `None`,
  which is not a list, so the fallback `==` reports a diff. Correct — the field
  is genuinely not present live.
- **Nested lists inside a declared field's elements** (a record dict holding a
  list value) are compared **in order**, because `canonical_key` serializes them
  positionally. Only the top level of a declared field is order-insensitive.
  Deliberate: nothing needs deep unordered semantics, and guessing at it would
  make the rule unpredictable.
- **A name in `UNORDERED_FIELDS` that isn't a `PARAM_SCHEMA` property** is not
  validated here. It is a driver-authoring mistake, and mechanically catching
  that class of error across all three field lists is #114's job, not this
  spec's.
- **Empty lists** compare equal to each other and unequal to any non-empty
  list, falling straight out of the definition. No special case.
- **A tuple rather than a list.** `unordered_equal` dispatches on `isinstance(x,
  list)`, so a driver whose `read()` returns `("a", "b")` silently gets ordered
  comparison, with no signal that its `UNORDERED_FIELDS` declaration had no
  effect. `orchestrator.refresh_resource()` hands `read()`'s value to the
  planner with no JSON round-trip, so nothing coerces it first. Left as-is
  rather than widened to accept tuples, because accepting them would make
  `["a"]` compare equal to `("a",)` -- the diff-hiding direction, which this
  module must never fail in. Drivers return lists; a future mechanical check on
  driver return shapes (#114) is the right place to catch a driver that
  doesn't.
- **Dict keys of mixed type, or values `json` cannot serialize natively.**
  Both are reachable from YAML: `1:` yields an int key, and an unquoted
  `2026-01-01` yields a `datetime.date`. `canonical_key` handles both without
  raising, and without conflating a `date` with the string that looks like it.
  See its implementation notes -- an earlier version used `json.dumps`'
  `sort_keys=True` and `default=str`, which respectively **raised `TypeError`**
  on a mixed-key dict (the exact "pre-empted by a TypeError from the diff"
  outcome this spec forbids) and reported `datetime.date(2026, 1, 1)` **equal**
  to `"2026-01-01"` (a diff-hiding collision). Both found in review.

## Verification

- **Unit**: `tests/test_compare.py` for the two functions — list-of-strings
  reordered, list-of-dicts reordered (the case that makes `sorted()` raise),
  dicts with differing key order, duplicates (`["x","x"]` vs `["x"]` must be
  **unequal**), `True`/`1` and `1`/`1.0` staying distinct, scalar fallback,
  nested-list positional comparison.
- **Unit**: `tests/test_planner.py` — a reordered declared field yields an empty
  diff; the same field undeclared still yields a diff; a genuinely changed
  declared field reports verbatim unsorted `current`/`desired`.
- **Unit**: `tests/test_orchestrator.py` — `UNORDERED_FIELDS` reaches
  `plan_resource()`; a driver not declaring it is unaffected.
- **No regression** in the existing suite. That is the whole live-behavior bar
  for this change, deliberately.

### Why there is no live ordering probe

#110 asks for one — assign `zebra` to a droplet already tagged `apple`, observe
whether `GET` returns them reordered — and this spec originally carried it.
**Declined, on the grounds that it cannot answer the question it is asked.**

If the CSP stores tags in a hash set, iteration order is a function of each
element's hash and the table's current capacity. Adding one more element can
trigger a rehash that reorders elements already present. So order can be stable
at two tags and unstable at nine, with no API change, no version bump, and no
warning — the same account, the same endpoint, a different answer.

That makes the probe's evidence **asymmetric**. A *failing* probe would prove
instability. A *passing* probe proves only "stable at this cardinality, on this
account, this once" — and would be recorded as a green check that licenses
precisely the wrong inference, that ordered comparison is safe here. A test
whose pass is uninformative and whose failure we already assume is not worth a
live droplet, and is actively misleading in the suite.

This supersedes #110's reasoning, which treated the question as empirically
settleable and the existing evidence as merely too weak to settle it. The
sharper point is that **no** amount of observation settles it: order stability
is not a property a CSP has ever promised, so it cannot be established by
sampling, only assumed and then violated.

The fix therefore rests where it always actually rested — on #110's own framing
that the generic layer "has to work correctly for the lowest common denominator
across every future driver" — and not on DigitalOcean's current observed
behavior. Whether DO reorders tags today is not a fact this design needs.

## Out of scope

- **`drivers/digitalocean/compute.py`'s own `update()` local diff**, which has
  the identical bug. Explicitly out of scope in #110, and left so. Its exposure
  is smaller than an earlier draft of this spec claimed: `update()` runs only
  once the planner has already produced a non-empty diff, and a
  reordered-but-equal `tags` value then reaches `_apply_tag_changes()`, which
  computes empty add/remove sets and issues **zero** API calls -- not the
  "wasted no-op API call" stated here previously (corrected in review). `tags`
  is also in `_IN_PLACE_UPDATABLE_FIELDS`, so it can never force a replace.
  Still worth fixing for consistency, now that `unordered_equal()` exists for
  it to call.
- **Validating the three field lists against `PARAM_SCHEMA`** — #114.
- **Deep/recursive unordered comparison** — see Edge cases.
- **Normalizing stored state or rewriting the user's `.aiform.md`.** This
  changes one equality test and nothing else.
- **Making a reordered `.aiform.md` free of an LLM call** — that is the
  sha256 check, a separate mechanism working as intended. See Purpose.

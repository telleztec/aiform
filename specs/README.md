# specs/

One file per module, written *before* that module's tests or code. See
`PROCESS.md` at the repo root for when and how these get used — this file
just covers the format.

## Format

```markdown
# specs/<module>.md — <module purpose in a few words>

## Purpose

One or two sentences.

## Interface

Functions/classes exposed, argument order, return types, exceptions
raised. If `PLAN.md` already fixes this (e.g. §4's resource module
contract), point at the section instead of restating it.

## Behavior

- Bullet list, each one small enough to map to one or a few tests.

## Edge cases / errors

- What's explicitly handled, and what it raises when it isn't a happy
  path.

## Out of scope

- What this module deliberately does not do yet. Reference `PLAN.md` §9
  if it's a known, already-flagged deferred item — don't re-litigate
  something that's already been decided there.
```

## Naming

`specs/<module>.md` mirrors the path of the file it specs, flattened —
`aiform/state.py` → `specs/state.md`, `modules/digitalocean/droplet.py` →
`specs/droplet_do.md` (matching the existing `tests/modules/test_droplet_do.py`
naming from `PLAN.md` §1).

## Lifecycle

A spec is written once, before its module's first implementation, and
updated in place whenever implementation reveals it was wrong or
incomplete — it is not a historical record, it should always describe
the module as it actually is. See `PROCESS.md`'s "Specs are living docs"
note.

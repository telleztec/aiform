# specs/state.md — `aiform/state.py`

## Purpose

Load and save `.aiform/state.json`: the top-level state-file container
(`PLAN.md` §3) wrapping the `StateEntry` models already defined in
`aiform/models.py`, plus the backup-before-overwrite behavior
`CLAUDE.md`'s "State handling" rule requires. Pure file I/O and
validation — no refresh (`driver.read()`), no diffing, no CLI flag
parsing. Those are `orchestrator.py`/`planner.py`/`cli.py`'s jobs.

## Interface

```python
DEFAULT_STATE_PATH = Path(".aiform/state.json")


class State(BaseModel):
    model_config = ConfigDict(extra="forbid")

    aiform_state_version: int = 1
    resources: dict[str, StateEntry] = Field(default_factory=dict)


def load(path: Path = DEFAULT_STATE_PATH) -> State: ...
def save(state: State, path: Path = DEFAULT_STATE_PATH) -> None: ...
```

### `State`

The literal shape of `.aiform/state.json` (`PLAN.md` §3), keyed by
`"<provider>.<resource_type>.<name>"`. `aiform_state_version` defaults
to `1` and is round-tripped as-is — nothing reads or acts on it yet
(`PLAN.md` §9: no migration story exists, deliberately deferred until
the schema actually changes).

`extra="forbid"`, matching `ResourceSpec` (`specs/models.md`): a
typo'd or garbled top-level key in a hand-edited state.json (e.g.
`"resourcess"` instead of `"resources"`) must raise, not silently fall
back to an empty `resources` dict — the latter would make `load()`
indistinguishable from "no resources have ever been applied," which is
exactly the corruption case this module exists to guard against.

A validator enforces that every dict key matches its own entry's
address: for `resources["digitalocean.compute.telleztec-app-01"]`, the
entry's `provider`/`resource_type`/`name` must reassemble to exactly
that key. This is a hand-edit/corruption check in the same spirit as
`ResourceSpec`'s path-safety validation (`specs/models.md`) — a state
file is user-editable text on disk, and a key/entry mismatch here would
otherwise silently misaddress a resource.

### `load(path=DEFAULT_STATE_PATH) -> State`

- File doesn't exist → returns `State(aiform_state_version=1,
  resources={})`. This is the *expected* condition before the first
  successful `apply` has ever run (`PLAN.md` §8 walkthrough starts from
  no state file at all) — not an error.
- File exists → parsed and validated as `State`. Malformed JSON or a
  schema/key-mismatch violation propagates as the underlying
  `json.JSONDecodeError` / Pydantic `ValidationError` — `state.py`
  doesn't wrap these in a custom exception (that's `exceptions.py`'s
  domain, intentionally not touched by this module yet).

### `save(state, path=DEFAULT_STATE_PATH) -> None`

- Backs up any existing file at `path` to `<path>.backup`
  (`path.with_name(path.name + ".backup")` — for the default path this
  is literally `.aiform/state.json.backup`, matching `PLAN.md` §1/§3)
  *before* writing. This is `CLAUDE.md`'s non-negotiable rule stated as
  code: "Write `.aiform/state.json.backup` before every overwrite of
  `.aiform/state.json`." The backup copy is made with
  `read_bytes()`/`write_bytes()`, not a text decode/re-encode round
  trip — the backup's only job is preserving exactly what was on disk,
  so there's no reason to risk a lossy or failing decode (e.g. a
  non-UTF-8 locale) getting in the way of that.
- No backup file is written on the very first save — there's nothing on
  disk yet to preserve.
- The backup is a single snapshot of "whatever was there before this
  write," overwritten again on the next save. Not a rotating history —
  `PLAN.md` §9 explicitly scopes this as "the cheapest possible
  mitigation, not a real history/rollback mechanism."
- Creates `path.parent` if it doesn't exist yet (`.aiform/` may not be
  there if `save()` is ever called outside the normal `aiform init` →
  `apply` flow, e.g. in tests against a temp directory).
- Writes pretty-printed JSON (`indent=2`), matching the human-readable,
  diffable style of `PLAN.md` §3's own example — state.json is meant to
  be inspectable, similar to Terraform's own state file convention.
- The primary file (unlike the backup) is written as text — `attributes`
  and other fields can carry non-ASCII strings (tags, names), so both
  `load()`'s read and `save()`'s primary write pin `encoding="utf-8"`
  explicitly rather than trusting the platform default.

## Behavior

- `load()` on a missing path returns an empty `State`, not an error.
- `load()` on a valid existing file reproduces a `State` equal to what
  produced it (round-trip fidelity).
- `load()` on a file whose `resources` key doesn't match its entry's
  `provider.resource_type.name` raises `ValidationError`.
- `save()` followed by `load()` from the same path returns an equal
  `State` (round-trip through the filesystem, not just `model_dump`).
- First `save()` to a fresh path: no `.backup` file appears.
- Second `save()` to the same path: `.backup` now contains exactly what
  the first `save()` wrote (byte-for-byte, before the second write's
  content lands in the primary file).
- `save()` to a path whose parent directory doesn't exist yet succeeds
  and creates it.

## Edge cases / errors

- Concurrent `save()` calls against the same path (two `aiform apply`
  processes racing) are **not** handled — no file locking, matching
  `PLAN.md` §9's explicitly deferred "single local state file, no
  locking, no multi-user story." Not this module's job to fix.
- A `.backup` file that itself doesn't parse is never read by this
  module — `state.py` only ever writes to `.backup`, never reads it
  back. Recovering from a corrupted primary file using the backup is a
  manual, human-driven action (per `PLAN.md` §9), not an `aiform`
  command.

## Out of scope

- Refreshing state against live reality (`driver.read()`) —
  `orchestrator.py`.
- Diffing desired (`ResourceSpec`) vs. actual (`StateEntry`) —
  `planner.py`.
- `--state-file` flag parsing / resolving the default path from CLI
  context — `cli.py`. `state.py` only ever receives a `Path` it's given.
- State schema version migration — deliberately deferred (`PLAN.md` §9).
- Any custom exception types for load/save failures — deferred until
  `exceptions.py` is built; underlying stdlib/Pydantic errors propagate
  as-is for now.

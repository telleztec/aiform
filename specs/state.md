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
DEFAULT_DEPLOYMENT = "default"


def validate_deployment_name(name: str) -> str: ...


class State(BaseModel):
    model_config = ConfigDict(extra="forbid")

    aiform_state_version: int = 1
    deployment: str
    resources: dict[str, StateEntry] = Field(default_factory=dict)


def load(path: Path = DEFAULT_STATE_PATH, *, deployment: str) -> State: ...
def save(state: State, path: Path = DEFAULT_STATE_PATH) -> None: ...
```

### `State`

The literal shape of `.aiform/state.json` (`PLAN.md` §3), keyed by
`"<provider>.<resource_type>.<name>"`. `aiform_state_version` defaults
to `1` and is round-tripped as-is — nothing reads or acts on it yet
(`PLAN.md` §10: no migration story exists, deliberately deferred until
the schema actually changes).

### Deployment identity (#201)

`deployment` is a **required** field: the name of the deployment this state
file belongs to. A deployment is one independent set of resources, and its
boundary is the directory `aiform` is run from; the name is what makes that
boundary visible in the file, so `cd` into the wrong directory is refused
instead of acted on (`plans/deployment-identity.md`). It is set by
`aiform init --deployment NAME` (`specs/cli.md`) and is never changed by any
later command.

`validate_deployment_name(name)` returns `name` unchanged or raises
`ValueError`. A valid name is 1 to 63 characters, matches
`[a-z0-9][a-z0-9_-]*`, and so: lowercase ASCII letters, digits, hyphen and
underscore only, starting with a letter or digit. That is deliberately the
intersection of what is safe as a single directory-name path segment on any
platform: no slash or backslash, no leading dot or hyphen (a leading hyphen
reads as a flag), no whitespace, no case folding surprises, bounded length. A
later ticket will use the name as a directory under a home directory, and a
name that is valid here must never need escaping there. `State` runs the
same function as a field validator, and `cli.py` runs it on every `--deployment`
value (as the argument type on the leaf; from `_resolve_deployment` for the root
and group positions, `specs/cli.md`), so the rule lives in one place.

The `ValueError` message is
`invalid deployment name {name!r}: {reasons}. Allowed: 1 to 63 characters from a-z, 0-9, '-' or '_', starting with a-z or 0-9`.
`{reasons}` names what is wrong, one phrase per cause that applies, joined with
`, `:

| Cause | Phrase |
|---|---|
| empty | `must not be empty` |
| over 63 characters | `longer than 63 characters` |
| contains `A-Z` | `uppercase letters are not allowed` |
| contains anything outside `A-Za-z0-9_-` (space, `.`, `/`, non-ASCII) | `characters other than a-z, 0-9, '-' and '_' are not allowed` |
| starts with `-` or `_` | `must start with a-z or 0-9` |

A correction is appended as `; try {suggestion!r}` (before the full stop) only
when one exists: the name with surrounding whitespace stripped, lowercased,
and every character outside `a-z0-9_-` replaced by `-`, offered only if that
string itself passes the rule. It is never offered for empty or whitespace-only
input, for input still over-long after stripping, or when the first character
is still `-` or `_` (`-Prod`), so the message never suggests a name that would
be refused.
Examples: `'Prod'` gives `uppercase letters are not allowed; try 'prod'`;
`'my app'` gives `characters other than a-z, 0-9, '-' and '_' are not allowed; try 'my-app'`;
`'prod '`, `'prod\n'` and `' prod'` each suggest `'prod'`, never `'prod-'`;
`'-prod'` gives `must start with a-z or 0-9` with no suggestion. The allowed
list says "characters from a-z, 0-9, ..." rather than "lowercase letters,
digits, ...", because the earlier phrasing read as "1 to 63 letters"; ASCII-only
is stated by the non-ASCII row above, not repeated in the tail.

Old state files are not read: a `state.json` with no `deployment` key is
refused by `load()` with `StateMissingDeploymentError` (`specs/exceptions.md`),
a short message naming the file and the two ways out, rather than the raw
Pydantic dump of the whole file. There is no migration and no default filled in
on load (`specs/MULTI_RESOURCE_PRD.md`'s "Non-requirements" already exempts
state-schema changes from owing one). A user with such a file deletes it or
adds the key by hand.

`aiform_state_version` is **not** bumped by this change: nothing reads it.

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

### `load(path=DEFAULT_STATE_PATH, *, deployment) -> State`

`deployment` is the name the caller is acting on, and it has **no default**:
this is the single choke point where the deployment check happens, so a call
site cannot forget it. Every caller in `cli.py`, `orchestrator.py` and
`observability.py` passes it.

- File doesn't exist → returns `State(deployment=deployment,
  aiform_state_version=1, resources={})`: a fresh state named for the
  requested deployment. Not an error: `aiform init` writes the file, but a
  directory that never ran it (or had its state deleted) is still a valid place
  to `plan create` from. A directory with no state file adopts
  whatever name it is asked for; the name is fixed only once a file is saved.
- File exists → parsed and validated as `State`, then its `deployment` is
  compared with the requested one. A difference raises
  `DeploymentMismatchError(requested, found, path)`
  (`specs/exceptions.md`) **before returning anything**, so no caller has a
  `State` it could act on. Nothing is written.
- A file with no top-level `deployment` key (one written before #201) raises
  `StateMissingDeploymentError(path)` (`specs/exceptions.md`), `path` absolute.
  It is detected as a Pydantic error located at `deployment` of type `missing`;
  the message never echoes the file's contents. Nothing is written and nothing
  is migrated.
- Malformed JSON or any other schema/key-mismatch violation propagates as the
  underlying `json.JSONDecodeError` / Pydantic `ValidationError` —
  `state.py` doesn't wrap these in a custom exception. Validation runs
  before the comparison, so a file whose `deployment` is not a valid name is a
  schema error, not a mismatch.
- A requested `deployment` that is not a valid name raises the same
  `ValueError` from `validate_deployment_name`. `cli.py` rejects a bad
  `--deployment` earlier, at argument parsing, so this is reachable only from
  a caller that skips the CLI.

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
  `PLAN.md` §10 explicitly scopes this as "the cheapest possible
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

- `load()` on a missing path returns an empty `State` named for the requested
  deployment, not an error.
- `load()` on an existing file whose `deployment` equals the requested one
  returns it.
- `load()` on an existing file whose `deployment` differs raises
  `DeploymentMismatchError` carrying both names and the absolute path, and the
  file is left byte-for-byte unchanged.
- `load()` with no `deployment` argument is a `TypeError`.
- `State(...)` without `deployment` raises `ValidationError`; so does a name
  with an uppercase letter, a slash, a leading dot or hyphen, whitespace, an
  empty string, or more than 63 characters.
- `load()` on a file with no `deployment` key raises
  `StateMissingDeploymentError` naming the absolute path, `"deployment":
  "default"` and deletion as the two ways out; `str(exc)` contains none of the
  file's contents. No default is supplied.
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

- Concurrent `save()` calls against the same path (two `aiform plan apply`
  processes racing) are **not** handled — no file locking, matching
  `PLAN.md` §10's explicitly deferred "single local state file, no
  locking, no multi-user story." Not this module's job to fix.
- A `.backup` file that itself doesn't parse is never read by this
  module — `state.py` only ever writes to `.backup`, never reads it
  back. Recovering from a corrupted primary file using the backup is a
  manual, human-driven action (per `PLAN.md` §10), not an `aiform`
  command.
- A `state.json` written **before `StateEntry.reference_edges` existed** (#234)
  loads unchanged: the field defaults to `{}`, and the next plan that touches the
  entry fills it in. Nothing reads it, so `{}` behaves exactly as before.
- A `state.json` written **before `StateEntry.depends_on` existed**
  loads unchanged: the field is `Field(default_factory=list)`, so an
  entry lacking it validates and comes back with `[]`.

  **This is a free side effect, not a requirement being met.**
  `MULTI_RESOURCE_PRD.md`'s "Non-requirements" section is explicit that
  backward compatibility is not owed in any form, state schema included —
  there are zero resources in production, so a field addition that *did*
  break old files would have been acceptable too. The default is there
  because a resource with no declared dependencies needs `[]` anyway; that
  it also reads pre-feature files costs nothing. Don't infer from this
  bullet that a future state-shape change owes a migration. It does not:
  `deployment` (#201) is a required field with no default, so a file written
  before it existed no longer loads at all, and that is accepted. It fails with
  one short, actionable message instead of a Pydantic dump.

## Out of scope

- Refreshing state against live reality (`driver.read()`) —
  `orchestrator.py`.
- Diffing desired (`ResourceSpec`) vs. actual (`StateEntry`) —
  `planner.py`.
- `--state-file` flag parsing / resolving the default path from CLI
  context — `cli.py`. `state.py` only ever receives a `Path` it's given.
- State schema version migration — deliberately deferred (`PLAN.md` §10). This
  includes files written before `deployment` existed.
- Resolving a deployment by name from anywhere but the current directory (an
  environment variable, a home directory), one deployment spanning several
  state files (#210), and binding a state file to the cloud account it was
  applied against — all recorded as deferred in `plans/deployment-identity.md`.
- Any custom exception types for load/save failures — deferred until
  `exceptions.py` is built; underlying stdlib/Pydantic errors propagate
  as-is for now.

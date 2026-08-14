# specs/orchestrator.md — `aiform/orchestrator.py`

## Purpose

`PLAN.md` §5's `plan`/`apply` algorithm end to end, plus §7's `refresh`:
file discovery, dynamic driver import, credential wiring, gate #1
(`code-review-model`, driver trust) and gate #2 (`review-orchestration-model`,
plan safety) invocation, state refresh/diff orchestration (delegating the
actual diff/categorize work to `planner.py`), and execution
(`driver.create`/`read`/`update`/`delete`) with per-resource state
persistence. This is the integration layer every other module's spec
already named as "not our job, `orchestrator.py`'s job" — `parser.py`
(file discovery, `AIFORM-DELETE-` detection, state hash lookups),
`planner.py` (refresh, driver-trust checks, destroy identification,
trash moves, printing), `driver.py`/`driver_gen.py` (dynamic import,
credential wiring). Everything converges here.

**This module does no CLI I/O beyond one injectable confirmation
callback** (see judgment call 8). Printing the plan, formatting errors,
and argument parsing are `cli.py`'s job, not built yet — this spec
defines what `cli.py` will call.

**Eight judgment calls made explicit here** (`PLAN.md` under-specifies
each of these at the level needed to implement; resolved now rather than
left to drift into whatever the first implementation happens to do):

1. **`id` is stripped out of a driver's returned dict before it becomes
   `StateEntry.attributes`.** `aiform/driver.py`'s docstrings (`PLAN.md`
   §4) say `create()`/`read()`/`update()` all return "dict with at least
   `{"id": str, **attributes}`" — confirmed by
   `drivers/digitalocean/compute.py`'s `_flatten()`, which always
   includes `"id"` in what it returns. But `PLAN.md` §3's state schema
   stores `id` and `attributes` as **separate** top-level `StateEntry`
   fields, and the example `attributes` block has no nested `"id"` key.
   `orchestrator.py` is therefore the layer that does `raw.pop("id")`
   before storing the remainder as `attributes` — every driver call site
   in this module does this the same way, so no other module needs to
   know about it.

2. **`ResourceDriver.PARAM_SCHEMA` is not validated against `spec.params`
   in the MVP, despite `PLAN.md` §4's `create()` docstring claiming
   `params` arrives "already validated by the orchestrator against
   `PARAM_SCHEMA`."** No JSON Schema library is a project dependency
   (`pyproject.toml` has `pydantic`/`pyyaml`/`anthropic` only), and every
   other spec that touches `PARAM_SCHEMA` (`specs/driver.md`,
   `specs/driver_gen.md`) explicitly defers shape validation — only
   `driver_gen.py`'s static check confirms the attribute *exists*, never
   what it contains. Flagged here as a real discrepancy with `PLAN.md`
   §4's docstring, per `CLAUDE.md`'s "flag the discrepancy and propose
   the change explicitly" instruction, rather than silently adding a new
   dependency to close it. Resolution: `orchestrator.py` passes
   `spec.params` straight through to `driver.create()`/`driver.update()`
   unvalidated; a malformed `params` dict surfaces as whatever error the
   CSP API itself returns, wrapped in `DriverExecutionError` like any
   other driver-call failure. Revisit if/when a JSON Schema dependency is
   deliberately added.

3. **Step 3's "credentials don't work" check is `config.resolve_credentials()`
   succeeding, not a live CSP API call.** `PLAN.md` §5 step 3 says this
   check is distinct from step 4's refresh specifically so a brand-new
   resource — no `id` yet, nothing to `read()` — "still fails fast on a
   bad credential." But `ResourceDriver` (§4) has exactly four methods,
   none of which validate a credential without either reading an
   existing resource or creating one; `plan create` must never call
   `create()`. There is no method on the fixed interface that can test
   an *expired or malformed* token (`PLAN.md`'s own example) without side
   effects. Resolved here as: step 3's check is `config.resolve_credentials(provider)`
   not raising — catches "nothing configured at all," zero API calls,
   works identically for a new or existing resource. Detecting a token
   that's present but rejected by the CSP is deferred to whatever call
   naturally happens next (`driver.read()` at refresh for a tracked
   resource, `driver.create()` at apply for a new one), surfacing as
   `DriverExecutionError` there instead of `PlanBlockedError` at plan
   time. This narrows `PLAN.md`'s literal claim to "fails fast on a
   *missing* credential" — a real, flagged divergence.

4. **Gate #1 (`ensure_driver_trusted()`) is skipped entirely for a
   `destroy` action, for both deletion mechanisms.** `PLAN.md`'s
   "Resource deletion" section states a destroy "gets a `destroy`
   `PlanEntry` directly, skipping steps 3-6 (driver-usability checks,
   refresh, diff, categorization) entirely" — step 3 is exactly the
   gate-#1 re-review/trust check. `orchestrator.py` still has to
   dynamically import and instantiate the driver to call `delete()` on
   it (mechanical, zero LLM cost), and still resolves credentials (per
   judgment call 3, zero LLM cost), but never calls
   `llm.review_driver()` on the destroy path — matching "skipping...
   entirely" literally, not just skipping the parts that happen to be
   expensive.

5. **In-memory caching of driver instances / `DriverInfo` / credentials,
   keyed by `(provider, resource_type)` or `provider` alone, scoped to
   one `build_create_plan()` call.** Not stated anywhere in `PLAN.md`,
   but required for correctness of the cost claim it does make: two
   `.aiform.md` files in the same `plan create` run sharing a driver
   (e.g. two `digitalocean.compute` resources) must not pay gate #1's
   `code-review-model` cost twice just because neither file's result has
   been written to `.aiform/state.json` yet when the second file is
   processed (the state-based "does any entry already trust this hash"
   check in `ensure_driver_trusted()` only sees what's on disk at the
   start of the run). Driver instances are stateless per `PLAN.md`'s own
   framing, so reusing one across files sharing a `(provider,
   resource_type)` pair is safe.

6. **`build_create_plan()` structurally cross-checks every `PlanEntry`'s
   `action` against whether a `state_entry` actually exists, immediately
   after `planner.plan_resource()` returns it, instead of trusting the
   model's categorization all the way to `apply_plan()`.** Nothing in
   `categorize_diff()`'s payload (`specs/planner.md`: `diff`,
   `intent_notes`, `param_schema`, `likely_replace_fields`,
   `drifted_missing`) explicitly tells the `intent-orchestration-model`
   whether this resource already has a tracked `id` — a brand-new
   resource's diff (every key differing from `current.get(key) is None`)
   is structurally similar enough to a heavily-drifted existing
   resource's diff that a miscategorization is not implausible, and
   `apply_plan()` would either crash (`update` on a `None` `state_entry.id`)
   or silently create a duplicate, orphaned CSP resource (`create` on an
   already-tracked one) if it trusted the category blindly. `planner.py`
   itself already sets the precedent for a structural guarantee over a
   prompt-level one here — narrowing `PLAN_CATEGORIZATION_SCHEMA` to
   exclude `"destroy"` entirely rather than relying on
   `prompts/diff_plan.md` telling the model not to pick it. This
   judgment call applies that same principle one level up: `action ==
   PlanAction.UPDATE and state_entry is None`, or `action ==
   PlanAction.CREATE and state_entry is not None`, raises
   `PlanBlockedError` naming the resource and the mismatch — treated as
   an internal-consistency failure of the categorization call, not a
   recoverable planning outcome, since it indicates either a malformed
   model response or a bug in this module's own state-lookup logic. See
   `build_create_plan()`'s step 7 in Interface below.

7. **The single-resource `review-orchestration-model` re-review triggered
   by `DriverUpdateNotSupported` (`PLAN.md` §5 apply step 3) is *not*
   skippable by `--yes`, unlike the batch gate #2 confirmation.**
   `PLAN.md` describes the batch case explicitly ("`--yes` skips only
   this prompt, never a `block`") but describes this case with different
   language — "require fresh confirmation" — for a destructive replace
   the original plan review never saw (it wasn't flagged
   `likely_replace: true`, or gate #2 never ran at all because nothing
   else in the plan warranted it). Treating this confirmation the same
   as the batch one would let `--yes` silently approve an unplanned
   delete-then-create the user had no chance to review — inconsistent
   with the project's whole "review gates aren't a formality" stance
   elsewhere (e.g. a `block` flag's unconditional, `--yes`-proof halt).
   Resolved here as its own, stricter rule: `apply_plan()`'s single-
   resource fallback confirmation always calls `confirm(...)`
   interactively, regardless of `yes`. `yes=True` still means "no prompt
   at all" for this specific case is not an option — if there's no TTY
   to prompt (a fully non-interactive `--yes` run hits an
   otherwise-unflagged `DriverUpdateNotSupported`), the caller-supplied
   `confirm` callback is responsible for deciding how to fail (e.g.
   `cli.py`'s default `confirm` raising rather than blocking on `input()`
   forever) — this module doesn't special-case a missing TTY itself.

8. **`PlannedResource` and `ApplyResult` are plain `@dataclass`es local
   to this module, not `Pydantic` models in `aiform/models.py`.**
   `specs/models.md`'s established pattern (`DriverReview`, `PlanReview`,
   `LLMConfig`) puts a shape in `models.py` when it's produced in one
   module and consumed in another. Both types here are produced by this
   module and consumed by `cli.py` (not built yet) — the same kind of
   crossing. But unlike every existing `models.py` type, both hold live,
   non-JSON-serializable object references (a `ResourceDriver` instance,
   an injected `confirm` callable) — they are runtime execution-context
   bundles, not data-interchange shapes, and `models.py`'s whole premise
   (`specs/models.md`: "Pure data definitions," round-trips through
   `model_dump(mode="json")` without loss) doesn't fit them. Kept here
   instead, imported directly by `cli.py` the same way it imports this
   module's functions — a deliberate, flagged divergence from the
   established precedent, not an oversight.

## Interface

```python
DRIVERS_DIR = Path(__file__).resolve().parent.parent / "drivers"
TRASH_DIR = Path(".aiform/trash")


def resource_key(provider: str, resource_type: str, name: str) -> str: ...


# --- file discovery / classification (PLAN.md §5 step 1, "Resource deletion" Mechanism B) ---

def discover_files(paths: list[Path] | None, *, cwd: Path = Path(".")) -> list[Path]: ...
def is_delete_marked(path: Path) -> bool: ...


# --- driver resolution & gate #1 (PLAN.md §5 step 3, §4's invocation contract) ---

def driver_path(provider: str, resource_type: str) -> Path: ...

def load_driver(provider: str, resource_type: str) -> ResourceDriver: ...

def ensure_driver_trusted(
    provider: str,
    resource_type: str,
    state: State,
    *,
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
) -> DriverInfo: ...


# --- refresh (PLAN.md §3's "Refresh mechanism", §7's `aiform plan refresh`) ---

def refresh_resource(
    driver: ResourceDriver, state_entry: StateEntry, credentials: dict[str, str]
) -> tuple[dict[str, Any], bool]: ...

def refresh_state(*, state_path: Path = state.DEFAULT_STATE_PATH) -> State: ...


# --- planning context (judgment call 8) ---

@dataclass
class PlannedResource:
    entry: PlanEntry
    provider: str
    resource_type: str
    name: str
    desired_params: dict[str, Any]
    aiform_md_path: Path
    current_aiform_md_sha256: str | None
    driver: ResourceDriver | None
    driver_info: DriverInfo | None
    credentials: dict[str, str] | None
    state_entry: StateEntry | None


# --- plan create (PLAN.md §5 "aiform plan create") ---

def build_create_plan(
    paths: list[Path] | None = None,
    *,
    cwd: Path = Path("."),
    state_path: Path = state.DEFAULT_STATE_PATH,
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
) -> tuple[list[PlannedResource], list[str]]: ...


# --- plan destroy, Mechanism A (PLAN.md "Resource deletion") ---

def build_destroy_plan(
    paths: list[Path] | None = None,
    *,
    state_path: Path = state.DEFAULT_STATE_PATH,
) -> list[PlannedResource]: ...


# --- apply (PLAN.md §5 "aiform plan apply", also used by `aiform plan destroy`) ---

ConfirmFn = Callable[[str], bool]


@dataclass
class ApplyResult:
    executed: list[PlanEntry]
    review_flags: list[PlanReviewFlag]
    aborted: bool


def build_plan_summary(planned: list[PlannedResource]) -> str: ...

def apply_plan(
    planned: list[PlannedResource],
    *,
    state_path: Path = state.DEFAULT_STATE_PATH,
    yes: bool = False,
    confirm: ConfirmFn | None = None,
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
) -> ApplyResult: ...


# --- trash (PLAN.md "Resource deletion" > "Trash directory") ---

def move_to_trash(path: Path, *, trash_dir: Path = TRASH_DIR) -> Path: ...
```

**Also required in the same PR** (tightly-coupled addition, per
`PROCESS.md`'s "one module, or one module and the exceptions it raises"
allowance): `aiform/exceptions.py` gains the two types `PLAN.md` §1
already named for it but that had no caller until now —

```python
class DriverExecutionError(Exception):
    def __init__(self, provider: str, resource_type: str, operation: str, original: Exception):
        self.provider = provider
        self.resource_type = resource_type
        self.operation = operation
        self.original = original
        super().__init__(f"{provider}.{resource_type} driver failed during {operation}: {original}")


class PlanBlockedError(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)
```

`specs/exceptions.md` is updated in the same PR to reflect these two
types are no longer deferred, and to note `orchestrator.py` as their
caller.

### `resource_key(provider, resource_type, name) -> str`

`f"{provider}.{resource_type}.{name}"` — this address format (`PLAN.md`
§3), reused by every function below instead of each re-deriving the same
f-string. **Not** the only place this exact format is assembled in the
codebase: `specs/state.md`'s `State` validator independently
reconstructs it from a `StateEntry`'s own fields to check a
`state.resources` key against its entry — unavoidably, since `state.py`
cannot import `orchestrator.py` without a cycle. If the address format
ever changes, both call sites need updating; this function only
guarantees every use *within `orchestrator.py`* stays consistent with
itself, not a codebase-wide single source of truth.

### `discover_files(paths, *, cwd=Path(".")) -> list[Path]`

`paths` truthy → returned as `Path` objects, in the given order,
unchecked for existence (a missing file raises `FileNotFoundError`
naturally the first time something reads it — no separate existence
check here). `paths` `None` or `[]` → `sorted(cwd.glob("*.aiform.md"))`,
sorted for deterministic file-order processing (`PLAN.md` §5 step 3's
"file order" execution requirement). `AIFORM-DELETE-`-prefixed files
match this same glob and are returned alongside everything else — see
`is_delete_marked()`.

### `is_delete_marked(path) -> bool`

`path.name.startswith("AIFORM-DELETE-")`. Filename-only check, per
`PLAN.md`'s Mechanism B — never inspects file contents.

### `driver_path(provider, resource_type) -> Path`

`DRIVERS_DIR / provider / f"{resource_type}.py"` — the absolute,
installed-package location (mirrors `llm.PROMPTS_DIR`'s construction),
**not** relative to the end user's project `cwd`. Drivers ship inside
the `aiform` package itself (`PLAN.md` §1), never per-project.

### `load_driver(provider, resource_type) -> ResourceDriver`

`importlib.util.spec_from_file_location(...)` /
`module_from_spec(spec)` / `spec.loader.exec_module(module)` against
`driver_path(provider, resource_type)`, then `module.Driver()` — exactly
`PLAN.md` §4's "Orchestrator invocation contract": the class name is
always literally `Driver`, never searched for. `FileNotFoundError`
(driver file doesn't exist) is caught and re-raised as
`PlanBlockedError` naming the missing `(provider, resource_type)` pair
(`PLAN.md` §5 step 3's "Driver file missing" case). Any other failure
while importing or instantiating (syntax error, missing concrete method
raising `TypeError` per `specs/driver.md`) propagates uncaught — a
curated, already-tested driver failing to import is a real bug, not a
recoverable planning-time condition.

### `ensure_driver_trusted(provider, resource_type, state, *, client=None, llm_config=None) -> DriverInfo`

Gate #1. Reads `driver_path(provider, resource_type)` as bytes and
hashes it (`hashlib.sha256(...).hexdigest()` over the raw bytes, not
decoded text — unlike `parser.compute_sha256()`, this hash's job is
literally "is this the exact file that was reviewed," so byte-exact is
the right comparison, with no BOM-stripping concern since this file was
never hand-edited by a non-technical user the way an `.aiform.md` or
`credentials.env` might be).

1. Search `state.resources.values()` for any entry with matching
   `provider`/`resource_type` and `driver.sha256 == on_disk_sha256`. If
   found, return that entry's `driver` (`DriverInfo`) unchanged — zero
   LLM calls, the "already trusted" path (`PLAN.md` §9 walkthrough step
   4).
2. Otherwise, read the file as UTF-8 text and call
   `llm.review_driver(source_text, client=client, llm_config=llm_config)`.
   `not review.approved` → raise `PlanBlockedError` naming the driver and
   `review.blocking_issues or review.concerns`
   (`PLAN.md` §5 step 3's "do not overwrite... fail... naming the
   concerns"). Otherwise, build and return a new `DriverInfo(path=f"drivers/{provider}/{resource_type}.py",
   sha256=on_disk_sha256, generated_at=<now, UTC>, code_review=review)` —
   `generated_at` is stamped with the same timestamp as this review, not
   a real generation event, since a curated MVP driver was never
   generated at all; the field exists for the deferred generation path
   (`PLAN.md` §6) and is reused here rather than left `None`, since
   `DriverInfo.generated_at` isn't optional.

Never itself writes to `.aiform/state.json` — the caller
(`build_create_plan()`/`apply_plan()`) is the one recording a new
`DriverInfo` into a `StateEntry`.

### `refresh_resource(driver, state_entry, credentials) -> tuple[dict[str, Any], bool]`

`driver.read(state_entry.id, credentials)`, judgment call 1's `id`
stripped from the result. On `ResourceNotFoundError`: returns
`(state_entry.attributes, True)` — last-known attributes, unchanged,
plus the `drifted_missing` flag (`PLAN.md` §3's refresh mechanism, step
2). On success: returns `(attrs_without_id, False)`. Any other exception
from `driver.read()` is wrapped: `raise DriverExecutionError(state_entry.provider,
state_entry.resource_type, "read", exc) from exc`.

### `refresh_state(*, state_path=state.DEFAULT_STATE_PATH) -> State`

`aiform plan refresh` (`PLAN.md` §7): **zero LLM calls, no `.aiform.md`
parsing, no plan** — for every entry in `state.load(state_path).resources`,
`load_driver()` + `config.resolve_credentials()` (translated to
`PlanBlockedError` on failure, same as judgment call 3) +
`refresh_resource()`, updating `attributes`/`last_refreshed_at` in
place. Unlike `apply_plan()`'s per-resource write (see Behavior below),
this saves **once**, after every tracked resource has been refreshed —
there are no create/destroy side effects here to protect against a
mid-run crash losing; a crash partway through just leaves some entries
with stale attributes, recoverable by re-running `refresh` again, so
batching the single backup-and-write is simpler and just as safe. A
resource whose `driver.read()` raises `ResourceNotFoundError` is not
removed from state or otherwise treated specially by this command —
`attributes` simply stays at its last-known value (per
`refresh_resource()`'s own contract); the drift becomes actionable the
next time `plan create` runs against that resource, not here.

### `build_create_plan(paths=None, *, cwd=Path("."), state_path=..., client=None, llm_config=None) -> (list[PlannedResource], list[str])`

`PLAN.md` §5 "aiform plan create" steps 1-7, per file discovered by
`discover_files(paths, cwd=cwd)`:

- **`is_delete_marked(path)` is true** (Mechanism B): read the file,
  `spec = parser.parse_frontmatter(content)` only — no `parse_file()`
  call, so intent extraction never runs, "unconditionally... regardless
  of hash" (`PLAN.md` §5 step 2) falls out naturally rather than needing
  a special-cased skip. `key = resource_key(spec.provider, spec.resource, spec.name)`,
  `state_entry = state.resources.get(key)`. `entry = planner.destroy_entry(key,
  rationale=f"marked for deletion via {path.name}")`. Resulting
  `PlannedResource` has `desired_params={}` (same reasoning as
  `build_destroy_plan()`'s identical choice below — unused by a destroy,
  kept uniform rather than threading `spec.params` through here just
  because it happens to be available) and
  `driver=driver_info=credentials=None` — resolved lazily by
  `apply_plan()` instead, per judgment call 4 (gate #1 is skipped for a
  destroy either way, so there's nothing to gain by resolving the driver
  this early, and every file not otherwise needing it should not pay for
  it).
- **Otherwise** (normal file):
  1. `content = path.read_text(encoding="utf-8-sig")`, `spec =
     parser.parse_frontmatter(content)` — a first, frontmatter-only pass
     purely to compute `key = resource_key(...)` before the hash lookup
     below; `parser.parse_file()` (step 3) re-reads and re-parses the
     same content internally (`specs/parser.md`'s own "independent,
     idempotent functions" design) — a deliberate small redundancy, not
     a bug, since `parse_file()`'s fixed interface requires the previous
     hash as an *input*, and that hash can't be looked up without
     already knowing which state entry (if any) this file addresses.
  2. `state_entry = state.resources.get(key)`,
     `previous_hash = state_entry.aiform_md_sha256 if state_entry else None`.
  3. `parsed = parser.parse_file(path, previous_aiform_md_sha256=previous_hash,
     client=client, llm_config=llm_config)`.
  4. Driver resolution, **cached per `(provider, resource_type)` for the
     lifetime of this call** (judgment call 5): `driver =
     load_driver(spec.provider, spec.resource)`; `driver_info =
     ensure_driver_trusted(spec.provider, spec.resource, state,
     client=client, llm_config=llm_config)`.
  5. Credentials, **cached per `provider`**: `credentials =
     config.resolve_credentials(spec.provider)`, `RuntimeError` caught
     and re-raised as `PlanBlockedError(str(exc))` (judgment call 3).
  6. Refresh: `state_entry is not None` → `current_attributes,
     drifted_missing = refresh_resource(driver, state_entry,
     credentials)`, and `state_entry.attributes`/`last_refreshed_at` are
     updated in place on the in-memory `state` object (§3's "written
     back... even during a bare plan with no changes"). `state_entry is
     None` (brand-new resource) → `current_attributes = {}`,
     `drifted_missing = False` — nothing to refresh yet.
  7. `entry = planner.plan_resource(key, current_attributes, spec.params,
     intent_notes=parsed.intent_notes, param_schema=driver.PARAM_SCHEMA,
     likely_replace_fields=driver.LIKELY_REPLACE_FIELDS,
     state_aiform_md_sha256=previous_hash,
     current_aiform_md_sha256=parsed.aiform_md_sha256,
     drifted_missing=drifted_missing, client=client, llm_config=llm_config)`.
  8. **Structural cross-check** (judgment call 6): `entry.action ==
     PlanAction.UPDATE and state_entry is None`, or `entry.action ==
     PlanAction.CREATE and state_entry is not None`, raises
     `PlanBlockedError` naming `key` and the mismatch — a categorization
     response that disagrees with this module's own ground truth about
     whether the resource is already tracked is never executed, no
     matter how it was produced. `NO_OP`/`DESTROY` (the latter never
     actually returned by `plan_resource()`, per `specs/planner.md`) need
     no check here — `NO_OP` is only ever returned when the no-op
     short-circuit already confirmed `current_attributes`/`desired_params`
     agree, and `plan_resource()` cannot return `DESTROY` at all.
  9. `PlannedResource(entry=entry, provider=spec.provider,
     resource_type=spec.resource, name=spec.name,
     desired_params=spec.params, aiform_md_path=path,
     current_aiform_md_sha256=parsed.aiform_md_sha256, driver=driver,
     driver_info=driver_info, credentials=credentials,
     state_entry=state_entry)`.

After every file: `state.save(state, state_path)` — once, matching
`refresh_state()`'s "no destructive side effects to protect, batch the
write" reasoning above (`build_create_plan()` never creates, updates, or
destroys anything itself; it only refreshes cached attributes). Returns
`(planned, warnings)`; `warnings` is populated **only** when `paths` was
falsy (default, discover-all mode): every `state.resources` key not
covered by any `PlannedResource` built this run (including ones targeted
for destroy) is reported as a warning string naming the resource, per
`PLAN.md` §5's "left alone... reported with a warning" rule for the
no-argument invocation. When explicit `paths` were given, `warnings` is
always `[]` — a tracked resource simply not named is expected scoping,
not an anomaly (same section).

### `build_destroy_plan(paths=None, *, state_path=...) -> list[PlannedResource]`

Mechanism A. `state = state.load(state_path)`.

- `paths` given: for each, `spec = parser.parse_frontmatter(path.read_text(encoding="utf-8-sig"))`,
  `key = resource_key(...)`, `state_entry = state.resources.get(key)`.
- `paths` falsy: one target per `state.resources` entry, `aiform_md_path =
  Path(state_entry.aiform_md_path)`.

Either way: `entry = planner.destroy_entry(key, rationale=...)`
(naming either the file or "no files given: destroying all tracked
resources"), `PlannedResource(..., desired_params={}, driver=driver_info=credentials=None,
state_entry=state_entry)` — same lazy-resolution stance as Mechanism B
above, and for the same reason (judgment call 4). `desired_params={}`
**unconditionally, in both branches** — even when `paths` was given and
`spec.params` was actually available from the frontmatter parse, it is
deliberately discarded rather than threaded through: `apply_plan()`'s
`DESTROY` branch never reads `desired_params` (a destroy needs the
resource's `id`, not its desired shape), and using `spec.params` in one
branch but `{}` in the other would be a real, silent inconsistency for
no caller that needs it. Never mutates or saves state — this command has
no refresh/diff step (`PLAN.md`: "skipping steps 3-6... entirely").

### `build_plan_summary(planned) -> str`

`json.dumps([{"resource_key": pr.entry.resource_key, "action":
pr.entry.action.value, "rationale": pr.entry.rationale, "likely_replace":
pr.entry.likely_replace} for pr in planned])` — the `plan_summary` string
`llm.review_plan()` (`PLAN.md` §5 apply step 2) takes as its sole
argument.

### `apply_plan(planned, *, state_path=..., yes=False, confirm=None, client=None, llm_config=None) -> ApplyResult`

`PLAN.md` §5 "aiform plan apply" steps 2-4 (step 1, re-running `plan` in
full, is the caller's job — see Behavior below), shared verbatim by
`aiform plan destroy`'s "plans and applies in one pass."
`state = state.load(state_path)` fresh at the start.

1. **Gate #2, conditionally**: `needs_review = any(pr.entry.action ==
   PlanAction.DESTROY or (pr.entry.action == PlanAction.UPDATE and
   pr.entry.likely_replace) for pr in planned)`. If true:
   `review = llm.review_plan(build_plan_summary(planned), client=client,
   llm_config=llm_config)`. Any `flag.severity == PlanReviewSeverity.BLOCK`
   → raise `PlanBlockedError` naming every blocking flag, **unconditionally**
   — `yes=True` never bypasses this (`PLAN.md`: "cannot be bypassed by
   `--yes`"). Non-blocking flags are carried into the final
   `ApplyResult.review_flags`. If `needs_review` is false, gate #2 is
   never called at all (`PLAN.md` §9 walkthrough step 3) —
   `review_flags` stays `[]`.
2. **Confirmation**, unless `yes=True`: `(confirm or default_confirm)(prompt_text)`.
   `False` → return `ApplyResult(executed=[], review_flags=<from step 1>,
   aborted=True)` immediately, nothing executed, state untouched.
   `default_confirm` reads a `y`/`N` answer via `input()`; injectable
   exactly like `client`/`llm_config` elsewhere in this codebase, for the
   same off-the-real-I/O testing reason.
3. **Execute**, in `planned`'s given order (`PLAN.md`: "trivial for
   MVP's single-resource-per-file model"):
   - `NO_OP` → skip; nothing to persist (`build_create_plan()` already
     persisted its refreshed attributes).
   - `CREATE` → `raw = pr.driver.create(pr.desired_params, pr.credentials)`
     (raw driver exceptions wrapped in `DriverExecutionError`, operation
     `"create"`); `id, attrs = raw.pop("id"), raw` (judgment call 1);
     new `StateEntry(provider=pr.provider, resource_type=pr.resource_type,
     name=pr.name, id=id, attributes=attrs, driver=pr.driver_info,
     last_applied_at=last_refreshed_at=<now>,
     aiform_md_path=str(pr.aiform_md_path),
     aiform_md_sha256=pr.current_aiform_md_sha256)` written into
     `state.resources[pr.entry.resource_key]`.
   - `UPDATE` → `try: raw = pr.driver.update(pr.state_entry.id,
     pr.state_entry.attributes, pr.desired_params, pr.credentials)`,
     any exception other than `DriverUpdateNotSupported` wrapped in
     `DriverExecutionError` (operation `"update"`), same as every other
     driver call site in this loop.
     - `DriverUpdateNotSupported` raised: if `not pr.entry.likely_replace`
       (this resource's replace wasn't already covered by step 1's batch
       review — either because `needs_review` was false, or it was true
       but this particular entry wasn't flagged `likely_replace`), run a
       **single-resource** gate #2: `review_plan(build_plan_summary([pr
       with entry.likely_replace forced True for the summary's benefit]))`.
       Block flags halt the same as step 1's batch review. **Unlike**
       step 1-2's confirmation, this one is never skipped by `yes=True`
       (judgment call 7) — `confirm(...)` is always called, and a decline
       here ends the loop the same way a top-level decline does (see
       Edge cases below for what `ApplyResult` reports in that case).
       Either way (already covered by the batch review, or freshly
       re-reviewed and confirmed here): `pr.driver.delete(pr.state_entry.id,
       pr.credentials)` then `raw = pr.driver.create(pr.desired_params,
       pr.credentials)` — the replace, both calls wrapped in
       `DriverExecutionError` (operations `"delete"`/`"create"`
       respectively) exactly like every other driver call in this loop.
     - No exception: `raw` is the updated attributes directly, no
       replace.
     - Either path: `id, attrs = raw.pop("id"), raw`; the existing
       `StateEntry` at `pr.entry.resource_key` is updated in place —
       `id`, `attributes`, `driver=pr.driver_info`, `last_applied_at=<now>`,
       `aiform_md_sha256=pr.current_aiform_md_sha256` (`driver`/`aiform_md_sha256`
       only actually change on a replace, but overwriting them
       unconditionally with the current values is simpler than branching,
       and idempotent when nothing changed).
   - `DESTROY` → if `pr.state_entry is not None`: `driver =
     load_driver(pr.provider, pr.resource_type)`, `credentials =
     config.resolve_credentials(pr.provider)` (`RuntimeError` →
     `PlanBlockedError`, same as judgment call 3) — **no gate #1 call**
     (judgment call 4). `driver.delete(pr.state_entry.id, credentials)`
     (wrapped in `DriverExecutionError`, operation `"delete"`, on raw
     failure — per "Verification," the file is **not** moved to trash if
     this raises). On success: `del state.resources[pr.entry.resource_key]`.
     If `pr.state_entry is None` (untracked `AIFORM-DELETE-` file): skip
     `driver.delete()` entirely — nothing tracked, nothing to remove from
     state, per `PLAN.md`'s "already satisfied without a wasted API
     call." Either way, once the CSP-side delete (if any) is verified:
     `move_to_trash(pr.aiform_md_path)`.
   - After each non-`NO_OP` entry completes: `state.save(state,
     state_path)` — **per-resource**, not batched (`PLAN.md` §5 apply
     step 4), unlike `build_create_plan()`/`refresh_state()`'s
     end-of-run save: a mid-`apply` crash here must not lose state for
     resources already successfully created/updated/destroyed before it.
4. Returns `ApplyResult(executed=[pr.entry for pr in planned if pr.entry.action
   != PlanAction.NO_OP], review_flags=<accumulated non-blocking flags>,
   aborted=False)`.

### `move_to_trash(path, *, trash_dir=TRASH_DIR) -> Path`

`trash_dir.mkdir(parents=True, exist_ok=True)`; base destination
`trash_dir / f"{utcnow:%Y%m%dT%H%M%SZ}-{path.name}"`. `PLAN.md`'s "Trash
directory" section states this naming exists specifically "so repeated
deletions of resources that happen to share a filename never collide" —
a second-resolution timestamp alone doesn't actually guarantee that (two
destroys of same-named files within the same UTC second collide), so
this function closes the gap itself: if the base destination already
exists, a `-2`, `-3`, ... suffix is appended before the extension
(`...Z-name-2.aiform.md`) until a free name is found. `shutil.move(path,
destination)` (not `Path.rename` — `trash_dir` may be a different
filesystem in principle, and `shutil.move` handles that transparently).
Returns the destination path.

## Behavior

- **Step 1 of `PLAN.md` §5's apply algorithm — "re-run plan in full
  immediately before executing" — is `cli.py`'s responsibility, not this
  module's.** `apply_plan()` takes an already-built `list[PlannedResource]`;
  it is `cli.py`'s job to call `build_create_plan()` (or
  `build_destroy_plan()`) immediately beforehand, every time `apply`
  runs, rather than reusing a plan object across a saved-file boundary
  that doesn't exist in the MVP (`PLAN.md`: "no separate saved-plan-file
  flow"). This module has no notion of a persisted, reusable plan at
  all.
- **`aiform plan create`'s Mechanism B destroys and `aiform plan
  destroy`'s Mechanism A destroys converge on the exact same
  `apply_plan()` execute-loop branch** — a `PlannedResource` with
  `entry.action == PlanAction.DESTROY` is handled identically regardless
  of which `build_*_plan()` function produced it, matching `PLAN.md`'s
  "Both converge on the same underlying behavior in `orchestrator.py`."
- Every top-level function (`build_create_plan`, `build_destroy_plan`,
  `apply_plan`, `refresh_state`) independently calls `state.load(state_path)`
  at its own start and `state.save(...)` at its own end (once or
  per-resource, per function) — none of them thread a shared, mutated
  `State` object across a function-call boundary. Safe in the MVP's
  single-process, synchronous execution model (no concurrent writers
  within one CLI invocation); simplest to test, since each function is a
  self-contained unit against a `tmp_path`-backed `state_path`.
- `driver`/`driver_info`/`credentials` on a `PlannedResource` are always
  either all populated or all `None` together — populated for every
  non-destroy resource (even one that turns out `NO_OP`, since refresh
  needed them regardless), `None` for every destroy target (resolved
  lazily inside `apply_plan()`, or never resolved at all for an
  untracked Mechanism-B destroy).

## Edge cases / errors

- A file that fails `parser.parse_frontmatter()`/`parse_file()`
  (malformed YAML, failed `ResourceSpec` validation) propagates
  `ValueError`/`pydantic.ValidationError` uncaught from
  `build_create_plan()`/`build_destroy_plan()` — a hand-edit error in one
  file is not caught and skipped in favor of processing the rest; the
  whole `plan create` run fails loudly, consistent with every other
  "let it fail loudly" stance already established (`specs/parser.md`,
  `specs/planner.md`).
- `ensure_driver_trusted()`'s in-memory cache (judgment call 5) is scoped
  to a single `build_create_plan()` call only — it is not module-level,
  global, or shared with `apply_plan()`'s own lazy driver resolution for
  destroy targets. A destroy's driver lookup always goes through
  `load_driver()`/`config.resolve_credentials()` freshly, since gate #1
  never runs on that path in the first place (nothing to cache).
- `DriverUpdateNotSupported`'s single-resource gate #2 re-review
  (`apply_plan()`'s `UPDATE` branch) can itself raise `PlanBlockedError`
  on a `block` flag, or trigger a decline via `confirm(...)` (never
  skipped here, per judgment call 7) — **mid-execute-loop**, after zero
  or more earlier entries in `planned` have already been successfully
  applied and persisted. Those earlier resources' state changes are not
  rolled back; the loop simply stops. This matches `PLAN.md` §5 step 4's
  own framing ("a crash mid-apply doesn't lose successfully-applied
  resources' state") — a blocked/declined replace partway through is
  treated the same as a crash for this purpose, not specially unwound. A
  declined mid-loop confirmation returns `ApplyResult(executed=[pr.entry
  for pr in planned already fully processed before this point,
  excluding the one that triggered the decline], review_flags=<flags
  accumulated so far, from both the initial batch review if it ran and
  this single-resource one>, aborted=True)` — the same field-by-field
  contract as the top-level decline in step 2, just computed over a
  prefix of `planned` instead of the empty list, so `cli.py` can report
  exactly what was and wasn't applied.
- `move_to_trash()`'s numeric-suffix collision handling (see its own
  Interface entry above) means two destroys of same-named files within
  the same UTC second never overwrite each other, closing the gap a
  plain timestamp alone would have left and matching `PLAN.md`'s literal
  "never collide" framing for the trash directory.

## Out of scope

- **All CLI argument parsing, output formatting/printing (the plan
  table, `--json`, error message formatting), and `--verbose`/`_redact()`
  logging** — `cli.py`, not built yet.
- **`aiform init`, `aiform plan show`, and everything under `aiform
  driver ...`** (`PLAN.md` §7) — `cli.py` (`init`/`show`) or the deferred
  mechanism-2 wiring (`driver ...`), neither this module's concern.
  `plan show` in particular needs no orchestrator involvement at all —
  it's a direct `state.load()` plus formatting, entirely in `cli.py`.
- **On-the-fly driver generation** (`aiform/driver_gen.py`'s
  `generate_driver()`) — still not wired into `plan`/`apply` here, per
  `PLAN.md`'s explicit sequencing ("only *after* the primary
  orchestration flow... is stable and proven"). `ensure_driver_trusted()`
  only ever re-reviews an existing on-disk file; it never calls
  `driver_gen.generate_driver()` when a driver is missing, it raises
  `PlanBlockedError` via `load_driver()` instead.
- **`PARAM_SCHEMA` shape validation** — judgment call 2.
- **Live credential validity checking** (an expired/malformed token
  detected before the CSP itself rejects a real call) — judgment call 3.
- **A dependency graph / multi-resource sequencing** — `PLAN.md` §10,
  unchanged; this module processes `planned` in the literal order it was
  built, one resource at a time, with no notion of one resource
  depending on another.

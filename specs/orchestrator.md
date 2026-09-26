# specs/orchestrator.md — `aiform/orchestrator.py`

## Purpose

`PLAN.md` §5's `plan`/`apply` algorithm end to end, plus §7's `refresh`:
file discovery, dynamic driver import, credential wiring, driver-provenance
recording, and gate #2 (`review-orchestration-model`,
plan safety) invocation, state refresh/diff orchestration (delegating the
actual diff/categorize work to `planner.py`), and execution
(`driver.create`/`read`/`update`/`delete`) with per-resource state
persistence. This is the integration layer every other module's spec
already named as "not our job, `orchestrator.py`'s job" — `parser.py`
(file discovery, `AIFORM-DELETE-` detection, state hash lookups),
`planner.py` (refresh, driver-trust checks, destroy identification,
trash moves, printing), `driver.py`/`driver_gen.py` (dynamic import,
credential wiring). Everything converges here.

**This module does no CLI I/O beyond two injectable callbacks: one
confirmation prompt and one review-flags-observed hook** (see judgment
calls 8 and 9). Printing the plan, formatting errors, and argument
parsing are `cli.py`'s job — this spec defines what `cli.py` calls.

**Nine judgment calls made explicit here** (`PLAN.md` under-specifies
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

4. **`driver_info_for()` is skipped entirely for a `destroy` action, for
   both deletion mechanisms.** `PLAN.md`'s "Resource deletion" section
   states a destroy "gets a `destroy` `PlanEntry` directly, skipping steps
   3-6 (driver-usability checks, refresh, diff, categorization) entirely"
   — step 3 is exactly this provenance lookup. `orchestrator.py` still has
   to dynamically import and instantiate the driver to call `delete()` on
   it, and still resolves credentials (per judgment call 3), but never
   computes or persists a `DriverInfo` for the destroy path: a resource
   being removed from state has no `driver` field left to fill in, so
   there is nothing for the hash to record. This is no longer about
   avoiding an LLM call (there isn't one, on any path) — it's that the
   provenance record has no destination on a destroy.

5. **In-memory caching of driver instances / `DriverInfo` / credentials,
   keyed by `(provider, resource_type)` or `provider` alone, scoped to
   one `build_create_plan()` call.** Not stated anywhere in `PLAN.md`, but
   required so that two `.aiform.md` files in the same `plan create` run
   sharing a driver (e.g. two `digitalocean.compute` resources) hash and
   `importlib`-load that driver's file exactly once, not once per file —
   `driver_info_for()`'s own hashing is deterministic and cheap, but
   re-reading and re-hashing the same on-disk file redundantly for every
   resource that happens to share it is still wasted work worth avoiding.
   Driver instances are stateless per `PLAN.md`'s own framing, so reusing
   one across files sharing a `(provider, resource_type)` pair is safe.

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
   PlanAction.CREATE and state_entry is not None and not
   drifted_missing`, raises `PlanBlockedError` naming the resource and
   the mismatch — treated as an internal-consistency failure of the
   categorization call, not a recoverable planning outcome, since it
   indicates either a malformed model response or a bug in this
   module's own state-lookup logic. The `not drifted_missing` exemption
   on the `CREATE` side is load-bearing, not incidental: `PLAN.md` §3's
   refresh mechanism sets `drifted_missing: true` specifically so a
   tracked-but-vanished resource is planned as a `create` (recreate)
   while `state_entry` is still the old, stale entry — without this
   exemption the check would block the one recreate flow the refresh
   mechanism exists to enable. (Before issue &#35;117 that `create` was the
   model's categorization of the diff; it is now `create_entry()`'s, per
   step 7. The exemption is needed either way, and for the same
   resource-state reason.) See `build_create_plan()`'s step 8 in
   Interface below.

   **The prediction above came true, and the guard was the wrong answer
   to it** (issue &#35;117). The live domain system test hit exactly the
   miscategorization this paragraph anticipated: the model answered
   `update` for a brand-new zone, the cross-check fired, and a user's
   first `plan apply` failed on an internal invariant —
   non-deterministically, succeeding on retry. Guarding a needless guess
   is strictly worse than not guessing, so step 7 no longer asks: an
   untracked or drifted-missing resource is `create` by construction.
   That also closes the sharper hole this paragraph missed — nothing
   here catches `update` on a *drifted-missing* resource (the first
   condition needs `state_entry is None`, the second needs `CREATE`), so
   a wrong answer there reached `apply_plan()` and called
   `driver.update()` against an id that no longer exists.

   The cross-check itself stays, for the categorizations that remain
   genuinely open. Its `UPDATE and state_entry is None` half is now
   unreachable; the `CREATE` half is still live and still reachable.

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
   Like the batch case, `on_review_fn` (judgment call 9) is called with
   this review's own flags immediately before this `confirm_fn(...)`
   call — the human must see what this specific re-review flagged before
   answering, for the same reason as the batch case, and this path is
   never skippable by `--yes` either.

8. **`PlannedResource` and `ApplyResult` are plain `@dataclass`es local
   to this module, not `Pydantic` models in `aiform/models.py`.**
   `specs/models.md`'s established pattern (`DriverReview`, `PlanReview`,
   `LLMConfig`) puts a shape in `models.py` when it's produced in one
   module and consumed in another. Both types here are produced by this
   module and consumed by `cli.py` — the same kind of
   crossing. But unlike every existing `models.py` type, both hold live,
   non-JSON-serializable object references (a `ResourceDriver` instance,
   an injected `confirm` callable) — they are runtime execution-context
   bundles, not data-interchange shapes, and `models.py`'s whole premise
   (`specs/models.md`: "Pure data definitions," round-trips through
   `model_dump(mode="json")` without loss) doesn't fit them. Kept here
   instead, imported directly by `cli.py` the same way it imports this
   module's functions — a deliberate, flagged divergence from the
   established precedent, not an oversight.

9. **`apply_plan()` takes a second injectable callback, `on_review`, rather
   than folding review flags into `confirm`'s prompt or having this module
   print them itself (issue #166).** Gate #2's non-blocking flags used to
   reach the user only inside the returned `ApplyResult`, printed by
   `cli.py` after `apply_plan()` had already returned — by which point
   both confirmation prompts this module can ask (the batch one and the
   single-resource fallback one) had already been answered blind. Two
   alternatives were rejected: widening `ConfirmFn` to
   `Callable[[str, list[PlanReviewFlag]], bool]` would touch every existing
   caller and test that passes a bare `lambda prompt: ...` as `confirm`,
   for no real gain — "what to tell the user" and "how to get a yes/no
   answer" are independent concerns; and formatting the flags into the
   prompt string *inside this module* would mean this module doing display
   formatting, exactly what this spec's opening line rules out. Instead,
   `on_review: OnReviewFn | None = None` (default: `confirm`/`default_confirm`'s
   pattern of "an injectable with a no-op fallback" — no separate named
   `default_on_review` exists, since there's nothing for a fallback to do
   here; `on_review or (lambda flags: None)` inline is the whole of it) is
   called with a review's flags — and only that review's flags, never a
   running total — immediately after each
   gate #2 call completes and before the confirmation that follows it, at
   both review points in `apply_plan()`. Under `yes=True` the call still
   happens (only the batch confirmation prompt is skippable; the flags
   that would have justified it are not).

## Interface

```python
DRIVERS_DIR = Path(__file__).resolve().parent.parent / "drivers"
TRASH_DIR = Path(".aiform/trash")


def resource_key(provider: str, resource_type: str, name: str) -> str: ...


# --- file discovery / classification (PLAN.md §5 step 1, "Resource deletion" Mechanism B) ---


def discover_files(paths: list[Path] | None, *, cwd: Path = Path(".")) -> list[Path]: ...
def is_delete_marked(path: Path) -> bool: ...


# --- driver resolution & provenance (PLAN.md §5 step 3, §4's invocation contract) ---


def driver_path(provider: str, resource_type: str) -> Path: ...


def load_driver(provider: str, resource_type: str) -> ResourceDriver: ...


def driver_info_for(
    provider: str,
    resource_type: str,
    state: State,
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
    force: bool = False,
) -> tuple[list[PlannedResource], list[str]]: ...


# --- apply (PLAN.md §5 "aiform plan apply", also used by `aiform plan destroy`) ---

ConfirmFn = Callable[[str], bool]
OnReviewFn = Callable[[list[PlanReviewFlag]], None]


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
    on_review: OnReviewFn | None = None,
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

### `driver_info_for(provider, resource_type, state) -> DriverInfo`

**No LLM call on this path, ever — nothing here gates driver execution.**
This function only records which driver version produced or will produce
a resource; it makes no trust decision and blocks nothing. (History: this
was `ensure_driver_trusted()`, gate #1's driver-review-then-trust flow. It
never actually delivered that guarantee even before removal —
`build_create_plan()` calls `load_driver()`, which `exec_module`s the
driver file, *before* calling this function, so any review ran after the
reviewed code had already executed. See issue #119.)

**Warning for whatever eventually replaces this function** (#119's
deferred validation mechanism): that ordering bug is still latent in
`build_create_plan()`'s call site (`load_driver()` before
`driver_info_for()`), merely unobservable now that nothing here does
anything the ordering could break. Adding a gate back into this function
without also moving the hash/validation step *before* `load_driver()`
at the call site reproduces #119's exact defect — a check that runs after
the code it's meant to gate has already executed.

Reads `driver_path(provider, resource_type)` as bytes and hashes it
(`hashlib.sha256(...).hexdigest()` over the raw bytes, not decoded text —
unlike `parser.compute_sha256()`, this hash's job is "which exact file
produced this resource," so byte-exact is the right comparison, with no
BOM-stripping concern since this file was never hand-edited by a
non-technical user the way an `.aiform.md` or `credentials.env` might be).

1. Search `state.resources.values()` for any entry with matching
   `provider`/`resource_type` and `driver.sha256 == on_disk_sha256`. If
   found, return that entry's `driver` (`DriverInfo`) unchanged (`PLAN.md`
   §9 walkthrough step 4).
2. Otherwise, build and return a new
   `DriverInfo(path=f"drivers/{provider}/{resource_type}.py",
   sha256=on_disk_sha256, generated_at=<now, UTC>)` — `generated_at` is
   stamped with the current time, not a real generation event, since a
   curated MVP driver was never generated at all; the field exists for
   the deferred generation path (`PLAN.md` §6) and is reused here rather
   than left `None`, since `DriverInfo.generated_at` isn't optional.

Never itself writes to `.aiform/state.json` — the caller
(`build_create_plan()`/`apply_plan()`) is the one recording a new
`DriverInfo` into a `StateEntry`.

### `refresh_resource(driver, state_entry, credentials) -> tuple[dict[str, Any], bool]`

`driver.read(state_entry.id, credentials)`, judgment call 1's `id`
stripped from the result via the same `_pop_id()` helper `apply_plan()`'s
`CREATE`/`UPDATE` branches use — a `read()` response missing `"id"`
entirely raises `DriverExecutionError` (operation `"read"`) rather than
being silently tolerated, matching `_pop_id()`'s stance everywhere else
it's used: a missing `"id"` is a driver-contract violation, not a
recoverable edge case, even though this call site doesn't actually need
the popped id value (the caller already has it as `state_entry.id`) —
the point is validating the contract, not consuming the return value. On
`ResourceNotFoundError`: returns `(state_entry.attributes, True)` —
last-known attributes, unchanged, plus the `drifted_missing` flag
(`PLAN.md` §3's refresh mechanism, step 2), checked *before* the `"id"`
popping above, since a resource that's gone has no response to validate
in the first place. On success: for every key in
`driver.NON_DIFFABLE_FIELDS` (`specs/driver.md`) absent from the fresh
`attrs_without_id` but present in `state_entry.attributes`, copies the
prior value forward into `attrs_without_id` before returning it —
`read()`'s inability to verify a write-only field (DigitalOcean's
`ssh_keys`, `specs/digitalocean_compute.md`) must not look like that
field silently reverting to unset on every refresh. This is the actual
fix for that gap; `planner.py`'s `diff_attributes()` (`specs/planner.md`)
has no special-casing for these fields at all — by the time
`current_attributes` reaches it, the carry-forward above has already
made it correct, so an ordinary diff does the right thing on its own: an
unchanged desired value diffs as unchanged, and a genuinely changed one
still diffs against the last-known value and surfaces as a real change.
(An earlier version of this fix excluded these fields from
`diff_attributes()` directly instead — reverted after `/code-review`
caught that it silently dropped genuine changes to the field, not just
spurious ones.) Returns `(attrs_without_id, False)`. Any
other exception from `driver.read()` is wrapped: `raise
DriverExecutionError(state_entry.provider, state_entry.resource_type,
"read", exc) from exc`.

### `refresh_state(*, state_path=state.DEFAULT_STATE_PATH) -> State`

`aiform plan refresh` (`PLAN.md` §7): **zero LLM calls, no `.aiform.md`
parsing, no plan** — for every entry in `state.load(state_path).resources`,
`load_driver()` + `config.resolve_credentials()` (translated to
`PlanBlockedError` on failure, same as judgment call 3) +
`refresh_resource()`, updating `attributes`/`last_refreshed_at` in
place. Both `load_driver()` and `config.resolve_credentials()` are
**cached** — per `(provider, resource_type)` and per `provider`
respectively — for the lifetime of one `refresh_state()` call, mirroring
`build_create_plan()`'s identical caching (judgment call 5): many tracked
resources sharing a driver or a provider's credentials must not re-import
the driver module or re-read `.aiform/credentials.env` once per resource.
Unlike `apply_plan()`'s per-resource write (see Behavior below), this
saves **once**, after every tracked resource has been refreshed —
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
`discover_files(paths, cwd=cwd)`.

The steps below are the contract; since &#35;198 they are *located* across
`build_create_plan()` itself (state load, discovery, the loop, the trailing
save) and a set of module-private helpers it delegates each per-file stage to
— `_plan_delete_marked()`, `_plan_one()`, and `_plan_one()`'s own callees
`_parsed_resource()`, `_driver_for()`, `_credentials_for()` and
`_decide_action()`, plus `_warnings_for_uncovered()` for the trailing
"tracked in state but has no `.aiform.md` this run" step. That factoring is
not part of the contract and this spec does not track it per helper (private
structure is deliberately out of scope here — see `specs/README.md`'s
"Functions/classes **exposed**"); the note exists only so a reader looking for
a step knows it is one call away rather than missing:

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
  `apply_plan()` instead, per judgment call 4 (`driver_info_for()` is
  skipped for a destroy either way, since a resource being removed from
  state has no `driver` field left to record).
- **Otherwise** (normal file):
  1. `content = path.read_text(encoding="utf-8-sig")`, `spec =
     parser.parse_frontmatter(content)` — a first, frontmatter-only pass
     purely to compute `key = resource_key(...)` before the hash lookup
     below.
  2. `state_entry = state.resources.get(key)`,
     `previous_hash = state_entry.aiform_md_sha256 if state_entry else None`.
  3. **`state_entry is None`** (brand-new resource): build a
     `ParsedResource` directly from the content and frontmatter already
     read in step 1 — `intent_notes=[]`,
     `aiform_md_sha256=parser.compute_sha256(content)` — and skip
     `parser.parse_file()` entirely (#140). **Otherwise**: `parsed =
     parser.parse_file(path, previous_aiform_md_sha256=previous_hash,
     client=client, llm_config=llm_config)`. `parse_file()` re-reads and
     re-parses the same content internally
     (`specs/parser.md`'s own "independent, idempotent functions"
     design) — a deliberate small redundancy on the tracked branch, not
     a bug, since `parse_file()`'s fixed interface requires the previous
     hash as an *input*, and that hash can't be looked up without
     already knowing which state entry this file addresses. This branch
     also covers a drifted-missing resource (state tracked, but gone
     from the CSP): `state_entry` is not `None` there, so `parse_file()`
     still runs, and still spends an `intent_orchestration_call` when
     the hash moved *and* the Intent section is non-empty (judgment
     call 4, `specs/parser.md`) — the `intent_notes` it produces just go
     unread, same as they always have for that case (see step 7's
     caveat below).
  4. Driver resolution, **cached per `(provider, resource_type)` for the
     lifetime of this call** (judgment call 5): `driver =
     load_driver(spec.provider, spec.resource)`; `driver_info =
     driver_info_for(spec.provider, spec.resource, state)`.
  5. Credentials, **cached per `provider`**: `credentials =
     config.resolve_credentials(spec.provider)`, `RuntimeError` caught
     and re-raised as `PlanBlockedError(str(exc))` (judgment call 3).
  6. Refresh: `state_entry is not None` → `current_attributes,
     drifted_missing = refresh_resource(driver, state_entry,
     credentials)`, and `state_entry.attributes`/`last_refreshed_at` are
     updated in place on the in-memory `state` object (§3's "written
     back... even during a bare plan with no changes"). `state_entry is
     None` (brand-new resource) → nothing to refresh, and no
     `current_attributes` is built at all: step 7 takes the
     `create_entry()` path, which needs no diff. Only
     `drifted_missing = False` is bound, so step 8's cross-check has the
     name available.
  7. **If `state_entry is None`, or the refresh reports
     `drifted_missing`**: `entry = planner.create_entry(key,
     rationale=...)`, and **no categorization call is made**. Both are
     `create` as a matter of this module's own records — for an
     untracked resource no diff is built at all (step 6), and a
     drifted-missing one must be recreated whatever its diff says,
     which `prompts/diff_plan.md` already stated as a forced answer. See
     `specs/planner.md`'s `create_entry()` and issue &#35;117.

     The claim used to need a caveat here, and no longer does.
     `parser.parse_file()` also runs `extract_intent_notes()` whenever
     the file's hash changed and its Intent section is non-empty — which
     for an untracked resource is always, since `previous_hash` is
     `None` — and those `intent_notes` are consumed only by
     `plan_resource()`, which these two branches skip. So the call was
     bought and discarded on every `plan create` until an `apply`
     persisted the hash, and it is what made `PLAN.md` §9's "a first
     `plan create` makes zero Anthropic calls" false (#125).

     Decided and removed (#140): when `state_entry is None` this loop
     does not call `parse_file()` at all (step 3 above). It builds a
     `ParsedResource` from the content it has already read and the
     frontmatter it has already parsed, which drops a redundant second
     read of the same file along with the call. `parse_file()` remains
     the tracked branch's path, where the notes are genuinely consumed.
     So the claim is now the plain one for a brand-new resource: **no
     model call at all** when `state_entry is None`. The drifted-missing
     half of this branch is not the same claim: there `state_entry` is
     not `None`, so step 3 still takes the `parse_file()` path and still
     spends the call when the hash moved *and* the Intent section is
     non-empty (judgment call 4, `specs/parser.md`), discarding
     `intent_notes` the same way it always has — a pre-existing cost
     #140 did not touch, because a drifted-missing resource is rare
     enough that removing it is its own decision, not a side effect of
     this one.

     **Otherwise**: `entry, params_agree = planner.plan_resource(key,
     current_attributes, spec.params, intent_notes=parsed.intent_notes,
     param_schema=driver.PARAM_SCHEMA,
     likely_replace_fields=driver.LIKELY_REPLACE_FIELDS,
     state_aiform_md_sha256=previous_hash,
     current_aiform_md_sha256=parsed.aiform_md_sha256,
     drifted_missing=drifted_missing, client=client, llm_config=llm_config)`.
     `current_attributes` already reflects step 6's `NON_DIFFABLE_FIELDS`
     carry-forward — `plan_resource()` itself needs no awareness of that
     mechanism at all.
  8. **Structural cross-check** (judgment call 6): `entry.action ==
     PlanAction.UPDATE and state_entry is None`, or `entry.action ==
     PlanAction.CREATE and state_entry is not None and not
     drifted_missing`, raises `PlanBlockedError` naming `key` and the
     mismatch — a categorization response that disagrees with this
     module's own ground truth about whether the resource is already
     tracked is never executed, no matter how it was produced.

     Since step 7, the **first** of those two conditions is unreachable
     by construction: an untracked resource is never categorized, so no
     model answer exists to disagree. It is deliberately retained rather
     than deleted — it costs nothing and states the invariant plainly.
     It is **not**, however, what would catch a regression here:
     unreachable code cannot fail a test. The call-count assertions in
     `tests/test_orchestrator.py` are what actually guard the branch.
     The second condition remains live and reachable: the model can
     still answer `create` for a resource that *is* tracked and present.

     A `CREATE` with `state_entry is not None` **and**
     `drifted_missing` is never blocked, and since step 7 that
     combination no longer arrives from the model at all — it is what
     `planner.create_entry()` itself produces for a drifted-missing
     resource, on a state entry that is still tracked (and still
     stale). The `not drifted_missing` exemption is therefore still
     load-bearing, but for a different reason than it was: it now
     admits this module's *own* deterministic `CREATE` rather than a
     model's categorization of the recreate path.
     `NO_OP`/`DESTROY` (the latter never actually
     returned by `plan_resource()`, per `specs/planner.md`) need no
     check here: neither can contradict this module's records about
     whether the resource is tracked, and `plan_resource()` cannot return
     `DESTROY` at all.

     **Corrected:** this paragraph used to say `NO_OP` "is only ever
     returned when the no-op short-circuit already confirmed
     `current_attributes`/`desired_params` agree." That is false, and
     `prompts/diff_plan.md` says so directly — the model may answer
     `no-op` for a diff that is "cosmetically different but semantically
     identical", so a `NO_OP` can arrive from `categorize_diff()` with a
     **non-empty** diff. The conclusion above survives (a `NO_OP` still
     needs no tracked-vs-untracked cross-check), but the reason did not,
     and step 9 below depends on not believing it.
  9. **Record the file hash on an empty-diff `NO_OP`** (issue &#35;195):
     when `entry.action == PlanAction.NO_OP`, `state_entry is not None`,
     **and** `plan_resource()` reported `params_agree`,
     `state_entry.aiform_md_sha256 = parsed.aiform_md_sha256`.

     This is the sha half of the zero-call guarantee, and without it the
     guarantee held only for files nobody ever edited. A change to a
     tracked file that is *not* a `params` value — reworded Intent prose,
     a new frontmatter key — moves `parser.compute_sha256()`'s whole-file
     digest, so `plan_resource()`'s short-circuit (`specs/planner.md`,
     three conjuncts) fails on the sha comparison even though the diff is
     empty, and the plan pays for an `extract_intent_notes()` call (when
     the Intent section is non-empty) plus a `categorize_diff()` call
     whose expected answer is `no-op`. Nothing then recorded the new
     digest: `apply_plan()` skips `NO_OP` before any state write, and its
     only writes to `aiform_md_sha256` are `_new_state_entry()` and the
     `UPDATE` branch. So the resource re-paid that toll on **every**
     subsequent plan, indefinitely — a standing violation of `CLAUDE.md`'s
     zero-calls-on-unchanged-input rule rather than a one-time cost.

     Writing it here rather than in `apply_plan()` is deliberate: the toll
     is spent by `plan`, so `plan` is what must clear it. A user who edits
     prose and re-plans without ever applying is exactly the case that was
     broken, and routing the fix through `apply` would leave it broken.

     **`params_agree` is the load-bearing condition, and the action alone
     is not sufficient.** Per the correction under step 8, a `NO_OP` may
     arrive from `categorize_diff()` over a non-empty diff. Recording the
     hash on *that* path is actively harmful rather than merely useless:
     the diff stays non-empty, so every later run still fails the
     `not diff` conjunct and still spends the categorization call — but
     `parser.parse_file()` would now see a matching hash, skip
     `extract_intent_notes()`, and hand that call `intent_notes=[]`
     permanently. The user's Intent guidance would be silently dropped
     from every subsequent categorization, and since
     `prompts/diff_plan.md` instructs the model to honor those notes over
     its own judgment, the answer can flip from `no-op` to
     `update`/`likely_replace` — so `plan create` and `apply`'s re-plan
     could disagree on identical inputs. Caught in review of this change's
     first cut, which gated on the action only.

     Note what this means about the fix's reach: where the deterministic
     short-circuit fires, the two hashes are already equal and the write
     is a no-op, so **every** case this step actually changes is a
     model-returned `NO_OP`. The `params_agree` half is what separates the
     two kinds of those.

     It cannot mask a real change, for a reason independent of the above:
     `diff_attributes()` recomputes against freshly-`read()` attributes on
     every run, so a resource whose `params` genuinely differ produces a
     diff and falls through regardless of what the sha says. The hash is a
     cheap pre-filter, never the authority on whether work is needed.
     Guarded on `state_entry is not None` because an untracked resource is
     never `NO_OP` (step 7 plans it `CREATE`) and has no entry to write to.

     **Known limitation, inherited not introduced:** the write is
     persisted by the single trailing `state.save()` below, so if a *later*
     file in the same run raises (`PlanBlockedError` from step 8, a
     `DriverExecutionError` from a refresh), this resource's recorded hash
     is discarded along with every other in-memory change from the run, and
     the toll is paid again next time. That is the pre-existing cost of the
     batched write, which the attribute refresh already had; making it
     per-resource would trade it for partial-write semantics this function
     deliberately does not have.
  10. `PlannedResource(entry=entry, provider=spec.provider,
     resource_type=spec.resource, name=spec.name,
     desired_params=spec.params, aiform_md_path=path,
     current_aiform_md_sha256=parsed.aiform_md_sha256, driver=driver,
     driver_info=driver_info, credentials=credentials,
     state_entry=state_entry)`.

After every file: `state.save(state, state_path)` — once, matching
`refresh_state()`'s "no destructive side effects to protect, batch the
write" reasoning above (`build_create_plan()` never creates, updates, or
destroys anything itself; it only refreshes cached attributes,
`last_refreshed_at`, and — per step 9 — a no-op resource's
`aiform_md_sha256`). Returns
`(planned, warnings)`; `warnings` is populated **only** when `paths` was
falsy (default, discover-all mode): every `state.resources` key not
covered by any `PlannedResource` built this run (including ones targeted
for destroy) is reported as a warning string naming the resource, per
`PLAN.md` §5's "left alone... reported with a warning" rule for the
no-argument invocation. When explicit `paths` were given, `warnings` is
always `[]` — a tracked resource simply not named is expected scoping,
not an anomaly (same section).

### `build_destroy_plan(paths=None, *, state_path=..., force=False) -> tuple[list[PlannedResource], list[str]]`

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

**`force` and the `(planned, warnings)` return.** Both private producers,
`_build_destroy_plan_from_paths()` and `_build_destroy_plan_from_state()`,
now classify every `depends_on` target via `_classify_destroy_edges()`
before ordering: a target inside the producer's own node set becomes an
edge, a target resolvable elsewhere (`st.resources`, for the file-driven
producer only — the state-driven producer's node set *is* `st.resources`,
so it has no "elsewhere") is dropped silently, and anything else is
dangling. `_resolve_dangling_targets()` raises `PlanBlockedError` naming
every dangling pair when `force` is false, or returns one warning string
per dropped pair when `force` is true. This is why `build_destroy_plan()`
had to widen its return type to match `build_create_plan()`'s
`(planned, warnings)` shape rather than keeping `list[PlannedResource]` —
`cli.py` needed a channel for those warnings that wasn't there before
(previously hardcoded to `[]` at the one call site). See
`specs/resource_dependencies.md`'s "Dangling dependency targets on a
destroy path, and `--force`" for the full rule.

### `build_plan_summary(planned) -> str`

`json.dumps([{"resource_key": pr.entry.resource_key, "action":
pr.entry.action.value, "rationale": pr.entry.rationale, "likely_replace":
pr.entry.likely_replace} for pr in planned])` — the `plan_summary` string
`llm.review_plan()` (`PLAN.md` §5 apply step 2) takes as its sole
argument.

### `apply_plan(planned, *, state_path=..., yes=False, confirm=None, on_review=None, client=None, llm_config=None) -> ApplyResult`

`PLAN.md` §5 "aiform plan apply" steps 2-4 (step 1, re-running `plan` in
full, is the caller's job — see Behavior below), shared verbatim by
`aiform plan destroy`'s "plans and applies in one pass."
`state = state.load(state_path)` fresh at the start.

As with `build_create_plan()` above, since &#35;198 the steps below are located
across `apply_plan()` and private helpers — `_batch_plan_review()`,
`_apply_create()`, `_replace_review()`, `_replace_resource()`,
`_record_update()`, `_apply_destroy()`, and `_extend_and_notify()` shared by the
two review paths. **What deliberately did not move** is the UPDATE arm's
`try`/`except DriverUpdateNotSupported`/`except Exception` skeleton and both
abort returns. The two handlers are siblings, so the delete/create calls made
from inside the first are not covered by the second (see the Logging bullet in
`## Behavior`); flattening them would silently relabel those failures
`"update"`. The abort returns stay because a helper cannot return `ApplyResult`
for its caller.

1. **Gate #2, conditionally**: `needs_review = any(pr.entry.action ==
   PlanAction.DESTROY or (pr.entry.action == PlanAction.UPDATE and
   pr.entry.likely_replace) for pr in planned)`. If true:
   `review = llm.review_plan(build_plan_summary(planned), client=client,
   llm_config=llm_config)`. Any `flag.severity == PlanReviewSeverity.BLOCK`
   → raise `PlanBlockedError` naming every blocking flag, **unconditionally**
   — `yes=True` never bypasses this (`PLAN.md`: "cannot be bypassed by
   `--yes`"). **Also raises** (same unconditional treatment) when
   `review.safe_to_proceed is False` even with no `block`-severity flag
   attached — a schema-compliant `PLAN_REVIEW_SCHEMA` response can
   legitimately set `safe_to_proceed: false` without a matching `block`
   flag naming why, and `prompts/review_plan.md` instructing the model
   not to do that is advisory, not structural; treating "no block flag"
   alone as a pass would silently execute a plan the model explicitly
   flagged unsafe. Mirrors `specs/driver_gen.md`'s identical stance on
   `DriverReview.approved` vs. `blocking_issues`. Non-blocking flags are
   carried into the final `ApplyResult.review_flags` (the machine-readable
   record of every review this call made), and, before moving on to
   confirmation, this review's own non-blocking flags — not the
   accumulator, just what this call just produced — are handed to
   `(on_review or a no-op)(...)` (issue #166, judgment call 9), the
   display path — **unconditionally, including when `yes=True`**, since
   `--yes` only skips the prompt in step 2, not the record of what gate #2
   said. If `needs_review` is false, gate #2 is never called at all
   (`PLAN.md` §9 walkthrough step 3) — `review_flags` stays `[]` and
   `on_review` is not called.
2. **Confirmation**, unless `yes=True`: `(confirm or default_confirm)(prompt_text)`.
   `False` → return `ApplyResult(executed=[], review_flags=<from step 1>,
   aborted=True)` immediately, nothing executed, state untouched.
   `default_confirm` reads a `y`/`n` answer via `input()`, injectable like
   `client`/`llm_config` elsewhere in this codebase. Prompt: `{prompt}
   (y/n): ` — no implied default. It re-prompts until the stripped,
   lowercased answer is exactly `y` or `n`; a blank line or anything else
   never counts as a decision (#182). Before each attempt it flushes the
   terminal's input queue (`termios.tcflush(sys.stdin, TCIFLUSH)`,
   best-effort), so a keystroke typed during gate #2's tens-of-seconds
   review call — or while answering wrong the first time — is never
   mistaken for the answer to a prompt the user hasn't seen yet (#163,
   #182). A non-tty stdin has nothing to flush, and a failed flush never
   blocks the confirmation itself.
   This only ever loops against a real TTY: `aiform/cli.py`'s `_confirm`
   raises `RuntimeError` before calling `default_confirm` at all when
   `sys.stdin` isn't one (see "Confirmation and non-interactive runs" in
   `specs/cli.md`). Called directly against exhausted stdin, it raises
   `EOFError` from `input()` instead of looping forever or picking a
   default — left to propagate, deliberately.
   `termios` is POSIX-only, so importing this module — and therefore
   `aiform.cli` — now requires a POSIX platform. That is a deliberate
   narrowing, not an oversight: macOS and Linux are the only platforms
   this project is developed, CI'd or live-tested on, and guarding the
   import would be branch logic for a platform nothing else here
   supports. Windows support, if it is ever wanted, is its own change
   and this is one of the things it has to handle.
3. **Execute**, in `planned`'s given order (`PLAN.md`: "trivial for
   MVP's single-resource-per-file model"):
   - `NO_OP` → skip; nothing to persist (`build_create_plan()` already
     persisted its refreshed attributes, and — since issue &#35;195 — its
     `aiform_md_sha256`, which is why that write lives in the planning
     pass and not here).
   - `CREATE` → `raw = pr.driver.create(pr.name, pr.desired_params,
     pr.credentials)`. `create()`'s contract gained a `name` parameter,
     passed positionally first (`aiform/driver.py`, `PLAN.md` §4), after
     the curated compute driver turned out to have been reading it out
     of `params` instead — which `params` never actually contains
     (`specs/driver.md`'s flagged discrepancy). Raw driver exceptions are
     wrapped in `DriverExecutionError`, operation
     `"create"`; `id, attrs = raw.pop("id"), raw` (judgment call 1) — a
     driver response missing `"id"` entirely is *also* a driver-contract
     violation, wrapped in the same `DriverExecutionError` (operation
     unchanged) rather than left as a raw `KeyError`, consistent with
     every other way a driver can misbehave in this loop. This is the
     first of two places that build a fresh `StateEntry` from a
     `PlannedResource` plus a driver's just-returned `id`/`attrs` (the
     other is `UPDATE`'s replace path, below) — both go through one
     shared private constructor rather than duplicating the same ten
     keyword arguments twice: `StateEntry(provider=pr.provider,
     resource_type=pr.resource_type, name=pr.name, id=id, attributes=attrs,
     driver=pr.driver_info, last_applied_at=last_refreshed_at=<now>,
     aiform_md_path=str(pr.aiform_md_path),
     aiform_md_sha256=pr.current_aiform_md_sha256)`, written into
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
       Block flags halt the same as step 1's batch review. Non-blocking
       flags are handed to `(on_review or a no-op)(<this review's flags
       only>)` before confirmation, same as step 1-2, and — like that
       call — never skipped regardless of `yes`. **Unlike** step 1-2's
       confirmation, this one is never skipped by `yes=True`
       (judgment call 7) — `confirm(...)` is always called, and a decline
       here ends the loop the same way a top-level decline does (see
       Edge cases below for what `ApplyResult` reports in that case).
       Either way (already covered by the batch review, or freshly
       re-reviewed and confirmed here): `pr.driver.delete(pr.state_entry.id,
       pr.credentials)`, both wrapped in `DriverExecutionError` (operation
       `"delete"`) like every other driver call in this loop. **On
       success, the old entry is removed from state and saved
       immediately** — a resource key present in `planned` but no longer
       found in the *freshly-loaded* `state` (this function's own
       `state.load(state_path)` at its start, not necessarily the same
       state `planned` was built against — see Behavior) raises
       `PlanBlockedError` naming the mismatch rather than a raw `KeyError`,
       then `del state.resources[pr.entry.resource_key]` then
       `state.save(state, state_path)` — *before* attempting `create()`,
       not after: the old resource is now verifiably gone on the CSP
       side, and state must reflect that even if `create()` itself then
       fails, rather than continuing to claim the old (now-nonexistent)
       `id`/`attributes` until a future refresh happens to notice via
       `drifted_missing`. Then `raw = pr.driver.create(pr.name,
       pr.desired_params, pr.credentials)` (operation `"create"`), same
       wrapping.
     - No exception: `raw` is the updated attributes directly, no
       replace.
     - Either path: `id, attrs = raw.pop("id"), raw`. On a replace, a
       **new** `StateEntry` is written to `state.resources[pr.entry.resource_key]`
       via the same shared constructor `CREATE` uses (`id`, `attributes`,
       `driver=pr.driver_info`, `last_applied_at=last_refreshed_at=<now>`,
       `aiform_md_path`, `aiform_md_sha256=pr.current_aiform_md_sha256`).
       On a plain in-place update, the existing `StateEntry` — looked up
       the same guarded way as the replace path's removal above, raising
       `PlanBlockedError` rather than a raw `KeyError` if it's no longer
       present in the freshly-loaded state — is updated in place: `id`,
       `attributes`, `driver=pr.driver_info`,
       `last_applied_at=last_refreshed_at=<now>`,
       `aiform_md_sha256=pr.current_aiform_md_sha256` (all fields
       overwritten unconditionally rather than branched on whether they
       actually changed, simpler and idempotent either way;
       `last_refreshed_at` is included here too — the attributes just
       returned by a successful `update()` are exactly as fresh as a
       `read()`'s would be, so there's no reason to leave the plan-time
       refresh's older timestamp in place).
     - **The `PlanEntry` appended to `executed` (see step 4) reflects
       what actually happened, not the plan-time prediction, in both
       directions**: either way it's `pr.entry.model_copy(update={"likely_replace": replaced})`
       — `replaced` (this branch's own local, `True` on a
       `DriverUpdateNotSupported` fallback, `False` otherwise) is the
       single source of truth for this field on the returned entry,
       regardless of what the plan-time categorization predicted. On a
       replace, this corrects a `likely_replace: False` prediction that
       only became a replace because `update()` raised
       `DriverUpdateNotSupported`. On a plain in-place update, this
       equally corrects a `likely_replace: True` prediction that
       `update()` turned out to handle without raising — a resource
       whose plan-time categorization flagged it as a likely replace but
       whose `update()` succeeded in place must not be reported to the
       caller (`cli.py`) as having been replaced just because the
       prediction said so. `pr.entry` itself is never mutated in either
       case; this is a copy built solely for the returned result.
   - `DESTROY` → if `pr.state_entry is not None`: `driver =
     load_driver(pr.provider, pr.resource_type)`, `credentials =
     config.resolve_credentials(pr.provider)` (`RuntimeError` →
     `PlanBlockedError`, same as judgment call 3) — **no `driver_info_for()`
     call** (judgment call 4). `driver.delete(pr.state_entry.id, credentials)`
     (wrapped in `DriverExecutionError`, operation `"delete"`, on raw
     failure — per "Verification," the file is **not** moved to trash if
     this raises). On success: same guarded removal as `UPDATE`'s replace
     path — `PlanBlockedError` naming the resource if it's no longer
     present in the freshly-loaded state, otherwise
     `del state.resources[pr.entry.resource_key]`.
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
     A replace's mid-flight checkpoint (the state-removal-then-save right
     after `delete()` succeeds, described above) is an *additional* save
     within that one entry's processing, not a substitute for this one —
     a replace that completes successfully still gets this final save too,
     once the new `StateEntry` is written.
4. Returns `ApplyResult(executed=<one entry per non-NO_OP `pr` in
   `planned`, in order — `pr.entry` unchanged except on an actual replace,
   where it's the `likely_replace: True`-corrected copy described above>,
   review_flags=<accumulated non-blocking flags>, aborted=False)`.

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
- **Logging** (`specs/log.md`). The `provider=... resource_type=...
  operation=... duration_ms=... outcome=success|error` shape — INFO on
  success, ERROR on failure — is centralized in one helper,
  `_log_driver_outcome(provider, resource_type, operation, duration_ms,
  *, outcome)`, rather than written out at each call site. `_call_driver()`
  (its existing callers — `create`/`delete`, the only two operations
  that actually route through it) calls it once on success and once
  in its `except Exception` before re-wrapping into
  `DriverExecutionError` and re-raising. **`refresh_resource()`'s
  `driver.read(...)` call and `apply_plan()`'s `update()` branch do
  *not* go through `_call_driver()`** — each has its own inline
  `try`/`except` (the `update()` branch specifically needs
  `DriverUpdateNotSupported` to propagate unwrapped, which
  `_call_driver()`'s blanket `DriverExecutionError` wrapping would
  swallow) — but both call the *same* `_log_driver_outcome()` helper
  at their own site instead of hand-copying the dict-literal shape:
  `refresh_resource()` logs nothing on the ordinary success path
  (would be redundant with a direct call) but a **WARNING** (not
  through the shared helper — a different shape entirely, see below)
  specifically when it returns `drifted_missing=True` — a resource
  vanished from the CSP side, genuinely new information — and calls
  `_log_driver_outcome(..., operation="read", outcome="error")` on its
  own `except Exception` — a genuine driver failure during `read()` (a
  transient CSP auth or network error, say) that isn't
  `ResourceNotFoundError` — immediately before re-wrapping into
  `DriverExecutionError` and re-raising; without this, that failure
  left zero trace in `.aiform/logs/`, undermining the file sink's whole
  non-interactive/CI diagnosis purpose for exactly the path most likely
  to need it. `apply_plan()`'s `update()` branch calls
  `_log_driver_outcome(..., operation="update", outcome="success")` on
  success and logs nothing at the `DriverUpdateNotSupported` catch
  itself (an expected, handled fallback signal, not an error — the
  delete+create that follows produces its own two
  `_call_driver()`-driven lines). A third outcome — the `except
  Exception` handler, which covers **only** the `pr.driver.update()` call
  and is reached when it raises anything other than
  `DriverUpdateNotSupported` — calls
  `_log_driver_outcome(..., operation="update", outcome="error")`
  before re-raising (matching the `operation="update"` label the
  existing `DriverExecutionError(..., "update", exc)` it raises already
  used). Caught during `/code-review`, twice, on two separate passes:
  the first pass of this logging only covered the success and
  `DriverUpdateNotSupported` outcomes and missed this one entirely,
  leaving a real driver failure on this path structurally invisible to
  a `grep operation=update outcome=error` the way `_call_driver()`'s
  own failures aren't; the second pass flagged that the fix for the
  first gap had been hand-copied into three separate call sites
  (`_call_driver()`'s two branches plus this one) instead of sharing
  one helper — exactly the kind of drift that let the first gap happen
  in the first place — which is what `_log_driver_outcome()` now
  prevents structurally rather than by vigilance.

  **That `except Exception` does not cover the replace path.** The
  `delete()`/`create()` calls `_replace_resource()` makes run inside the
  sibling `except DriverUpdateNotSupported` block, and Python never
  re-enters a sibling handler, so a failure there surfaces through
  `_call_driver()` as `operation="delete"` or `"create"` — never
  relabelled `"update"`. Corrected here (&#35;198): this bullet previously
  described the handler as "covering the whole `update()`-or-replace
  attempt", which contradicted its own next clause and would tell a
  maintainer it is safe to flatten the two handlers. It is not:
  `tests/test_orchestrator.py`'s
  `test_replace_create_failure_reports_create_not_update_as_the_operation`
  and its `delete` twin pin the labels, because the older
  `test_replace_removes_stale_state_entry_before_attempting_create`
  asserts only the exception *type* and stays green through a relabel.

  `driver_info_for()` logs whether the sha256 matched an existing state
  entry — `reused=true` on a hash-match, `reused=false` when a new
  `DriverInfo` had to be built (a first resolution, or a hand-edited
  driver). Neither branch makes an LLM call, so there is no `approved`
  field any more. `apply_plan()` logs the gate #2 plan-review outcome
  (`safe_to_proceed=<bool> flags_count=<n>`, WARNING when blocked)
  before `_raise_if_review_blocked()` runs. No call site in this module
  logs a raw `params`/`credentials`/`*args` dict — every field above is
  a named scalar, a count, or a boolean.

## Edge cases / errors

- A file that fails `parser.parse_frontmatter()`/`parse_file()`
  (malformed YAML, failed `ResourceSpec` validation) propagates
  `ValueError`/`pydantic.ValidationError` uncaught from
  `build_create_plan()`/`build_destroy_plan()` — a hand-edit error in one
  file is not caught and skipped in favor of processing the rest; the
  whole `plan create` run fails loudly, consistent with every other
  "let it fail loudly" stance already established (`specs/parser.md`,
  `specs/planner.md`).
- `driver_info_for()`'s in-memory cache (judgment call 5) is scoped to a
  single `build_create_plan()` call only — it is not module-level,
  global, or shared with `apply_plan()`'s own lazy driver resolution for
  destroy targets. A destroy's driver lookup always goes through
  `load_driver()`/`config.resolve_credentials()` freshly, since
  `driver_info_for()` never runs on that path in the first place (nothing
  to cache, per judgment call 4).
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
- `move_to_trash()` itself (`shutil.move`) can raise a raw, unwrapped
  filesystem exception (e.g. `FileNotFoundError` if the source
  `.aiform.md` was removed or renamed out-of-band between `plan` and
  `apply`) — reached in `apply_plan()`'s `DESTROY` branch *after* the
  CSP-side `driver.delete()` and the state removal/save have both
  already durably committed. A resource in this state is correctly
  destroyed and correctly untracked — "verified" per `PLAN.md`'s own
  definition, which covers exactly those two things and nothing about
  trash archival — but the caller gets an uncaught exception instead of
  a clean `ApplyResult` for what is, substantively, a successful destroy
  whose purely cosmetic cleanup step failed. Deliberately not wrapped in
  a new exception type or given a recovery path here: this is a raw
  filesystem operation, not a driver call (`DriverExecutionError` doesn't
  fit) or a policy decision (`PlanBlockedError` doesn't either), and
  `state.save()`'s own filesystem writes are equally unwrapped elsewhere
  in this module — inventing a bespoke exception type for this one call
  site would be exactly the premature abstraction `CLAUDE.md` warns
  against for a case this narrow. Accepted as a known, low-probability
  edge case rather than designed around.
- `driver_info_for()` reads the driver file (`path.read_bytes()` for
  hashing) independently of `load_driver()`'s own read via `importlib`
  moments earlier — two reads of the same small file per driver
  resolution instead of one. Deliberately not consolidated: hashing must
  stay byte-exact (per its own Interface entry above, "which exact file
  produced this resource"), and `importlib`'s module loading doesn't
  expose the raw bytes it read in a form worth threading back out for
  this. Already bounded by judgment call 5's caching to at most once per
  `(provider, resource_type)` pair per `build_create_plan()`/
  `refresh_state()` call, not once per resource.

## Out of scope

- **All CLI argument parsing, output formatting/printing (the plan
  table, `--json`, error message formatting), and `--verbose`/`_redact()`
  logging** — `cli.py`'s concern, not this module's.
- **`aiform init`, `aiform plan show`, and everything under `aiform
  driver ...`** (`PLAN.md` §7) — `cli.py` (`init`/`show`) or mechanism 2's
  unbuilt `driver ...` command surface, neither this module's concern.
  `plan show` in particular needs no orchestrator involvement at all —
  it's a direct `state.load()` plus formatting, entirely in `cli.py`.
- **Driver generation of any kind** (`aiform/driver_gen.py`'s
  `generate_driver()`) — deliberately unreachable from `plan`/`apply`,
  permanently. `PLAN.md`'s "Driver curation" abandoned the mid-`plan`
  generation trigger rather than deferring it, so this is a design
  boundary, not a wiring task somebody should finish.
  `driver_info_for()` only ever hashes an existing on-disk file; when a
  driver is missing it never calls `driver_gen.generate_driver()`, it
  raises `PlanBlockedError` via `load_driver()` instead — and that is the
  permanent behavior.
- **Any review or trust decision over a driver's source at plan/apply
  time** (#119) — a hash mismatch simply produces a new `DriverInfo` with
  no gate of any kind. Deciding how (or whether) a driver's local
  imports should be validated before it runs belongs to the future driver
  repository/download design, not this module. See `driver_gen.md` for
  the one place `llm.review_driver()` still runs, at development time.
- **`PARAM_SCHEMA` shape validation** — judgment call 2.
- **Live credential validity checking** (an expired/malformed token
  detected before the CSP itself rejects a real call) — judgment call 3.
- **Cross-resource attribute references, automatic edge detection,
  orphan refusal, and parallel execution** — Phases 2, 3, 4 and 6 of
  `MULTI_RESOURCE_PRD.md`. Dependency *ordering* is no longer out of
  scope here: see the `resource_dependencies` addendum below. What
  remains true is that this module applies `planned` one resource at a
  time, in the literal order the list carries — it is the plan
  *builders* that now decide that order, and `apply_plan()` is unchanged
  and unaware of the graph.

## Addendum: `unordered_fields` (`specs/unordered_fields.md`)

`build_create_plan()` passes `unordered_fields=driver.UNORDERED_FIELDS` into
`planner.plan_resource()`, beside the existing
`likely_replace_fields=driver.LIKELY_REPLACE_FIELDS`. That is the whole of this
module's involvement -- it reads the declaration off the driver and forwards
it, exactly as it already does for the other per-field lists, and makes no
decision of its own about it. See `specs/unordered_fields.md`.

## Addendum: `resource_dependencies` (`specs/resource_dependencies.md`)

This module owns the ordering half of Phase 1. `specs/resource_dependencies.md`
is the full spec; what belongs here is which of this module's functions
changed and which deliberately did not.

- **`build_create_plan()`** gains a private discovery/validation pass ahead of
  its existing loop. The pass reads and parses each discovered file's
  frontmatter, then performs four checks in order -- duplicate resource key,
  per-target resolution (live files only), same-run destroy conflict, and
  topological ordering via `aiform/graph.py`. It makes **zero LLM calls, zero
  driver loads and zero credential resolutions**, which is the point of doing
  it first: a plan that is going to be refused must not first spend money and
  hit a provider's API.

  The loop then iterates the computed **order**, calling `_plan_one()` /
  `_plan_delete_marked()` unchanged -- re-deriving each record from its path
  rather than reusing the pass's. An earlier draft claimed the loop "iterates
  those records"; it does not.

  Counted honestly, a **tracked** file is now read three times: the discovery
  pass, the loop, and `parse_file()` inside the loop. Two other cases read
  twice, for two different reasons -- an **untracked** file because
  `_parsed_resource()` returns early without `parse_file()` when there is no
  state entry, and a **delete-marked** one because `_plan_delete_marked()` never
  goes through `_parsed_resource()` at all: it does its own `read_text()` plus
  `parse_frontmatter()` and returns. (Not because a delete-marked file lacks a
  state entry -- it normally has one, and that branch looks it up.) The third
  read is
  pre-existing and is the one PR #199 filed out of scope, because closing it
  means changing `parse_file()`'s interface; the discovery-pass read is what
  this phase adds, and closing *that* is a different job -- threading the
  pass's records into `_plan_one()` -- **not** a `parse_file()` change. Neither
  is fixed here.

  The cost is `read_text()` plus a pure-YAML parse, with no LLM call either
  way, so this is wasted IO rather than a spent toll. It does leave a narrow
  TOCTOU window: a file edited between two reads was validated and ordered on
  content that is not what gets planned. `specs/resource_dependencies.md`
  carries the same account -- keep the two in step.
- **`build_destroy_plan()`** orders **both** of its paths in reverse
  topological order -- the file-driven one from frontmatter, the state-driven
  destroy-all one from `StateEntry.depends_on`. The second matters more: it is
  the invocation a user actually types. The file-driven path additionally gained
  the duplicate-key check, which it previously lacked. Before this PR, two files
  declaring one key produced **two** plan entries rather than collapsing into
  one -- the failure was at apply time, where the second `_apply_destroy()`
  raised `PlanBlockedError` from `_require_tracked()`, the first having already
  dropped the key from state, so the apply aborted part-way and the second file
  stayed on disk to recreate the resource. See
  `specs/resource_dependencies.md` for the full mechanism.

  Two earlier drafts of this line were wrong in different ways, both recorded
  because the corrections are the useful part: the first described a silent
  collapse, which was the current code minus the check rather than the actual
  history; the second blamed the abort on the second delete hitting a stale id,
  which every shipped driver swallows as success (they treat a 404 on DELETE as
  "already gone"), so it never raises.
- **Both producers now classify every target instead of filtering
  silently.** They used to hand `depends_on` straight to
  `_reverse_topological()` with no restriction, relying on
  `graph.topological_order()` to drop anything outside its `keys` -- which
  it no longer does (`graph.UnknownDependencyError`, see the graph.py
  entry above). A target resolving nowhere -- not in the producer's own
  node set, and, for the file-driven producer, not in `st.resources`
  either -- blocks the plan with `PlanBlockedError` naming every dangling
  pair, unless `force=True`, which drops the edge and returns one warning
  per pair instead. Both producers exclude every dangling target from the
  edge sets *before* calling `_topological()`, so
  `graph.UnknownDependencyError` cannot reach `graph.topological_order()`
  from the orchestrator at all -- there is no remaining call site that
  passes it an unrestricted edge set. `_topological()` deliberately does
  **not** catch `graph.UnknownDependencyError`: unlike `CycleError`, which
  is genuinely user-reachable (a user's own files can declare a cycle,
  and `tests/test_orchestrator.py` exercises that conversion), an
  `UnknownDependencyError` surfacing here would mean an orchestrator
  caller failed to restrict its edges -- a bug in this module, not a bad
  `depends_on` declaration. Converting it into a `PlanBlockedError` phrased
  as "your dependency doesn't resolve" would misdirect a future reader
  investigating that bug toward the user's `.aiform.md` files instead of
  the actual defect; an uncaught exception's traceback names the real
  cause. Full rule in `specs/resource_dependencies.md`.
- **`PlannedResource.depends_on`** carries the declared list through to the
  CLI and into state, defaulted so every existing construction site and test
  helper keeps working.
- **`_new_state_entry()`** and **`_record_update()`**'s in-place branch persist
  it, so a destroy-all can order by it later.
- **`apply_plan()` is unchanged.** It applies the list in the order it is
  given and has no notion of a graph. Everything about ordering lives in the
  two plan builders.
- **`build_plan_summary()` is deliberately unchanged.** Adding `depends_on`
  would inject an unexplained key into gate #2's review prompt with no
  corresponding `prompts/review_plan.md` change and no test that the reviewer
  uses it. Deferred to Phase 4, where orphan reasoning needs it.

All five new failure modes -- the original four plus the destroy paths'
dangling-target refusal -- raise the existing `PlanBlockedError`; no new
exception type was added on the orchestrator side. `graph.CycleError` is
converted to `PlanBlockedError` in `_topological()`, since a cycle is
genuinely reachable from a user's own `depends_on` declarations.
`graph.UnknownDependencyError` is not caught anywhere in this module --
every call site restricts its edges to its own node set before calling
in, so the exception cannot actually reach `graph.topological_order()`
from here; if it ever did, that would mean an orchestrator caller has a
bug, not that a user's declaration is wrong, and an uncaught exception
naming the real cause is the correct outcome for that case, not a
`PlanBlockedError` phrased as a dependency problem.

## Addendum: `resource_references` (`specs/resource_references.md`)

Phase 2's value flow. `specs/resource_references.md` is the full spec; what
belongs here is which of this module's functions changed.

- **`referenceable(st)`** — new, and public rather than private because
  `observability.py` needs the same namespace: a tracked resource's
  `attributes` plus its `id`, merged back in because `_pop_id()` moved it to
  `StateEntry.id`. Keeping this State-aware adapter here is what lets
  `aiform/references.py` stay free of a `State` import.
- **`_dependency_targets(spec, key)`** — new. Unions reference-derived targets
  with declared `depends_on` ones *before* the per-target classification runs,
  so a reference adds targets to that table rather than rules to it. Declared
  order is preserved and reference-only targets appended sorted, so which
  target a `PlanBlockedError` names first is unchanged from Phase 1 and still
  deterministic.
- **`_resolve_dependency_edges()`** iterates `_dependency_targets()` instead of
  `spec.depends_on`. Every classification rule is Phase 1's, untouched.
- **`_plan_one()`** resolves params before `_decide_action()`, and stores three
  things on `PlannedResource`: `desired_params` (resolved as far as plan time
  could), `raw_params` (references intact), `unresolved_references`. It writes
  the **unioned** target list to `StateEntry.depends_on`, not just the declared
  one — otherwise `plan destroy` from state alone would tear a target down
  before the resource pointing at it, and no apply could repair it.
- **`_decide_action()`** gains an `unresolved` branch between the
  `drifted_missing` arm and `plan_resource()`. It fires in a narrower case than
  it first appears: a target that is tracked but drifted missing is **still in
  state**, so a reference to it resolves from stored attributes. Only a target
  absent from state entirely is unknown.
- **`build_create_plan()` threads a `volatile` set, and a `replaced` subset of
  it, through its loop.** A key joins `volatile` when
  `_will_get_new_attributes()` holds — any `CREATE` (including the recreate of a
  drifted resource, which is still sitting in `st.resources` with its old
  attributes) or any `UPDATE`. Not only `UPDATE` with `likely_replace`: that
  field is the model's advisory guess, and gating on it missed both an update
  `driver.update()` refuses at apply time and the middle of a dependency chain,
  whose own action is the deterministic `UPDATE` this mechanism produces. Those
  keys are passed to `references.resolve()` as `volatile` — present in the
  namespace, so their attribute names are still validated at plan time, but
  reported unresolved so the real value is read during the apply. `replaced`
  (action `CREATE`, per `_will_be_recreated()`) is the subset for which a
  currently-unset value is *not* refused, since a recreate is what supplies it. Without it, a dependent
  resolves to the doomed value, diffs clean, plans `NO_OP`, and is skipped by
  `apply_plan()` before the apply-time re-resolve can correct it — leaving a DNS
  record pointing at a host the same apply just destroyed. Accumulating the set
  in topological order is what makes it correct: every target is classified
  before any dependent of it is planned.
- **Both destroy producers union reference-derived edges.**
  `_build_destroy_plan_from_state()` gets it free from the persisted
  `StateEntry.depends_on`; `_build_destroy_plan_from_paths()` calls
  `_dependency_targets()` itself. Missing the second one let
  `plan destroy <files>` invert the order for a reference-only edge.
- **`_apply_params(pr, st)`** — new. Re-resolves the whole raw tree immediately
  before each `create`/`update` — the replace path reuses the value computed for
  the `update()` attempt that raised — rather than reusing plan time's answer, so the value handed to a driver is the one live at that moment —
  correct precisely when a target was replaced earlier in the same apply. It
  raises `PlanBlockedError` on a path still unresolved; topological ordering
  means that cannot happen, and the guard exists because the alternative to
  raising is sending a literal `${...}` to the provider.
- **`_replace_resource()`** takes the already-computed `desired` rather than
  re-resolving: the resource has just been dropped from state, and re-resolving
  would differ only if it referenced itself, which is a cycle and already
  refused.

Unchanged on purpose: `graph.py`, `build_plan_summary()` (adding references to
gate #2's prompt is Phase 4, same reasoning as `depends_on`), and every
`PlanBlockedError` reason Phase 1 defined.

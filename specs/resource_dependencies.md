# specs/resource_dependencies.md — declared resource dependencies (`aiform/graph.py`, + `models.py`/`orchestrator.py`/`cli.py`/`state.py`)

**Naming note**: like `specs/unordered_fields.md` and
`specs/resource_tagging.md`, this filename deliberately doesn't follow
`specs/README.md`'s per-module mirroring rule. The change is one feature spread
across four already-specced modules (`specs/models.md`, `specs/orchestrator.md`,
`specs/cli.md`, `specs/state.md`) plus one tiny new one. Named for the feature
so it's discoverable from any of them, with each cross-referencing it.

Closes #200. Phase 1 of `MULTI_RESOURCE_PRD.md`.

## Purpose

Let a user declare that one resource must exist before another, and have
`aiform` order the plan accordingly — dependencies created before dependents,
destroys in reverse, cycles refused at plan time.

## Use cases

Delivers two of `MULTI_RESOURCE_PRD.md`'s:

- **UC2 — manual dependency override.** A user declares a relationship
  `aiform` cannot infer. Phase 1 delivers the *declaration* half only; value
  flow (one resource reading another's attributes) is Phase 2, and *automatic*
  detection is Phase 3. Note the deliberate inversion in the PRD's phasing:
  UC2 ships before UC1, because explicit declaration is the foundation that
  auto-detection later populates.
- **UX1 — textual dependency display.** A `plan` that silently reorders
  resources without showing the graph it derived is not reviewable, and a user
  cannot correct a dependency they cannot see.

## The gap this closes

`orchestrator.discover_files()` returns `sorted(cwd.glob("*.aiform.md"))`, and
`build_create_plan()` walks that list straight through, so **execution order is
alphabetical filename order** and nothing can be told otherwise. With
`app.aiform.md` and `db.aiform.md` in one directory, `app` is created first
because `a` sorts before `d`. The user's only recourse is renaming files until
the alphabet agrees with their topology.

Destroy is worse: `aiform plan destroy` with no arguments — the common
invocation — iterates `state.resources` in dict order, tearing resources down
with no regard for what still points at them.

## Relationship to PLAN.md §10

§10's "No dependency graph" entry named this an explicit, undesigned gap, and
§486 called the one-file-one-resource model "the natural extension point for a
future graph, deliberately not built now". This is that extension point being
built; §10, §73 and §486 are updated rather than left claiming it doesn't
exist. What remains deferred there, now phase by phase: attribute references
(Phase 2), automatic detection (Phase 3), orphan refusal and partial-failure
recovery (Phase 4), concurrency-safe state (Phase 5), parallel execution
(Phase 6), graphical visualization (Phase 7).

## Scope: what a "deployment" is

A dependency graph exists **within one deployment**, and a deployment is the
directory `aiform` runs from. Three relative paths define that boundary, and
this feature does not widen any of them:

- `discover_files()` globs `cwd` **non-recursively** — subdirectories are never
  picked up.
- `state.DEFAULT_STATE_PATH` is the relative `.aiform/state.json`.
- `config.resolve_credentials()` falls back to the relative
  `.aiform/credentials.env`.

So two directories, each with its own `.aiform/state.json`, are two independent
deployments, and **a `depends_on` target in another deployment's state is not
resolvable** — it is simply an unknown key, and raises like any other. This is
correct rather than a limitation: the two deployments are separate invocations
with separate state, and there is no ordering `aiform` could enforce between
them. Cross-deployment orchestration is not a deferred item; it is not a thing
this model has.

A consequence, pre-existing and not changed here: resource names are unique
**per deployment, not globally**. Two directories may each declare
`digitalocean.compute.db-01`, and they are two different real droplets that
happen to share a name. The duplicate-key check below is likewise per-run.

**Deferred, filed as #201:** that isolation is entirely positional. Nothing
records which deployment a state file belongs to, and nothing in `plan` output
says which one is about to be acted on — so `aiform plan destroy` with no file
arguments, run from the wrong directory, is indistinguishable from the run the
user intended until it has happened. Out of scope here by decision; this spec
only pins that dependency resolution never crosses the boundary.

## Interface

### `aiform/models.py`

```python
class ResourceSpec(BaseModel):
    ...
    depends_on: list[str] = Field(default_factory=list)


class StateEntry(BaseModel):
    ...
    depends_on: list[str] = Field(default_factory=list)


def parse_dependency_key(key: str) -> tuple[str, str, str]:
    """Split a fully-qualified key into (provider, resource_type, name).

    Raises ValueError if the key is malformed."""
```

Both fields default to empty, so every existing `.aiform.md` and every existing
`state.json` stays valid. A `field_validator` on `ResourceSpec.depends_on` runs
`parse_dependency_key()` over **every** element.

`parse_dependency_key()` lives here, next to the validator that needs it,
rather than in `graph.py` — putting it there would make `models` → `graph` →
`models` an import cycle waiting to happen.

### `aiform/graph.py` — new

```python
class CycleError(Exception):
    """Carries `path: list[str]`, the cycle as a walkable sequence."""


def topological_order(keys: set[str], edges: dict[str, set[str]]) -> list[str]:
    """Kahn's algorithm. `edges[k]` is the set of keys `k` depends on.

    Raises CycleError when the graph is not acyclic."""
```

Pure: no I/O, no LLM, no import of `models`, `llm` or `anthropic`. Same shape
as `aiform/compare.py`.

### `aiform/orchestrator.py`

```python
@dataclass
class PlannedResource:
    ...
    depends_on: list[str] = field(default_factory=list)
```

Plus a private discovery/validation pass in front of `build_create_plan()`'s
existing loop. `build_create_plan()` and `build_destroy_plan()` keep their
signatures; only the **order** of the list they return changes.

### `aiform/cli.py`

`_print_plan()` emits one extra indented line per resource with dependencies;
`_plan_to_json()` gains a `depends_on` key per plan entry. Neither signature
changes.

## Behavior

### Declaration syntax

An optional frontmatter key holding **any number** of fully-qualified resource
keys, in the existing `orchestrator.resource_key()` address format
(`provider.resource_type.name`) — which is also the state key format, so the
system has one address syntax rather than two:

```yaml
---
resource: compute
name: app-01
provider: digitalocean
depends_on:
  - digitalocean.compute.db-01
  - digitalocean.compute.cache-01
params:
  region: sfo3
---
```

- **Fully-qualified keys only, no shorthand.** Shorthand resolution is inferred
  cleverness with ambiguity failure modes.
- **Parsed with `split(".", 2)`.** `provider` and `resource_type` match
  `RESOURCE_OR_PROVIDER_PATTERN` (`models.py:10`) and cannot contain dots, but
  `name` is only `min_length=1` and may. A naive `split(".")` corrupts a dotted
  name — `digitalocean.domain.example.com` is a legitimate key.
- **Any number of targets.** Fan-in is a first-class case, not a later
  widening: the graph counts in-degree, the CLI renders every edge, state
  persists every edge, and the tests cover a resource with several
  dependencies.

### `aiform/graph.py`

- **Kahn's algorithm**, with the ready set drained in `sorted()` order.
  Determinism is a requirement, not an accident: identical input must always
  yield identical output, because both the unit tests and reviewable `plan`
  output depend on it.
- The result is a **total order**, not a set of independent batches. Two
  resources with no edge between them are still sequenced, deterministically.
  Phase 1 executes strictly sequentially; identifying what *could* run
  concurrently is Phase 6's problem, and nothing here anticipates it.
- `edges[k]` is a **set**, so a node with several dependencies is just an
  in-degree above one. Fan-in needs no special case in the algorithm.
- **No separate cycle detector**, but the leftover set needs care. An earlier
  draft of this spec said "after Kahn's terminates, the leftover set *is* the
  cycle." **That is false**, and it shipped a bug before being caught: the
  leftover set is the cycle(s) **plus every node that transitively depends on
  one**, because such a node's in-degree never reaches zero either. With
  `a → b`, `b → c`, `c → b`, all three survive Kahn's, yet `a` is not in the
  cycle and its only edge is legitimate.

  So the reported path is the walk **trimmed to its cycle**: walk dependency
  edges from `min(leftover)` until a node repeats, then return the suffix
  beginning at that node's first occurrence. The guarantee callers rely on is
  `path[0] == path[-1]`, which the untrimmed walk does not provide. Reporting
  a lead-in node would send a user hunting for a bad edge on a resource whose
  edges are all correct.

  A separate `find_cycle()` is still not needed — this is the same traversal,
  trimmed.
- A key in `edges` that is not in `keys` is ignored — the orchestrator
  restricts edges before calling, and `graph.py` does not second-guess it.

### The discovery/validation pass

A new pass in front of `build_create_plan()`'s existing loop. **Zero LLM calls,
zero driver loads, zero credential resolution** — it is YAML and string work
only. Per discovered file: read the text, one `parser.parse_frontmatter()`,
compute the key, note whether it is delete-marked. Then, in this order:

1. **Duplicate-key check.** Two files in one run declaring the same
   `provider.resource_type.name` raise `PlanBlockedError` naming both paths.
   This is undetected today — both get planned and both create — and a graph
   keyed by resource key would silently collapse them into one node with two
   records. The nastiest variant is a user copying `x.aiform.md` to
   `AIFORM-DELETE-x.aiform.md` and leaving the original: same key, one live and
   one destroy.
2. **Resolution, for live files only, per target.** A target resolves if it
   names a file in this run or a key in this deployment's state. Anything else
   raises `PlanBlockedError` naming **that target** and the declaring
   resource — so a resource with several dependencies reports the one that is
   actually wrong, not the whole list. **Delete-marked files are exempt from
   resolution errors**; otherwise a resource whose dependency was already
   removed from state becomes permanently undestroyable.
3. **Same-run destroy conflict.** A *live* file whose `depends_on` names a key
   that is **delete-marked in the same run** raises `PlanBlockedError`. Keyed
   on **file kind, not plan action** — keying it on the action would fire only
   after the loop had already spent the LLM calls and driver reads,
   contradicting the whole point of doing this first, and would let a NO_OP
   dependent through silently while blocking an UPDATE one.
4. **Ordering.** `graph.topological_order()` over the live keys, with edges
   restricted to keys in this run. `CycleError` becomes `PlanBlockedError`
   carrying the path, rendered ASCII (`a -> b -> c -> a`) — plan output is
   terminal text, and this is the one string in it a user may paste into an
   issue.

The existing loop then iterates those records **in the computed order**,
calling `_plan_one()` / `_plan_delete_marked()` unchanged.

### Which targets contribute an edge

**The generating principle, from which the whole table follows:** the run is
sorted as **two separate node sets** — the live keys, ordered topologically,
and the delete-marked keys, ordered reverse-topologically — with edges
restricted to keys *within* each set. So an edge exists only when the
declaring file and its target are in the **same** group. A target outside the
declarer's group may still be perfectly *resolvable*; it just contributes no
ordering constraint.

Resolution and edges are both decided **per target**, not per resource, so one
resource may legally have a mix.

An earlier draft of this table omitted the declaring file's kind, which made
its first rows read as though they applied to any declarer — contradicting the
prose below it for one combination. Both columns are now explicit:

| Declaring file | Target is | Resolvable | Edge |
|---|---|---|---|
| live | a live file in this run | yes | **yes** |
| live | a live file in this run that turns out NO_OP | yes | **yes** |
| live | in state, not in this run | yes | no ¹ |
| live | delete-marked in this run | — | **rejected** (rule 3) |
| live | nowhere | **no — raises** | — |
| delete-marked | delete-marked in this run | yes | **yes** (orders the destroys) |
| delete-marked | a live file in this run | yes | no |
| delete-marked | in state, not in this run | yes | no |
| delete-marked | nowhere | yes (exempt, rule 2) | no |

- **In state but not in this run contributes no edge** — it already exists and
  nothing is happening to it. This is precisely what keeps
  `aiform plan create one-file.aiform.md` working when that file declares a
  dependency on something already deployed.

  **¹ This row has no observable consequence, and no test can prove it.** Said
  plainly rather than left as a claim a reader assumes is pinned: `_order_files`
  restricts the node set to the live keys, and `graph.topological_order()`
  ignores any target outside that set. So "resolved, no edge added" and "edge
  added to a key that isn't a node" are behaviorally identical. The row
  documents intent — that such a target is *resolvable* rather than an error,
  which is genuinely observable — not a distinction the code makes.
- **A NO_OP target keeps its edge.** Ordering it costs nothing, and dropping it
  would need information the pass does not have yet — the action isn't known
  until the loop runs, which is after ordering.
- **A delete-marked file depending on a live one is allowed and contributes no
  edge**, because the two are in different node sets. The ordering constraint
  is then satisfied by the returned order below, which puts every destroy after
  every live action. This is the mirror of rule 3 and is deliberately *not* an
  error.

### Returned order

Live entries in topological order, then delete-marked destroys in **reverse**
topological order.

**Destroys-last is a behavior change this phase makes, not a pre-existing
invariant.** An earlier draft of this spec said destroys "already" ran last;
they did not. `discover_files()` returns `sorted(cwd.glob(...))`, and
`AIFORM-DELETE-` sorts *before* any lowercase name (`'A'` is 65, `'a'` is 97),
so a delete-marked file was previously planned and applied **first**. The
change is deliberate and is the better order — destroying a resource before
its replacement exists opens a capacity gap that destroying afterwards does
not — but it is a change, and a reader comparing against `main` should not be
told otherwise. A test pins it.

Note the two claims here are independent, not mutually supporting: rule 3
guarantees no *live* resource depends on a same-run destroy, and the returned
order guarantees destroys follow live actions. An earlier draft justified each
by the other, which is circular; both are separately true of the ordering code.

### Destroy ordering, all three producers

Ordering only one of them would make the feature's central claim false, so all
three are covered:

- **`build_create_plan()`'s delete-marked branch** — reverse topological, per
  above.
- **`build_destroy_plan()`'s file-driven path** — files exist, so `depends_on`
  is readable from frontmatter; reverse topological.
- **`build_destroy_plan()`'s state-driven destroy-all path** — reads
  `StateEntry.depends_on` and orders in reverse topological. This is the
  invocation a user actually types (`aiform plan destroy`, no arguments), so
  leaving it unordered would mean the feature ordered only the invocation
  nobody uses.

### `StateEntry.depends_on`

Written by `_new_state_entry()` and by `_record_update()`'s in-place branch,
both from `PlannedResource.depends_on`.

**Known limitation, documented rather than fixed:** it records dependencies *as
of the last apply*. Editing `depends_on` and then destroying without applying
orders by the stale edges. That is the correct trade — state is a record of
what was built, and the alternative (reading files during a destroy that
explicitly ignores files) is worse.

### CLI output

`_print_plan()` gains **one** indented line per resource that has
dependencies, listing **all** of its targets comma-separated in declared
order — not one line per edge, which would bury the rationale line under a
fan-in. A dependency-free plan looks exactly as it does today; there is no
separate `Order:` footer, since the `depends on:` lines already convey it.

```
+ digitalocean.compute.db-01: create
    no state entry is tracked for this resource yet
+ digitalocean.compute.cache-01: create
    no state entry is tracked for this resource yet
+ digitalocean.compute.app-01: create
    no state entry is tracked for this resource yet
    depends on: digitalocean.compute.db-01, digitalocean.compute.cache-01
Plan: 3 to create, 0 to update, 0 to destroy, 0 no-op.
```

`_plan_to_json()` gains `"depends_on": [...]` per entry — the full list,
verbatim in declared order. The array order of `plan` itself **is** execution
order, documented rather than duplicated into a second key. Markers, colors and
`--no-color` are untouched.

### `build_plan_summary()` is deliberately not changed

Adding `depends_on` would inject an unexplained key into gate #2's review
prompt with no corresponding `prompts/review_plan.md` change and no test that
the reviewer uses it. Deferred to Phase 4, where orphan reasoning actually
needs it.

### Zero-Anthropic-call property

The validation pass is YAML and string work, and `graph.py` imports nothing
from `llm.py`, so `CLAUDE.md`'s "zero calls on unchanged input" rule is
untouched. The three existing guards — `planner.py`'s short-circuit, the
untracked-resource branch that skips `parser.parse_file()`, and `parser.py`'s
sha-match skip — are unmodified.

Cost of *adopting* `depends_on` on an already-tracked file, stated precisely:
the file's hash moves, costing **one** call if it has no `## Intent` prose,
**two** if it does — and then zero from the next run onward, because issue #195
(merged as PR #196) made `plan` itself persist the new sha on a no-op over an
empty diff. Before that fix it would have been one-or-two calls *forever*.

## Edge cases / errors

**No new exception type.** `PlanBlockedError(reason)` already means "this plan
cannot proceed" and already has CLI exit-code handling. It gains four reasons:
duplicate resource key, unresolvable target, live-depends-on-same-run-destroy,
and a cycle with its path. `graph.CycleError` is internal to the graph module
and never escapes the orchestrator.

- **Malformed `depends_on` shape** — a non-list, a non-string element, a key
  with too few segments, or a segment violating
  `RESOURCE_OR_PROVIDER_PATTERN` — is a Pydantic field validator on
  `ResourceSpec`, surfacing as `ValidationError` from `parse_frontmatter()`
  like every other frontmatter schema error. Validation runs over **every**
  element, not just the first.
- **A dotted resource name** (`digitalocean.domain.example.com`) round-trips,
  because of `split(".", 2)`. This is the case a naive `split(".")` corrupts.
- **Self-dependency** is a length-1 cycle. Not a special case — it falls out of
  Kahn's leftovers like any other cycle, and reports the same way.
- **Duplicate targets within one `depends_on` list** collapse to one edge,
  because `edges[k]` is a set. This is **not** an error, and the list is **not**
  rewritten: `depends_on` is stored in state and displayed verbatim as the user
  wrote it. Silently rewriting a user's declaration is worse than a harmless
  repeat, and erroring on it is a rule with no failure behind it.
- **An empty `depends_on: []`** is indistinguishable from omitting the key.
  Both yield `[]`, no edges, and no `depends on:` line in the output.
- **A target in another deployment's state** is simply unknown — see "Scope"
  above. It raises the ordinary unresolvable-target error.
- **A `state.json` written before this feature** loads unchanged;
  `StateEntry.depends_on` defaults to `[]`.
- **Every plan-blocking check fires before any driver load, credential
  resolution or Anthropic call.** This is a property the tests pin, not a
  side effect: a plan that is going to be refused should not first spend money
  and hit a provider's API.

## Verification

- **`tests/test_graph.py`** (new), modeled on `tests/test_compare.py` — pure
  imports, behavior-named `Test*` classes, each docstring stating its
  invariant. (Not `test_compare.py`'s strict `is True`/`is False` style —
  `topological_order()` returns a list and raises; it has no boolean result to
  assert on. An earlier draft of this line claimed otherwise.)
  - order correct, and **deterministic across input permutations**;
  - **fan-in**: one node with several dependencies, all of which precede it;
  - fan-out; diamond; disconnected components;
  - duplicate targets collapsing to one edge;
  - self-dependency as a length-1 cycle;
  - a multi-node cycle carrying its path.
- **`tests/test_models.py`** — `depends_on` defaults to `[]`; a multi-entry
  list is accepted; non-list, malformed key, and pattern-violating
  provider/resource_type rejected, **including a list whose second element is
  the bad one**, so validation is proven to check every element; a dotted
  resource name round-trips; `StateEntry.depends_on` defaults and round-trips a
  multi-entry list.
- **`tests/test_state.py`** — a `state.json` written without `depends_on` still
  loads.
- **`tests/test_parser.py`** — `depends_on` survives `parse_frontmatter()`,
  single- and multi-entry, including alongside a block scalar (the
  `---`-in-`user_data` case).
- **`tests/test_orchestrator.py`** —
  - ordering applied; a three-file run where one resource depends on the other
    two asserts both precede it;
  - a resource with one target in this run and one only in state;
  - cycle, duplicate key, unresolvable target, and
    live-depends-on-same-run-destroy each raise `PlanBlockedError` **before any
    driver load or Anthropic call** — `FakeClient([])` plus asserting the fake
    driver recorded nothing;
  - the unresolvable-target error names the offending target when the list has
    several;
  - delete-marked file exempt from resolution errors; delete-marked depending
    on a live file allowed;
  - state-only target resolves with no edge; NO_OP target keeps its edge;
  - delete-marked destroys reverse-ordered; both `build_destroy_plan()` paths
    reverse-ordered, including a fan-in destroyed before all of its targets;
  - zero Anthropic calls on an unchanged graph.
- **`tests/test_cli.py`** — the `depends on:` line is present with edges and
  absent without; a multi-dependency resource renders all targets on one
  comma-separated line; `--json` carries the full `depends_on` list.
- **No pre-existing zero-Anthropic-call test weakened** to accommodate the new
  pass. `aiform/graph.py` imports neither `llm` nor `anthropic` nor `models`.
- **Live**, before merge: three `.aiform.md` files in a scratch directory —
  `db-01`, `cache-01`, and `app-01` depending on both — verifying that `plan
  create` lists both targets first and prints the `depends on:` line, that
  `plan apply --verbose` completes both creates before `app-01` begins, that a
  re-run makes zero Anthropic calls, and that `plan destroy` **with no file
  arguments** destroys `app-01` before either target. Smallest droplet size,
  `aiform`-tagged, destroyed immediately.

  Name the files so that **alphabetical order contradicts the required order**
  (`app-01` sorts before `cache-01` and `db-01`). Otherwise the check passes
  against the old filename-glob behavior and proves nothing.

- **Live, the retrofit case** — added after review found the check above cannot
  see it. Apply the three files *without* `depends_on`, then add `depends_on`
  to the already-tracked `app-01` and run `plan` again, then `plan destroy`
  with no arguments. Every resource in the first check is brand new, so it
  reaches state through `_new_state_entry()`; adopting `depends_on` on a
  tracked file is a NO_OP and takes an entirely different path, which is
  exactly where it was found broken. A check that only ever creates fresh
  resources cannot distinguish the two.

## Out of scope

- **Cross-resource attribute references** — one resource's output flowing into
  another's `params`. Phase 2.
- **Automatic dependency detection** from driver-declared metadata. Phase 3;
  it produces the same edges this phase already consumes, so the ordering
  engine won't change.
- **Refusing a destroy that would orphan a still-tracked dependent**, and
  partial-failure recovery for a graph apply. Phase 4 — deliberately after
  this one, since failure semantics are hard enough serially.
- **Concurrency-safe state** (Phase 5) and **parallel execution** (Phase 6).
  Phase 1's order is total and strictly sequential.
- **Graphical visualization** (UX2). Phase 7.
- **Cross-deployment dependencies.** Not deferred — see "Scope" above; the
  model has no such concept.
- **Deployment identity** — recording which deployment a `state.json` belongs
  to, and showing it in `plan` output so a wrong-directory destroy-all is
  visible before it runs. Deferred by decision, filed as #201. Not part of the
  `MULTI_RESOURCE_PRD.md` phase sequence.
- **Adding `depends_on` to `build_plan_summary()`**, i.e. to gate #2's review
  prompt. Phase 4.

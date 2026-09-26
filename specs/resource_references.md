# specs/resource_references.md — cross-resource attribute references (`aiform/references.py`, + `orchestrator.py`/`planner.py`/`parser.py`/`cli.py`/`observability.py`)

**Naming note**: like `specs/resource_dependencies.md`, `specs/unordered_fields.md`
and `specs/resource_tagging.md`, this filename deliberately doesn't follow
`specs/README.md`'s per-module mirroring rule. The change is one feature spread
across five already-specced modules (`specs/orchestrator.md`, `specs/planner.md`,
`specs/parser.md`, `specs/cli.md`, `specs/driver_observability.md`) plus one tiny
new one. Named for the feature so it's discoverable from any of them, with each
cross-referencing it.

Closes #215. Phase 2 of `MULTI_RESOURCE_PRD.md`. Approved plan committed at
`plans/phase2-cross-resource-references.md`.

## Purpose

Let one resource's `params` read another resource's live attribute — a DNS
record's `data` reading a droplet's `ipv4_address` — so aiform resolves the
value instead of the user copying it by hand.

## Use cases

Delivers `MULTI_RESOURCE_PRD.md`'s **"Reference mechanism"** additional
requirement, the *value flow* half:

> Phase 1 delivers the *declaration* half (naming another resource); Phase 2
> delivers *value flow* (reading its attributes).

It is a prerequisite for UC1 (automatic detection, Phase 3) and UC3 (parallel
execution, Phase 6), both of which presuppose that a reference can exist at all.

## The gap this closes

Standing up a droplet plus an A record pointing at it takes two applies and a
hand-copied value: apply the droplet, read `ipv4_address` out of
`.aiform/state.json`, paste it into the domain's `records[].data`, apply again.

The worse half is what happens afterwards. A change to `region`, `image`,
`ssh_keys` or `monitoring` forces a replace (`LIKELY_REPLACE_FIELDS` in
`drivers/digitalocean/compute.py`), which assigns a new IP. The A record still
holds the old literal, and **nothing notices**: `planner.diff_attributes()`
compares the record's `data` against the same literal in state, agrees, and
`plan` reports a clean no-op while DNS points at a host that no longer exists.
That silence is why #215 is P1-correctness rather than a missing convenience.

`specs/digitalocean_domain.md` records the narrowing as deliberate — "record
`data` is always a literal the user types … There is no way to reference a
`compute` resource's `ipv4_address`, and this spec does not add one" — and that
sentence is retired by this spec.

## Relationship to PLAN.md §10

§10's entry — titled **"Dependency graph: ordering exists, value flow does
not"** until this phase retired that title — named this phase precisely:

> cross-resource *attribute* references — a DNS record's `data` reading a
> droplet's `ipv4_address`, the canonical example — are Phase 2, and are the
> half of this gap that actually needs a reference syntax.

That entry is updated rather than left claiming the gap exists, including its
title, which stops being true. What remains deferred there: automatic detection
(Phase 3), orphan refusal and partial-failure recovery (Phase 4),
concurrency-safe state (Phase 5), parallel execution (Phase 6), graphical
visualization (Phase 7). The PRD's open question 1 (this syntax) is answered;
open question 2 (file-per-resource) is untouched.

## Interface

### `aiform/references.py` — new

Pure string and tree work. No I/O, no LLM, no `anthropic` import, and no
`State` import — it takes a plain mapping, so `orchestrator.py` and
`observability.py` can both feed it. Same shape and same purity rationale as
`aiform/graph.py` and `aiform/compare.py`. It may import `aiform.models` for
`parse_dependency_key()`; `models` does not import it, so there is no cycle.

```python
class Reference(NamedTuple):
    target_key: str  # provider.resource_type.name
    attribute: str


class ReferenceResolutionError(Exception):
    """A reference that cannot be honoured. Carries `path` (the dotted param
    path) and a message naming what was wrong."""

    def __init__(self, path: str, message: str): ...


def find_references(params: dict[str, Any]) -> dict[str, list[Reference]]:
    """Dotted param path -> the references appearing at that path, in order of
    appearance. Walks dicts and lists to arbitrary depth. Paths index lists
    with brackets: `records[0].data`. Only paths carrying at least one
    reference appear."""


def reference_targets(params: dict[str, Any]) -> set[str]:
    """Just the target keys -- what the edge pass needs without resolving."""


def resolve(
    params: dict[str, Any], available: Mapping[str, Mapping[str, Any]]
) -> tuple[dict[str, Any], list[str]]:
    """Returns (a new params tree with references substituted, sorted dotted
    paths still unresolved). `available` maps resource key -> that resource's
    referenceable attributes.

    A path is *unresolved* -- returned rather than raised -- only when its
    target key is absent from `available`. Every other failure raises
    ReferenceResolutionError. An unresolved path keeps its literal text in the returned
    tree."""


def describe(
    params: dict[str, Any], resolved: dict[str, Any]
) -> list[tuple[str, list[Reference], Any]]:
    """(path, the references at it, what that path resolved to), sorted by
    path -- what the plan display and --json render from."""
```

`describe()`'s walk is driven by the **raw** tree's shape, not the resolved
tree's, because the resolved side alone is ambiguous: `{"t": "${...:tags}"}`
resolves to a list at path `t`, while `{"ids": ["${...:id}"]}` has its reference
at `ids[0]`. Walking the resolved tree would have to guess which list came
*from* a reference and which merely *contains* one — a first attempt did exactly
that and got it wrong.

Its value is per *path*, not per reference: several references embedded in one
string share the single string they produced.

`params` is never mutated; `resolve()` returns a new tree.

One property worth knowing rather than guarding: for a whole-value reference to
a list or dict attribute, the returned tree **aliases** the target's state
attributes rather than copying them (`resolved["t"] is entry.attributes["tags"]`).
No shipped driver mutates the `params` it is handed, so this is not a defect
today, and a defensive `deepcopy` would be error handling for a scenario that
cannot currently happen. It becomes one the moment a driver mutates in place.

### Changes to existing modules

- **`aiform/orchestrator.py`** — `PlannedResource` gains `raw_params` and
  `unresolved_references`; `_resolve_dependency_edges()` unions
  reference-derived targets with `depends_on` ones; `_plan_one()` resolves
  before `_decide_action()`; `_decide_action()` gains the unresolved branch;
  `apply_plan()` re-resolves before each driver call. It also gains a **public**
  `referenceable(st, *, exclude=...)` returning the `attributes`-plus-`id`
  namespace — public because `observability.py` needs the same mapping, and
  keeping this State-aware adapter here is what lets `references.py` stay free
  of a `State` import. Plus `_will_get_new_attributes()` and the `volatile` set
  `build_create_plan()` threads through the loop; both destroy producers now
  union reference-derived edges.
- **`aiform/planner.py`** — `unresolved_entry()`, a third deterministic
  zero-LLM `PlanEntry` producer alongside `create_entry()`/`destroy_entry()`.
- **`aiform/parser.py`** — `parse_frontmatter()` appends a hint to a YAML
  error when the source contains `${`.
- **`aiform/cli.py`** — `_print_plan()` prints one line per reference;
  `_plan_to_json()` carries them.
- **`aiform/observability.py`** — `_status_for_entry()` resolves before
  `_config_status()`'s diff.

**Two specs the approved plan listed are not touched.** `specs/models.md`,
because no model change turned out to be needed (below); and
`specs/system_test_domain.md`, because the live test it describes changes with
the live-suite run rather than ahead of it — describing a test that does not
exist yet would be the overclaim this repo treats as a defect.

**`aiform/models.py` does not change.** The approved plan listed
`specs/models.md` among the specs to update; it turned out no model change is
needed. `ResourceSpec.params` is already `dict[str, Any]`, references live
inside those values, `parse_dependency_key()` is reused as-is, and `Reference`
belongs in `references.py` next to the code that builds it.

## Behavior

### Grammar

```
${ <provider>.<resource_type>.<name> : <attribute> }
   \________________ key ___________/   \__ attr _/
```

- The **key** is byte-identical to a `depends_on` target and to a state key,
  and is parsed by `models.parse_dependency_key()` unchanged. `provider` and
  `resource_type` match `RESOURCE_OR_PROVIDER_PATTERN`; `name` is non-empty and
  may contain dots (`digitalocean.domain.example.com`), which is the whole
  reason the attribute is not a fourth dotted segment.
- The **attribute** matches `^[a-z][a-z0-9_]*$`.
- **The last colon separates**, so a `name` that itself contains a colon still
  parses.

### What is, and is not, a reference

This is the load-bearing rule, and it is narrower than the approved plan's
"a malformed `${…}` raises, never passes through".

**Why it had to narrow.** `parser.py`'s own frontmatter logic already
anticipates a cloud-init `user_data: |` block scalar in `params`, and
`compute.PARAM_SCHEMA` is `additionalProperties: True`, so a params value
legitimately containing shell text is an anticipated case — not a hypothetical.
Shell parameter expansion is spelled `${HOME}` and `${PORT:-8080}`. A rule that
raised on every `${…}` it could not parse would reject a valid droplet.

So:

| Text in a params value | Treated as |
|---|---|
| `${digitalocean.compute.web:ipv4_address}` | a **reference** |
| `${HOME}`, `${PATH}` | a literal, untouched — no colon |
| `${PORT:-8080}`, `${FOO:+x}` | a literal, untouched — `PORT` is not a valid key |
| `${digitalocean.compute.web:ipv4_address` (unclosed) | a literal, untouched |
| `${digitalocean.compute.web.ipv4_address}` | **raises** — dot, not colon |
| `${Digitalocean.compute.web:ipv4_address}` | **raises** — bad provider |
| `${digitalocean.compute:ipv4_address}` | **raises** — no name segment |
| `${digitalocean.compute.web:ipv4-address}` | **raises** — bad attribute |
| `${digitalocean.compute.web:}` | **raises** — empty attribute |
| `${digitalocean.compute.web:nosuchattr}` | **raises** — unknown attribute |
| `${VERSION:-1.2.3}` | a literal — the dot is in the *default*, not the key |
| `${BIND:-0.0.0.0:8080}` | a literal — dot and colon both in the default |
| `${IMAGE:-ghcr.io/org/app:latest}` | a literal, same reason |

A `${…}` is an attempted reference **iff** the segment before its **first**
colon contains a dot. That segment is the provider in a real reference
(`digitalocean` in `digitalocean.compute.web-01:ipv4_address`), and a shell
*variable name* cannot contain a dot.

**It must be the first colon, not the last.** In shell, everything after the
first colon is a default value, which routinely contains dots *and* colons:
`${BIND:-0.0.0.0:8080}`, `${HOST:-db.internal:5432}`,
`${IMAGE:-ghcr.io/org/app:latest}`. An earlier version of this rule tested the
text left of the *last* colon — `BIND:-0.0.0.0`, which is dotted — so every one
of those raised, and `plan` refused outright on any droplet whose `user_data`
carried ordinary cloud-init. Both `plan` and `plan destroy <file>` were blocked,
since both route through `_dependency_targets()`. The split itself still uses
the last colon, because `name` may contain one.

Once that test passes, the text is an *attempted* reference and is validated
rather than silently passed through: a key that does not parse as
`provider.resource_type.name` raises, and so does a malformed attribute. The
capitalised `${Digitalocean.compute.web-01:ipv4_address}` and the hyphenated
`${digitalocean.compute.web-01:ipv4-address}` therefore both raise. An earlier
draft gated on the key being *valid* rather than merely dotted, which sent both
of those to the provider verbatim — the exact failure this rule exists to
prevent.

**The one exception, and why it earns its complexity.** The likeliest user
error is writing the key with a fourth dot instead of a colon — that is what
Terraform habits produce. Passing it through silently sends the literal
`${digitalocean.compute.web.ipv4_address}` to DigitalOcean as a DNS record's
value. So a `${…}` with **no** colon whose content has three or more
dot-separated segments, and whose first two segments match
`RESOURCE_OR_PROVIDER_PATTERN`, raises `ReferenceResolutionError` naming the missing
colon. That test is narrow by construction: `${HOME}` has no dots, and
`${PORT:-8080}` has a colon, so neither can reach it.

### Whole-value versus embedded

- **Whole value** — the string is exactly one reference, e.g.
  `data: ${…:ipv4_address}`. The attribute's own value is substituted **with
  its own type**: a list stays a list, an int stays an int.
- **Embedded** — the reference sits inside a larger string, e.g.
  `data: "web is ${…:ipv4_address}"`. Each reference is stringified and
  interpolated. Several references in one string all substitute.
- **Embedding a non-scalar raises.** If an embedded reference resolves to
  anything but `str`/`int`/`float`/`bool`, `ReferenceResolutionError` is raised rather
  than interpolating a Python `repr` — `"tags are ['a', 'b']"` is never what
  was meant. Whole-value references may resolve to any type.

### The referenceable namespace

A target's **state `attributes`, plus `id`**. `id` is not in `attributes` —
`orchestrator._pop_id()` moves it to `StateEntry.id` — but it is the most useful
cross-resource value, so callers pass `{**entry.attributes, "id": entry.id}`.

An attribute not in that mapping raises `ReferenceResolutionError` listing the names that
are available, because a typo'd attribute is otherwise indistinguishable from a
driver that stopped returning a field.

**A resolved `None` raises.** `compute._flatten()` returns
`ipv4_address: None` when no public v4 is attached — a real race, tracked as
#178 — and a DNS record whose `data` is the string `"None"` is worse than a
refusal.

### YAML constraints the user must respect

Verified against PyYAML, not assumed:

| Written as | Result |
|---|---|
| `data: ${digitalocean.compute.web:ipv4_address}` | works unquoted |
| `data: ${digitalocean.domain.example.com:ttl}` | dotted name fine |
| `data: host-${…:ipv4_address}-end` | embedded, works |
| `- ${digitalocean.compute.web:id}` | block sequence, unquoted |
| `ids: ["${…:id}"]` | flow sequence — **must quote** |
| `ids: [${…:id}]` | `ParserError` |
| `data: ${…web: ipv4_address}` | `ScannerError` — **no space after the colon** |
| a newline after the colon | `ScannerError` |

The `[ ]` failure is caused by the `{}` braces and affects any delimiter
equally, so it is not a cost of choosing the colon. The no-space rule is, and
it is mitigated: when `parse_frontmatter()` catches a YAML error from a source
containing `${`, it appends a hint naming both sharp edges, so the user sees
something actionable instead of a bare `mapping values are not allowed here`.

### Resolution happens twice, deliberately

**Plan time**, in `_plan_one()` before `_decide_action()`, against
`st.resources`. Not optional: `planner.plan_resource()` diffs `desired_params`
against live attributes, so an unresolved literal would defeat the no-op
short-circuit at `planner.py:176` **permanently** and bill a categorization on
every future plan. Because the plan is walked in topological order, a target
that is also in this run has already been refreshed by the time its dependent
resolves.

**Apply time**, in `apply_plan()`, re-resolving `raw_params` from the in-loop
`st` immediately before `driver.create()` and `driver.update()`. The replace
path does **not** re-resolve a third time: `_replace_resource()` receives the
value computed for the `update()` attempt that raised
`DriverUpdateNotSupported`, because the only entry to leave state in between is
its own, and a resource referencing itself is a cycle already refused. `apply_plan()` saves state after every resource, so a
just-created droplet's `ipv4_address` is present by the zone's turn.

Apply re-resolves **the whole tree**, not only the paths that were unknown at
plan time: one code path rather than two, and the value handed to a driver is
the one live at the moment of the call — which is the correct answer precisely
when a target was replaced earlier in the same apply.

If a path is *still* unresolved at apply time, `PlanBlockedError` is raised
naming it. Topological ordering means this cannot happen, and the guard exists
because the alternative to raising is sending a literal `${…}` to the provider.

### Which targets contribute an edge

Reference-derived targets are unioned with `depends_on` targets and classified
by **exactly** the table in `specs/resource_dependencies.md`'s "Which targets
contribute an edge" — the same generating principle, the same per-target
resolution, the same `PlanBlockedError` reasons. A reference adds a target to
that classification; it does not add a rule to it.

Consequences worth stating because they are what "a reference implies the edge"
means concretely:

- A resource referencing another **needs no `depends_on:` entry**. The edge is
  derived from the reference.
- **The derived edge is persisted to `StateEntry.depends_on`**, alongside any
  declared ones. This is not cosmetic: Phase 1's destroy-all-from-state path
  orders purely by what state records, so an edge that existed only in the
  `.aiform.md` would let `aiform plan destroy` tear a droplet down before the
  DNS record pointing at it — and no apply could repair it, since the ordering
  is read from state. Phase 1's third write site in `_plan_one()` already
  writes `depends_on` on every plan run regardless of action, including
  `NO_OP`, so adopting a reference on an otherwise-unchanged resource reaches
  state the same way retrofitting `depends_on:` does.
- Declaring both is legal and collapses to one edge, exactly as two identical
  `depends_on` entries already do.
- A reference to a target **delete-marked in this run** is a
  `PlanBlockedError`, as `depends_on` already is.
- A reference to a target **tracked in state but not in this run** resolves
  against its stored attributes and contributes no edge — that is what keeps
  `aiform plan create one-file.aiform.md` working. Those attributes are *not*
  refreshed in that run, so the value is as fresh as the last run that touched
  the target.
- A reference to a target **nowhere at all** raises.
- **Both destroy producers honour reference-derived edges.**
  `_build_destroy_plan_from_state()` gets it for free by reading the persisted,
  already-unioned `StateEntry.depends_on`; `_build_destroy_plan_from_paths()`
  derives edges from the files and so unions them itself. An earlier draft
  switched every other site to the unioned list and missed this one, which let
  `aiform plan destroy app.aiform.md db.aiform.md` destroy a referenced droplet
  before the record pointing at it — the precise inversion Phase 1 exists to
  prevent. Covered by a test.
- **A delete-marked file's params are still scanned for malformed references.**
  Phase 1 exempts a delete-marked file from *target resolution* errors, but a
  dot-typo inside one raises during the edge pass, before that exemption is
  reached. Narrow and arguably harmless — nothing is resolved for a resource
  being destroyed — but it is a behaviour Phase 1's table does not describe, so
  it is recorded here rather than left for someone to discover.

### Unresolved references and the plan action

Ordering inside `_decide_action()`, first match winning:

1. `state_entry is None` → `create_entry()`, zero LLM. A first run takes this
   path, so an unknown never reaches a diff.
2. `drifted_missing` → `create_entry()`, zero LLM.
3. **`unresolved_references` non-empty** → `planner.unresolved_entry()`: a
   deterministic `UPDATE`, zero LLM, rationale naming the unresolved paths.
4. Otherwise → `planner.plan_resource()` as today.

Case 3 covers two situations, and the second is the one that matters most:

- The target is brand new in this run, so nothing about it is in state yet.
- **The target is in state but this run is about to give it a new value** — see
  "A target this run will replace" below.

There is no honest diff to categorize either way, because the desired value is
not known yet, so the model is not asked. `params_agree` is `False`, which keeps
`_plan_one()` from recording the `.aiform.md` hash on a run whose params were
never fully known — the #195 invariant.

Case 3 never returns `NO_OP`, so a resource with an unresolved reference is
always applied.

### A target this run will replace

`build_create_plan()` accumulates a `volatile` set as it walks the plan in
topological order, and `_resolve_params()` withholds those keys from the
namespace. A key joins it when `_will_get_new_attributes()` says so:

**Any `CREATE` or `UPDATE`** — deliberately not only an `UPDATE` flagged
`likely_replace`. The question is "may this target's attributes differ after the
apply", and an update is by definition an answer of yes; `likely_replace` only
describes *how* the value changes, and it is the model's advisory guess rather
than a fact. Gating on it missed two real cases:

- an update the model called in-place that `driver.update()` then refuses,
  producing a delete + create and a new address;
- the **middle of a chain** (`a → b → c`), whose own action is the deterministic
  `UPDATE` this very mechanism produces, and which therefore carries
  `likely_replace=False` by construction — so `a` resolved against `b`'s
  pre-update value.

`CREATE` covers the **recreate of a drifted resource**, the case that matters
most, since a drifted entry is still in `st.resources` with its old attributes.
`DESTROY` and `NO_OP` are never volatile.

Without this, the feature fails at its own purpose. A tracked droplet deleted
out-of-band still holds its old `ipv4_address` in state, so a zone referencing
it resolves to the **dead** address, diffs clean, plans `NO_OP` — and
`apply_plan()` skips `NO_OP` before `_apply_params()` runs, so the apply-time
re-resolve never fires. The apply then recreates the droplet with a new address
and leaves DNS pointing at the old one, with `plan` having displayed the stale
value as though it were the answer. That is exactly the P1 failure #215 was
filed against. The same held for an ordinary replace (a `region` change → new
droplet → new address). Both are covered by tests.

Three properties worth stating:

- **Being broad costs a dependent update that rewrites an identical value**, at
  zero LLM cost, and it converges: once the target is applied, its next plan is
  `NO_OP`, nothing is volatile, and the dependent is `NO_OP` too. A missed
  volatile instead leaves a record pointing at a dead host until the next plan.
  The trade is not close.
- **A withheld target's attribute name is still validated at plan time.**
  `resolve()` takes `volatile` rather than having the caller delete those keys
  from the mapping, precisely so the target's *shape* is still visible: a typo
  like `${…:ipv4_addres}` is refused by `plan`, not discovered mid-apply after
  the target had already been created. Only key presence is checked, not the
  `None` rule — a value about to be replaced is allowed to be `None` now, which
  is exactly a drifted droplet's `ipv4_address`.
- **Apply time withholds nothing.** `_apply_params()` uses the full namespace,
  because by then the target has actually been created or replaced and state
  holds its real new value. A `ReferenceResolutionError` there — reachable only
  for a brand-new target, whose attribute names could not be checked earlier —
  is wrapped as `PlanBlockedError`, which `cli.py` already handles, so it cannot
  reach the user as a traceback with the apply half-done.

### Plan output

One indented line per reference, under the existing `depends on:` line:

```
+ digitalocean.domain.example.com: create
    no state entry is tracked for this resource yet
    depends on: digitalocean.compute.web-01
    records[0].data = ${digitalocean.compute.web-01:ipv4_address}
```

An **unresolved** reference prints the reference text verbatim, with no
annotation — no "(known after apply)". It is self-evidently unresolved, and the
suffix would be noise on every first apply. A **resolved** reference prints the
value it resolved to, which is what makes the plan reviewable:

```
    records[0].data = 203.0.113.5
```

`_plan_to_json()` carries a typed list rather than the rendered text, since
`--json` is the closest thing aiform has to an API:

```json
"references": [
  {
    "path": "records[0].data",
    "target": "digitalocean.compute.web-01",
    "attribute": "ipv4_address",
    "resolved": "203.0.113.5"
  }
]
```

`resolved` is `null` when the value is not yet known. A resource with no
references carries an empty list, not a missing key.

### `aiform resource status`

`observability._config_status()` re-parses the `.aiform.md` and diffs
`spec.params` against `attributes` (`observability.py:614`). Without resolution
it would report permanent drift on every referencing resource. It resolves
first; `_status_for_entry()` already receives the whole `State`, so nothing new
is plumbed. A reference that cannot be resolved makes the config status
**undetermined**, via the existing `_undetermined()` helper, rather than
reporting a drift that is really a missing target.

### Zero-Anthropic-call property

Resolution is deterministic string and tree work: no LLM call on the reference
path, in any phase, per the PRD's cost constraint. On unchanged input every
reference resolves to the same value, `diff_attributes()` is empty, and
`planner.py:176` short-circuits to `NO_OP` exactly as today — so a repeat
`plan` still costs **zero** Anthropic calls, and the tests pin it.

When a target's attribute genuinely changes, the dependent's diff is non-empty
and one categorization is paid. That is the feature working, not a regression:
it is the case that used to report a false no-op.

## Edge cases / errors

- **Malformed key** inside an otherwise well-formed reference (bad provider,
  bad `resource_type`, empty name) → `ReferenceResolutionError` naming the key. The
  message comes from `parse_dependency_key()`.
- **Self-reference** — a resource referencing its own key — is a length-1
  cycle, refused by `graph.topological_order()` exactly as a self
  `depends_on` already is. No special case.
- **The same reference twice** in one params tree resolves twice to the same
  value and collapses to one edge.
- **A reference in a key rather than a value** is not supported and not
  detected: `find_references()` walks values only. Frontmatter keys are
  `params` field names, which drivers declare; a reference there is meaningless.
- **Empty `params`** → `find_references()` returns `{}`, `resolve()` returns
  the tree unchanged and no unresolved paths.
- **A non-string scalar** (`ttl: 3600`) is never scanned; only `str` values can
  carry a reference.
- **`ReferenceResolutionError` never reaches `cli.py`.** Every site that can
  raise it — the edge pass, plan-time resolution, and apply-time
  re-resolution — wraps it as `PlanBlockedError`, so the CLI's existing
  exit-code handling applies and no new exception type escapes.

## Verification

### `tests/test_references.py` — new

Modelled on `tests/test_graph.py`: imports only the public names, uses `#`
comments rather than docstrings, and pins determinism directly.

- whole-value reference preserves `str`, `int`, `bool` and `list` types
- embedded reference stringifies; several in one string all substitute
- embedding a non-scalar raises
- nesting: inside a dict, inside a list, inside a list of dicts
  (`records[0].data`), and two levels deep
- dotted target name (`digitalocean.domain.example.com:ttl`)
- last colon separates when the name contains a colon
- **literals pass through untouched**: `${HOME}`, `${PORT:-8080}`, `${FOO:+x}`,
  an unclosed `${…`
- the dot-instead-of-colon typo raises, naming the missing colon
- unknown attribute raises **and lists the available names**
- resolved `None` raises
- unresolved target returns its path rather than raising, and the returned tree
  keeps the literal at that path
- `find_references()` path spellings; `reference_targets()` returns keys only
- `resolve()` does not mutate its input

### Existing test files

- **`tests/test_orchestrator.py`** — an edge derived from a reference with no
  `depends_on`; fan-in from two references; both declared together collapsing
  to one edge; reference to a delete-marked target blocked; reference to
  nowhere blocked; **both blocking cases proved to fire before any driver load
  or LLM call**, using the `FakeClient([])` plus empty `drivers_dir` idiom
  Phase 1's tests use; apply-time resolution against a resource created earlier
  in the same loop; the tracked-dependent deterministic `UPDATE` making zero
  calls; and **unchanged input with a resolved reference makes zero Anthropic
  calls**.
- **`tests/test_planner.py`** — `unresolved_entry()`'s shape; the no-op
  short-circuit still fires when references are resolved.
- **`tests/test_parser.py`** — a reference survives `parse_frontmatter()`,
  including alongside a block scalar; the YAML hint appears for a source
  containing `${` and not otherwise.
- **`tests/test_cli.py`** — the text line for resolved and unresolved
  references; the JSON shape including `"resolved": null`; a resource with no
  references carries an empty list.
- **`tests/test_observability.py`** — `resource status` reports in-sync for a
  resolved reference rather than permanent drift, and undetermined for an
  unresolvable one.

No pre-existing zero-Anthropic-call test is weakened.

### Live

The PRD's acceptance case, in a **single** `apply`: a droplet plus a zone whose
A record reads its `ipv4_address`. **Name the files so alphabetical order
contradicts the required order** — otherwise the check passes against the old
filename-glob behaviour and proves nothing, the point
`specs/resource_dependencies.md` makes about its own live test. Then a second
`plan` to confirm a clean no-op and zero Anthropic calls, and `aiform resource
status` to confirm no phantom drift. Scoped to the aiform tag with the usual
cleanup discipline.

## Out of scope

- **References into integer-typed fields.** The firewall's `droplet_ids` is
  `{"type": "integer"}` while `compute`'s `id` is `str(droplet["id"])`, so
  `droplet_ids: ["${…:id}"]` resolves to a string its own validation rejects.
  This phase's acceptance case is the string-valued DNS one. Filed separately;
  not solved here with a cast syntax, and not by changing the compute driver's
  attribute types.
- **An escape for a literal `${`.** Not needed: the narrowed
  what-is-a-reference rule above means only text that parses as a real
  reference is substituted, so a literal never has to be escaped.
- **Automatic edge detection** from driver-declared metadata — Phase 3, and the
  PRD's open question 3. A driver class attribute declaring which `params` keys
  hold references would be answering that question; resolution here is
  deliberately driver-agnostic.
- **Orphan refusal and partial-failure recovery** (Phase 4),
  **concurrency-safe state** (Phase 5), **parallel execution** (Phase 6),
  **graphical visualization** (Phase 7). Execution here stays strictly
  sequential and the order is total.
- **Cross-run cycle detection**, issue **#206** — a cycle recorded in
  `StateEntry.depends_on` across several runs is not caught at plan time and
  then blocks `plan destroy` for the whole deployment. References make such a
  cycle easier to create by accident, which strengthens that issue's case, but
  it owns a refuse-versus-degrade decision of its own and is not decided here.
- **Adding references to `build_plan_summary()`**, i.e. to gate #2's review
  prompt — same reasoning `specs/resource_dependencies.md` gives for
  `depends_on`: it would inject an unexplained key into that prompt. Phase 4.
- **`.aiform/state.json` schema changes.** None. `StateEntry.attributes`
  already holds what the CSP echoed back and the reference expression lives in
  the `.aiform.md`.

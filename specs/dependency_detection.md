# specs/dependency_detection.md — automatic dependency detection, and why it is deferred

**Naming note**: like `specs/resource_dependencies.md`,
`specs/unordered_fields.md` and `specs/resource_tagging.md`, this filename
deliberately doesn't follow `specs/README.md`'s per-module mirroring rule. It
describes one feature that would span `aiform/driver.py`,
`aiform/orchestrator.py` and every driver, and it is named for the feature so
it is discoverable from any of them.

Closes #220. Phase 3 of `MULTI_RESOURCE_PRD.md`.

**Build status.** **Not built, and deferred by decision** — not pending, not
in progress, not next. This file is the decision record and the contract a
future Phase 3 starts from. Nothing here is implemented: no driver declares
reference metadata, `aiform/driver.py` has four declarative class attributes
and not five, and no code path infers an edge.

| Piece | State |
|---|---|
| The decision to defer | made, recorded here |
| `REFERENCE_FIELDS` contract (PRD open question #3) | **designed, not built** |
| `REFERENCE_FIELDS` on any driver | not built |
| Edge inference in `_order_files()` | not built |
| The firewall-deletion probe | named here, not run |

## Purpose

Record what automatic dependency detection would be, what it would be worth
today, and why the answer is "not enough to build" — so the question is
settled with evidence rather than re-derived from scratch, and so a future
session inherits a decided declaration contract instead of three sentences
of PRD text.

## Use cases

`MULTI_RESOURCE_PRD.md`'s **UC1 — automatic dependency detection**: a user
wants `aiform` to know that one resource depends on another, and why, without
declaring every relationship by hand.

UC1 has **no committed phase**. Phase 1 delivered UC2 (manual declaration),
which is the escape hatch UC1 was always going to need anyway — the PRD's own
"deliberate inversion" note explains why declaration shipped first.

## Relationship to PLAN.md §10

§10's "Dependency graph: ordering exists, value flow does not" entry lists
automatic detection as Phase 3, phrased as deferred-but-coming. This spec
**narrows** that entry: the mechanism is unchanged, but the phase is deferred
by decision rather than by sequencing, and §10 is updated to say so rather
than left implying a schedule.

Nothing in §10's *delivered* claims changes. Phase 1's ordering engine,
cycle detection and `depends_on` persistence are untouched by this decision.

## The decision

**Do not build automatic detection now.** The grounds are narrow and specific
to today's driver set, not a general claim that inference is a bad idea.

### There is one inferable edge in the entire driver set

Every reference-shaped field across the three drivers:

| Field | Points at | Verdict |
|---|---|---|
| `droplet_ids` (`firewall.py:83`) and `sources`/`destinations.droplet_ids` (`firewall.py:52`) | a droplet | **The one real edge.** Integer, matched `str(int)` against `StateEntry.id`, which `compute.py:205` produces as `str(droplet["id"])` |
| `tags` (`firewall.py:84`, `compute.py:173`) and `sources`/`destinations.tags` (`firewall.py:53`) | a DigitalOcean tag | No `tag` driver exists; a tag cannot be an aiform resource, so there is no node to point at |
| `ssh_keys` (`compute.py:170`) | a DigitalOcean SSH key | No `ssh_key` driver exists |
| `load_balancer_uids`, `kubernetes_ids` (`firewall.py:54-55`) | a load balancer, a k8s cluster | No drivers. The k8s edge was never probed at all (`specs/digitalocean_firewall.md:289`) |
| `records[].data` (`domain.py:103`) | a droplet's IP | An **attribute** match against `attributes["ipv4_address"]` (`compute.py:211`), not an id match. This is Phase 2's problem — value flow — not detection's |

`specs/resource_tagging.md`'s marker tag contributes no edge: it is a fixed
constant, never a lookup key, and explicitly invisible to the diff engine.

Worth noting where the metadata already lives, because it shortens a future
Phase 3 considerably: `firewall.py:40-42` already carries a comment saying
every rule target key *is* a reference to another resource kind, and
`specs/digitalocean_firewall.md:279-295` already tabulates the edges with
their id types and pre-existence requirements, probe by probe. Phase 3's real
work is moving that from prose into something the planner can read, not
discovering it.

### The one edge cannot change create ordering

A droplet's id is assigned by DigitalOcean at creation. A user cannot write
it into a firewall's `droplet_ids` before the droplet exists — the API
refuses an unknown id with a 422 (`specs/digitalocean_firewall.md:287`). So
any config containing that literal was written after a prior successful apply
of that droplet, which means the droplet is already tracked, and at the run
where the firewall is first created the droplet's action is `NO_OP`.
`apply_plan()` skips `NO_OP` before any driver call
(`aiform/orchestrator.py:927`). Ordering a `CREATE` against a `NO_OP` is
inert.

A replace does assign a new id, leaving a stale literal in the firewall's
params — but that surfaces as a phantom diff on a later run, not a same-run
ordering hazard.

**This argument is scoped to this field type, not to inference in general.**
It holds because the id is *provider-assigned*. A **user-chosen** reference
value — a zone name, a tag string — is writable before its referent exists,
and would break the argument outright. No such field exists in any current
driver. Do not read this section as evidence that detection's create-ordering
value is structurally zero; it is zero for the one edge that exists.

### Phase 2 supersedes most of the value

Whatever reference syntax Phase 2 lands (PRD open question #1 — interpolation
in `params`, a separate reference block, something else), the edge is explicit
*in the syntax*. Parsing it yields a deterministic, zero-cost edge at full
precision with no driver metadata at all.

The PRD sequences Phase 2 ahead of Phase 3 and permits one phase in flight at
a time, so by the time detection could ship, the explicit-reference edges
already exist. Detection's residual scope is then only the hardcoded-literal
case — which is the provably-inert case above.

### Destroy ordering has no failure mode for this edge

Two corrections to an earlier draft of this argument, both recorded because
the wrong version is the intuitive one.

First, the mechanism. `_build_destroy_plan_from_state()` does run a real
topological sort over `StateEntry.depends_on`
(`aiform/orchestrator.py:803`, `:807`); it degenerates to reverse-alphabetical
only when there are no edges at all. That is the undeclared case, because
`StateEntry.depends_on` is written solely from `resource_spec.depends_on`
(`aiform/orchestrator.py:124`, `:503`, `:1116`) — never from anything
inferred.

Second, today the ordering that fallback produces cannot break anything, by
naming accident rather than design. `parse_dependency_key()`
(`aiform/models.py:15`) yields `provider.resource_type.name` and the sort
compares `resource_type` before `name`; since `"compute" < "domain" <
"firewall"`, every firewall is destroyed before every droplet, for any pair
of names. That flips the moment a resource type sorting after `firewall`
arrives — a `load_balancer` driver, whose edge
`specs/digitalocean_firewall.md:288` already documents.

But it flips into a hazard only for a resource that actually *breaks* when
its referent disappears, and a firewall does not:

> **A firewall does not break when a droplet in it is removed**, and a
> firewall can exist ahead of the droplets it targets — the configuration is
> simply inert until they exist.

That is owner-reported, not probed; see "Knowledge-confidence" below. It
closes the question `specs/digitalocean_firewall.md:293-295` left open, and
it means destroy ordering carries no correctness value for this edge.

### Inferring this edge would make Phase 4 refuse safe destroys

This is the argument that looked strongest *for* detection, and it inverts.

Phase 4's orphan refusal blocks a destroy that would leave a still-tracked
dependent broken. Phase 1 already gives it everything it needs for declared
edges, so detection's marginal contribution would be covering the user who
hardcodes a droplet id and never writes the redundant `depends_on:` line.

But if the firewall is not broken by the droplet's removal, then an inferred
`firewall → droplet` edge makes Phase 4 refuse a perfectly safe destroy,
forcing `--force` for no reason. `prompts/review_plan.md:48` already states
the principle for gate #2 — "a false block trains the user to stop trusting
this gate" — and it applies identically to a refusal derived from a guessed
edge.

A declared edge does not have this problem: the user asserted the
relationship, so refusing on it honors an instruction. An inferred one
asserts a relationship the user never claimed.

## The declaration contract

This answers `MULTI_RESOURCE_PRD.md`'s open question #3 — *what does a driver
declare so Phase 3 can infer edges, a new class attribute alongside
`PARAM_SCHEMA` or metadata inside it?* Designed, not built.

**A fifth declarative class attribute, `REFERENCE_FIELDS`**, alongside the
four in `aiform/driver.py` (`PARAM_SCHEMA` `:62`, `LIKELY_REPLACE_FIELDS`
`:73`, `NON_DIFFABLE_FIELDS` `:94`, `UNORDERED_FIELDS` `:112`) — not metadata
inside `PARAM_SCHEMA`. Three reasons:

- **`PARAM_SCHEMA` is a prompt payload.** It is passed verbatim to the
  intent-orchestration model (`aiform/planner.py:99`,
  `prompts/diff_plan.md:15`). Adding aiform-private keys to it changes what
  the model sees, for no model benefit — and the model is told to reason about
  that schema's types, so unexplained keys are a live risk, not a cosmetic one.
- **The existing four establish one concern, one attribute.** Each is a flat
  declaration the base class reads and the driver reassigns rather than
  mutates. A fifth fits the pattern; a nested annotation inside a JSON Schema
  does not.
- **`PLAN.md` §4's contract is stability-critical.** An additive sibling
  leaves the existing four untouched. Mutating a shared schema makes every
  driver's `PARAM_SCHEMA` load-bearing for a second purpose.

**Shape.** Each entry names a field path, the resource kind it points at, and
which state field the value matches:

- **field path**, including nesting, because the real edges are nested —
  `droplet_ids` and `inbound_rules[].sources.droplet_ids` are different paths
  to the same target kind.
- **target `provider` and `resource_type`**, so the match is against a
  resource key rather than a guess.
- **which state field the value equals** — `StateEntry.id`, or a named key in
  `StateEntry.attributes`. The `domain.records[].data` row above is the whole
  reason this has to be explicit: an attribute match and an id match are
  different lookups, and only the id one is available before Phase 2.

Note that `PLAN.md` §4 still omits `UNORDERED_FIELDS` from its list of
declarative attributes (#133). A fifth attribute inherits that documentation
debt — fix #133 first or the same gap doubles.

## Where detection would run, and what it would cost

This is the part `MULTI_RESOURCE_PRD.md:341-343` understates. "The ordering
engine doesn't change" is true. The pass in front of it does.

`_order_files()` (`aiform/orchestrator.py:373`) is called at
`aiform/orchestrator.py:400`, deliberately **before** the driver cache is
built at `:402` and before any credential resolution. That ordering is what
makes the pass free: pure YAML and string work, no driver import, no CSP call,
no model call.

Detection needs the driver class to read `REFERENCE_FIELDS`. So it either
moves the driver load ahead of the ordering pass, or adds a second pass after
it — and both perturb a pass whose zero-cost property is pinned by tests:
`tests/test_orchestrator.py:1614`
(`test_unchanged_dependency_graph_makes_zero_llm_calls`) and `:1919`
(`test_cycle_raises_plan_blocked_error_before_driver_load_or_llm_call`). The
second test's name *is* the invariant — it asserts the cycle check happens
before a driver load, which is precisely what detection would need to undo.

A future Phase 3 must therefore state which it does and re-establish the
invariant, not discover the conflict mid-implementation. Loading a driver is
not free of side effects either: `load_driver` re-executes the module on every
call.

## Edge cases a future implementation must answer

Recorded so they aren't rediscovered. None is resolved here.

- **A literal matching more than one resource.** Two tracked droplets cannot
  share a DigitalOcean id, but an *attribute* match (an IP, a tag) can match
  several. Detection must refuse or pick deterministically; a nondeterministic
  edge breaks Phase 1's determinism requirement.
- **A literal matching a resource in state but not in this run.** Phase 1's
  declared-edge rule is that a state-only target contributes no edge
  (`aiform/orchestrator.py:352-353`). An inferred edge must follow the same
  rule or the two mechanisms disagree.
- **A stale literal after a replace.** The id no longer matches anything, so
  detection finds no edge where the user believes one exists. Silence here is
  worse than an unhelpful edge.
- **Precedence against a declared edge.** If a user declared `depends_on` and
  detection infers a different set, declared must win — UC2 exists as the
  correction mechanism for exactly this.
- **Whether an inferred edge is persisted.** Writing it to
  `StateEntry.depends_on` would make it indistinguishable from a declared one
  on the next run, and would feed Phase 4's refusal. It should not be
  persisted without deciding that deliberately.

## Verification

Nothing to test — nothing is built. What a future Phase 3 would need, and what
this decision rests on:

**The named probe, not run.** It would promote the owner-reported firewall
behavior to `verified` and close
`specs/digitalocean_firewall.md:293-295`'s open note. Shape: create a
disposable droplet and a firewall carrying its id in `droplet_ids`, `DELETE`
the droplet, then `GET` the firewall and observe whether `droplet_ids` still
carries the dead id, and whether a later `PUT` carrying it returns 422.
Follow `specs/driver_creation.md`'s loop, with the transcript under
`probes/transcripts/` and findings in
`knowledge/drivers/<session>/FINDINGS.md`.

It is **not** a decision gate. The deferral holds on the deterministic
arguments above whichever way the probe comes out; a dangling reference that
*did* break would restore the destroy-ordering and orphan-refusal cases, which
is why it appears under "Conditions that reopen this" rather than here.

## Conditions that reopen this

Any one of these invalidates a specific argument above:

- **A driver whose reference value is user-chosen** rather than
  provider-assigned — a zone name, a tag string, anything writable before its
  referent exists. Breaks the create-ordering argument outright.
- **A resource type that genuinely breaks when its referent is deleted.**
  Restores both the destroy-ordering and the orphan-refusal cases.
- **A driver set where the edge inventory is no longer one row** — a `tag`,
  `ssh_key`, `load_balancer` or k8s driver would each add real edges, and a
  `load_balancer` additionally sorts after `firewall`, flipping the
  destroy-order accident described above.
- **Phase 2 landing a reference syntax that does not make its own edges
  self-evident.** Unexpected, but it would restore detection's main scope.

## Out of scope

- **Cross-resource attribute references.** Phase 2, and PRD open question #1.
  The `domain.records[].data` row above belongs to it, not here — a value
  flowing from one resource into another is a different mechanism from noticing
  that two resources are related.
- **Orphan refusal and partial-failure recovery.** Phase 4. This spec takes a
  position on what detection would *do to* Phase 4, and none on Phase 4's own
  design.
- **Inferring edges with a model call.** Permanently excluded, not deferred.
  `CLAUDE.md` and `MULTI_RESOURCE_PRD.md:116-121` both require the graph path
  to be deterministic, and a repeat `plan` on unchanged input to make zero
  Anthropic calls. An LLM-inferred edge would also be nondeterministic across
  runs, which Phase 1's ordering guarantees forbid independently of cost.
- **A lint that warns instead of inferring** — "you hardcoded an id matching a
  tracked resource, did you mean a reference?". A real and much cheaper idea
  than detection, and deliberately not designed here: it is a different feature
  with a different failure mode (a false warning costs attention, not a refused
  destroy), and it would need its own use case rather than inheriting UC1's.
- **Renumbering the PRD's phases.** Phase 3 keeps its number while deferred, so
  Phases 4-7 and every reference to them stay valid.

## Knowledge-confidence

Grading each load-bearing claim, per the convention in
`specs/driver_observability.md` and `specs/driver_creation.md`.

| Claim | Grade | Basis |
|---|---|---|
| The edge inventory is complete for the three current drivers | **verified** | Every `PARAM_SCHEMA` read in full; file:line cited per row |
| A droplet id is provider-assigned and a firewall 422s on an unknown one | **verified** | `specs/digitalocean_firewall.md:287`, transcript `21-` |
| `apply_plan()` skips `NO_OP` before any driver call | **verified** | `aiform/orchestrator.py:927` |
| The destroy-from-state path topologically sorts `StateEntry.depends_on` | **verified** | `aiform/orchestrator.py:803`, `:807` |
| `"compute" < "domain" < "firewall"` makes the zero-edge fallback safe today | **verified** | `aiform/models.py:15`, read rather than reasoned about |
| **A firewall does not break when a droplet in it is removed** | **owner-reported** | Stated directly by the repo owner, 2026-09-26. Not probed. The probe above is what would promote it |
| A firewall can exist ahead of the droplets it targets, config inert | **owner-reported** | Same statement, same date |
| Phase 2's syntax will make its own edges self-evident | **inferred** | True of every reference syntax under consideration, but Phase 2 is undesigned (PRD open question #1) |
| Detection would force a driver load before the ordering pass | **inferred** | Follows from `aiform/orchestrator.py:400` preceding `:402`; no implementation has tested it |

The two owner-reported rows are the ones a future reader should treat with
most care. They are decisive for the destroy and orphan-refusal arguments, and
they rest on operational knowledge rather than a transcript. They must not be
silently upgraded to `verified` without the probe.

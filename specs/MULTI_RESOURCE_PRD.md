# Multi-Resource Support — Product Requirements

Status: requirements settled. **Phase 0 shipped** (PR #199, merged
2026-09-24 as `53ace29`, closing #198). **Phase 1 shipped** (PR #204,
merged 2026-09-25 as `6c5b2bd`, closing #200, spec at
`specs/resource_dependencies.md`). **Phase 2 shipped** (PR #217, merged
2026-09-27 as `15cbcb6`, closing #215, spec at
`specs/resource_references.md`), with one known limitation tracked as #216.
**Phase 3 is paused by decision** (issue #220,
`specs/dependency_detection.md`) and keeps its number. **Next: fix #216**,
then reassess Phase 3 from what that teaches. This
document is the durable record of what multi-resource support must do and
the order it gets built in. `PLAN.md` remains the architecture spec —
§10's "No dependency graph" entry points here, and each phase reconciles
its outcome back into `PLAN.md` as it lands.

## Problem

`PLAN.md` §10 names "No dependency graph" as an explicit, undesigned gap:
resources are planned/applied independently, one `.aiform.md` file at a
time, in file-glob order. There is no way for one resource's output (e.g.
a compute resource's IPv4 address) to feed into another resource's
`params` (a DNS record pointing at that IP is the canonical example from
`PLAN.md` and `specs/digitalocean_domain.md`).

## Use cases

**This is the single home for the dependency use cases.** Earlier drafts stated
three here and five more in `specs/resource_dependencies.md`; they are consolidated
below. Specs reference this table rather than restating it.

Ordered by priority, not by number. **Priorities are proposed, not decided** — the
scale is the repo's own `P0`-`P3` from `.claude/skills/prioritize-issue`, reused so
a second vocabulary is not invented.

| Use case | What must be true | Priority | State |
|---|---|---|---|
| **UC-B — Delete in dependency order** | Destroying a set of resources produces no error caused by removing something another resource still references, and leaves nothing silently pointing at what is gone. | **P0** | partial — within one run only |
| **UC-A — Create in dependency order** | Applying a set of resources produces no error caused by a resource being absent when something that needs it is created. | **P1** | delivered |
| **UC-C — Know what a change touches** | A plan that will alter a resource others depend on shows that consequence before it is applied. | **P1** | delivered, deliberately over-reports |
| **UC-E — Recover in dependency order** | After a partial failure, a re-run completes the work rather than compounding the damage. | **P1** | partial |
| **UC2 — Declare a dependency by hand** | A user can state a relationship `aiform` cannot see, and have it honoured. Uniquely expresses ordering with **no** value flow. | **P1** | delivered |
| **UC-D — Know what a failure touches** | When a resource fails or degrades, an operator can learn what else is affected without reading the configuration by hand. | **P2** | **not delivered** — no implementation |
| **UC3 — Parallel execution** | Resources with no dependency between them are applied concurrently, so N independent resources do not take N times as long as one. | **P2** | not delivered — Phase 6 |
| **UC1 — Automatic dependency detection** | `aiform` knows that one resource depends on another, and why, without every relationship being declared by hand. | **P3** | **half delivered** — see below |

### Why those priorities

- **UC-B is P0** because getting a destroy order wrong destroys or orphans real
  resources, and the rubric's first test is whether something bad and
  hard-to-reverse happens unnoticed.
- **UC-A, UC-C, UC-E and UC2 are P1**: each is about the tool being *correct* or
  *reviewable*. A wrong create order fails loudly; an under-reporting plan gets
  approval for a change the user did not see; a re-run that compounds damage turns
  one failure into two.
- **UC-D is P2** rather than P1 because nothing malfunctions without it — an
  operator is merely unaided. It is the only use case with no implementation at all.
- **UC1 is P3** because Phase 2 already delivers its useful half, and the
  remaining half was assessed as marginal: see `specs/dependency_detection.md`.

### Two of these need their scope stated precisely

**UC1 is half delivered.** Writing a reference implies its edge, so a user who
expresses a relationship as `${provider.type.name:attribute}` never declares it
separately. What is missing is inference from a value *not* written as a reference
— a literal the user pasted. Phase 3 would have delivered that and is paused by
decision (#220).

**UC2 is not really an override.** It was framed as correcting what UC1's detection
missed, but detection does not exist, so `depends_on:` is one of the two ways an
edge comes into existence rather than a correction to the other.

### A note on how these were written

UC1, UC2 and UC3 were originally phrased as capabilities — "I want the system to
automatically know…" — which names a mechanism and cannot be tested: no run proves
or disproves it. UC-A through UC-E restate the same intent as outcomes a run either
has or does not have, which is what the `D`-requirements in
`specs/resource_dependencies.md` are derived from. The original phrasings are
preserved above in the "What must be true" column rather than kept as a second
list.

## Requirements implied by the use cases

**R1 — State integrity under concurrency (single instance, single
machine).** Parallel execution (UC3) means concurrent writers to `aiform`
state. State reads/writes must stay correct and non-corrupting regardless
of how many resources are being created/updated/destroyed at once —
**scoped to concurrency within one `aiform` process on one machine**
(e.g. threads/tasks inside a single `apply` invocation). Two separate
`aiform` invocations running concurrently — same machine or different
machines — are explicitly out of scope for this phase; see "Concurrency
scope" below.

**R2 — Concurrency execution framework.** Supporting UC3 requires
`aiform`'s execution engine to actually run independent work concurrently
(today `apply_plan()` is a strictly sequential loop) — not just tolerate
concurrency if it happened to occur.

**R3 — Idempotency & restartability of failed actions.** When a
concurrent create/update/destroy action fails partway, it must be
possible to recover: re-running should resume or retry cleanly rather
than re-running blindly into a resource that's actually half-created, or
requiring manual state surgery.

**R4 — Durable, atomic state storage (contingent, scope-narrowed).** We
may need a durable layer (e.g. a database) so that atomicity/integrity of
state writes hold under concurrent access. Per the concurrency-scope
decision below, "concurrent access" here means **multiple threads/tasks
within one `aiform` process on one machine** — not multiple `aiform`
processes, and not multiple machines. R4 should be evaluated against that
narrower bar, not against distributed-system requirements.

> Flag: even narrowed to single-process concurrency, R4 is a materially
> larger architectural change than R1–R3. Today, state is a single
> `.aiform/state.json` file (+ `.backup`), replaced wholesale on every
> save — a deliberate MVP choice (`CLAUDE.md`'s State handling rules),
> not an oversight. Moving to a DB introduces a new runtime dependency, a
> migration story, and an operational burden `aiform` doesn't have today.
> This PRD does not decide R4 either way — it's carried to the design
> pass as an open question, to be evaluated only once R1–R3's actual
> single-instance concurrency model is chosen (an in-process lock may
> turn out to be sufficient — see "Open questions" below).

## UX requirements

**UX1 — Textual dependency display in the CLI.** The CLI must show
resource dependencies in a textual form — which resources depend on which,
and, *if* UC1 ever lands fully, why an edge exists. Every edge a user sees
today is traceable to something they wrote — a `depends_on:` line or a
reference — so "why" is answered by their own config, and provenance only
becomes load-bearing once an edge can come from neither. This is a
prerequisite for UC2,
not a nicety: a user cannot correct a dependency they cannot see, and
`plan` output that silently reorders resources without showing the graph
it derived is not reviewable. Ships in Phase 1, alongside the graph
itself.

**UX2 — Graphical dependency visualization.** Because the resulting
dependency model will be non-trivial, users will eventually also want a UX
that graphically shows resources and the relationships between them. This
is the richer successor to UX1, not a replacement — the textual form stays
the reviewable default inside `plan` output.

Neither UX1 nor UX2 covers a standalone command that prints the dependency
graph on its own, outside of a `plan` run — UX1 is scoped to the line
inside `plan` output, UX2 to a graphical form. Phase 7 (below) picks up
that gap alongside UX2's original graphical scope.

## Additional requirements

- **Reference mechanism.** UC1/UC3 both presuppose *some* way for a
  resource's config to actually reference another resource's attribute
  (e.g. "this DNS record's `data` is that compute resource's
  `ipv4_address`"). Phase 1 delivers the *declaration* half (naming
  another resource); Phase 2 delivers *value flow* (reading its
  attributes).
- **Cycle detection.** A dependency graph needs an explicit plan-time
  error on a cycle — never a silent wrong-order apply.
- **Destroy-order semantics.** Destroy runs in reverse dependency order.
  Refusing a destroy that would orphan a still-tracked dependent is a
  separate, harder problem — see Phase 4.
- **Partial-failure semantics for a graph apply.** If resource B fails
  after resource A succeeded, what does state look like, what does the
  user see, and how does a re-run recover cleanly (ties directly to R3)?
  Today the exception escapes `apply_plan()` and the `ApplyResult` is
  lost; Phase 4 owns this.
- **Cost/model-tiering constraint.** `CLAUDE.md`'s non-negotiable rule:
  a repeat `plan`/`apply` on unchanged input must make zero Anthropic API
  calls. Dependency resolution must be fully deterministic — no LLM call
  on the graph path, in any phase. This binds UC1's automatic detection
  should it ever be built: edges must derive from driver-declared metadata,
  never a model call on the hot path. `specs/dependency_detection.md` treats
  an LLM-inferred edge as permanently excluded rather than deferred, since it
  would also be nondeterministic across runs.
- **Backward compatibility — not required at all.** See
  "Non-requirements" below; it is stated there rather than here because
  it governs what we deliberately will *not* spend effort on.
- **Concurrency scope — decided.** This phase addresses only concurrency
  *within a single `aiform` process on a single machine* (e.g. threads/
  tasks inside one `apply` invocation). It explicitly does **not** support
  two or more separate `aiform` invocations running concurrently against
  the same state — whether both on one machine (two terminals) or across
  machines (e.g. a CI job overlapping a local run). The behavior of doing
  so is **undefined** for now — not guarded against, not guaranteed to
  fail loudly, just unaddressed. Multi-invocation/multi-machine
  concurrency is fully anticipated as a future phase, not a rejected
  idea — it's out of scope now specifically so R1–R4's design isn't
  forced to solve distributed coordination before single-instance
  concurrency is even working.
- **Deployment scope — decided, and unchanged by any phase here.** A
  dependency graph lives inside **one deployment**, and a deployment is the
  directory `aiform` runs from: `discover_files()` globs `cwd`
  non-recursively, and both `.aiform/state.json` and
  `.aiform/credentials.env` are relative paths. Two directories with their
  own state files are two independent deployments, and a `depends_on`
  target in another one's state is simply unknown. That is not a deferred
  capability — there is no ordering `aiform` could enforce between two
  separate invocations, so cross-deployment dependencies are not a thing
  this model has.

  What *is* deferred, filed as **#201**: the isolation is entirely
  positional. Nothing records which deployment a state file belongs to, so
  `aiform plan destroy` with no file arguments, run from the wrong
  directory, looks exactly like the run the user intended. Out of scope for
  the phase sequence below, and tracked separately.
- **Explicit non-goals for v1** — moved to "Non-requirements" below.

## Non-requirements

Things this project deliberately will **not** spend effort on. They are
listed as first-class requirements because a non-requirement is a decision
with the same force as a requirement: it licenses work being left undone,
and without it written down someone re-derives the obligation and pays for
it.

Distinguish two kinds. A **non-requirement** is something we will never
owe. A **deferred item** is something we will owe later — those live in
"Delivery phasing" and "Open questions", not here.

### Backward compatibility, in every form

**There are zero resources in production and no users but the repo owner,
so nothing in this project owes compatibility with anything it shipped
earlier.** That covers, non-exhaustively:

- **The `.aiform.md` file format.** Breaking it is acceptable if the design
  calls for it. Update the system-test and unit-test generators to match
  rather than preserving the old format for its own sake.
- **The `.aiform/state.json` schema.** A phase may change the shape of
  state, rename or remove a field, or change what a field means, without
  providing a migration. `aiform_state_version` exists and is round-tripped
  but nothing reads it, and that stays true until there is a reason it
  should not be. Phase 5's durable-store question (R4) is the most likely
  place this matters — it does **not** owe a migration from today's JSON
  file.
- **CLI flags, output shape and exit codes.** `--json` output in particular
  is the closest thing `aiform` has to an API, and it is still free to
  change.

**What this does not license.** Two distinctions worth keeping sharp,
because conflating them is how a real defect gets waved through:

1. **A resource already tracked in state is not a legacy artifact.** The
   common case is a resource created *by the current version*, whose
   `.aiform.md` is then edited. Handling an edit to a tracked resource is
   ordinary forward behavior, not a migration, even though both involve
   reading a state entry written by an earlier run.
2. **"No migration owed" is not "state may be silently wrong."** A change
   may require the user to destroy and recreate, or to delete
   `.aiform/state.json` and start over — those are acceptable costs. A
   change that leaves state *quietly disagreeing* with the files, and
   therefore produces a wrong plan, is a defect regardless of this section.

### Concurrent `aiform` invocations

Two or more separate `aiform` processes against the same state — whether
two terminals on one machine or a CI job overlapping a local run — are
**undefined**, not guarded against and not guaranteed to fail loudly. This
is the one item here that is *also* anticipated as a later phase, so it sits
on the boundary: not required now, not rejected forever. See the
concurrency-scope decision above.

### Multi-cloud dependency graphs, and remote or shared state backends

Per `PLAN.md` §10 and `CLAUDE.md`'s "don't build for a hypothetical future"
rule. A single-provider graph on a local state file is the target.

### Cross-deployment dependencies

Not deferred — **the model has no such concept.** A graph lives inside one
directory's state file; see the deployment-scope decision above. There is no
ordering `aiform` could enforce between two separate invocations, so this is
not a capability being postponed.

## Delivery phasing

This is a critical, load-bearing change to the core of `aiform` — the
planner, the execution engine, and state. It ships as a sequence of
separate, individually-validated phases, **not** as a stack of parallel
PRs.

### Delivery rules

- **One phase per PR. One PR in flight at a time.** No stacked PRs, no
  parallel branches, no concurrent implementation agents working
  different phases.
- **A phase is done when it's merged**, not when its code is written: full
  `PROCESS.md` loop per phase (spec in `specs/` → tests red → green →
  `/code-review` on Opus 5 or newer → human merge approval).
- **Every phase leaves `aiform` working end to end.** No phase may land
  the system in a half-migrated state that only the next PR repairs.
- **Live validation before the next phase starts** — each phase gets a
  real `plan`/`apply` run against DigitalOcean (scoped to the aiform tag,
  with the usual cleanup discipline), not just green unit tests.
- **A phase that turns out to be wrong stops the sequence.** Re-plan and
  get approval again rather than patching forward into the next phase.

### Phases

**Phase 0 — Orchestrator readability refactor. Behavior-preserving.**
*(SHIPPED — PR #199, merged 2026-09-24. Outcome: `build_create_plan`
205 → 35 lines, `apply_plan` 187 → 85; 14 new private helpers; zero
existing test assertions changed; live suite 14/14 twice. The text below
is the contract as written beforehand, kept because several of its
guardrails still bind Phase 1.)*

Break `aiform/orchestrator.py`'s overlong functions into smaller ones a
human can review a screen at a time. `build_create_plan()` (~180 lines)
and `apply_plan()` (~190 lines) are the two targets; both carry several
distinct responsibilities in one body, which is why reviewing a change to
either means re-reading the whole thing.

**The logic is identical. This phase changes no behavior at all.** That is
the whole contract, and it is what makes the phase safe to do before the
feature work rather than after.

Placed first, ahead of Phase 1, for three reasons:

1. Phase 1 restructures `build_create_plan()` into two passes. Doing that
   inside an already-overlong function and refactoring afterwards is two
   rounds of churn on the same code, and makes Phase 1's diff harder to
   review at exactly the point it most needs review.
2. A refactor's safety argument is only legible in isolation: "logic
   identical, no test assertions changed, suite green, live suite green."
   Bundled with or after feature work, a refactor bug and a feature bug are
   indistinguishable.
3. The benefit compounds — every later phase's diff is reviewed against the
   smaller functions.

**Guardrails, because this module's invariants are subtler than it looks**
(the review of the #195 fix found a reproduced regression in a two-line
change here):

- **No test assertion may change.** Mechanical updates only — an import, a
  renamed symbol. If a behavioral assertion has to move, the refactor
  changed behavior: stop and re-plan rather than editing the test.
- Preserve all three zero-Anthropic-call guards exactly: `planner.py`'s
  short-circuit, the untracked-resource branch that skips
  `parser.parse_file()` entirely, and `parser.py`'s sha-match skip.
- Preserve the **in-place** mutation of `state_entry` and its object
  identity as threaded into `PlannedResource` — several behaviors depend on
  the plan's entry and the saved state being the same object.
- Preserve the deliberate asymmetry that `driver.update()` is called
  outside `_call_driver()` so `DriverUpdateNotSupported` escapes unwrapped,
  and every other driver call goes through it.
- Preserve the single trailing `state.save()` in `build_create_plan()` and
  the per-resource saves in `apply_plan()`. Batching vs. eager is load-
  bearing, not incidental.
- Preserve exception types, messages, and the order the structural
  cross-checks fire in.

Verification: full suite green with unchanged assertions; `ruff` clean; the
live suite (both changed files are runtime paths, so `system-test` cannot be
recorded N/A); and a review pass that walks each extracted function against
the original block it came from rather than reading the result on its own.

**Phase 1 — Declaration, ordering, cycle detection, textual display.**
*(SHIPPED — PR #204, merged 2026-09-25. Spec at
`specs/resource_dependencies.md`.)*

An explicit `depends_on:` frontmatter field, a deterministic dependency
graph, topological ordering of the plan (dependencies created before
dependents, destroys in reverse, including the destroy-all-from-state
path), cycle detection as a plan-time error, and CLI output showing the
edges. Delivers UC2 and UX1.

Two properties worth stating because neither is implied by "a graph
exists", and the second is easy to misread once one does:

- **A resource may declare any number of dependencies.** Fan-in is a
  first-class case, covered through the ordering, the display, what state
  persists, and the live validation — not a later widening of a field
  that happens to be a list.
- **Execution stays strictly sequential.** The order Phase 1 produces is
  *total*, not a set of independent batches: two resources with no edge
  between them are still applied one after the other, deterministically.
  Identifying what could run concurrently, and then doing so, is Phase 6.

Phase 1 needed no prerequisite work in the end. Its design named the
"record the `.aiform.md` hash on a no-op" fix as one; that landed
independently as issue #195 / PR #196 before this phase started.

> Note the deliberate inversion: **UC2 (manual declaration) ships before
> UC1 (automatic detection).** Explicit declaration is the foundation any
> auto-detection would populate — building detection first would mean
> inferring edges with nothing to feed them into. That ordering turned out to
> matter more than expected: UC1's remaining half is now paused
> (`specs/dependency_detection.md`), so had the inversion gone the other way,
> the project would have built inference and still had no way to declare an
> edge by hand.

**Phase 2 — Cross-resource attribute references.** *(SHIPPED — PR #217,
merged 2026-09-27 as `15cbcb6`, closing #215, spec at
`specs/resource_references.md`; one known limitation tracked as #216.)* One resource's output
attribute flowing into another's `params` — the canonical DNS-record-
pointing-at-a-droplet-IP case. Depends on Phase 1's graph. Still
sequential execution.

Open question 1 below is answered by this phase:
`${provider.resource_type.name:attribute}`, a colon rather than a fourth
dot because a resource `name` may legally contain dots. Writing a
reference implies the dependency edge. Two decisions worth recording
because neither is implied by "references exist":

- **A reference is not every `${...}`.** Only one whose content holds a
  colon whose left side parses as a real resource key. Shell parameter
  expansion in a params value — `${HOME}`, `${PORT:-8080}` — is an
  anticipated case, since `compute.PARAM_SCHEMA` is
  `additionalProperties: True` and `parser.py` already accommodates a
  cloud-init `user_data: |` block scalar. The dot-instead-of-colon typo is
  still refused, by a check those literals cannot reach.
- **References into integer-typed fields do not work yet.** The
  firewall's `droplet_ids` is typed `integer` while `compute`'s `id` is a
  string, so a reference there resolves to a value its own validation
  rejects. Filed separately rather than solved with a cast syntax.

**Phase 3 — Automatic dependency detection (UC1). PAUSED BY DECISION — see
`specs/dependency_detection.md`.** The mechanism is unchanged from what this
phase always described: infer edges from driver-declared reference metadata,
using `specs/digitalocean_firewall.md`'s "Resource graph" table as the prior
art for what an edge looks like, producing the same edges Phase 1 already
consumes so the ordering engine doesn't change, and staying deterministic —
no LLM call on the plan hot path.

What changed is the decision to build it now. A design pass (issue #220)
found **one** inferable edge in the entire driver set — the firewall's
`droplet_ids` naming a droplet — which provably cannot change create
ordering and carries no destroy-order failure mode.

That edge is also the one #216 blocks from being written as a reference at
all, which is the reason for the ordering here: **fix #216 first, then
reassess.** #216 is a prerequisite for Phase 3 either way — inference over a
reference mechanism that cannot express the edge would be building on a
known-broken foundation — and fixing it may remove the need for detection
entirely, since a working reference implies its own edge. Whether Phase 3 is
still worth doing is a question to answer from what #216 teaches, not before
it.

`specs/dependency_detection.md` holds the evidence, the answer to open
question #3 below, and the conditions that reopen this. **The phase keeps its
number while paused** — Phases 4-7 are not renumbered, and nothing else in
the sequence moves.

**Phase 4 — Orphan refusal, partial-failure recovery, restartability
(R3).** Refusing (or explicitly forcing) a destroy that would orphan a
still-tracked dependent, plus making `apply_plan()` survive a
mid-sequence failure with a well-formed result and an idempotent re-run.
Deliberately **before** concurrency: failure semantics are hard enough to
get right serially, and concurrency multiplies the failure modes rather
than creating them.

**Phase 5 — Concurrency-safe state (R1, and the R4 decision).** Make
state reads/writes safe under concurrent mutation within one process, and
resolve R4 (in-process lock vs. durable store) with evidence from Phases
1–4. Lands **before** parallelism is switched on, so the safety mechanism
exists and is tested before anything stresses it.

**Phase 6 — Parallel execution (UC3, R2).** Actually run independent
resources concurrently. Last of the engine work because it is the riskiest
piece and it depends on every phase above being solid.

**Phase 7 — Dependency graph display beyond `plan` output (UX2, widened).**
UX1 only ever covered the `depends on:` line *inside* `plan create`/
`apply`/`destroy` output, and UX2 as originally scoped is specifically
*graphical* — so a standalone command that prints the dependency graph on
its own (not attached to a plan run) fell in the gap between the two and
was never actually covered by either. Phase 7 now covers both forms: a
textual/CLI graph display as the cheaper first deliverable, and the
graphical visualization as its richer successor. Additive, lowest risk,
touches no execution path — and no longer urgent for either form, since
UX1's textual display shipped in Phase 1.

## Open questions for later phases

Answered for Phase 1 in `specs/resource_dependencies.md`; still open
beyond it:

1. ~~How does a resource reference another's *attribute values* (Phase 2)
   — an interpolation syntax in `params`, a separate reference block, or
   something else?~~ **Answered by Phase 2:** an interpolation syntax in
   `params`, spelled `${provider.resource_type.name:attribute}`. See
   `specs/resource_references.md`.
2. Does the file-per-resource model survive, or does a multi-resource file
   format become worthwhile once graphs get large?
3. What single-instance concurrency model satisfies R1–R3, and does it
   require R4 (a durable store) or does an in-process lock suffice?

**Answered since.** *What does a driver declare so Phase 3 can infer edges —
a new class attribute alongside `PARAM_SCHEMA`, or metadata inside it?* A
fifth declarative class attribute, `REFERENCE_FIELDS`, not metadata inside
`PARAM_SCHEMA` — because `PARAM_SCHEMA` is passed verbatim to the
intent-orchestration model and aiform-private keys inside it change a prompt
payload for no model benefit. Designed but not built, since Phase 3 is
deferred: see `specs/dependency_detection.md`'s "The declaration contract".

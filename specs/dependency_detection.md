# Detailed Design Specification for Resource Dependency Detection

Would span `aiform/driver.py`, `aiform/orchestrator.py` and every driver.

Closes #220. Phase 3 of `specs/MULTI_RESOURCE_PRD.md`.

## Introduction

This document contains the detailed requirements, interfaces, algorithms, and
design decisions for the implementation of automatic dependency detection.

**Build status.** **Not built, and paused by decision** — not pending, not in
progress, not next. This file is the decision record and the contract a
future Phase 3 starts from. Detection is not implemented: no code path infers
an edge from a literal value. The contract is partly built:
`aiform/driver.py` has the fifth declarative class attribute,
`REFERENCE_FIELDS`, with `path`, `target` and `id_type` (the destroy repair
reads them); the `attribute` column is not built, and only
`digitalocean/firewall` declares anything. The
named prerequisite, #216, **is fixed** — the pause itself is unchanged by
that alone; reassessing whether Phase 3 is still worth building is what
#216 being fixed makes due, and that reassessment is the repo owner's call,
not made in this edit.

| Piece | State |
|---|---|
| The decision to pause | made, recorded here |
| `REFERENCE_FIELDS` contract (PRD open question #3) | **partly built**: `path`, `target` and `id_type` exist and are read by the destroy repair; `attribute` is not built |
| `REFERENCE_FIELDS` on any driver | built for `digitalocean/firewall` (three entries, `id_type` `"integer"`); no other driver |
| Edge inference from literal values | not built |
| The firewall-deletion probe | named here, not run |
| #216, the named prerequisite | **fixed** (`plans/fix-216-reference-into-integer-field.md`) — prerequisite met, reassessment due |

## Purpose

Record what automatic dependency detection would be, what it would be worth,
and why the decision was "not before #216 is fixed" — so the question is
settled with evidence rather than re-derived, and so a future session
inherits a decided declaration contract rather than three sentences of PRD
text. #216 is now fixed; see "Conditions that reopen this" for whether that
condition is met.

## Use cases

`MULTI_RESOURCE_PRD.md`'s **UC1 — automatic dependency detection**: a user
wants `aiform` to know that one resource depends on another, and why, without
declaring every relationship by hand.

UC1 is **half delivered, and the other half is paused.** Phase 2
(`specs/resource_references.md`) made a reference imply its own edge — so a
user who expresses a relationship as `${digitalocean.compute.web-01:ipv4_address}`
never declares it separately, and `aiform` derives the edge without being
told. What remains is inferring an edge from a value that is *not* written as
a reference: a literal the user pasted. That residue is what this spec is
about, and the whole question is how much of it is real.

## The decision

**Do not build automatic detection now. Fix #216 first and reassess from what
that teaches.** The grounds are narrow and specific to today's driver set.
**#216 is now fixed** — see "Conditions that reopen this" for what that does
and does not settle; the pause itself stays in force until the repo owner
reassesses.

### #216 was the prerequisite, and it reframed the whole question

`droplet_ids: ["${digitalocean.compute.web-01:id}"]` did not work
(#216, now fixed via a second attribute — see below). Understanding *why* is
what settled the ordering of this phase, and the intuitive reading was
wrong.

It is **not** a limitation of references. `aiform/references.py:267-272`
handles a whole-value reference by returning the attribute's own Python
object: "the attribute's own type survives — an int stays an int, a list
stays a list. This is what lets a reference feed a non-string field." So the
grammar is not where the type is being lost.

The string comes from the other end. `orchestrator.py:94` pops `"id"` out of
the attributes a driver returns and moves it to `StateEntry.id`, which
`aiform/models.py:222` types as `str` — necessarily, since it is `aiform`'s
primary key and has to be uniform across every provider and resource kind.
`orchestrator.py:104`'s `referenceable()` then merges that string back into
the reference namespace under the name `id`. So `compute.py:205`'s
`str(droplet["id"])` is not a driver quirk; it is satisfying the identity
contract `PLAN.md` §4 imposes.

**`id` was doing two unrelated jobs under one name:** `aiform`'s identity
token, legitimately a string; and a provider attribute a user wants to
reference, whose native type is whatever the CSP says — an integer for a
DigitalOcean droplet. A reference names an `aiform`-tracked *object*, and
`:attribute` already expresses which value inside that object to hand the
provider. The repo owner's stated direction followed from that reading: make
the right attribute available with the right type, rather than add syntax. Be
precise about what the type analysis alone established — a cast would also
have worked mechanically for this edge, so preferring the attribute was a
design judgement, not a consequence. #216's chosen fix
(`plans/fix-216-reference-into-integer-field.md`) added `provider_id`: a
second, native-typed attribute beside the unchanged string `id`, on
`compute` only. `id` itself was not changed, and no cast syntax was added to
the reference grammar.

Two consequences for this phase:

- **#216 was a prerequisite for Phase 3 regardless of the outcome.** Building
  inference over a reference mechanism that cannot express the one edge that
  exists would be building on a known-broken foundation.
- **Fixing it removes the need for detection for this edge**, since a
  working reference implies its own edge already, and `droplet_ids` now has
  one via `provider_id`. That is the specific thing this reassessment is
  about — see "Conditions that reopen this."

A Phase 2 defect was not an argument *for* Phase 3. It was an argument for
fixing Phase 2, which is what happened.

### The edge inventory

Every reference-shaped field across the three drivers. Graded `inferred`
rather than `verified`: the fields are read from source, but whether the list
is *complete* is a judgement about what counts as reference-shaped, and an
earlier draft of this table missed one row and misclassified another.

| Field | Points at | Match kind | Verdict |
|---|---|---|---|
| `droplet_ids` (`firewall.py:83`), `sources`/`destinations.droplet_ids` (`firewall.py:52`) | a droplet | **id**, integer vs. `StateEntry.id`'s string | **The one id-match edge.** Was the edge #216 blocked from being a reference; reachable now via `provider_id` (`compute.py:210`) for top-level `droplet_ids` and, since #224, for the nested field with any number of references (`specs/digitalocean_firewall.md`) |
| `tags` (`firewall.py:84`), `sources`/`destinations.tags` (`firewall.py:53`), `compute.tags` (`compute.py:173`) | a droplet, via its `tags` attribute (`compute.py:215`) | **attribute** | A *user-chosen* value, writable before the droplet carrying it exists — though the **tag itself** must already exist, or the create 422s `"tag <name> does not exist"` (`specs/digitalocean_firewall.md:198-202`). `compute.py:215` declares `tags` referenceable, so Phase 2 references work here |
| `addresses` (`firewall.py:51`) | a droplet, via `ipv4_address` (`compute.py:216`) | **attribute** | Same class as `records[].data`. Missed by this table's first draft |
| `records[].data` (`domain.py:103`) | a droplet, via `ipv4_address` | **attribute** | Phase 2's canonical case; references work |
| `ssh_keys` (`compute.py:170`) | a DigitalOcean SSH key | id | No `ssh_key` driver exists; no node to point at |
| `load_balancer_uids`, `kubernetes_ids` (`firewall.py:54-55`) | a load balancer, a k8s cluster | id | No drivers. The k8s edge was never probed (`specs/digitalocean_firewall.md:318`) |

`specs/resource_tagging.md`'s marker tag contributes no edge: it is a fixed
constant, never a lookup key, and explicitly invisible to the diff engine.

Two corrections worth recording, because the wrong versions are the intuitive
ones. The `tags` rows do **not** point at a "tag resource" — there is no such
driver, but that is beside the point, because what a user references is the
*droplet carrying the tag*. And a tag is user-chosen, so it is writable before
its referent exists. An earlier draft asserted that no such field exists in
any current driver; that was false, and it mattered, because that field type
is the one the create-ordering argument below cannot cover.

Where the metadata already lives, since it shortens a future Phase 3:
`firewall.py:40-42` carries a comment stating that every rule target key *is*
a reference to another resource kind, and
`specs/digitalocean_firewall.md:308-324` tabulates the edges with id types and
pre-existence evidence, probe by probe. Phase 3's real work is moving that
from prose into something the planner can read.

### The id-match edge cannot change create ordering

A droplet's id is assigned by DigitalOcean at creation, and the API refuses
an unknown id with a 422 (`specs/digitalocean_firewall.md:316`). So a literal
in `droplet_ids` was written after a prior successful apply of that droplet.
In the common case the droplet is therefore already tracked and its action is
`NO_OP`, and `apply_plan()` skips `NO_OP` before any driver call
(`orchestrator.py:1095`) — ordering a `CREATE` against a `NO_OP` is inert.

The premise does not hold universally, and the honest statement of the
conclusion does not need it to. If state was lost, the config directory was
copied to a fresh machine, the droplet drifted missing, or the droplet was
created outside `aiform`, then the literal is either stale or names nothing
this run knows about. In every one of those cases no ordering helps: the
value is wrong, and it fails under any order. So: **either the target is a
`NO_OP`, or the literal is stale and no ordering saves it.**

Same for a replace. A droplet replaced in the same run leaves the firewall
holding the old id, and a whole-object `PUT` carrying it would presumably 422
mid-apply under either order — presumably, because that is the unrun probe's
second question below, inferred from transcript `21-`'s 422 at *create* rather
than observed on an update. Either way it is a failure no ordering fixes, not
an ordering hazard.

**This argument covers id-match fields only.** It works because the id is
provider-assigned. It says nothing about the `tags` and `addresses` rows,
whose values are user-chosen and writable ahead of their referents. For those,
Phase 2 references already produce the edge, which is why the argument does
not need to stretch.

### Destroy ordering has no failure mode for this edge

`_build_destroy_plan_from_state()` runs a real topological sort over
`StateEntry.depends_on` (`orchestrator.py:971`, `:975`); it degenerates to
reverse-lexical only when there are no edges at all.

In that zero-edge case the order cannot currently break anything, by naming
accident rather than design. `graph.topological_order()` drains a heap of raw
key strings (`aiform/graph.py:56-66`), and those keys are
`provider.resource_type.name` as `resource_key()` formats them
(`orchestrator.py:45`) — so plain lexical string order happens to compare
`resource_type` before `name`, and with `"compute" < "domain" < "firewall"`
every firewall is destroyed before every droplet, for any pair of names.
**Single-provider only**: the provider segment sorts first, so this holds
because there is exactly one provider today.

It flips the moment a resource type sorting after `firewall` arrives — a
`load_balancer` driver, whose edge `specs/digitalocean_firewall.md:317`
already documents. But it flips into a *hazard* only for a resource that
actually breaks when its referent disappears, and a firewall does not:

> **A firewall does not break when a droplet in it is removed.** A firewall
> can also exist ahead of the droplets it targets *by tag*, with the
> configuration inert until matching droplets exist.

Owner-reported, not probed — see "Knowledge-confidence". The second half is
scoped to tag targeting deliberately: it cannot be true of `droplet_ids`,
which 422s on an id that does not exist yet.

The question `specs/digitalocean_firewall.md`'s "Resource graph" section left
open is now **answered** there, by this PR's probes: the reference does not
shrink, and the firewall reports itself converged anyway.

### An inferred edge would cost more than it pays

Inferring the id-match edge has two costs and no observable benefit.

**It would start refusing plans immediately.** Not a Phase 4 hypothetical:
`_resolve_dependency_edges()` (`orchestrator.py:409`) already raises
`PlanBlockedError` when a live resource's target is delete-marked in the same
run. An inferred `firewall → droplet` edge would therefore refuse a
`plan create` that removes the droplet — today — for a removal that does not
in fact break the firewall.

**It is a worse mechanism than the one it substitutes for.** A working
reference gives a live, typed value: replace the droplet and the reference
re-resolves. A detected literal is a stale-able copy — when the id changes the
literal rots, and detection then finds *no* edge at all, which is quietly
wrong rather than absent.

Whether a block or a warning is the right response to a stale literal is a
genuine judgement, not settled here. There is a real case for saying
something: the config *is* wrong, and this spec's own edge cases note that
silence about a rotted literal is worse than noise. What is not defensible is
refusing a safe destroy on an edge the user never asserted. A declared edge
honors an instruction; an inferred one asserts a relationship nobody claimed.
**Phase 4 has since partially shipped (#225, `1ed84bf`), and it distinguishes
the two by construction rather than by an explicit design decision this spec
worried was missing:** `_reverse_dependents()` refuses only on a tracked
resource's *persisted* `StateEntry.depends_on` — the union Phase 1's
`depends_on:` and Phase 2's references already produce — and since Phase 3
inference still doesn't exist, there is no inferred edge for it to refuse on
yet. The distinction this paragraph asked for holds today only because one
side of it is still unbuilt, not because Phase 4 chose it; if Phase 3 is ever
built, Phase 4's refusal would need deciding whether an inferred edge
qualifies too, and nothing shipped in #225 answers that.

## The declaration contract

This answers `MULTI_RESOURCE_PRD.md`'s open question #3 — *what does a driver
declare so Phase 3 can infer edges, a new class attribute alongside
`PARAM_SCHEMA` or metadata inside it?* Partly built, see the status table above.

**A fifth declarative class attribute, `REFERENCE_FIELDS`**, alongside the
four in `aiform/driver.py` (`PARAM_SCHEMA`, `LIKELY_REPLACE_FIELDS`,
`NON_DIFFABLE_FIELDS`, `UNORDERED_FIELDS`) — not metadata
inside `PARAM_SCHEMA`. Three reasons:

- **`PARAM_SCHEMA` is a prompt payload.** It is passed verbatim to the
  intent-orchestration model (`aiform/planner.py:122`,
  `prompts/diff_plan.md:15`), which is told to reason about its types. Adding
  aiform-private keys changes what the model sees for no model benefit.
- **The existing four establish one concern, one attribute.** Each is a flat
  declaration the base class reads and a driver reassigns rather than mutates.
- **`PLAN.md` §4's contract is stability-critical.** An additive sibling
  leaves the four untouched; mutating a shared schema makes every driver's
  `PARAM_SCHEMA` load-bearing for a second purpose.

**Shape.** Each entry names a field path, the resource kind it points at, and
which value inside that object the provider wants:

- **field path**, including nesting — `droplet_ids` and
  `inbound_rules[].sources.droplet_ids` are different paths to the same kind.
- **target `provider` and `resource_type`**, so the match is against a
  resource key rather than a guess.
- **which attribute the value equals** — and this is the same question #216
  raised from the other direction. A reference says *which attribute of the
  object to read*; `REFERENCE_FIELDS` would say *which attribute a literal
  here would have come from*. Both need the attribute to exist with the right
  type, which is why #216 was upstream of this design and not merely adjacent
  to it — for `droplet_ids`, that attribute is now `provider_id`.

`path`, `target` and `id_type` are built (`ReferenceField` in
`aiform/driver.py`, `specs/driver.md`) because the destroy repair reads exactly
those: it needs to know where a driver names another resource's id, which
resource that is, and how the id is typed (`"integer"` or `"string"`, the
JSON-schema type of one id). `attribute` is not built, because nothing would
read it until detection exists.

Note `PLAN.md` §4 still omits `UNORDERED_FIELDS` from its declarative-attribute
list (#133), though §4 now lists `REFERENCE_FIELDS`. The gap is that one
missing attribute.

### The declaration has three uses, and generation is the most expensive

Once a driver declares that a field holds references to a resource kind, matched
on a named attribute, a literal in that field stops being an opaque string. Three
things become possible, and this spec had only reasoned about the first:

| Use | What it does | Confidence it must clear |
|---|---|---|
| **Generate** | create an edge from a matched literal | high — a wrong edge changes ordering and can refuse a destroy |
| **Lint** | "you hardcoded an id matching a tracked resource; did you mean a reference?" | low — a false warning costs attention |
| **Verify** | check a declared `depends_on` against what the field's value actually resolves to, and prompt | low — a false prompt costs a question |

**Verification is the cheapest and it catches what nothing else can.** Generation
must be trusted enough to *create* an edge; verification only has to be trusted
enough to *ask*. It clears the lint's low bar while addressing a class of error the
lint cannot see — not a literal that should have been a reference, but a
**declaration that disagrees with the configuration it describes**.

### A wrong `depends_on` is worse than none

Recorded because nothing in this repo said it, and it is the case verification
exists for. Suppose a user writes:

```yaml
depends_on: [digitalocean.compute.web-01]
params:
  droplet_ids: [<web-02's id>]
```

`_dependency_targets()` returns the declared target only — the literal contributes
nothing — so:

- the ordering graph gains an edge to `web-01` that buys nothing;
- the **real** dependency on `web-02` is absent, so a destroy can tear `web-02`
  down before the firewall that points at it;
- and any future blast-radius answer is wrong in both directions.

All silently, and undetectably by anything that exists today. A user who declares
nothing at least gets alphabetical order and no false confidence; a user who
declares the wrong thing gets a graph that is confidently wrong.

That is the strongest available argument for the declaration, and it is independent
of whether generation is ever built: the same metadata that would infer an edge can
check one, and checking is the cheaper half.

## Where detection would run, and what it would cost

`MULTI_RESOURCE_PRD.md`'s "the ordering engine doesn't change" is true. The
pass in front of it is not.

`_order_files()` (`orchestrator.py:447`) is called at `orchestrator.py:474`,
before the driver cache is built at `:476` and before any credential
resolution. That ordering is what makes the pass free: pure YAML, string and
tree work, no driver import, no CSP call, no model call. Phase 2 fits inside
it because a reference is visible in the *text* of `params` —
`references.reference_targets()` (`aiform/references.py:154`) needs no driver.

Detection is different: it needs the driver's `REFERENCE_FIELDS` to know that
an integer in `droplet_ids` means a droplet at all. Three options, none free:

- move the driver load ahead of the ordering pass;
- add a second pass after it;
- read `REFERENCE_FIELDS` by AST without importing, as `driver_gen.py`
  already does for drivers.

The first two perturb a pass whose zero-cost property is pinned by
`tests/test_orchestrator.py:1699`
(`test_unchanged_dependency_graph_makes_zero_llm_calls`) and `:2004`
(`test_cycle_raises_plan_blocked_error_before_driver_load_or_llm_call`) —
whose *name* is the invariant, asserting the cycle check precedes any driver
load. A future Phase 3 must say which option it takes and re-establish the
invariant, not discover the conflict mid-implementation.

## Edge cases a future implementation must answer

None is resolved here.

- **A literal matching more than one resource.** An attribute match (a tag, an
  IP) can match several; detection must refuse or pick deterministically,
  since a nondeterministic edge breaks Phase 1's determinism requirement.
- **A literal matching a resource in state but not in this run.** Declared
  edges resolve a state-only target to no edge. An inferred edge must follow
  the same rule or the two mechanisms disagree.
- **A stale literal after a replace.** The id matches nothing, so detection
  silently finds no edge where the user believes one exists. Silence here is
  worse than an unhelpful edge — and this is no longer hypothetical: the
  provider is now **verified** to keep the dead id while reporting the resource
  converged, so nothing anywhere surfaces the break. Filed as **#232**, P0.
- **Precedence against a declared edge.** Declared must win the *ordering* — UC2
  exists as the correction mechanism. But "declared wins" must not mean the
  disagreement is discarded: see "A wrong `depends_on` is worse than none" above.
  If the declaration and the field's value name different resources, the graph
  should honour the declaration **and say so**, because a silently-resolved
  disagreement is how a user's mistake becomes permanent.
- **Whether an inferred edge is persisted — already decided, follow the
  precedent.** `_dependency_targets()` (`orchestrator.py:400`) unions
  reference-derived targets with declared ones, and `orchestrator.py:603`
  persists the union to `StateEntry.depends_on`, deliberately, so that
  `plan destroy` from state alone does not tear a target down before the
  resource pointing at it. So `aiform` already derives and persists edges the
  user never declared, and the design question is not *whether* but whether a
  *literal*-derived edge deserves the same trust as a syntax-derived one.

## Verification

Nothing to test — nothing is built. What this decision rests on, and what a
future Phase 3 would need:

**The named probe has since run, and answered half of its two questions.** It
asked whether a firewall's `droplet_ids` still carries a deleted droplet's id,
and whether a later `PUT` carrying that id returns `422`.

- **Question one: partly answered.** `verified` — the dead id is listed *while
  the firewall reports itself converged*, `status: "succeeded"` with
  `pending_changes: []` (`knowledge/drivers/digitalocean_vpc/FINDINGS.md`,
  transcript `13`). So the firewall's own status cannot be used to detect a
  missing member. **Not** verified: that the entry is permanent. The read is 12
  seconds after the `DELETE`, on a droplet that never reached `active`, against a
  firewall whose attach for that droplet was still `waiting` — see the FINDINGS
  entry, which names the three-read probe that would settle it.
- **Question two: still unrun and ungraded as an observation.** Whether a `PUT`
  carrying a dead id returns `422` is graded `inferred` in **this document's**
  Knowledge-confidence table, extrapolated from transcript `21-`'s create-time
  `422` and never observed on an update. `specs/digitalocean_firewall.md` does
  not grade it at all.

What is answered **sharpens** the stale-literal argument without settling the
phase: a literal that goes stale is invisible to the firewall's own status, so
nothing the provider reports surfaces the break. That is what **#232** rests on.
A future Phase 3 inherits a partly-verified hazard here — enough to act on, not
enough to call permanent.

It remains **not** a decision gate. The pause holds either way.

## Conditions that reopen this

- **#216 is fixed and detection is still the only way to get this edge** — for
  example if the chosen fix leaves integer-typed fields unreachable by
  reference. Then the literal is permanent, and this is the reassessment the
  decision defers to. **#216 is fixed, and this condition is not met**: the
  chosen fix (`compute._flatten()`'s new `provider_id` key,
  `plans/fix-216-reference-into-integer-field.md`) makes `droplet_ids`
  reachable by reference — `${digitalocean.compute.<name>:provider_id}`
  resolves to a real `int` and passes `_reject_wrong_scalars()` — so
  integer-typed fields are not left unreachable, and detection is not "the
  only way to get this edge" any more than it was for the `tags`/`addresses`
  rows above. (The edge inventory row above narrows this further: a nested
  `sources`/`destinations.droplet_ids` reaches this only for a single
  reference, not several — #224 — but the top-level `droplet_ids` field this
  design pass actually found as the one inferable edge has no such limit.)
  That answers this condition; it does not itself decide
  whether Phase 3 is worth building for some other reason, which is the
  repo owner's call and outside this edit's scope.
- **A resource type that genuinely breaks when its referent is deleted.**
  Restores the destroy-ordering and orphan-refusal cases. **Such a type is now
  known to exist at the provider** — a VPC refuses deletion while it holds a
  member, `409 "Can not delete VPC with members"` — but this condition is about
  the **driver set**, and no `network` driver exists, so it is not met. #233
  notes that a user-created VPC holding droplets would be the first such edge
  with a driver at both ends.
- **A reference that fails silently rather than loudly.** The stale-literal
  argument assumes a `422` on write. Note this condition is now **partly
  triggered**: a stale reference is verified to fail silently on *read* — the
  firewall reports itself converged — while the write-path `422` remains
  `inferred`. It is the read-side silence that #232 is filed for.
- **A driver set where the id-match inventory is more than one row** — an
  `ssh_key`, `load_balancer` or k8s driver. A `load_balancer` additionally
  sorts after `firewall`, flipping the destroy-order accident above.
- **A second provider.** The single-provider caveat on the destroy-order
  argument stops holding.

## Out of scope

- **Fixing #216 itself.** Named here as the prerequisite; its design was its
  own issue and its own plan
  (`plans/fix-216-reference-into-integer-field.md`). This spec took a
  position on *why* it came first, and recorded the owner's stated direction
  above, but did not choose among #216's candidate fixes or work out what
  the chosen one costs — that's #216's plan, not this file.
- **Orphan refusal and partial-failure recovery.** Phase 4, **partially
  shipped since** (#225, paths-driven destroy only; then 4a's repair on both
  destroy routes and 4b's failure report and restartable apply — see
  `specs/resource_dependencies.md` and `MULTI_RESOURCE_PRD.md`'s Phase 4
  entry). This spec takes a
  position on what an inferred edge would do to Phase 4, and none on Phase 4's
  design; #225 didn't need to settle that question either, since it only acts
  on edges Phase 1/2 already produce.
- **Inferring edges with a model call.** Permanently excluded, not deferred.
  `CLAUDE.md` and `MULTI_RESOURCE_PRD.md` both require the graph path to be
  deterministic and a repeat `plan` on unchanged input to make zero Anthropic
  calls. An LLM-inferred edge would also be nondeterministic across runs,
  which Phase 1's ordering guarantees forbid independently of cost.
- **A lint that warns instead of inferring** — "you hardcoded an id matching a
  tracked resource, did you mean a reference?". Cheaper than detection and
  deliberately not designed here: it is a different feature with a different
  failure mode (a false warning costs attention, not a refused destroy), and
  it would need its own use case rather than inheriting UC1's. It is more
  attractive now that #216 is fixed, not less — the warning has somewhere to
  point the user, `:provider_id`, where before it would not have.
- **Verifying a declared edge against the configuration**, and prompting on a
  disagreement. Also out of scope *here*, and for the same reason as the lint — it
  needs its own use case. But note it is the cheapest of the declaration's three
  uses and the only one that catches a wrong declaration; see "The declaration has
  three uses" above, which is where the reasoning lives so a future design pass
  does not have to re-derive it.
- **Renumbering the PRD's phases.** Phase 3 keeps its number while paused, so
  Phases 4-7 and every reference to them stay valid.

## Knowledge-confidence

| Claim | Grade | Basis |
|---|---|---|
| References preserve an attribute's native type for a whole-value reference | **verified** | `aiform/references.py:267-272`, comment and code |
| `id` reaches the reference namespace as `StateEntry.id`, a `str` | **verified** | `orchestrator.py:94`, `:104`; `models.py:222` |
| A droplet id is provider-assigned; a firewall 422s on an unknown one at *create* | **verified** | `specs/digitalocean_firewall.md:316`, transcript `21-` |
| A later `PUT` carrying a *deleted* droplet's id also 422s | **inferred** | Extrapolated from `21-`'s create-time 422; never observed on an update. It is the unrun probe's second question |
| `apply_plan()` skips `NO_OP` before any driver call | **verified** | `orchestrator.py:1095` |
| Destroy-from-state topologically sorts `StateEntry.depends_on` | **verified** | `orchestrator.py:971`, `:975` |
| Reference-derived edges are unioned into `depends_on` and persisted | **verified** | `orchestrator.py:400`, `:603` |
| The zero-edge destroy order puts firewalls first today | **verified, single-provider** | `graph.py:56-66`, `orchestrator.py:45`; holds because one provider exists |
| The edge inventory is *complete* | **inferred** | Fields read from source, but completeness is a judgement; the first draft missed `addresses` and misclassified `tags` |
| **A firewall does not break when a droplet in it is removed** | **owner-reported** | Stated by the repo owner, 2026-09-26. Not probed. Also recorded in #220. Note what *was* since probed is a narrower claim — the firewall keeps the dead id and still reports `succeeded` (`knowledge/drivers/digitalocean_vpc/`, transcript `13`) — which is about the reference going stale, not about the firewall's rules ceasing to work |
| A deleted droplet's id stays in `droplet_ids`, and the firewall reports itself converged | **verified** | `knowledge/drivers/digitalocean_vpc/`, transcript `13`. Supersedes this table's earlier framing of the question as unprobed |
| A VPC refuses deletion while it has a converged member, `409 "Can not delete VPC with members"` | **verified** | `knowledge/drivers/digitalocean_vpc_member/`, transcript `10`. The first edge in the repo whose parent the provider refuses to release — the counterexample the edge inventory lacked. Note the narrower claim: the refusal is verified, not that a droplet breaks without its VPC, which the provider prevents anyone from observing |
| A tag-targeted firewall can exist ahead of its droplets, config inert | **owner-reported** | Same conversation, 2026-09-27. Not probed, and scoped to tag targeting |
| Detection would force a driver load, or an AST read, before the ordering pass | **inferred** | Follows from `orchestrator.py:474` preceding `:476`; no implementation has tested it |
| #216 is fixed, and the reopen condition naming it is not met | **verified** | `drivers/digitalocean/compute.py`'s `provider_id` key; `${digitalocean.compute.<name>:provider_id}` resolves to a real `int` and passes `_reject_wrong_scalars()` — `plans/fix-216-reference-into-integer-field.md`, its tests |

The two owner-reported rows are decisive for the destroy-ordering and
orphan-refusal arguments and rest on operational knowledge rather than a
transcript. They must not be silently upgraded to `verified` without the probe.

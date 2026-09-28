# specs/dependency_detection.md — automatic dependency detection, and why it is paused

**Naming note**: like `specs/resource_dependencies.md`,
`specs/resource_references.md`, `specs/unordered_fields.md` and
`specs/resource_tagging.md`, this filename deliberately doesn't follow
`specs/README.md`'s per-module mirroring rule. It describes one feature that
would span `aiform/driver.py`, `aiform/orchestrator.py` and every driver, and
it is named for the feature so it is discoverable from any of them.

Closes #220. Phase 3 of `MULTI_RESOURCE_PRD.md`.

**Build status.** **Not built, and paused by decision** — not pending, not in
progress, not next. This file is the decision record and the contract a
future Phase 3 starts from. Nothing here is implemented: no driver declares
reference metadata, `aiform/driver.py` has four declarative class attributes
and not five, and no code path infers an edge from a literal value. The
named prerequisite, #216, **is fixed** — the pause itself is unchanged by
that alone; reassessing whether Phase 3 is still worth building is what
#216 being fixed makes due, and that reassessment is the repo owner's call,
not made in this edit.

| Piece | State |
|---|---|
| The decision to pause | made, recorded here |
| `REFERENCE_FIELDS` contract (PRD open question #3) | **designed, not built** |
| `REFERENCE_FIELDS` on any driver | not built |
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

## Relationship to PLAN.md §10

§10's "Dependency graph: ordering exists, value flow does not" entry lists
automatic detection as Phase 3, phrased as deferred-but-coming. This spec
**narrows** that entry: the mechanism is unchanged, but the phase is paused by
decision behind a named prerequisite rather than merely sequenced, and §10 is
updated to say so. §10's *delivered* claims for Phases 1 and 2 are untouched.

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
| `droplet_ids` (`firewall.py:83`), `sources`/`destinations.droplet_ids` (`firewall.py:52`) | a droplet | **id**, integer vs. `StateEntry.id`'s string | **The one id-match edge.** Was the edge #216 blocked from being a reference; reachable now via `provider_id` (`compute.py:210`) for top-level `droplet_ids` unconditionally, and for the nested field only with a single reference — more than one hits a sorted-list check a reference can't generally satisfy (#224, `specs/digitalocean_firewall.md`) |
| `tags` (`firewall.py:84`), `sources`/`destinations.tags` (`firewall.py:53`), `compute.tags` (`compute.py:173`) | a droplet, via its `tags` attribute (`compute.py:215`) | **attribute** | A *user-chosen* value, writable before its referent exists. Phase 2 references already work here (`specs/digitalocean_firewall.md:352-356`) |
| `addresses` (`firewall.py:51`) | a droplet, via `ipv4_address` (`compute.py:216`) | **attribute** | Same class as `records[].data`. Missed by this table's first draft |
| `records[].data` (`domain.py:103`) | a droplet, via `ipv4_address` | **attribute** | Phase 2's canonical case; references work |
| `ssh_keys` (`compute.py:170`) | a DigitalOcean SSH key | id | No `ssh_key` driver exists; no node to point at |
| `load_balancer_uids`, `kubernetes_ids` (`firewall.py:54-55`) | a load balancer, a k8s cluster | id | No drivers. The k8s edge was never probed (`specs/digitalocean_firewall.md:301`) |

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
`specs/digitalocean_firewall.md:282-298` tabulates the edges with id types and
pre-existence evidence, probe by probe. Phase 3's real work is moving that
from prose into something the planner can read.

### The id-match edge cannot change create ordering

A droplet's id is assigned by DigitalOcean at creation, and the API refuses
an unknown id with a 422 (`specs/digitalocean_firewall.md:299`). So a literal
in `droplet_ids` was written after a prior successful apply of that droplet.
In the common case the droplet is therefore already tracked and its action is
`NO_OP`, and `apply_plan()` skips `NO_OP` before any driver call
(`orchestrator.py:1046`) — ordering a `CREATE` against a `NO_OP` is inert.

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
`StateEntry.depends_on` (`orchestrator.py:922`, `:926`); it degenerates to
reverse-lexical only when there are no edges at all.

In that zero-edge case the order cannot currently break anything, by naming
accident rather than design. `graph.topological_order()` drains a heap of raw
key strings (`aiform/graph.py:56-61`), and those keys are
`provider.resource_type.name` as `resource_key()` formats them
(`orchestrator.py:45`) — so plain lexical string order happens to compare
`resource_type` before `name`, and with `"compute" < "domain" < "firewall"`
every firewall is destroyed before every droplet, for any pair of names.
**Single-provider only**: the provider segment sorts first, so this holds
because there is exactly one provider today.

It flips the moment a resource type sorting after `firewall` arrives — a
`load_balancer` driver, whose edge `specs/digitalocean_firewall.md:300`
already documents. But it flips into a *hazard* only for a resource that
actually breaks when its referent disappears, and a firewall does not:

> **A firewall does not break when a droplet in it is removed.** A firewall
> can also exist ahead of the droplets it targets *by tag*, with the
> configuration inert until matching droplets exist.

Owner-reported, not probed — see "Knowledge-confidence". The second half is
scoped to tag targeting deliberately: it cannot be true of `droplet_ids`,
which 422s on an id that does not exist yet.

This closes the question `specs/digitalocean_firewall.md:305-307` left open.

### An inferred edge would cost more than it pays

Inferring the id-match edge has two costs and no observable benefit.

**It would start refusing plans immediately.** Not a Phase 4 hypothetical:
`_resolve_dependency_edges()` (`orchestrator.py:408`) already raises
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
Phase 4 would need to distinguish the two, which `MULTI_RESOURCE_PRD.md`'s
Phase 4 text does not currently contemplate.

## The declaration contract

This answers `MULTI_RESOURCE_PRD.md`'s open question #3 — *what does a driver
declare so Phase 3 can infer edges, a new class attribute alongside
`PARAM_SCHEMA` or metadata inside it?* Designed, not built.

**A fifth declarative class attribute, `REFERENCE_FIELDS`**, alongside the
four in `aiform/driver.py` (`PARAM_SCHEMA` `:62`, `LIKELY_REPLACE_FIELDS`
`:73`, `NON_DIFFABLE_FIELDS` `:94`, `UNORDERED_FIELDS` `:112`) — not metadata
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

Note `PLAN.md` §4 still omits `UNORDERED_FIELDS` from its declarative-attribute
list (#133). A fifth attribute inherits that debt — fix #133 first or the gap
doubles.

## Where detection would run, and what it would cost

`MULTI_RESOURCE_PRD.md`'s "the ordering engine doesn't change" is true. The
pass in front of it is not.

`_order_files()` (`orchestrator.py:446`) is called at `orchestrator.py:473`,
before the driver cache is built at `:475` and before any credential
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
`tests/test_orchestrator.py:1626`
(`test_unchanged_dependency_graph_makes_zero_llm_calls`) and `:1931`
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
  worse than an unhelpful edge.
- **Precedence against a declared edge.** Declared must win; UC2 exists as the
  correction mechanism.
- **Whether an inferred edge is persisted — already decided, follow the
  precedent.** `_dependency_targets()` (`orchestrator.py:399`) unions
  reference-derived targets with declared ones, and `orchestrator.py:602`
  persists the union to `StateEntry.depends_on`, deliberately, so that
  `plan destroy` from state alone does not tear a target down before the
  resource pointing at it. So `aiform` already derives and persists edges the
  user never declared, and the design question is not *whether* but whether a
  *literal*-derived edge deserves the same trust as a syntax-derived one.

## Verification

Nothing to test — nothing is built. What this decision rests on, and what a
future Phase 3 would need:

**The named probe, not run.** It would promote the owner-reported firewall
behavior to `verified` and close
`specs/digitalocean_firewall.md:305-307`'s open note. Shape: create a
disposable droplet and a firewall carrying its id in `droplet_ids`, `DELETE`
the droplet, then `GET` the firewall and observe whether `droplet_ids` still
carries the dead id, and whether a later `PUT` carrying it returns 422.
Follow `specs/driver_creation.md`'s loop, transcript under
`probes/transcripts/`, findings in `knowledge/drivers/<session>/FINDINGS.md`.

It is **not** a decision gate. The pause holds whichever way it comes out; a
dangling reference that *did* break would restore the destroy-ordering case,
which is why it appears under "Conditions that reopen this".

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
  Restores the destroy-ordering and orphan-refusal cases.
- **A reference that fails silently rather than loudly.** The stale-literal
  argument assumes a 422; a silent wrong answer inverts it.
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
- **Orphan refusal and partial-failure recovery.** Phase 4. This spec takes a
  position on what an inferred edge would do to Phase 4, and none on Phase 4's
  design.
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
- **Renumbering the PRD's phases.** Phase 3 keeps its number while paused, so
  Phases 4-7 and every reference to them stay valid.

## Knowledge-confidence

| Claim | Grade | Basis |
|---|---|---|
| References preserve an attribute's native type for a whole-value reference | **verified** | `aiform/references.py:267-272`, comment and code |
| `id` reaches the reference namespace as `StateEntry.id`, a `str` | **verified** | `orchestrator.py:94`, `:104`; `models.py:222` |
| A droplet id is provider-assigned; a firewall 422s on an unknown one at *create* | **verified** | `specs/digitalocean_firewall.md:299`, transcript `21-` |
| A later `PUT` carrying a *deleted* droplet's id also 422s | **inferred** | Extrapolated from `21-`'s create-time 422; never observed on an update. It is the unrun probe's second question |
| `apply_plan()` skips `NO_OP` before any driver call | **verified** | `orchestrator.py:1046` |
| Destroy-from-state topologically sorts `StateEntry.depends_on` | **verified** | `orchestrator.py:922`, `:926` |
| Reference-derived edges are unioned into `depends_on` and persisted | **verified** | `orchestrator.py:399`, `:602` |
| The zero-edge destroy order puts firewalls first today | **verified, single-provider** | `graph.py:56-61`, `orchestrator.py:45`; holds because one provider exists |
| The edge inventory is *complete* | **inferred** | Fields read from source, but completeness is a judgement; the first draft missed `addresses` and misclassified `tags` |
| **A firewall does not break when a droplet in it is removed** | **owner-reported** | Stated by the repo owner, 2026-09-26. Not probed. Also recorded in #220 |
| A tag-targeted firewall can exist ahead of its droplets, config inert | **owner-reported** | Same conversation, 2026-09-27. Not probed, and scoped to tag targeting |
| Detection would force a driver load, or an AST read, before the ordering pass | **inferred** | Follows from `orchestrator.py:473` preceding `:475`; no implementation has tested it |
| #216 is fixed, and the reopen condition naming it is not met | **verified** | `drivers/digitalocean/compute.py`'s `provider_id` key; `${digitalocean.compute.<name>:provider_id}` resolves to a real `int` and passes `_reject_wrong_scalars()` — `plans/fix-216-reference-into-integer-field.md`, its tests |

The two owner-reported rows are decisive for the destroy-ordering and
orphan-refusal arguments and rest on operational knowledge rather than a
transcript. They must not be silently upgraded to `verified` without the probe.

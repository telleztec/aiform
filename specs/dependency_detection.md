# Detailed Design Specification for Resource Dependency Detection

Would span `aiform/driver.py`, `aiform/orchestrator.py` and every driver.

Closes #220. Phase 3 of `specs/MULTI_RESOURCE_PRD.md`.

## Introduction

This document contains the detailed requirements, interfaces, algorithms, and
design decisions for the implementation of automatic dependency detection.

**Build status.** **Not built, and paused by decision** — not pending, not in
progress, not next. This file is the decision record and the contract a
future Phase 3 starts from. Nothing here is implemented: no driver declares
reference metadata, `aiform/driver.py` has four declarative class attributes
and not five, and no code path infers an edge from a literal value.

| Piece | State |
|---|---|
| The decision to pause | made, recorded here |
| `REFERENCE_FIELDS` contract (PRD open question #3) | **designed, not built** |
| `REFERENCE_FIELDS` on any driver | not built |
| Edge inference from literal values | not built |
| The firewall-deletion probe | named here, not run |
| #216, the named prerequisite | open, not started |

## Purpose

Record what automatic dependency detection would be, what it would be worth
today, and why the answer is "not before #216 is fixed" — so the question is
settled with evidence rather than re-derived, and so a future session
inherits a decided declaration contract rather than three sentences of PRD
text.

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

### #216 is the prerequisite, and it reframes the whole question

`droplet_ids: ["${digitalocean.compute.web-01:id}"]` does not work
(#216). Understanding *why* is what settles the ordering of this phase, and
the intuitive reading is wrong.

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

**`id` is doing two unrelated jobs under one name:** `aiform`'s identity
token, legitimately a string; and a provider attribute a user wants to
reference, whose native type is whatever the CSP says — an integer for a
DigitalOcean droplet. A reference names an `aiform`-tracked *object*, and
`:attribute` already expresses which value inside that object to hand the
provider. The repo owner's stated direction follows from that reading: make
the right attribute available with the right type, rather than add syntax. Be
precise about what the type analysis alone establishes — a cast would also
work mechanically for this edge, so preferring the attribute is a design
judgement, not a consequence. Which of #216's candidate fixes is chosen
belongs to #216.

Two consequences for this phase:

- **#216 is a prerequisite for Phase 3 regardless of the outcome.** Building
  inference over a reference mechanism that cannot express the one edge that
  exists would be building on a known-broken foundation.
- **Fixing it may remove the need for detection entirely** for this edge,
  since a working reference implies its own edge already. That is the
  specific thing to reassess afterwards.

A Phase 2 defect is not an argument *for* Phase 3. It is an argument for
fixing Phase 2.

### The edge inventory

Every reference-shaped field across the three drivers. Graded `inferred`
rather than `verified`: the fields are read from source, but whether the list
is *complete* is a judgement about what counts as reference-shaped, and an
earlier draft of this table missed one row and misclassified another.

| Field | Points at | Match kind | Verdict |
|---|---|---|---|
| `droplet_ids` (`firewall.py:83`), `sources`/`destinations.droplet_ids` (`firewall.py:52`) | a droplet | **id**, integer vs. `StateEntry.id`'s string | **The one id-match edge.** Exactly the edge #216 blocks from being a reference |
| `tags` (`firewall.py:84`), `sources`/`destinations.tags` (`firewall.py:53`), `compute.tags` (`compute.py:173`) | a droplet, via its `tags` attribute (`compute.py:210`) | **attribute** | A *user-chosen* value, writable before the droplet carrying it exists — though the **tag itself** must already exist, or the create 422s `"tag <name> does not exist"` (`specs/digitalocean_firewall.md:175-179`). `compute.py:210` declares `tags` referenceable, so Phase 2 references work here |
| `addresses` (`firewall.py:51`) | a droplet, via `ipv4_address` (`compute.py:211`) | **attribute** | Same class as `records[].data`. Missed by this table's first draft |
| `records[].data` (`domain.py:103`) | a droplet, via `ipv4_address` | **attribute** | Phase 2's canonical case; references work |
| `ssh_keys` (`compute.py:170`) | a DigitalOcean SSH key | id | No `ssh_key` driver exists; no node to point at |
| `load_balancer_uids`, `kubernetes_ids` (`firewall.py:54-55`) | a load balancer, a k8s cluster | id | No drivers. The k8s edge was never probed (`specs/digitalocean_firewall.md:292`) |

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
an unknown id with a 422 (`specs/digitalocean_firewall.md:290`). So a literal
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
`load_balancer` driver, whose edge `specs/digitalocean_firewall.md:291`
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
  raises from the other direction. A reference says *which attribute of the
  object to read*; `REFERENCE_FIELDS` would say *which attribute a literal
  here would have come from*. Both need the attribute to exist with the right
  type, which is why #216 is upstream of this design and not merely adjacent
  to it.

Note `PLAN.md` §4 still omits `UNORDERED_FIELDS` from its declarative-attribute
list (#133). A fifth attribute inherits that debt — fix #133 first or the gap
doubles.

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

**The named probe has since run, and answered half of its two questions.** It
asked whether a firewall's `droplet_ids` still carries a deleted droplet's id,
and whether a later `PUT` carrying that id returns `422`.

- **Question one: answered, `verified`.** The id stays, and the firewall reports
  itself converged — `status: "succeeded"`, `pending_changes: []`
  (`knowledge/drivers/digitalocean_vpc/FINDINGS.md`, transcript `13`).
  `specs/digitalocean_firewall.md`'s open note is closed on that basis. **The
  provider does not self-heal a stale reference.**
- **Question two: still unrun.** Whether a `PUT` carrying a dead id returns
  `422` is graded `inferred` in `specs/digitalocean_firewall.md`'s
  knowledge table, extrapolated from a create-time `422` and never observed on
  an update.

The answer to question one **sharpens** the stale-literal argument rather than
settling the phase. A literal that goes stale is now known to be invisible
rather than merely suspected: the live read and the stale file agree, so the
diff finds nothing and the resource plans `NO_OP`. That is **#232**, filed at
P0. A future Phase 3 inherits a verified hazard here, not a hypothesis.

It remains **not** a decision gate. The pause holds either way.

## Conditions that reopen this

- **#216 is fixed and detection is still the only way to get this edge** — for
  example if the chosen fix leaves integer-typed fields unreachable by
  reference. Then the literal is permanent, and this is the reassessment the
  decision defers to.
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

- **Fixing #216 itself.** Named here as the prerequisite; its design is its
  own issue and its own plan. This spec takes a position on *why* it comes
  first, and records the owner's stated direction above, but does not choose
  among #216's candidate fixes or work out what the chosen one costs.
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
  it would need its own use case rather than inheriting UC1's. It becomes more
  attractive, not less, if #216 is fixed — at that point the warning has
  somewhere to point the user.
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
| A droplet id is provider-assigned; a firewall 422s on an unknown one at *create* | **verified** | `specs/digitalocean_firewall.md:290`, transcript `21-` |
| A later `PUT` carrying a *deleted* droplet's id also 422s | **inferred** | Extrapolated from `21-`'s create-time 422; never observed on an update. It is the unrun probe's second question |
| `apply_plan()` skips `NO_OP` before any driver call | **verified** | `orchestrator.py:1046` |
| Destroy-from-state topologically sorts `StateEntry.depends_on` | **verified** | `orchestrator.py:922`, `:926` |
| Reference-derived edges are unioned into `depends_on` and persisted | **verified** | `orchestrator.py:399`, `:602` |
| The zero-edge destroy order puts firewalls first today | **verified, single-provider** | `graph.py:56-61`, `orchestrator.py:45`; holds because one provider exists |
| The edge inventory is *complete* | **inferred** | Fields read from source, but completeness is a judgement; the first draft missed `addresses` and misclassified `tags` |
| **A firewall does not break when a droplet in it is removed** | **owner-reported** | Stated by the repo owner, 2026-09-26. Not probed. Also recorded in #220. Note what *was* since probed is a narrower claim — the firewall keeps the dead id and still reports `succeeded` (`knowledge/drivers/digitalocean_vpc/`, transcript `13`) — which is about the reference going stale, not about the firewall's rules ceasing to work |
| A deleted droplet's id stays in `droplet_ids`, and the firewall reports itself converged | **verified** | `knowledge/drivers/digitalocean_vpc/`, transcript `13`. Supersedes this table's earlier framing of the question as unprobed |
| A VPC refuses deletion while it has a converged member, `409 "Can not delete VPC with members"` | **verified** | `knowledge/drivers/digitalocean_vpc_member/`, transcript `10`. The first edge in the repo whose parent the provider refuses to release — the counterexample the edge inventory lacked. Note the narrower claim: the refusal is verified, not that a droplet breaks without its VPC, which the provider prevents anyone from observing |
| A tag-targeted firewall can exist ahead of its droplets, config inert | **owner-reported** | Same conversation, 2026-09-27. Not probed, and scoped to tag targeting |
| Detection would force a driver load, or an AST read, before the ordering pass | **inferred** | Follows from `orchestrator.py:473` preceding `:475`; no implementation has tested it |

The two owner-reported rows are decisive for the destroy-ordering and
orphan-refusal arguments and rest on operational knowledge rather than a
transcript. They must not be silently upgraded to `verified` without the probe.

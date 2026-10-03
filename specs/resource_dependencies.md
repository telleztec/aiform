# Resource Dependencies Detailed Design Specification

Implemented across `aiform/graph.py`, `models.py`, `orchestrator.py`, `cli.py`
and `state.py`.

Closes #200. Phase 1 of `specs/MULTI_RESOURCE_PRD.md`.

## Purpose

The purpose of this document is to describe the relationships and dependencies
that will be implemented by `aiform`. This detailed specification may describe
more relationships than initially implemented. Each relationship type will be
analyzed against specific use cases listed here, and those that are important use
cases will be implemented. Creating one or more internal graphs of the
relationships between resources, with edges that describe those relationships,
will enable the use cases listed below. We expect to be able to enhance the
**efficiency, accuracy, and resiliency** of the infrastructure deployment and the
operations of that infrastructure.

Three consequences of that purpose, because they govern how the rest of this
document is read:

- **Describing a relationship is not committing to it.** A kind may be specified
  here and left unimplemented; what earns implementation is a use case, not a
  taxonomy.
- **Every relationship kind must be analysed against the use cases**, which is
  what "Relationship kinds observed in the current driver set" below does. A kind
  with no use case is documentation, not work.
- **"One or more internal graphs" is deliberate.** Provisioning needs a DAG it can
  order; an operational view may not be acyclic. The purpose permits more than one
  rather than assuming a single structure serves both.

Phase 1 delivers the first slice of this: a user declares that one resource must
exist before another, and `aiform` orders the plan accordingly — dependencies
created before dependents, destroys in reverse, cycles refused at plan time. The
three effects those relationships have map onto the goals above: create and
destroy ordering buy **efficiency and accuracy**, and failure impact is where
**resiliency** would come from — which is the effect with no implementation.

## Definitions

Vocabulary this spec uses, and the distinctions the rest of it rests on.

### Dependency types

`aiform` adopts Terraform's vocabulary rather than inventing its own.

| Source | Written as | Carries a value? | Terraform's equivalent |
|---|---|---|---|
| **Explicit** | `depends_on: [key]` | no — ordering with no value flow | `depends_on`, its "last resort" |
| **Implicit** | `${provider.type.name:attribute}` in `params` | yes, by construction — the edge exists *because* a value flows | an expression reference, its default |
| **Intrinsic** | nothing at all — no configuration requested it | no | no equivalent |

**Intrinsic resource** — one the provider creates on the user's behalf, that no
configuration requested and that cannot be deleted. `aiform` never creates, tracks
or manages one. The term is this project's, not established IaC vocabulary.

Say **"an edge to an intrinsic resource"** rather than "an intrinsic edge".
*Implicit* and *intrinsic* read as near-synonyms in ordinary speech while the
distinction here is load-bearing: an implicit edge comes from a reference the user
**wrote**, an intrinsic one exists with **no configuration at all**. Keeping the
three-way parallel out of the sentence keeps them apart.

**Explicit and implicit are Terraform's; intrinsic is not**, and the DigitalOcean
region default VPC is the verified example — every droplet `aiform` creates lands in one
it never named, which makes it a dependency that is neither declared nor
referenced (`knowledge/drivers/digitalocean_vpc_default/FINDINGS.md`, and
`knowledge/drivers/digitalocean_vpc_member/FINDINGS.md` for the containment
rules).

**An edge to an intrinsic resource is a NOOP.** `aiform` does not order it, refuse
a destroy for it, or repair it. The repo owner's scoping decision; the properties
it rests on are in those findings.

**Two things this decision does not cover**, kept separate so they do not ride
along on it:

1. **An `aiform` user cannot choose a VPC**, so a deployment cannot be
   network-isolated. The limit is `aiform`'s, not the provider's: DigitalOcean
   accepts `vpc_uuid` on droplet create and this repo's probes have used it
   (`probes/digitalocean_vpc.py`, step `04`), but `compute.py`'s `create()` builds
   its request body from a fixed allowlist — `name`, `region`, `size`, `image`,
   then `ssh_keys`, `backups`, `monitoring`, `tags` — so a `vpc_uuid` written into
   `params` is dropped rather than sent, and every droplet joins the region
   default. That is an expressiveness gap about *user-chosen* VPCs, not about
   intrinsic ones, and treating intrinsic resources as inert does not touch it.
   Filed as **#233**, which also notes that a
   user-created VPC containing droplets would be this model's **first aligned
   `Hosts` edge** — declared, existentially coupled, and provider-enforced —
   i.e. the second example the taxonomy currently lacks.
2. **The name collision returns if VPCs are ever modelled.** Every team has a
   `default-<region>`, so `digitalocean.network.default-sfo3` names a different
   resource in each — and a resource key carries no account or team component. Inert
   while nothing models VPCs; live the moment a `network` driver adopts existing
   ones. Recorded on #201, which owns deployment identity.

Nothing is built for intrinsic resources and this spec proposes nothing beyond
recording them. The row stays in the table because a future `network`/VPC driver
has to reckon with the shape — a resource that already exists, unmanaged and
undeletable, as a dependency of things `aiform` does manage — and because the NOOP
decision rests on DigitalOcean's properties, which another provider may not share.

**The type is retained (#234), and nothing consumes it yet.** `StateEntry.depends_on`
stays the union `_dependency_targets()` computes, and `StateEntry.reference_edges`
records, beside it, which of those targets are implicit edges and which attributes
flow along each: `{target_key: [attribute, ...]}`, attributes sorted and unique. A
target in `depends_on` but not in `reference_edges` is an explicit-only edge. A
target that is both declared and referenced appears in `reference_edges`: the
value-flow fact is the one that cannot be re-derived from `depends_on`, and the
file still says what was declared. No behaviour reads it: ordering, destroy,
refusal and every plan are unchanged. It exists so a later per-edge behaviour does
not have to re-parse `.aiform.md` files at a point where only state is loaded.

### Other Historical Terms

Terms already in this tree that a reader may take for one of the three above.
This spec uses **explicit**, **implicit** and **intrinsic**, and does not rename
these in place.

| Term | Where it appears | Synonym? |
|---|---|---|
| **reference-derived** | `specs/orchestrator.md`, `specs/resource_references.md`, `specs/dependency_detection.md`, `specs/system_test_references.md` | **Yes** — read it as *implicit* |
| **derived edge** | those four, plus `PLAN.md` | **Yes** — read it as *implicit* |
| **cross-resource attribute reference** | `specs/resource_references.md` | **Yes**, of the syntax rather than the edge: it is what writing one produces an *implicit* edge from |
| **declared** | `specs/dependency_detection.md`'s "declared vs inferred" axis | **Yes** — read it as *explicit* |
| **provider default** | this spec, before #228 | **Yes** — superseded by *intrinsic resource*, which is wider: not every implicitly created CSP object carries a `default` flag |
| **inferred** | every spec's Knowledge-confidence table; `specs/dependency_detection.md`'s "An inferred edge would cost more than it pays" | **No.** Two unrelated jobs, neither of them *implicit* — see below |

**`inferred` is the one to watch.** It means a confidence grade in every
Knowledge-confidence table, and separately it is Phase 3's name for an edge
`aiform` derives on its own by matching a *literal* value against tracked
resources. An implicit edge is written by the user, in the text of `params`; an
inferred one is guessed from a value the user never marked as a reference.
`specs/dependency_detection.md`'s "An inferred edge would cost more than it pays"
draws exactly that line — a declared edge
"honors an instruction; an inferred one asserts a relationship nobody claimed."
Reading the two as synonyms makes Phase 3 look shipped when it is paused.

### Roles belong to edges, not to resources

A droplet is a *dependent* of the VPC that hosts it and a *target* of the
firewall that lists it, simultaneously. Any vocabulary that classifies
*resources* rather than *edges* will contradict itself on the second example.


## Background

What exists today, and the evidence it rests on. Everything in this section is
observation or prior art; nothing here is a commitment. The commitments are the
requirements below.

### Relationship kinds observed in the current driver set

Terraform has one relationship. ITSM practice has many — a CMDB joins items
with named types (`Depends on :: Used by`, `Hosted on`, `Connects to`), stores
each once but names it from both ends, and derives impact from the type. That is
the tradition the missing half of this model belongs to, not to IaC.

The kinds `aiform` can currently observe, with what was verified about each:

| Kind | Instance | Target must pre-exist? | Provider refuses the destroy? | Dependent survives target's loss? |
|---|---|---|---|---|
| **Hosts** | VPC → droplet | yes — `404` on an unknown `vpc_uuid` | **yes — `409 "Can not delete VPC with members"`** | **unobservable** — the provider prevents the situation |
| **Uses** | firewall → droplet's id | yes — `422` on an unknown id | no | yes, and its reference goes stale |
| **Protects** | firewall → droplet, operationally | n/a | no | n/a — the operational direction is the inverse of the configuration edge, and is not expressible. **#235** |

A kind whose target the provider will not release is categorically different from
one it will: every edge in the driver set was the latter until the `Hosts` refusal
was probed. Each claim in the table is graded, with its transcript, in
**Knowledge-confidence** below.

A relationship kind earns implementation from a use case, not from being
describable. Use-case names are `specs/MULTI_RESOURCE_PRD.md`'s.

| Kind | Use cases it serves | Already served by | Worth implementing as a kind? |
|---|---|---|---|
| **Hosts** | UC-A (the host must exist first), UC-B (the provider *enforces* the order — `409`), UC-D (a host's loss takes its members with it) | Phase 1 ordering already covers UC-A and UC-B for it | **Only for UC-D.** Ordering needs no kind; the existing edge suffices |
| **Uses** | UC-A, UC-B, and the repair case behind #227 | Phase 1 ordering covers UC-A and UC-B | **For repair.** #227 cannot decide refuse-versus-repair without knowing the dependent survives |
| **Protects** | **UC-D only** | nothing | **No, not yet.** Its only use case is the one with no implementation |

Three conclusions:

1. **Ordering needs no relationship kinds at all.** UC-A and UC-B are delivered by
   an untyped edge. Every kind above is already ordered correctly today.
2. **Repair needs one distinction**, not a taxonomy: whether the dependent survives
   its target. That is the single property #227 turns on.
3. **`Protects` earns nothing until UC-D is built**, because the effect it serves
   has no implementation and no committed phase — #235 is what it would buy.

So the kinds are **specified ahead of need**, which the Purpose permits, and the
first to earn implementation is whichever the first operational use case requires.

### `aiform`'s graph has to be its own

Tempting alternative: ask the provider. `GET /v2/vpcs/{id}/members` exists, so
for a VPC the provider answers "what is inside me" natively. Two probed facts
rule it out as a foundation:

1. **The answer is in a different namespace.** A member is identified by URN —
   `do:droplet:89a3a30d-…` — while the droplet's id is `604558243` and
   `StateEntry.id` is `"604558243"`. Nothing `aiform` records joins to it.
2. **Readability is asymmetric per kind.** A VPC can list its members; a
   droplet cannot be asked what protects it, because firewall membership lives
   on the firewall.

And a third, from reading rather than probing: an edge is not readable from live
state immediately. A droplet's record carries **no `vpc_uuid` key at all** just
after creation — absent, not null, which is a different thing to code against —
and the key appears within a poll or two, while its status is still
`new`, so a graph built by asking the provider is empty exactly when a plan is
being applied.


### What a dependency determines

Three effects. The first two are what Terraform's graph is for: its dependencies
tutorial scopes them to determining *"the correct order in which to create the
different resources"*, and its graph internals describe the graph as used to
*"generate plans and refresh state"*. The third is outside Terraform's job and has
no IaC prior art.

| Effect | When | Explicit edge | Implicit edge |
|---|---|---|---|
| **Create order** | plan | honours an assertion | *necessary* — the value does not exist yet |
| **Destroy order** | plan | reverse of the assertion | reverse |
| **Failure impact** | runtime | suggestive of operational coupling | names exactly what to repair |

On the third column's first cell: an explicit edge carries no value, so the only
reason to write one is a requirement the configuration does not reveal — which
makes it the better signal of operational coupling of the two. That is reasoning
from *why someone would declare one*, not an observation: there is **no
explicit-only edge in the driver set** to check it against. Graded `inferred`
below.


## Use cases

**The use cases live in `specs/MULTI_RESOURCE_PRD.md`**, in one priority-ordered
table, and are deliberately not restated here. That table is their single home.

This spec covers **UC-A** and **UC-B** (create and delete in dependency order),
contributes the declaration half of **UC2**, and delivers **UX1 — textual
dependency display**: a `plan` that silently reorders resources without showing the
graph it derived is not reviewable, and a user cannot correct a dependency they
cannot see.

**UC-C**, **UC-D**, **UC-E** and **UX2 — graphical visualization** are named
there and are the open work. The requirements below are what this spec holds
itself to.

## Requirements

Numbered `D` for dependency, so they do not collide with the PRD's `R1`-`R4`.
Each is a property a test can assert, and each traces to a use case in
`specs/MULTI_RESOURCE_PRD.md`.

| | Requirement | From | State |
|---|---|---|---|
| **D1** | A resource is created only after every resource it depends on exists. | UC-A | met — `_topological()` |
| **D2** | A create ordering failure is refused at plan time, before any provider mutation, rather than surfacing as an apply error. | UC-A | met — cycles and unresolvable targets raise in `_order_files()` |
| **D3** | A resource is destroyed only after every resource that depends on it is destroyed, or the destroy is refused. | UC-B | met within one run; met across runs for the paths-driven route (#225: refused unless `--force`); **not** met across runs for the delete-marker route — a dependent outside the run is not consulted (#226) |
| **D4** | No destroy leaves a surviving resource holding a reference to something that no longer exists, without telling the user. | UC-B | **not met** — verified: a firewall lists a deleted droplet's id while reporting itself converged, so nothing the provider says surfaces the break |
| **D5** | A plan that changes a value another resource consumes reports the consumer as changing. | UC-C | met, but **over-reports**: any change to a target marks every dependent as changing, whether the consumed value moved or not |
| **D6** | A plan does not report a consumer as changing when the value it consumes is unaffected. | UC-C | **not met** — the converse of D5, and the reason D5's "met" is qualified |
| **D7** | Given a resource that has failed, aiform can name the resources affected by that failure. | UC-D | **not met** — `observability.py` never reads `depends_on` |
| **D8** | Re-running after a partial failure never destroys or duplicates a resource that succeeded. | UC-E | met for the succeeded prefix — a live read plus the dependency-closed-prefix invariant; **not** met for a resource whose create succeeded on the provider but never reached state |
| **D9** | A resource recreated after drift does not silently lose a relationship a dependent's params expressed. | UC-B, UC-D | **met for an implicit edge, not met for a literal** — see "Drift" below. The literal case is #232 |
| **D10** | `aiform` must provide a way for an end-user to describe a dependency. | UC-A, UC-B | met, two ways — `depends_on: [key]` for an explicit edge, and a `${provider.type.name:attribute}` reference in `params` for an implicit one |
| **D11** | `aiform` must provide a way to observe the dependency graph. | UX1, UX2 | **partially met** — D11.1 and D11.2 below are the two halves |
| **D11.1** | `aiform` must provide a CLI mechanism that describes the graph. | UX1 | **partially met** — `plan create` and `plan apply` print a `depends on:` line per resource (`cli.py:176`) and `--json` carries `depends_on` (`:203`), which is all UX1 asks for. But both are plan-scoped adjacency lists: `plan show` prints no dependencies at all (`_print_state()`), so the graph of what is **deployed** is recorded in `StateEntry.depends_on` and displayable nowhere. The PRD names this gap itself — *"Neither UX1 nor UX2 covers a standalone command that prints the dependency graph"* — and assigns it to Phase 7 |
| **D11.2** | `aiform` must provide a rich UI mechanism, such as HTML, to see the graph. | UX2 | **not met** — Phase 7; nothing is built or designed |
| **D12** | An invalid `depends_on` declaration is refused before any provider call or model call, naming what is wrong. | UC2 | met, for four kinds — see below. Reported clumsily for one of them (**#236**) |
| **D13** | `aiform` tells the user when a declared dependency is well-formed but disagrees with the configuration it sits beside. | UC2 (its failure mode) | **not met** — the "Verify" use of a driver's reference declaration, which is Phase 3 (#220) |

D4, D6, D7, D8's exception, D9's literal half, D11's two halves and D13 are the
open work. Each is traceable to an issue: D4 to the orphan-reference issues, D6 to
this spec's "Change propagation", D7 to the absence of any dependency-aware
observability, D8 to the PRD's R3, D13 to #220.

**D11.1's gap is the sharper of the two**, because the deployed graph already
exists in `StateEntry.depends_on` — showing it is a presentation change rather
than new machinery, and it is what a user needs to answer "what depends on this?"
without first running a plan.

### D12: the four kinds of invalid declaration

All four are refused before any driver is loaded, any provider is called, or any
model is called. `tests/test_orchestrator.py` asserts that directly, with
`FakeClient([])` plus a fake driver that recorded nothing.

| What the user wrote | Refused by | At |
|---|---|---|
| A key that is not `provider.resource_type.name` | `ResourceSpec`'s `depends_on` validator → `parse_dependency_key()`, which checks every element, not just the first | parse, before planning begins |
| A target that is neither a file in this run nor a resource tracked in state | `_resolve_dependency_edges()` → `PlanBlockedError`, naming the offending target; `graph.topological_order()` enforces the same precondition independently → `UnknownDependencyError` | plan |
| A cycle | `graph.CycleError`, carrying the cycle as a walkable path so the message renders `a -> b -> c -> a` | plan |
| A resource depending on itself | the same, as a length-1 cycle | plan |

The first kind reaches the user wrapped in pydantic's own rendering, including a
`pydantic.dev` URL — the refusal is right and the message is not. **#236**.

### D13: a declaration can be valid and still be wrong

D12 catches a declaration that is **malformed**; nothing catches one that is
**mistaken**. A `depends_on` naming `web-01` beside a `droplet_ids` literal holding
`web-02`'s id is well-formed, resolvable and acyclic, so it passes every check
above — and the ordering graph then gains an edge that buys nothing while the real
dependency on `web-02` goes unrecorded, so a destroy can take `web-02` out from
under the firewall. `specs/dependency_detection.md`'s "A wrong `depends_on` is
worse than none" works the case through.

**D13 is stated ahead of its use case, deliberately and visibly.**
`specs/dependency_detection.md` holds that verifying a declared edge "needs its own
use case rather than inheriting UC1's", and `specs/MULTI_RESOURCE_PRD.md` does not
have one yet. It is traced to UC2's failure mode here because that is the closest
honest anchor: UC2 promises a hand-declared relationship is *honoured*, and a wrong
one is honoured faithfully. Earning a phase should mean getting a PRD use case
first.


## Decisions

What this spec commits to, as distinct from the Background above.

### Change propagation

What a plan says when one resource's change alters a value another consumes.
This is UC-C.

**None of what follows is newly discovered.** `specs/resource_references.md`'s
"A target this run will replace" already designs this deliberately and argues
the trade, and a test pins the cost. This section exists to connect that design
to the dependency model, not to report a defect.

#### The mechanism

`references._resolve_text()` sets `deferred = True` for **any** target in
`volatile`, and `_will_get_new_attributes()` puts every `CREATE` *and* every
`UPDATE` there. So a reference whose target is changing at all resolves as
unresolved at plan time, and `_apply_params()` resolves it from live state after
the target has been applied.

A tracked dependent carrying unresolved references then does **not** reach the
diff at all. `_decide_action()` routes it to `planner.unresolved_entry()`, which
returns a deterministic `UPDATE` — the code's own words are "the answer is
produced deterministically instead, at zero cost", because handing the model a
literal `${...}` "would invite it to categorize the placeholder".

#### What that costs

- **No stale value is ever shown or sent**, which is the direction that matters.
  `specs/resource_references.md` records what the alternative cost: a narrower
  rule let a zone resolve against a **dead** `ipv4_address`, plan `NO_OP`, and
  leave DNS pointing at the old address — the P1 failure #215 was filed for.
- **Every dependent of a changing target is reported as changing**, whether the
  consumed value moved or not. A droplet gaining a tag marks the DNS record
  reading its `ipv4_address` as changing, and the resulting `UPDATE` rewrites an
  identical value.
- **At zero LLM cost.** The dependent's `UPDATE` is deterministic;
  `tests/test_orchestrator.py` asserts one categorization call for the target and
  none for the dependent.

The existing spec's verdict on the trade is "not close", and this spec agrees.
Over-reporting a no-op update is cheap; under-reporting a dead address is a P1.

#### Where it connects to the model

The distinction the code cannot make is **whether the referenced attribute is
among the ones this change will alter.** `volatile` is per-resource; the question
is per-attribute. Nothing in `PlanEntry` records which attributes an `UPDATE`
will touch — a driver computes that inside `update()`, long after the plan is
built.

So **D6 is genuinely not met**, and it is not met for a reason that has nothing
to do with edge typing: it is a granularity gap, not a taxonomy gap. Worth
stating because the rest of this model is about typing edges, and this is the one
requirement typing would not fix.


### Drift, and why the two edge kinds diverge

D4 is scoped to a destroy. **Drift** is the case where aiform destroyed nothing and
the relationship breaks anyway, and the two edge kinds behave oppositely — which
makes this the sharpest practical consequence of the explicit/implicit distinction
in the whole model.

**An implicit edge survives drift, in both directions it can go.**

- *Target drifts and is recreated.* `refresh_resource()` reports
  `drifted_missing`, the target is planned as a `CREATE`, and so joins both
  `volatile` and `replaced`. The dependent's reference is withheld at plan time,
  `_decide_action()` routes it to `planner.unresolved_entry()`, and
  `_apply_params()` re-resolves **after** the target is recreated. The dependent
  follows the target to its new value with no user action. This is #215's fix, and
  drift is the case it was filed for.
- *Target is gone for good*, its file removed too. `_resolve_dependency_edges()`
  raises `PlanBlockedError` — "neither a file in this run nor a resource tracked in
  state" — with no `--force`. Loud, and the user must fix the file.

**A literal does not survive drift, and fails silently.** A firewall whose
`droplet_ids` holds `[123]` still holds it after droplet `123` is deleted out of
band. DigitalOcean keeps the dead id too — verified at least across the
firewall's own convergence, and it reports `status: succeeded` while doing so
(`knowledge/drivers/digitalocean_vpc/FINDINGS.md`, transcript `13`) — so the live
read and the stale file **agree**, `unordered_equal` finds no diff, and the
firewall is `NO_OP`. The droplet is then recreated with a new id and comes back
behind no firewall, with the plan reporting nothing. Filed as **#232**.

Note what produces the silence: the provider's helpfulness. Had DigitalOcean
dropped the dead id, the diff would have shown a change and the firewall would have
been repaired on the next apply. The stale reference is invisible *because* both
sides of the comparison are equally stale.

**The dependency on that provider behaviour is worth stating, because it is the
weaker half of the evidence.** If DigitalOcean *does* reap dead ids on a slower
sweep — untested, and graded `inferred` below — then the window in which the diff
is blind is bounded rather than permanent, and #232 becomes a race rather than a
steady state. It is a hazard either way: an `apply` inside that window still
plans `NO_OP` on an unprotected droplet. The unbounded reading is the one that
needs the probe.

So for any identity-valued field, an implicit edge is not merely tidier than a
literal — it is the difference between a self-healing relationship and a silently
broken one. That is a **safety** argument for #216, which is what makes
`droplet_ids` expressible as a reference at all. Until it lands, the unsafe form is
the only form available for that field.


### What this means for each remaining phase

The point of writing this down is that the phases stop being re-argued.

#### Phase 3 — automatic detection (paused, #220)

**The model tells us how to implement it, and sharpens why it was paused.**

`specs/dependency_detection.md` proposed a fifth driver class attribute
declaring, per field: the field path, the target provider and resource type, and
which attribute a literal in that field would have come from. **That is a subset
of what the relationship kinds above need declared anyway** — add the kind
(`Hosts`, `Uses`, `Protects`) and one declaration serves both. Detection then
reduces to a lookup: match the literal against tracked resources of that kind on
that attribute.

Three things change about the decision:

1. **The strongest objection dissolves.** The pause rested partly on inferred
   edges producing false refusals — asserting a relationship nobody claimed and
   then blocking a safe destroy on it. Under this model a refusal comes from the
   relationship *kind*, not from an edge existing, so an inferred `Uses` edge on
   a survivable target refuses nothing.
2. **A reason appears that the pause never weighed.** That analysis evaluated
   detection against ordering alone, and correctly found it worthless there — a
   provider-assigned id in `droplet_ids` proves the droplet already exists, so
   the target is `NO_OP`. But **failure impact was not in the model**, and there
   detection is the difference between a complete graph and one with a hole
   wherever a user wrote a literal.
3. **The blanket "never infer from a literal" becomes precise.** Terraform never
   does it, and the reason is ambiguity. The declaration splits that: matching on
   an **identity** attribute is unambiguous, because ids are unique and a literal
   that matches a tracked resource's identity *is* that resource; matching on a
   **value** attribute is not, because a literal `10.0.0.5` in a rule's
   `addresses` may be a tracked droplet, a coincidence, or an external host. So
   detection is sound for identity fields and unsound for value fields, and the
   declaration says which.

What does **not** change: detection still buys nothing for ordering, and it still
needs the declaration during `_order_files()`, which runs before the driver cache
is built — so it pays a driver-load cost the operational uses do not.

#### Phase 4 — orphan refusal, partial-failure recovery, restartability

Orphan refusal is **D3** and **D4**. The model supplies what the current
behaviour lacks: whether to refuse or to repair is decided by whether the
dependent survives its target. The `Hosts`-versus-`Uses` distinction is the
shape of that question, and note what is actually established on each side: for
`Uses`, survival is observed (a firewall outlives its droplet, transcript `13`);
for `Hosts`, only the provider's refusal is observed, and survival is
unobservable because the provider prevents the state.

Recovery inherits one property worth naming, because it is stronger than it
looks. `apply_plan()` stops at the first failure and saves state per resource, so
**the applied set is always a dependency-closed prefix of a topological order** —
every resource in state has all of its dependencies in state, and no dangling
edge can exist after a partial apply. That is an invariant, not an aspiration,
and it is what makes **D8** achievable by re-running rather than by unwinding.

#### Phase 5 and 6 — concurrency and parallelism

The model adds one constraint and removes none. Independent resources may run
concurrently; an edge is exactly the thing that forbids it. But note that
`volatile` is accumulated **in topological order** today
(`build_create_plan()`), and reference resolution depends on that: every target
is classified before any dependent of it is planned. A parallel planner has to
preserve that property or reference withholding breaks.

#### Phase 7 — visualization

Typed edges are what make a rendered graph worth looking at. An untyped graph
draws the firewall below the droplet and is *correct about ordering* while being
misleading about consequence.

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
says which one is about to be acted on — so `aiform plan destroy --all` (then spelled
with no file arguments), run from the wrong directory, is indistinguishable from the run the
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
    reference_edges: dict[str, list[str]] = Field(default_factory=dict)  # #234


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


class UnknownDependencyError(Exception):
    """Carries `key: str` and `target: str` -- `key` names a target outside
    `keys`, and this is never ignored."""


def topological_order(keys: set[str], edges: dict[str, set[str]]) -> list[str]:
    """Kahn's algorithm. `edges[k]` is the set of keys `k` depends on. Every
    target in every `edges[k]` must be a member of `keys`.

    Raises CycleError when the graph is not acyclic, UnknownDependencyError
    when an edge names a target outside `keys`."""
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
existing loop. `build_create_plan()` keeps its signature; only the
**order** of the list it returns changes. `build_destroy_plan()` gains a
`force: bool = False` keyword-only parameter and its return type widens
from `list[PlannedResource]` to `tuple[list[PlannedResource], list[str]]`
— see "Dangling dependency targets on a destroy path, and `--force`"
below for why the return type has to change here even though
`build_create_plan()`'s doesn't.

### `aiform/cli.py`

`_print_plan()` emits one extra indented line per resource with dependencies;
`_plan_to_json()` gains a `depends_on` key per plan entry. Neither signature
changes. `plan destroy` gains a `--force` flag, `action="store_true"`,
passed as `build_destroy_plan(..., force=args.force)`; `_cmd_plan_destroy`
unpacks the returned `(planned, warnings)` tuple and passes the real
`warnings` to `_plan_apply_and_report()` instead of the `[]` it hardcoded
before this. `--force` is independent of `--yes`: `--yes` auto-approves
the apply confirmation prompt, `--force` overrides the dangling-dependency
refusal, and neither implies the other — a `--yes` run with a dangling
target still refuses.

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
  `RESOURCE_OR_PROVIDER_PATTERN` (`models.py:11`) and cannot contain dots, but
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
- **The precondition is enforced, not merely assumed.** Every target in
  every `edges[k]` must itself be a member of `keys`; a target outside
  `keys` raises `graph.UnknownDependencyError(key, target)` rather than
  being silently ignored. This changed from the original MVP behavior
  (which ignored it) after review found two of `graph.py`'s four call
  sites — `build_destroy_plan()`'s two producers, below — did not
  actually restrict edges first, contradicting the docstring's claim.
  `_order_files`' `_resolve_dependency_edges` already did the restricting
  itself before calling in; the two destroy producers now do the same
  restricting explicitly (see "Destroy ordering, all three producers"
  below) rather than relying on `graph.py` to filter for them. With all
  four call sites restricting correctly, `graph.UnknownDependencyError`
  cannot actually reach `graph.topological_order()` from the
  orchestrator — and `_topological()` deliberately does **not** catch it.
  This is not the same posture as `graph.CycleError`, which *is* caught
  and converted to `PlanBlockedError`: a cycle is genuinely reachable from
  a user's own `depends_on` declarations, while an `UnknownDependencyError`
  reaching `_topological()` would mean an orchestrator caller has a bug —
  converting that into a user-facing "your dependency doesn't resolve"
  message would misdirect whoever investigates it toward the wrong files
  entirely. See `specs/orchestrator.md`'s `_topological()` entry for the
  full reasoning.

### The discovery/validation pass

A new pass in front of `build_create_plan()`'s existing loop. **Zero LLM calls,
zero driver loads, zero credential resolution** — it is YAML and string work
only. Per discovered file: read the text, one `parser.parse_frontmatter()`,
compute the key, note whether it is delete-marked. Then, in this order:

0. **Path normalization, before the duplicate check.** Explicitly-named paths
   are deduplicated by normalized absolute path (`os.path.abspath`, which folds
   `.` and `..`), so `plan create ./a.aiform.md a.aiform.md` is one file rather
   than a duplicate-key error. Normalization deliberately does **not** follow
   symlinks: a symlink and its target stay two distinct paths and therefore
   still raise, because which of the two to trash on a destroy is genuinely
   ambiguous, and silently trashing one leaves the other to recreate the
   resource. Case is not folded either, so on a case-insensitive filesystem
   `App.aiform.md` and `app.aiform.md` also still raise. Both are the safe
   direction: refuse rather than guess.
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

The existing loop then iterates **the computed order**, calling `_plan_one()` /
`_plan_delete_marked()` unchanged — which means the loop re-derives each record
from its path rather than reusing the pass's, so every file is read and parsed
again.

Counting honestly, a **tracked** file is now read three times: once by the
discovery pass, once by the loop, and once more by `parse_file()` inside it. The
third is pre-existing and is the one PR #199 filed out of scope, because closing
it means changing `parse_file()`'s interface. The second is new here, and
closing *it* is a different job — threading the pass's records into
`_plan_one()` rather than re-deriving — not a `parse_file()` change. Neither is
fixed in this phase.

The cost is `read_text()` plus a pure-YAML parse, with no LLM call either way,
so this is wasted IO rather than a spent toll. It does leave a narrow TOCTOU
window: a file edited between two reads was validated and ordered on content
that is not what gets planned. See `specs/orchestrator.md`.

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

Both columns are explicit, because resolvability turns on the **declaring**
file's kind as well as the target's — a row naming only the target would be
ambiguous for one combination:

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
invariant.** `discover_files()` returns `sorted(cwd.glob(...))`, and
`AIFORM-DELETE-` sorts *before* any lowercase name (`'A'` is 65, `'a'` is 97),
so a delete-marked file was previously planned and applied **first**. The
change is deliberate and is the better order — destroying a resource before
its replacement exists opens a capacity gap that destroying afterwards does
not — but it is a change, and a reader comparing against `main` should not be
told otherwise. A test pins it.

Note the two claims here are independent, not mutually supporting: rule 3
guarantees no *live* resource depends on a same-run destroy, and the returned
order guarantees destroys follow live actions. Do not justify either by the
other — that is circular; both are separately true of the ordering code.

### Destroy ordering, all three producers

Ordering only one of them would make the feature's central claim false, so all
three are covered:

- **`build_create_plan()`'s delete-marked branch** — reverse topological, per
  above.
- **`build_destroy_plan()`'s file-driven path** — files exist, so `depends_on`
  is readable from frontmatter; reverse topological. It also now runs **rule 1's
  duplicate-key check**, which it did not before.

  What actually happened before, verified against the merge base rather than
  reasoned from this tree: two files declaring one key produced **two**
  `PlannedResource` entries with the same `resource_key`, both listed in the
  plan. Nothing was silently collapsed. The damage was at apply time:
  `_apply_destroy()` ran twice for one key, the first deleting the resource,
  dropping it from state, saving, and trashing file 1. The second then hit
  `_require_tracked()`, which raised `PlanBlockedError` because the key was
  already gone — so **the apply aborted part-way through and file 2 stayed on
  disk**, ready to recreate the resource on the next `plan create`.

  A second failure looks possible here — the provider 404ing into a
  `DriverExecutionError` — and **that arm is unreachable with any shipped
  driver**: all three DigitalOcean drivers deliberately swallow a 404 on DELETE
  as "already gone" (`compute.py`, `domain.py`, `firewall.py` — the last of
  which notes "Verified live: a second delete 404s. Idempotent by ..."), so
  `_call_driver()` never sees an exception to wrap. The `_require_tracked()`
  path is the only one that fires.

  So the conclusion rule 1 draws still holds, and the pre-PR behavior was if
  anything worse than a collapse: a half-completed destroy that fails mid-apply.
  Refusing at plan time is the same answer for the same reason.

- **`build_destroy_plan()`'s state-driven destroy-all path** — reads
  `StateEntry.depends_on` and orders in reverse topological. This is the
  invocation a user actually types (`aiform plan destroy --all`), so
  leaving it unordered would mean the feature ordered only the invocation
  nobody uses.
  A cycle recorded in state does not block it (#206): the reported cycle is
  named in a warning, its first edge is dropped, and ordering is retried until
  the remainder is acyclic. See `specs/orchestrator.md`'s
  `build_destroy_plan()` for the rule. The file-driven destroy path and every
  create path still refuse a cycle with `PlanBlockedError`.

### Dangling dependency targets on a destroy path, and `--force`

The discovery/validation pass above classifies every target for
`build_create_plan()`'s two node sets, live and delete-marked. The two
`build_destroy_plan()` producers have no such pass in front of them — they
read `depends_on` straight from frontmatter or `StateEntry`, and each must
classify every target itself before handing edges to
`graph.topological_order()`, which now enforces its precondition (see
`aiform/graph.py` above) rather than filtering silently.

Each target resolves one of three ways:

- **In this destroy's own node set** — an edge, ordering the two destroys
  against each other.
- **Resolvable elsewhere, silently** — dropped, no warning, no error. This
  is the everyday case: `aiform plan destroy one-file.aiform.md` where that
  file depends on a resource tracked in state but not named in this run.
  Refusing or warning here would make the single-file invocation
  unusable — a resource almost always depends on something outside the one
  file being destroyed.

  What counts as "elsewhere" differs by producer, because the file-driven
  path and the state-driven path don't have the same node set:

  - `_build_destroy_plan_from_paths()`: the node set is the discovered
    files' keys; "elsewhere" is `st.resources`.
  - `_build_destroy_plan_from_state()`: the node set **is** `st.resources`
    — the run is state, so there is no "elsewhere" distinct from the node
    set itself. Every target is either in `st.resources` (an edge) or
    dangling.
- **Dangling** — resolves in neither place. Warn and refuse, unless
  `--force`.

**All dangling pairs are collected before any decision is made**, unlike
the create path's discovery pass, which raises on the first unresolvable
target it finds (rule 2, above). That asymmetry is deliberate: the create
path's validation pass runs once per file in a loop that can cheaply
re-run after a fix, while a destroy's `PlanBlockedError` is the thing
standing between the user and a `--force` decision — naming only the
first offender would produce a fix-rerun-fix loop where each `--force`
still doesn't clear the plan, since another dangling target is discovered
address by address on each retry.

Without `--force`, `PlanBlockedError`'s reason names **every** dangling
`(key, target)` pair. With `--force`, the edge is dropped exactly as
"resolvable elsewhere" drops it, but each dropped pair also produces one
warning string — through the same `(planned, warnings)` channel
`build_create_plan()` already uses — so the CLI can tell the user which
edges it silently gave up on.

`graph.UnknownDependencyError` never reaches either producer: dangling
targets are excluded from the edge sets handed to
`graph.topological_order()` before it is called, not caught after.

### Reverse dependents on a paths-driven destroy, and `--force` (#225)

Everything above classifies edges pointing **out of** the nodes being
destroyed. `_build_destroy_plan_from_paths()` had no check at all in the
other direction: a resource **outside** this run that persists a
`depends_on` **into** a node being destroyed was never consulted, so
`aiform plan destroy droplet.aiform.md` would destroy the droplet and leave
a firewall that depends on it tracked, live, and pointed at an id that no
longer exists — silently. Nothing warned: the uncovered-resource warning is
gated on `if not paths`, so a paths-driven run never reaches it, and gate
#2's review model can't see the hazard either, since the plan summary
carries no `depends_on` (see `build_plan_summary()`'s own note, above).

`_reverse_dependents(node_keys, st)` walks every entry in `st.resources`
that is **not** in `node_keys` (this destroy's own nodes) and records every
`(dependent, target)` pair where `target` is one of `node_keys` and
`dependent`'s persisted `StateEntry.depends_on` names it.
`_resolve_reverse_dependents()` mirrors `_resolve_dangling_targets()`'s
shape rather than folding into it — the two hazards are semantic mirror
images (a target this run can't find, versus a dependent this run doesn't
know about) and read better as separate messages: without `--force`,
`PlanBlockedError` names every orphaned pair; with it, each pair drops to a
warning instead, through the same `(planned, warnings)` channel.

**Reads the dependent's persisted `StateEntry.depends_on`, not its
`.aiform.md`.** Two reasons, both load-bearing: the dependent's file may not
exist any more (hand-deleted, moved), and even when it does, it is very
likely not part of *this* run — the whole scenario is "a resource outside
the run depends on one inside it." Re-parsing every `.aiform.md` on disk to
answer this question would add a filesystem scan to the destroy path that
no other check here needs.

**`--force` warns and proceeds, and `_apply_destroy()` makes the warning's
"dropping the edge" true at apply time — for exactly the dependents it
named.** `_build_destroy_plan_from_paths()` puts each warned dependent on the
destroyed resource's `PlannedResource.dropped_dependents`, and
`_apply_destroy()` removes the destroyed key from those entries' persisted
`StateEntry.depends_on` (`_prune_dependents_on()`) after deleting the destroyed key
from `st.resources`. It does this even when the destroyed target itself is
untracked in state, because the warning is emitted for that case too. An
entry that does not appear in `dropped_dependents` is never touched. This is
deliberately a `state.json`-only edit:
**the dependent's own `.aiform.md` may still declare the dead
`depends_on:` in its frontmatter, and this leaves that alone on purpose** —
rewriting a file nobody asked to edit is worse than the next `plan` on it
blocking with an actionable error. Concretely: after a forced destroy, the
survivor's *state* no longer orders against the gone resource, but the next
`plan` that touches the survivor's own file recomputes
`_dependency_targets()` from that file's frontmatter, sees the same
now-nonexistent target again, and — since it resolves nowhere — hits the
same dangling-target refusal the file-driven destroy path already has,
until the user actually edits the file — and this particular check,
`_resolve_dependency_edges()`'s `else` branch (`orchestrator.py:428-432`),
has **no** `--force` escape of its own; a stale `depends_on:` in
frontmatter naming a resource no longer tracked anywhere must be edited out
of the file, not forced past. Pruning removes the stale ordering edge from
state; it does not, and is not meant to, remove the user's obligation to
update their own `.aiform.md`.

**Every other destroy prunes nothing.** `_plan_delete_marked()`
(`build_create_plan()`'s `AIFORM-DELETE-` route) and
`_build_destroy_plan_from_state()` leave `dropped_dependents` empty, so a
survivor's persisted `depends_on` keeps naming the destroyed key. The edge
staying put is what makes the next state-driven `plan destroy` refuse with
the dangling-target reason (`--force` to proceed) rather than silently
forgetting the survivor was orphaned. The delete-marker route still classifies
edges only out of the nodes it processes and has no reverse check, so it can
still orphan a dependent silently at destroy time. Filed separately as
**#226**, `priority: P1-correctness` — not fixed here, and not to be read as
covered by this section.

### `StateEntry.depends_on` and `StateEntry.reference_edges`

`reference_edges` (#234) is written at exactly the same sites, from the same
inputs, and pruned in the same place as `depends_on`, so the two cannot disagree:
every key of `reference_edges` is also in `depends_on`. It is computed by
`_reference_edges(params)` from `references.find_references()`: for each
reference, `target_key -> sorted unique attributes`. `_prune_dependents_on()`
removes the destroyed target from both.

`depends_on` is written at **three** sites, and all three are needed:

- `_new_state_entry()` and `_record_update()`'s in-place branch, at apply time,
  from `PlannedResource.depends_on`.
- **`_plan_one()`, at plan time**, from `ResourceSpec.depends_on`, whenever the
  resource is already tracked — unconditionally, regardless of the action
  decided below it.

That third write is not redundant: without it, an already-tracked resource whose
`depends_on` changed keeps the old list in state until an apply happens to rewrite
it, so a `plan destroy` in between orders against a stale graph.

**This is not a migration case.** `specs/MULTI_RESOURCE_PRD.md`'s
"Non-requirements" section says backward
compatibility is not owed in any form — correctly, since there are zero
resources in production. That rule does **not** cover this. "Already tracked"
means "has a state entry", which the *current* version writes on every apply;
it does not mean "created by an older version". The ordinary path is:

1. Write `db.aiform.md` and `app.aiform.md`, neither declaring `depends_on`.
2. `aiform plan apply` — both created by this version, state records `[]`.
3. Realize `app` needs `db` first, and add `depends_on` to `app.aiform.md`.

Step 3 is the most likely way a user reaches for this feature at all — you
learn the ordering matters *after* deploying without it — and nothing in it
involves a legacy artifact. Adding `depends_on:` changes no `params`, so the
action is **NO_OP**, and `apply_plan()` skips NO_OP before any state write. NO_OP is therefore the
one action that never reaches either apply-time write. Without the plan-time
write, adopting `depends_on` on an existing resource never reached `state.json`
at all, so `aiform plan destroy --all` kept ordering by empty
edges — *permanently*, since there is nothing non-NO_OP to apply and no way to
repair it but hand-editing state. For the canonical `app`-depends-on-`db` case
that was **worse than not having the feature**: the prior code iterated state
in insertion order and happened to be right, while reverse-topological over
zero edges is reverse-alphabetical and is wrong.

It is kept separate from the `aiform_md_sha256` write beside it rather than
folded into that guard. The sha write is gated on `params_agree` for a reason
specific to intent notes (see the comment there); `depends_on` is ordering
metadata, not resource config, and its correctness does not depend on whether
params agree.

**The remaining limitation, stated accurately:** state records the edges as of
the last **plan**, not the last apply. So a `plan` the user then declines to
apply still updates the recorded edges. This is the right direction — the edges
describe declared ordering intent rather than what was built, a later `plan`
re-syncs them from the file, and it is what makes the adopt-only case work at
all.

The consequence lands on exactly one invocation: **`aiform plan destroy --all`
(destroy-all, no file arguments).** That form reads `StateEntry.depends_on`, so a
`depends_on` edit made since the last `plan` is not reflected — run `plan`
first and it is. Every other producer reads the current frontmatter and is
never stale: `plan destroy <files>` builds its edges from
`entry.spec.depends_on`, and `build_create_plan`'s delete-marked branch does
the same.

Reading files during a destroy that explicitly ignores files remains the worse
alternative, so destroy-all keeps reading state.

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
cannot proceed" and already has CLI exit-code handling. It gains five reasons:
duplicate resource key, unresolvable target, live-depends-on-same-run-destroy,
a cycle with its path, and a destroy path's dangling dependency target(s)
(naming every one, see "Dangling dependency targets on a destroy path, and
`--force`" above). `graph.CycleError` is caught in `_topological()` and
converted to one of those `PlanBlockedError`s, since a cycle is genuinely
reachable from a user's own `depends_on` declarations.
`graph.UnknownDependencyError` is not caught anywhere in the orchestrator —
every call site restricts its edges before calling in, so it cannot
actually reach `graph.topological_order()` from there; if it ever did,
that would be an orchestrator caller's bug rather than a bad user
declaration, and it is deliberately left uncaught rather than relabeled as
one.

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
  imports and behavior-named `Test*` classes, with the reason for each case
  stated inline as `#` comments. It does not use `test_compare.py`'s strict
  `is True`/`is False`, because `topological_order()` returns a list and raises,
  so there is no boolean to assert on. Read the file for its conventions; this
  spec pins the coverage below, not the prose style.
  - order correct, and **deterministic across input permutations**;
  - **fan-in**: one node with several dependencies, all of which precede it;
  - fan-out; diamond; disconnected components;
  - duplicate targets collapsing to one edge;
  - self-dependency as a length-1 cycle;
  - a multi-node cycle carrying its path;
  - a target outside `keys` raises `UnknownDependencyError` naming the
    declaring key and the target, rather than being ignored.
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
  - zero Anthropic calls on an unchanged graph;
  - **both `build_destroy_plan()` producers**: a target resolved only in
    state (file-driven path) resolves **silently** — no warning, not
    blocked, the everyday `plan destroy one-file.aiform.md` case; a target
    resolving nowhere raises `PlanBlockedError` on **both** producers; the
    error names every dangling target when there are several; `force=True`
    proceeds, drops the edge, and returns one warning per dropped pair
    through the same `(planned, warnings)` channel `build_create_plan()`
    already uses; ordering is still correct once a dangling edge is
    dropped under `--force`, with a genuine edge elsewhere in the same
    resource's `depends_on` preserved.
- **`tests/test_cli.py`** — the `depends on:` line is present with edges and
  absent without; a multi-dependency resource renders all targets on one
  comma-separated line; `--json` carries the full `depends_on` list;
  `--force` is accepted on `plan destroy`; a dropped-edge warning renders
  through the same `Warning:` line `build_create_plan()`'s warnings already
  use; `--yes` alone does not bypass the dangling-dependency refusal
  (`test_destroy_blocked_by_dangling_dependency_without_force` runs with `--yes`).
- **No pre-existing zero-Anthropic-call test weakened** to accommodate the new
  pass. `aiform/graph.py` imports neither `llm` nor `anthropic` nor `models`.
- **Live**, before merge: three `.aiform.md` files in a scratch directory —
  `db-01`, `cache-01`, and `app-01` depending on both — verifying that `plan
  create` lists both targets first and prints the `depends on:` line, that
  `plan apply --verbose` completes both creates before `app-01` begins, that a
  re-run makes zero Anthropic calls, and that `plan destroy --all` destroys `app-01` before either target. Smallest droplet size,
  `aiform`-tagged, destroyed immediately.

  Name the files so that **alphabetical order contradicts the required order**
  (`app-01` sorts before `cache-01` and `db-01`). Otherwise the check passes
  against the old filename-glob behavior and proves nothing.

- **Live, the retrofit case** — added after review found the check above cannot
  see it. Apply the three files *without* `depends_on`, then add `depends_on`
  to the already-tracked `app-01` and run `plan` again, then `plan destroy --all
  --deployment <name> --yes`. Every resource in the first check is brand new, so it
  reaches state through `_new_state_entry()`; adopting `depends_on` on a
  tracked file is a NO_OP and takes an entirely different path, which is
  exactly where it was found broken. A check that only ever creates fresh
  resources cannot distinguish the two.

## Out of scope

- **Cross-resource attribute references** — one resource's output flowing into
  another's `params`. Shipped as Phase 2, `specs/resource_references.md`; still
  out of scope for *this* spec, which covers declaration only.
- **Automatic dependency detection** from driver-declared metadata. Phase 3,
  **paused by decision**, with reassessment gated on #216 — now fixed —
  `specs/dependency_detection.md` holds
  the evidence and the reopening conditions. It would produce the same edges
  this phase already consumes, so the ordering engine would not change either
  way. Note that Phase 2 already derives edges from *references*; what is
  paused is deriving them from literal values.
- **Refusing a destroy that would orphan a still-tracked dependent**, and
  partial-failure recovery for a graph apply. Phase 4 — deliberately after
  this one, since failure semantics are hard enough serially. **Partially
  shipped since** (#225, `1ed84bf`): the paths-driven destroy producer this
  spec's "Dangling dependency targets on a destroy path, and `--force`"
  section describes (above) now also refuses (or, with `--force`, warns and
  proceeds) when a tracked resource outside the run persists a `depends_on`
  into a node being destroyed — the mirror image of a dangling target, not
  covered by that section. See `specs/orchestrator.md`'s
  `resource_dependencies` addendum for the mechanism. The delete-marker
  producer has no equivalent check yet (**#226**, `priority:
  P1-correctness`); partial-failure recovery and restartability remain
  entirely undelivered.
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
- **Implementing anything in "The dependency model".** That section defines
  vocabulary and records what was probed. It changes no code, no driver
  contract, no `PARAM_SCHEMA` and no state schema. Every concept it names as
  missing — retaining the edge type, declaring a relationship kind, criticality,
  health propagation, recovery ordering — is named so the next design pass
  inherits the question rather than re-deriving it.
- **A VPC / `network` driver.** Probed, not built. `probes/digitalocean_vpc.py`
  and `probes/digitalocean_vpc_member.py` exist so the model has one edge whose
  parent the provider refuses to release; the transcripts and findings are
  deliberately durable so a future driver author recalls them instead of paying
  for the droplet again.
- **Criticality.** CMDB impact analysis needs it and it is the one concept the
  model names that is **not derivable from configuration** — somebody has to
  assert that one resource matters more than another. That may belong to the
  operator rather than to `aiform`, and this spec does not decide.
- **How many graphs there are.** Not an open question so much as a deliberately
  unforced one: the Purpose says "one or more internal graphs", so nothing here
  assumes a single structure serves both provisioning and operations. Provisioning
  needs a DAG it can order; an operational view may not be acyclic, since mutual
  dependency is normal in running systems. What is out of scope is *choosing* —
  that follows from the first operational use case built, not from this document.

## Knowledge-confidence

For "The dependency model" and "Change propagation". Earlier sections'
claims are covered by this spec's own tests.

| Claim | Confidence | Source |
|---|---|---|
| A VPC refuses deletion while it has a converged member | **documented** | DigitalOcean's `vpcs_delete` description, read 2026-09-29 — not an `aiform` discovery |
| That refusal is `409 conflict`, not the `403` DigitalOcean documents | **verified** | `knowledge/drivers/digitalocean_vpc_member/`, transcript `10`; the paired `204` at `14` |
| A region's default VPC cannot be deleted at all | **documented, not probed** | same description. Deliberately not attempted — a passing result is a destroyed region default |
| A droplet naming a nonexistent `vpc_uuid` is refused `404`, not auto-created | **verified** | `knowledge/drivers/digitalocean_vpc/`, transcript `01` |
| A firewall naming a nonexistent droplet id is refused `422` | **verified** | `specs/digitalocean_firewall.md`'s pre-existence table, probe `21-` of the `digitalocean_firewall` session — not one of the VPC sessions |
| Whether a droplet survives losing its VPC is **unobservable** | **inferred** | The provider refuses the destroy, so the state never arises. The one VPC deleted with a droplet nominally inside (`digitalocean_vpc`, transcript `07`, mid-provisioning) left the droplet working, which is weak evidence the other way |
| A firewall lists a deleted droplet's id in `droplet_ids` while reporting `status: succeeded`, `pending_changes: []` — so its own status cannot detect a missing member | **verified** | `knowledge/drivers/digitalocean_vpc/`, transcript `13` |
| …and that entry is **permanent**, i.e. DigitalOcean never reaps it | **inferred** | `13` is a single read **12s** after the `DELETE`, on a droplet that never reached `active`, against a firewall whose attach for it was still `waiting` at `08`. Nothing observed a steady state. `knowledge/drivers/digitalocean_vpc/FINDINGS.md` names a probe that would raise this to "survives ten minutes at least" — **no cheap probe reaches *permanent***, so this row is unlikely ever to be graded `verified` |
| A VPC's member list identifies members by URN, in a namespace nothing in `aiform` records | **verified** | `knowledge/drivers/digitalocean_vpc_member/`, transcript `09` |
| A droplet's record carries no `vpc_uuid` key immediately after creation | **verified** | `digitalocean_vpc`, transcript `05` |
| The key then appears while the droplet is still `new`, before it reaches `active` | **inferred** | `digitalocean_vpc_member`'s polls showed it, but those calls were `record=False` and left no transcript, which `specs/driver_creation.md` does not permit grading `verified` |
| The type of an edge is discarded by `_dependency_targets()`'s union | **verified** | `orchestrator.py`, the one return statement, and `StateEntry.depends_on` |
| Terraform treats implicit as the default and `depends_on` as a last resort ("only use `depends_on` as a last resort") | **verified** | `developer.hashicorp.com/terraform/language/meta-arguments/depends_on`, read 2026-09-29 |
| Terraform's graph is used for provisioning operations — "generate plans and refresh state" | **verified** | `developer.hashicorp.com/terraform/internals/graph`, read 2026-09-29 |
| Terraform scopes dependencies to ordering — "so that resources are created and destroyed in the correct order" | **verified** | `developer.hashicorp.com/terraform/tutorials/configuration-language/dependencies`, read 2026-09-29. An earlier draft of this spec attributed this phrase to the two pages above; it is the tutorial's, verbatim |
| A dependent of a changing resource is reported as changing, at zero LLM cost | **verified** | `tests/test_orchestrator.py`'s `test_tracked_resource_with_an_unresolved_reference_updates_without_an_llm_call` — asserts the dependent's `UPDATE` and zero calls. Its target is a `CREATE`, so it does not cover the "or not" half |
| …including when the consumed value did **not** move | **verified** | `test_a_target_planned_update_in_place_also_withholds_its_dependent` in the same file — an in-place `UPDATE` of the target, dependent still withheld, one call for the target and none for the dependent. This is the test the "Change propagation" section is actually describing |
| Two `default: true` VPCs exist that `aiform` did not create and no `.aiform.md` mentions | **verified** | observed on the account 2026-09-29, read-only |
| A default is named `default-<region slug>` | **verified** | `default-nyc3` and `default-sfo3`, observed 2026-09-29 |
| DigitalOcean **generates** one the first time a resource is provisioned in a region | **owner-reported, consistent with observation** | Consistent with the naming convention above, with DigitalOcean's "all applicable resources are placed into the default VPC network unless otherwise specified", and with the probed fact that an unplaced droplet lands there. **Not** established by this repo's evidence: the VPCs' `created_at` dates cannot bear it, because this token has pointed at more than one team and no archived droplet records which. Two earlier drafts of this row used those dates as support and then as refutation; both were unsound. `knowledge/drivers/digitalocean_vpc/FINDINGS.md` records why, and names the one clean way to settle it |
| DigitalOcean's documentation does not state how a region's default VPC comes to exist | **verified** | its "How to Set a Default VPC" page says only that every region containing resources *has* one |
| **Every droplet `aiform` creates lands in the region's default VPC** | **verified** | `knowledge/drivers/digitalocean_vpc_default/`, transcripts `06` and `07`. A droplet created with `compute.py`'s exact body — no `vpc_uuid` — converged reporting the `default: true` VPC, and that VPC listed it as a member. Previously graded `inferred` from documentation plus the field's absence in our code |
| A region's default VPC cannot be deleted | **documented, owner-confirmed** | DigitalOcean's `vpcs_delete` description, and the repo owner reports no delete is available on the account. Not probed — a passing attempt would destroy a region default |
| An explicit edge is the better signal of operational coupling | **inferred** | reasoning from why one would be declared. **No explicit-only edge exists in the driver set** to check it against |
| A firewall does not break when a droplet in it is removed | **owner-reported** | stated by the repo owner 2026-09-26. Not probed — and note transcript `13` verifies the *reference* goes stale, which is a different claim |
| DigitalOcean cloud-firewall filtering semantics — that a deleted firewall leaves a droplet more exposed rather than less | **owner-reported** | not probed in this repo. The `Protects` row rests on it |

## Addendum: the filed work this model bears on

Every issue this spec's reasoning produced or touches, in one place, so a reader
does not have to reconstruct it from prose. Two rows are honest "no"s.

| Issue | Priority | What it is | Does this model help? |
|---|---|---|---|
| **#232** | P0-safety | A recreated droplet comes back unprotected and the plan reports the firewall as `no-op` | **Decisive.** It is the literal half of **D9**; the implicit half already self-heals. See "Drift" |
| **#201** | P0-safety | Nothing ties a state file to its deployment | **Indirectly.** Every team has a `default-<region>`, so a resource key carries no account component and collides by construction |
| **#225** | P1-correctness | Destroying a droplet by file silently orphans a dependent firewall | **Decisive.** The relationship kind picks refuse-versus-repair |
| **#226** | P1-correctness | The same hazard via the `AIFORM-DELETE-` route | **Partly.** The missing call site is mechanical; the kind decides what it should do once called |
| **#224** | P1-correctness | A firewall rule admitting two droplets by reference fails partway through apply | **None.** A driver validation quirk with no dependency content. Fixed in the firewall driver and `compare.py`, with no change to edges |
| **#235** | P1-correctness | A destroy that fails on the droplet leaves it running with its firewall already deleted | **Decisive.** The `Protects` row is the unmodelled operational direction that causes it; expressing it is what would let a plan warn |
| **#206** | P1-correctness | A cycle recorded in state blocks `plan destroy` | **Possibly.** A cycle in a symmetric kind may be legal where one in `Hosts` is not — the one-graph-or-two question |
| **#227** | P2-usability | Repair a firewall's live `droplet_ids` on force-destroy, and warn instead of refusing | **Decisive.** Needs the relationship kind for refuse-versus-repair, and transcript `13` shows there *is* something to repair — at least for as long as the provider keeps the dead id |
| **#233** | P2-usability | A deployment cannot be network-isolated; no file can choose a VPC | **Indirectly.** A user-created VPC containing droplets would be this model's first *aligned* `Hosts` edge — the second example the taxonomy lacks |
| **#223** | P2-usability | A reference to a drifted-missing target blocks the plan that would recreate it | **Little.** That is resolution order versus replacement, not typing |
| **#220** | P3-cosmetic (closed) | Automatic detection, paused | **Mechanism yes, decision partly.** See "Phase 3" |
| **#236** | P3-cosmetic | A malformed `depends_on` key is reported with pydantic's internals and a `pydantic.dev` URL | **None.** D12's refusal is correct; only its rendering is wrong |
| **#234** | P3-cosmetic | The edge type is computed and then discarded | **It is the constraint**, not a consumer. Now retained as `StateEntry.reference_edges` (see "Definitions"); every per-edge behaviour this model describes would need it |

Priorities in this table are the issues' **actual labels**, read from GitHub
rather than transcribed from memory — three were wrong before this was checked.

# Pause Phase 3 (automatic dependency detection) and record the decision

Point-in-time record, per `plans/README.md`. Not rewritten as implementation
proceeded; the amendment at the end is a second approval, dated, rather than an
edit to the first.

Closes #220.

## The ask

Write a plan for `MULTI_RESOURCE_PRD.md`'s Phase 3 — automatic dependency
detection — with the repo owner's framing taken as the brief:

> highly speculative, and of marginal benefit as it primarily improves the user
> experience, not correctness. I want to test this strongly to make sure it
> makes sense to do at this time and I want a good critical review from the llm.
> We may go through a probing mechanism during the plan, if we feel it will
> help.

So the deliverable is a design-and-decision document, not an implementation.

## Decisions taken before any writing

Settled with the owner up front:

- **Mechanism to evaluate:** driver-declared reference schema — which is also
  what the PRD's Phase 3 text already commits to.
- **Depth:** design doc only, no code. No probe run.
- **Bar for shipping:** decide after the evaluation rather than pre-committing.

## What was approved

1. **Write `specs/dependency_detection.md`** — a decision record carrying: a
   build-status table marking it not built; the relationship to `PLAN.md` §10;
   an edge inventory with file:line evidence; the argument for the decision; the
   answer to the PRD's open question #3 (what a driver declares); where
   detection would run and what that costs; a named-but-unrun probe; the
   recommendation with reopening conditions; and `Out of scope` and
   `Knowledge-confidence` sections per this repo's spec conventions.
2. **Reconcile every other doc** that read as though Phase 3 were scheduled, so
   no file contradicts the decision.
3. **File one GitHub issue** capturing both the work and the decision, framed
   from the use case, labeled via the priority rubric — so the PR carries a
   closing keyword in its body.
4. **Open a PR**, review it independently, and merge only on the owner's
   explicit approval.

Explicitly **not** in scope: no `REFERENCE_FIELDS` on any driver or in
`aiform/driver.py`, no change to `_order_files`, `graph.py` or any test, no
probe, no live provider calls.

## Pairing

**Opus 5 authors, Fable 5.1 reviews**, capped at two rounds with non-blocking
findings deferred. Named in the plan and approved with it. Opus authoring
forces Fable as reviewer under `.claude/skills/github-commit-process/SKILL.md`'s
"Choosing who authors the diff also chooses who reviews it", which is the
expensive pairing — hence the cap.

## Approval

The owner approved this plan in the planning conversation of 2026-09-26, and on
2026-09-27 directed the issue, the PR and the doc reconciliation: *"I agree to
postpone phase 3, let's create a GH issue to capture the work and the decision.
Let's create a PR that includes the plan and deferall decision, and make any
other md's coherent with the decision to postpone."* That last clause is the
approval for the `PLAN.md` and PRD edits, which `PROCESS.md` counts as design
changes.

---

## Amendment, 2026-09-27: pause behind #216 rather than defer

A second approval, not a revision of the above.

**Why it was needed.** Phase 2 (cross-resource references, PR #217) merged
while this branch was open. The approved plan's argument leaned on Phase 2
being undesigned and on its future syntax making detection's edges
self-evident. Phase 2 is built, and issue #216 means it cannot express the one
edge the design pass found — the firewall's `droplet_ids` naming a droplet.

**What the owner established.** That #216 is not a defect in the reference
mechanism. References already preserve type; the string comes from `id` serving
both as aiform's identity token and as a provider attribute. A reference names
an aiform-tracked object and `:attribute` already says which value inside it to
hand the provider, so the fix is exposing the right attribute with the right
type, not new syntax. And a bug in Phase 2 is not an argument for building
Phase 3:

> I don't understand how this bug in Phase 2 forces us to implement phase 3.
> Let's do this. pause on Phase 3. Land the plan and merge it (without an
> implementation), Fix #216, which is a prerequisite of phase 3 anyway, then see
> what we learn from doing that and whether we still think it is worth doing
> phase 3.

**What changed in the deliverable.** The decision is *paused behind a named
prerequisite* rather than deferred on its own merits. `specs/dependency_detection.md`
leads with why #216 is upstream of this phase, and the reassessment is scheduled
for after #216 lands rather than folded in now. Everything else about the
approved scope is unchanged, and still no implementation.

**Follow-on, planned separately:** fixing #216 gets its own plan and its own
approval. It is not part of this PR.

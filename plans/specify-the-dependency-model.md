# Plan: specify the dependency model (#228)

**Status: approved 2026-09-29 by the repo owner, in seven numbered decisions.
Implemented by the PR that carries this file.**

## Amendment, 2026-09-29 — after review round 1

`plans/README.md` makes a plan a point-in-time record, so the body below is left
as it was approved. Three of its claims were refuted by the Fable 5.1 review and
are corrected in the spec itself, not here:

- "within hours of each other ... changing its recommendation three times" —
  the issues span one day, and the recommendation changes happened in
  conversation, not on the issues, so they are unverifiable from the record.
- "The first verified existentially-coupled edge in the repo" — what is verified
  is that DigitalOcean **refuses to delete** the VPC. No probe observed a droplet
  after losing its VPC, because the provider prevents that state.
- "*Change propagation* — the diffing answer, and it is a defect" — the opposite
  of what shipped. `specs/resource_references.md` already documented the
  behaviour as a deliberate trade, with a test pinning its zero-LLM cost; the
  section now connects that design to the model rather than reporting a defect.

Read the spec for what is true; read this for what was planned.

## Context

`specs/resource_dependencies.md` is 826 lines of ordering machinery with no
statement of what a dependency *means*. The cost showed up as a burst of issues
— #223, #224, #225, #226, #227 — all filed within hours of each other from one
review of one PR, each needing a fresh judgement about destroy semantics, with
one of them changing its recommendation three times as facts arrived. That is an
absent definition, not a hard problem. Filed as **#228**.

The design was worked out in conversation and published as an artifact, *The
operational dependency graph*, whose thesis is that Terraform is a sound base for
the two ordering effects and supplies nothing for the third, whose prior art is
ITSM/CMDB rather than IaC.

## The owner's decisions

1. Take the artifact's plan and produce a **detailed design spec**, by modifying
   `specs/resource_dependencies.md` rather than adding a new file.
2. It must crisply inform the remaining phases, **including Phase 3**.
3. **Do not build the VPC driver**, but run probes against the VPC resource and
   keep them for a future driver. Use them to understand implied dependencies.
4. **Copy the use cases from `specs/MULTI_RESOURCE_PRD.md`** and derive explicit
   requirements from them — of the form "delete in order must produce no errors
   due to removing resources that still have references".
5. The document **must not prevent completing Phase 3 (#220)** — it should help.
6. It should **inform diffing logic**: whether modifying one part of the graph
   produces a change in a depending resource. The artifact's
   does-this-help-issue-X table may serve as an addendum.
7. **Opus authors, Fable 5.1 reviews** — the pairing
   `.claude/skills/github-commit-process/SKILL.md:523` forces for an
   Opus-authored diff.

A mid-course correction: the owner judged the existing use cases in both
documents "not particularly strong". They are mechanism-shaped and not
falsifiable, which the spec now says plainly before restating them as outcomes.

## What was built

**Probes, run before writing.** Two sessions, because the first was misdesigned
and its transcripts are worth keeping:

- `probes/digitalocean_vpc.py` — 14 calls. Established that a droplet naming a
  nonexistent VPC is refused `404`; that `GET /v2/vpcs/{id}/members` exists so a
  VPC answers the reverse-edge query natively; and — the load-bearing one — that
  **a firewall keeps a deleted droplet's id in `droplet_ids` while reporting
  `status: succeeded`, `pending_changes: []`**. Its step 07 raced convergence and
  so proved nothing about the empty requirement; that failure is itself a finding
  and is recorded as one.
- `probes/digitalocean_vpc_member.py` — 14 calls, waiting for convergence.
  **A VPC refuses deletion while it has a live member: `409 "Can not delete VPC
  with members"`**, and `204` once empty. The first verified
  existentially-coupled edge in the repo. Also found that a member is identified
  by **URN**, in a namespace nothing in `aiform` records.

Both have `FINDINGS.md` in the house format, with promotion candidates held
pending a second observation.

**The spec.** New sections in `specs/resource_dependencies.md`:

- Use cases restated — the PRD's three verbatim, why they have been hard to build
  against, five outcome-framed replacements, and **eight numbered requirements
  D1-D8** with their delivery state.
- *The dependency model* — three edge sources (explicit, implicit, and
  **provider-created defaults**, which Terraform has no equivalent for and which
  the probes found live on the account); the type being computed and discarded;
  roles belonging to edges not resources; relationship kinds with probed
  evidence; why `Protects` does not fit and how that explains the issue history;
  why `aiform`'s graph must be its own.
- *Change propagation* — the diffing answer, and it is a defect: `deferred` is
  set for **any** volatile target, so every dependent of a changing resource is
  reported as changing whether the consumed value moved or not.
- *What the model means for each remaining phase*, Phase 3 longest.
- A knowledge-confidence table, and the addendum table.

**Reconciled elsewhere**: `specs/digitalocean_firewall.md`'s "not yet probed"
note and `specs/dependency_detection.md`'s knowledge table, both of which the
probes settled.

## Deliberately not done

No code. No driver contract change, no `PARAM_SCHEMA` change, no state-schema
change. No VPC driver. Criticality, health propagation, recovery ordering and
"one graph or two" are named as undecided, not decided.

## Verification

- Full offline suite under `env -u DIGITALOCEAN_TOKEN`, and `ruff` clean —
  confirming nothing but `.md` and new probe files moved.
- Probes dry-run before running live; both ran with `--mutate` and the account
  was verified empty afterwards (0 droplets, 0 firewalls).
- `system-test` is **N/A** by the path check: no `aiform/**`, `drivers/**`,
  `prompts/**`, `tests/**` or `pyproject.toml` change. `probes/**` is not a
  runtime path.

## Process

- Branched off `main`, **not** off #222, which is parked. So the spec describes
  `main`'s behaviour, where orphan refusal genuinely is not implemented, and
  treats #225/#227 as open issues the model informs.
- Closes **#228**.

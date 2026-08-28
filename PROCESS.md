# PROCESS.md — Development workflow for aiform

`PLAN.md` is the architecture spec: what gets built. This document is the
*how*: the loop every module goes through, from nothing to a merged PR.
It applies for the rest of this project's implementation — don't
improvise a different flow partway through.

This is a meta-level process for building aiform itself. It is not the
same thing as the four-role model-tiering described in `PLAN.md` for
aiform's *runtime* behavior (`intent-orchestration-model`,
`code-generator-model`, `code-review-model`, `review-orchestration-model`
— parsing intent, drafting generated modules, reviewing them, reviewing
plans) — that's a separate concern about what the shipped tool does, and
those four roles are independently configurable per `.aiform/config.yaml`.
This document happens to reuse the same author/reviewer split (Sonnet
writes, Opus reviews, both fixed, not configurable) for building the tool
itself, because it's the same philosophy at a different level, not
because the two are the same mechanism.

## The loop

One pass of this loop = one module = one PR. Don't batch multiple modules
into one pass to save time — that's exactly the "overwhelming PR" failure
mode this process exists to avoid.

1. **Spec.** First, grep `PLAN.md` §9 ("Not Yet Implemented") and §10
   ("Planned, not yet designed in detail") for an existing entry on the
   same topic — a new spec that duplicates or contradicts one of those
   without noticing costs a whole separate reconciliation PR to fix
   later (this happened once: `specs/resource_tagging.md` shipped
   without checking §10's pre-existing "Resource tagging convention"
   entry, caught only by a later `/code-review` pass). If one exists,
   the new spec must explicitly cross-reference it — implement it,
   narrow it with a stated reason, or extend it, never silently ignore
   it. Then write `specs/<module>.md` before touching code. See format
   below. If a spec for this module already exists from a prior pass and
   nothing about it changed, skip re-writing it — but check it's still
   accurate against what's actually being built.
2. **Tests first (red).** Write the test file in `tests/` against the
   spec, not against implementation that doesn't exist yet. Run it.
   **It must fail** (or error — module not found is a valid "red"). If it
   passes immediately, the test isn't testing anything real — fix the
   test before writing any implementation.
3. **Implement.** Write the minimum code to satisfy the spec and make the
   tests pass. No abstractions, config knobs, or error handling beyond
   what the spec calls for — same rule CLAUDE.md already states for the
   codebase generally.
4. **Tests pass (green).** Rerun the module's tests, then the full suite.
   All green before moving on.
5. **Independent review.** Run Claude Code's `/code-review` (Opus 5)
   against the diff. Address findings, or explicitly note in the PR why a
   finding is being deferred — don't silently ignore one either.
6. **PR.** Small, one module (or one tightly-coupled pair, e.g. a module
   and the exceptions it raises) per PR, following
   `.claude/skills/github-commit-process/SKILL.md`. CI must be green.
   Human reviews and approves — nothing merges without that, same rule as
   always.
7. **Move on.** The next module's spec may treat this module's interface
   as fixed. If building the next module reveals this one's interface was
   wrong or incomplete, don't quietly work around it — go back, fix the
   spec and code in a small follow-up, and flag it, the same way
   CLAUDE.md asks for `PLAN.md` discrepancies to be flagged rather than
   silently diverged from.

## Why this loop, specifically

- **Spec before code** gives the human something short to review *before*
  a diff exists, and keeps mid-implementation scope creep out.
- **Red before green** guards against the single most common TDD failure:
  a test that passes no matter what the implementation does, because it
  was never actually seen failing.
- **Opus review gate** mirrors the runtime philosophy this project
  already commits to — cheap/fast model produces, a more careful model
  reviews before it's trusted. Applying it to the build process itself
  keeps the project internally consistent.
- **One module per PR** is what makes "human reads and understands
  without being overwhelmed" achievable in practice, not just an
  aspiration.

## Spec format (`specs/<module>.md`)

Keep it short — half a page, not a design essay. Sections:

- **Purpose** — one or two sentences.
- **Interface** — functions/classes exposed, argument order, return
  types. Where `PLAN.md` already fixes this (e.g. §4's module contract),
  point at it instead of restating it.
- **Behavior** — bullet list of what it must do, phrased so each bullet
  maps to one or a few tests.
- **Edge cases / errors** — what's explicitly handled, what raises what.
- **Out of scope** — what this module deliberately does not do yet,
  referencing `PLAN.md` §9 if it's a known deferred item.

## Mapping onto CLAUDE.md's implementation order

CLAUDE.md's "Suggested implementation order" groups work into 5 broad
steps. This process doesn't reorder or change that sequencing — it just
adds finer-grained PR boundaries inside it, since some of those steps
bundle more than one module:

1. `aiform/models.py` (+ `exceptions.py`) → `state.py` → `config.py` —
   likely 2–3 specs/PRs, not one.
2. `aiform/llm.py` — 1 spec/PR.
3. `aiform/driver.py` (the `ResourceDriver` ABC) → `aiform/driver_gen.py` —
   likely 2 specs/PRs (check whether `parser.py` is a dependency that
   needs to land first; if so, split it out too).
4. `drivers/digitalocean/compute.py` — 1 spec/PR.
5. `aiform/planner.py` → `orchestrator.py` → `cli.py` — likely 3 specs/PRs.

Exact splitting is decided when each step is actually started, not locked
in here.

## Supporting practices

- **Branching, commits, PRs**: already fully specified in
  `.claude/skills/github-commit-process/SKILL.md`. Nothing new added by
  this document — that skill is the authority. See "PR approval and
  merge" below for a human-readable summary of how that skill decides
  when a PR is actually allowed to merge.
- **CI**: a GitHub Actions workflow (`.github/workflows/tests.yml`) runs
  `pytest` on every PR. This turns "tests pass" from something someone
  remembers to check into something that blocks merge. It's a no-op
  until `pyproject.toml` and `tests/` exist, then activates automatically.
- **Lint/format**: `ruff check` and `ruff format` (config in
  `pyproject.toml`'s `[tool.ruff]`), enforced two ways — a local
  `pre-commit` hook (`.pre-commit-config.yaml`, installed via
  `pre-commit install` once per clone) so the feedback loop is
  immediate, and the same two checks in CI so a `--no-verify`d commit
  still gets caught before merge. Run `ruff format .` before committing
  if you ever bypass the hook.
- **Definition of done**, per module: spec exists and is accurate; tests
  exist and were actually observed failing; implementation makes them
  pass; `/code-review` ran and findings were addressed or explicitly
  deferred; CI is green; PR is open and awaiting human approval.
- **Specs are living docs, not write-once**: if implementation reveals a
  spec was wrong, update it in the same PR and say so — same treatment
  `PLAN.md` itself asks for at the top of CLAUDE.md.

## PR approval and merge

`.claude/skills/github-commit-process/SKILL.md` is the authority for the
exact mechanics (GitHub comment polling, trigger-ordering rules, and so
on) — this section is a human-readable summary of what it does and why,
not a duplicate. If the two ever disagree, the skill wins; update this
section to match rather than the other way around.

Step 6 of the loop above says "nothing merges without human approval."
Concretely, that approval is **three independent GitHub signals, all
required**, before a merge happens:

- **`/claude-merge`** — a PR comment or review body from the repo
  owner's GitHub account, authorizing the merge itself. A native GitHub
  "Approve" review doesn't substitute for this: GitHub blocks a PR's
  author from approving their own PR, and every PR in this repo is
  opened by the same account.
- **A passing `opus-review` status** — either a real `/code-review` run
  against the diff, or an explicit **`/claude-skip-review`** comment
  authorizing skipping it. `/claude-skip-review` exists for small,
  mechanical changes not worth a full review (e.g. a docs-only PR) —
  it's a deliberate escape hatch, not a way around the human-approval
  rule itself, since `/claude-merge` is still separately required.
- **A green `test` CI check** on the exact head SHA being merged. Unlike
  the other two this has **no override** — no comment waives it — and it
  is the only one enforced by GitHub rather than by convention (`main`
  carries branch protection requiring it, with `enforce_admins: true`, so
  even the repo owner cannot merge past a red build). Added 2026-08-28
  after `main` sat red from 2026-08-17 through thirteen merges, every one
  of which satisfied the previous two-signal rule exactly: the merge check
  simply never looked at CI.

A **`/claude-reject`** comment stops the merge instead, overriding
anything else posted. If more than one trigger comment is present, only
the most recently posted one counts.

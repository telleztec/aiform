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

The same rule applies to bug fixes, where the unit is an issue rather than
a module: **one GitHub issue is closed by one PR.** A PR may close zero
issues — process changes and chores don't need one invented — but never
two without an explicit human waiver: the issues disclosed in the PR
description, and the human approving with `/claude-merge-approved-multi`
rather than the plain trigger. If an issue turns out to be
too big for a single PR, split the *issue*; two PRs both claiming to fix
one issue leave it half-fixed with no record of which half landed.
`.claude/skills/github-commit-process/SKILL.md`'s "One issue, one PR"
section is the authority; this paragraph exists so the loop reads
completely on its own.

1. **Spec.** First, grep `PLAN.md` §10 ("Not Yet Implemented", including
   its "Planned, not yet designed in detail" subsection) for an existing
   entry on the same topic — a new spec that duplicates or contradicts
   one of those without noticing costs a whole separate reconciliation PR to fix
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
5. **Live system test.** `pytest` proves the code does what its mocks
   were told to expect. It cannot prove DigitalOcean agrees. Before the
   PR can merge, run the live suite against a real account —
   `.venv/bin/python scripts/run_system_tests.py`
   (`specs/run_system_tests.md`) — from a checkout of the exact commit
   being merged, and record the result as the `system-test` status. See
   "PR approval and merge" below for when this is required, when it may
   be carried forward, and when it is N/A.

   This step costs real money and creates real droplets. That is the
   point: every bug this process has caught late was a bug about what a
   provider actually does, not about what the code says it does.
6. **Independent review.** Run Claude Code's `/code-review` (Opus 5 or newer,
   and never the model that authored the diff) against the diff. You launch
   this yourself — it does not wait on the human. Address findings, or
   explicitly note in the PR why a finding is being deferred — don't
   silently ignore one either. **Then review your own fixes**: they are code
   no pass has read, so run `/code-review-since <PR>` over each round until
   the head commit has been covered. This step and the human's review are
   independent; neither blocks the other.
7. **PR.** Small, one module (or one tightly-coupled pair, e.g. a module
   and the exceptions it raises) per PR, following
   `.claude/skills/github-commit-process/SKILL.md`. A PR closes at most
   one GitHub issue; if the pair is two issues, that needs a human waiver
   (SKILL.md's "Closing more than one issue"), not a second exception.
   CI must be green.
   Human reviews and approves — nothing merges without that, same rule as
   always.
8. **Move on.** The next module's spec may treat this module's interface
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
  referencing `PLAN.md` §10 if it's a known deferred item.

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
- **Start from current `origin/main`, and don't code in the main
  checkout**: two habits, both cheap, both learned from real misses.
  - *When planning*, `git fetch origin` first, then read from
    `origin/main` (`git show origin/main:PLAN.md`) rather than from
    whatever the main checkout happens to have checked out. A stale
    checkout silently answers "does this exist yet?" with the wrong
    answer, and a plan built on that answer is wrong in its structure,
    not just its details: one planning pass concluded the DigitalOcean
    domain driver was still unmerged work on a branch and designed a
    whole stacking-and-duplication strategy around reaching
    `UNORDERED_FIELDS` and `_common.py` — all of which had landed on
    `main` days earlier, importable in one line. The checkout was 39
    commits behind. Before trusting any "X doesn't exist yet"
    conclusion, run
    `git rev-list --left-right --count main...origin/main`, and prefer
    `git grep <pattern> origin/main` over grepping the working tree.
  - *When coding and testing*, work in a dedicated worktree —
    `git fetch origin && git worktree add --no-track .claude/worktrees/<branch> -b <branch> origin/main`
    — not in the main checkout. Each pass then starts from current
    `main` by construction; several passes can be in flight without one
    pass's half-finished tree breaking another's test run; and the main
    checkout stays a clean, current place to read from. Run
    `direnv allow` once in each new worktree: `.envrc` and `.aiform/`
    are both per-directory, so until you do, a fresh worktree has
    neither `DIGITALOCEAN_TOKEN` in the environment nor a
    `credentials.env` to fall back to. `config.resolve_credentials()`
    then raises a `RuntimeError` naming both the env var and the file, so
    the cost is one wasted run rather than a mystery. Two caveats worth
    knowing: an ambient token exported globally would be inherited
    instead, which is the failure `.envrc`'s own preamble exists to
    prevent; and direnv does not fire in non-interactive shells, so an
    agent's shell inherits whatever the session started with regardless
    of `direnv allow`. `--no-track` keeps the new branch from taking
    `origin/main` as its upstream, which would otherwise make
    `git status` report ahead/behind against the wrong ref.
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
  pass; the live system test ran green (or is recorded N/A, per "PR
  approval and merge"); `/code-review` ran and findings were addressed or
  explicitly deferred; CI is green; PR is open and awaiting human
  approval.
- **Specs are living docs, not write-once**: if implementation reveals a
  spec was wrong, update it in the same PR and say so — same treatment
  `PLAN.md` itself asks for at the top of CLAUDE.md.

## PR approval and merge

`.claude/skills/github-commit-process/SKILL.md` is the authority for the
exact mechanics (GitHub comment polling, trigger-ordering rules, and so
on) — this section is a human-readable summary of what it does and why,
not a duplicate. If the two ever disagree, the skill wins; update this
section to match rather than the other way around.

Step 7 of the loop above says "nothing merges without human approval."
Concretely, a merge needs **four gates, all green on the exact head SHA**:

- **`human-approval`** — posted when the repo owner leaves
  `/claude-merge-approved` as a PR comment or review body — or
  `/claude-merge-approved-multi`, when the PR closes more than one issue. A native GitHub
  "Approve" review doesn't substitute: GitHub blocks a PR's author from
  approving their own PR, and every PR here is opened by the same account.
- **`llm-review`** — means the SHA's content was read by a reviewer (Opus 5
  or newer, never the authoring model). On head it is the gate and means
  read *and* resolved — every finding fixed or explicitly deferred on the
  PR; it is never posted on head while anything is open. On earlier SHAs it
  is review history, and the checkpoint `/code-review-since` walks back to.
  The author triggers all of this itself, and there is no skip path. Fix
  commits are unread code, so each round is re-reviewed incrementally until
  head is covered.
- **`test`** — CI green. No override exists; no comment waives it.
- **`system-test`** — the live suite ran green against real DigitalOcean
  and Anthropic APIs, on this SHA's content. Posted by the author, like
  `llm-review`. CI cannot post it: it has no credentials, and must never
  create billable resources.

**When `system-test` requires an actual run.** The check is path-based,
like the cosmetic carry-forward below. Runtime paths are `aiform/**.py`,
`drivers/**.py` and `pyproject.toml` — everything else (prose, specs,
`tests/**`, `scripts/**`) cannot change what the tool does against a
provider:

```sh
# Prints the paths that make a live run mandatory. Empty output means none.
# awk, not `grep -v` -- grep here is ugrep, whose -qv does not invert.
git diff --name-only <since-sha> <pr-head-sha> \
  | awk '/^aiform\/.*\.py$/ || /^drivers\/.*\.py$/ || $0=="pyproject.toml"'
```

Three outcomes, and `<since-sha>` differs between them:

1. **Nothing to run** — the check against the PR's *base* is empty. Post
   `success` with description `n/a: no runtime path in this diff`.
2. **Carry forward** — an earlier SHA on this branch already has a green
   `system-test`, and the check from *that SHA* to head is empty. Post
   `success` naming it: `carried from <sha>: prose-only delta`. A
   ten-minute billable suite should not re-run for a typo fix, and the
   path check is what makes that safe to say.
3. **Run it** — everything else. `.venv/bin/python
   scripts/run_system_tests.py` from a checkout of the head SHA; it must
   exit 0. Put the log filename it wrote in the description, so the
   status points at evidence rather than asserting a result.

**The check is deliberately conservative about `.py` files.** It reads
paths, not content, so a comment-only edit to `aiform/driver.py` re-triggers
the suite. Making it content-aware — "this diff is only comments, skip it" —
is exactly the cleverness that produces a false N/A on the one gate that
says anything about real infrastructure. Pay the run.

Be precise about what this buys, the same way the paragraph below is
about the other two: `system-test` is posted **by the author**, so it
catches "the agent skipped the live run", not "the agent lied about it".
The log file under `.aiform/testlog/` is the artifact a human can check.

**The two reviews are order-independent.** The human may approve before the
LLM review runs or after; either order ends in a merge. Nothing waits on
anything else.

**Any new commit clears all four**, because each is pinned to a SHA and a
new commit mints a new one. That single rule covers every restart case: the
author fixing review findings, the human pushing their own commits, or a
branch update to catch up with `main`. The lone exception is that
`human-approval` may be carried forward onto a new SHA when the delta since
the approved commit is provably prose — `*.md` files **excluding**
`.claude/**`, `prompts/**`, `CLAUDE.md` and `PROCESS.md`, which are markdown
that agents execute and can therefore rewrite the rules themselves. The check
is path-based; if it doesn't pass cleanly the change is not cosmetic, however
small it looks. `llm-review` is never carried forward; it re-runs, which now
costs no round-trip.

**`test`, `llm-review` and `human-approval` are required by branch
protection** (`strict: true`, `enforce_admins: true`), so even the repo
owner cannot merge past a missing one. Worth being precise about what that
guarantees: `llm-review` and `human-approval` are posted *by the agent*, so
requiring them catches "the agent forgot", not "the agent misbehaves". Only
`test` is enforced against an actively wrong agent.

**`system-test` is NOT in branch protection yet**, deliberately: adding a
required context is a repo-settings change that can block every open PR if
the new gate is wrong, so it is the repo owner's call rather than an
agent's. Until they make it, this document is the requirement and the
status is the record — which means for this one gate, "the agent forgot"
is not caught either. The command, when they want it:

```sh
gh api -X PATCH repos/{owner}/{repo}/branches/main/protection/required_status_checks \
  -f 'contexts[]=test' -f 'contexts[]=llm-review' \
  -f 'contexts[]=human-approval' -f 'contexts[]=system-test'
```

A **`/claude-merge-rejected`** comment stops the merge instead. Its feedback
must be read and addressed in a new commit, which by the rule above restarts
the cycle. If more than one trigger is present, only the most recent counts
— and triggers older than the current head commit are ignored entirely,
since they refer to code that no longer exists.

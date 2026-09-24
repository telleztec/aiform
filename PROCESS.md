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
writes, Opus reviews by default, chosen per change per "What counts as a
plan" below) for building the tool itself, because it's the same
philosophy at a different level, not because the two are the same
mechanism.

## Before the loop: plan and get explicit approval

This section is a gate, not a step of the loop below — it runs *before*
the loop's step 1 (Spec), and answers a different question. Step 1 asks
"what does this module's interface look like"; this gate asks "should any
implementation work start at all, and on which approach" for anything the
human hasn't already committed to. Both must pass; neither substitutes for
the other.

It exists because of a concrete failure on 2026-09-18: the coordinating
session, on hearing the human describe a live-test timeout, spawned an
implementation agent immediately — no written approach, no human sign-off
on one — and the agent shipped a full exponential-backoff redesign of
`_poll_until` (PR #172) when what the human actually wanted was something
larger and different: a centralized, configurable, runtime-modifiable
timeout policy, separately proposed in issue #154 — which the coordinating
session hadn't even noticed was related before building #172. The same
evening also produced PR #170 from the identical undisciplined pattern —
smaller, and this time the right change, but arrived at the same
unapproved way. Getting the right answer once doesn't validate the
process that produced it: this gate exists so the outcome doesn't depend
on which one the coordinator happens to land on.

### Who this binds

**The coordinating Claude Code session's own delegation behavior** —
specifically, the decision to spawn a subagent that will write
implementation code, or to start writing implementation code itself. It is
not a rule about what an already-spawned implementation agent does
internally; that agent still follows the loop below and
`.claude/skills/tdd-workflow/SKILL.md` exactly as before. This gate decides
whether, and on what approach, that agent gets spawned in the first place.

### When it applies

Before any implementation work begins on a change — a new module, a bug
fix, a behavior or design change, a refactor with behavior implications:
anything that will produce a diff to `.py` files, `drivers/**`,
`prompts/**`, `.github/workflows/**`, `PLAN.md` (a `PLAN.md` edit *is* a
design change, by this document's own opening line), or any other path
"PR approval and merge" below already treats as runtime-affecting. It also
covers edits to this repo's own governing process — `CLAUDE.md`,
`PROCESS.md`, `.claude/**` — since those are markdown the agents in this
repo execute, not inert prose; the same reasoning that keeps them out of
`human-approval`'s cosmetic carry-forward below applies here.

Does **not** apply: a pure prose/documentation edit with no behavior
change — the same category the cosmetic carry-forward recognizes (`*.md`
files, excluding `.claude/**`, `prompts/**`, `CLAUDE.md`, `PROCESS.md`,
and, per this section's own "when it applies" above, `PLAN.md` — the
cosmetic carry-forward's own exclusion list doesn't need `PLAN.md` since
`PLAN.md` isn't `human-approval`'s concern, but this gate's does, so it's
added here rather than borrowed unmodified) — or work the human has
already approved a specific plan for and is now simply asking to be
executed. A comment-only edit inside a `.py` file does **not** qualify
for this exemption: this document already refuses that exact "it's only
a comment" judgment call for `system-test`'s path check ("The check is
deliberately conservative about `.py` files"), for the same reason —
content-aware exemptions are where a gate like this quietly stops meaning
anything.

There is no size exception. "Small" or "mechanical" is not a reason to
skip this gate — PR #170 was both, and still should have gone through it.

### What counts as a plan

A written, specific description of the proposed approach, visible to the
human **before** any implementation agent is spawned or any code or test
file is written:

- **what** will change — files/modules affected, at the level of
  specificity the human could actually disagree with;
- **why** — what problem it solves, referencing the actual complaint or
  issue, not a paraphrase of it;
- **how** — the mechanism. "Add exponential backoff with a cap to
  `_poll_until`" and "introduce a config-driven, LLM-adjustable timeout
  policy store" are two different plans even though both respond to the
  same timeout report — proposing one is not proposing the other.
- **which author/reviewer model pairing it will use, and why.**
  `llm-review`'s rule is "Opus 5 or newer, and never the model that
  authored the diff" — that constrains the reviewer, not the author, so it
  doesn't reduce to a fixed pair of choices. `.claude/skills/github-commit-process/SKILL.md`'s
  "Choosing who authors the diff also chooses who reviews it" has the
  current roster and the two pairings this repo defaults to — cite that
  table rather than restating it here, since the roster is the most
  perishable fact in either document. What matters for this bullet:
  authoring with Opus is the one choice that *forces* the reviewer, to the
  more expensive Fable 5.1 (the repo owner's 2026-09-21 standing
  instruction is to stop defaulting to Fable), so naming the pairing here
  is what makes that cost the human's choice instead of one the
  coordinating session makes for them by picking who to spawn. A
  Sonnet-or-Haiku-authored change still names a pairing — usually the
  cheap default — because that's also where the human would object if the
  change warrants a stronger reviewer than the default.

A chat message counts if it is specific enough to satisfy those four
bullets. Plan mode's own output, or a short scratch doc, is preferable for
anything non-trivial, because it leaves an artifact the human can point
back to when approving. A `specs/<module>.md` file (loop step 1) is not
itself this plan — it is normally written by the implementer, after the
decision to implement has already been made, and it's scoped per module
rather than per change; a bug fix in particular often has no `specs/`
entry to double as one.

The pairing is a per-change judgment, not a per-project or per-session
default: anticipated complexity varies change to change, so say what you
expect and why for *this* change — a hard one may be worth starting with
Opus, a routine one usually isn't.

This is deliberately not a second gate, and not a standalone prompt asking
which model to use before work starts. The plan-approval gate above
already fires at exactly the moment this decision needs making, and
`.claude/skills/github-commit-process/SKILL.md`'s "Closing more than one
issue" warns against exactly this shape of fix — "a second exception with
its own boundary to argue about" — for the same reason: another checkpoint
here would just be one more thing to remember to run. A prompt would also
ask a question nobody records the answer to; naming the pairing as a plan
item instead means it's approved in the same act as the rest of the plan,
and — because the approved plan carries into the PR's `## Plan` section
(see "Recording it" below) — a reviewer can see whether the diff they're
reading was authored by the cheaper-and-weaker or the costlier-and-stronger
model, which is exactly the context needed to judge how much weight the
review pass was carrying.

### What counts as approval

The human, having seen that specific written plan, says to proceed with
**it**. Concretely:

- **Counts:** the human has read a written approach (per the bullets
  above) and responds with something that unambiguously greenlights that
  approach — "yes, do that," "go ahead with the backoff-cap approach,"
  approving a plan-mode exit, and the like.
- **Does not count:** the human describing a bug or problem — "the live
  test keeps timing out." That's a problem report, not a plan.
- **Does not count:** the human agreeing the problem is worth fixing —
  "yeah, that's worth fixing." That's agreement on priority, not approval
  of an approach; nothing has been proposed yet at that point.
- **Does not count:** silence, or the absence of an objection. If the
  coordinator described a plan and got no response, that is not approval.
- **Does not count:** a follow-up idea mentioned in passing while
  discussing something else, unless a specific plan for *that* idea was
  then written down and the human responded to it.

When in doubt whether a given human message clears this bar, it doesn't —
ask a direct yes/no question against the specific plan rather than
proceeding on a generous reading.

This is a distinct mechanism from `human-approval` on a PR
(`/claude-merge-approved`, see "PR approval and merge" below): that gate
approves a finished diff at merge time; this one approves an approach
before the diff exists. Don't conflate the two — a plan approval doesn't
skip PR review, and a PR merge approval doesn't retroactively excuse
skipping this gate.

### If implementation already started without this gate

Stop adding commits. Write the plan now — covering what's already been
done and what remains, using the "what/why/how/pairing" bar above — and
get the human's explicit approval on it before any further implementation
work,
exactly as if no code existed yet. Work already merged or already shipped
isn't undone by this gate retroactively; it's simply a reason the
*remaining* work on that change needs a plan before it continues, not a
precedent that skipping the gate once makes skipping it again acceptable.

### Recording it

Every other gate this document defines is an external, GitHub-visible
artifact, precisely because chat history is not durable — a long
conversation, a compaction, or a fresh agent instance resuming the same
work can all silently lose a chat-only approval
(`.claude/skills/github-commit-process/SKILL.md`'s "critically, all four
are external... never something inferred from conversation history" makes
exactly this argument for the merge gates). This gate is chat-native —
there's no GitHub artifact to attach it to before a PR exists — so it
doesn't get that guarantee for free. To get as close as this shape of gate
can: when the PR is opened, its description must include a `## Plan`
section — one lead sentence or short paragraph stating what was approved,
then bullets for the key points, where practical quoting or summarizing
the human's approval (`.claude/skills/github-commit-process/SKILL.md`'s
"Opening a PR" has the exact template). A PR without one is a PR whose
plan-approval cannot be checked by anyone reading only the PR later, which
defeats the point.

### Once approved

Implementation proceeds through the loop below starting at step 1, exactly
as before. The approved plan doesn't replace the module spec
(`specs/<module>.md`) — it's coarser and comes earlier — but the spec
should not contradict it. If writing the spec reveals the approved
approach was wrong at the level of detail a spec captures, that's the same
situation this document's "Specs are living docs, not write-once" practice
already covers: update it in the same PR and say so. But if what's wrong
is the approach itself, not just its write-up — the mechanism the human
approved turns out to be the wrong mechanism — that's not a spec fix. Stop
and take the revised approach back to the human for approval, the same as
if none had been given yet; don't quietly implement something else under
the old approval.

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
   were told to expect. It cannot prove DigitalOcean agrees. Run the live
   suite against a real account **at merge time**, on the settled head —
   a run on a commit that review then changes proves nothing about what
   merges —
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
  `llm-review`. **The default `pull_request`/`push` CI triggers must
  never run it** — `.github/workflows/tests.yml` holds no credentials
  and a PR-triggered run would create billable resources on every push.
  That is narrower than "CI cannot": `specs/system_test.md`'s "Edge
  cases / errors" prescribes a `schedule` + `workflow_dispatch` workflow
  that *does* hold both tokens as repo secrets, and nothing here forbids
  a future opt-in workflow posting this status from such a run. (Its
  "Orphan cleanup" section prescribes a second, different scheduled
  workflow, with `DIGITALOCEAN_TOKEN` only — an earlier version of this
  paragraph cited that one by mistake.)

**When `system-test` requires an actual run.** The check is path-based,
like the cosmetic carry-forward below. A **runtime path** is anything that
can change what the tool does against a provider, or what the live suite
proves about it:

```sh
# Prints the paths that make a live run mandatory. Empty output means none.
# awk, not `grep -v` -- grep here is ugrep, whose -qv does not invert.
# Note awk exits 0 whether or not it matched, so key on the OUTPUT being
# empty; `... | awk '...' && foo` is always true.
git diff --name-only <since-sha> <pr-head-sha> | awk '
    /^aiform\/.*\.py$/ ||
    /^drivers\/.*\.py$/ ||
    /^prompts\// ||
    /^tests\/system\// ||
    /(^|\/)conftest\.py$/ ||
    $0=="tests/__init__.py" ||
    $0=="scripts/run_system_tests.py" ||
    $0=="pyproject.toml"'
```

Four of those are not obvious and were missed by the first version of
this rule:

- **`prompts/**`** — `aiform/llm.py` `read_text()`s these on every
  Anthropic call. `diff_plan.md` *is* the plan categorizer; rewording it
  so a size change classifies as `update` rather than a replace changes
  live behaviour with no `.py` diff at all. `SKILL.md`'s cosmetic
  carry-forward already treats `prompts/**` as markdown that executes;
  the two lists must not disagree.
- **`tests/system/**`** — a changed suite changes what a green gate
  *proves*. Exempting it would let a PR weaken an assertion and inherit
  a pass.
- **`scripts/run_system_tests.py`** — same reason, one level up: it is
  the runner whose exit code the gate reads.
- **Any `conftest.py`, and `tests/__init__.py`** — `tests/conftest.py` is
  loaded for a `tests/system/` run too, not just the unit suite:
  `pytest tests/system --fixtures` lists `forbid_llm_client` from it. So
  a PR that patched `urlopen` in an autouse fixture there, or dropped
  `_scan_for_leaked_credentials`'s assert, would change what a green
  gate proves while the path check printed nothing. Exempting it was a
  hole found by review, on exactly the reasoning that had already
  earned `tests/system/**` its place.

Do not shorten this list on the reasoning that some path "is only
tests" or "is only prose". A false N/A is the failure mode this gate
exists to prevent; a false "must run" only costs ten minutes.

Three outcomes, and `<since-sha>` differs between them:

1. **Nothing to run** — the check against the PR's base is empty, using
   `git fetch origin` then `origin/<base>...<head>` (**three** dots, so
   it compares against the merge base and lists only what this PR
   touched). Two dots on a branch behind its base lists what the *base*
   changed too, producing a false "must run"; a stale `origin` does the
   same, hence the fetch. Use the PR's **own** base, not `main` —
   `gh pr view <n> --json baseRefName`. A stacked PR whose base is
   another branch gets the whole stack's files against `main`, which is
   only ever a false "must run", never a false n/a, but it wastes a
   ten-minute suite. Post `success` with description
   `n/a: no runtime path in this diff`.
2. **Carry forward** — an earlier SHA on this branch already has a green
   `system-test`, and the check from *that SHA* to head is empty. Use a
   **two**-dot diff here. Not for the reason an earlier version of this
   line gave — while that SHA is an ancestor of head the two forms are
   identical, since `merge-base(X, head) == X` — but because a rebase can
   orphan the SHA the status sits on, and two-dot then still compares the
   two trees rather than searching for a merge base that no longer
   means anything. Post `success` naming the SHA, e.g.
   `carried from <sha>: no runtime path since`. A
   ten-minute billable suite should not re-run for a typo fix, and the
   path check is what makes that safe to say.
3. **Run it** — everything else. `.venv/bin/python
   scripts/run_system_tests.py` from the root of a checkout of the head
   SHA (`LOG_DIR` is relative to the working directory); it must exit 0.
   Put the log filename it wrote in the description, so the status points
   at evidence rather than asserting a result.

   **Read the log, do not trust a shell's exit status.** The first real
   use of this gate nearly recorded a false green: the runner was invoked
   in a compound command whose trailing `tail` supplied the exit code, so
   a suite that failed two tests reported success. Run the script as the
   last command, or capture `$?` immediately.

**The suite was not green on `main` when this gate was written, so until
that is fixed the gate blocks every PR.** The fix is **#140**; once it
merges this paragraph is history rather than a live warning, and can go.
A full run at the time of writing failed two tests —
`test_cli_digitalocean.py::TestFullLifecycleSequence::test_full_lifecycle`
and `test_cli_domain.py::TestDomainLifecycleSequence::test_full_lifecycle`
— both on the same assertion, `[verbose] 0 Anthropic API call(s) made` on
a first `plan create`, which actually costs exactly 1 because
`parser.parse_file()` calls `extract_intent_notes()` for a non-empty
`## Intent` section. That is issue #125, which diagnoses it and states
the correct count; #125 names only the domain suite, and the droplet
suite carries the identical bug. This gate is deliberately **not** given
a "known failures" allowance — an allowance is how a gate rots — so #125
had to be fixed first, and that was the honest cost of adding this gate
at all. #140 does it, and a full run on #140 is green: 10 passed, 448s.

**The check is deliberately conservative about `.py` files.** It reads
paths, not content, so a comment-only edit to `aiform/driver.py` re-triggers
the suite. Making it content-aware — "this diff is only comments, skip it" —
is exactly the cleverness that produces a false N/A on the one gate that
says anything about real infrastructure. Pay the run.

Be precise about what this buys. `system-test` is posted **by the
author** and is **not** in branch protection (see below), so unlike
`llm-review` it does not even catch "the agent forgot" — it is a
convention plus a record, and the record is only as honest as the agent
writing it.

Nor is the log a durable audit trail: `.aiform/testlog/` is gitignored,
rotates after ten runs, and records no commit SHA or dirty-tree state, so
it cannot by itself confirm the run happened *on this SHA's content*.
What ties the two together is only that the status is pinned to the SHA.
Making the log self-describing — a `git rev-parse HEAD` and
`git status --porcelain` header — would fix that, and belongs with
`scripts/run_system_tests.py` rather than in this document.

**The two reviews are order-independent.** The human may approve before the
LLM review runs or after; either order ends in a merge. Nothing waits on
anything else.

**Any new commit clears all four**, because each is pinned to a SHA and a
new commit mints a new one — with two carry-forward exceptions,
`human-approval`'s cosmetic one below and `system-test`'s runtime-path
one above. They are keyed on different path lists because they answer
different questions. That single rule covers every restart case: the
author fixing review findings, the human pushing their own commits, or a
branch update to catch up with `main`. The first exception is that
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

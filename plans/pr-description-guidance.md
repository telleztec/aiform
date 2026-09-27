# Plan: PR descriptions describe the change, not the process that made it

## Problem

A PR description in this repo currently accumulates process narrative. PR #217
is the worked example: a `### What review found` subsection listing eight
review rounds and four defects, plus round counts and commit-by-commit history,
all inside the body. That content is already in the commit log and the review
statuses. In the body it crowds out the thing a reader actually needs, which is
what the change does and why.

Three concrete defects in `.claude/skills/github-commit-process/SKILL.md`'s
"Opening a PR" template:

1. `## Summary` is specified as "What changed, as 1-3 bullets" — bullets only.
   There is no lead sentence, so every PR opens by dropping the reader straight
   into specifics with no statement of what the PR is.
2. `## Plan` asks for "what the human approved and how", but never says to
   **link** the plan. Since #217 this repo commits plans to `plans/`, so there
   is a file to point at and no reason to paraphrase it.
3. Nothing says the body is not a changelog of its own review. The template
   even invites some of it ("If live/discovery work surfaced something during
   implementation: one bullet per finding").

## What changes

All edits are to `.claude/skills/github-commit-process/SKILL.md`.

**A. Summary gets a lead.** Template becomes a 1-3 sentence prose lead stating
what the PR does and the problem it solves, *then* bullets for specifics. A
reader who stops after the lead should know what changed and why. Add a
matching prose rule next to the existing "Keep `## Plan` terse" rule.

**B. Plan links the plan.** `## Plan` must link the committed plan file
(`plans/<name>.md`) rather than restate it. If the work went through the plan
gate, the plan is committed in the same PR. The existing plan-gate-exempt
branch of that section stays as-is — an exempt PR has no plan to link, and the
template already handles that case.

**C. Body is not review history.** New rule: the description states the change
as it will be read six months from now. It does not carry review rounds,
defect counts, "what review found", or a narrative of which commit fixed what —
git log and the `llm-review` statuses hold that. Paired with an explicit
positive rule in "Satisfying `llm-review`": **every review round posts its own
PR comment**, so N rounds leave N comments. #217's
[round-8 comment](https://github.com/telleztec/aiform/pull/217#issuecomment-5851317837)
is the reference shape.

**D. Reconcile the contradiction C creates.** The `## Plan` bullet "If
live/discovery work surfaced something during implementation: one bullet per
finding, terse" is process narrative by C's rule. The agent must resolve this
explicitly — either scope it to findings that change what was approved (a
deviation the reader needs) or drop it — not leave two rules that disagree.
Same check across `PROCESS.md` and `CLAUDE.md` for any sibling claim about PR
body contents, per the standing "grep every corrected claim" rule.

**E. State the principle.** Nothing in this repo mentions Torvalds, so the
guidance the template is meant to follow is currently unwritten. Add one short
grounding statement: a description explains the problem and the change,
self-contained, for a reader who was not present — it is not a log of how the
change was produced. This is an assumption being made explicit, flagged here
rather than smuggled in.

## Out of scope

No change to the four merge gates, the plan gate, or the pairing rules.

## Pairing

**Sonnet authors, Opus 5 reviews** — the repo's cheap default, and permitted
since an Opus coordinator may review a Sonnet subagent's diff. Chosen by the
human in the request.

## Gate

`.claude/**` is covered by `PROCESS.md`'s plan gate ("markdown the agents in
this repo execute", no size exception), so this is not a prose-edit exemption.
It needs explicit approval of this plan before implementation.

## Verification

Prose only, no tests. Verified by rewriting PR #217's body against the revised
template and checking all three defects are gone.

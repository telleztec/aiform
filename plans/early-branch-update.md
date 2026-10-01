# Plan: update the branch before review (#250)

Approved by the repo owner on 2026-10-01 through the decision page
https://claude.ai/artifact/LWLyiHekBBGmGDZcmBLSPX (plan id `strict-reapproval`,
page version `2026-10-01T18:48:48Z`), read back from the page database after
the owner said in chat they had answered. Recorded choices: `d1` early-only,
`d2` yes, `d3` owner (not applicable: `strict` stays true), `approve`
approved.

Pairing: Sonnet 5 authors, Opus 5 reviews.

## Problem

An approved PR goes BEHIND `main` when another PR merges first. Updating the
branch makes a new head SHA, which clears `human-approval`, so the owner posts
`/claude-merge-approved` a second time for a change that did not change.

## Decision

- `strict: true` stays on. No script, no Action, no setting change.
- Update the PR branch from `main` before review starts, so the reviewed and
  approved commit is the one that merges.

## What is built

- One step in `.claude/skills/github-commit-process/SKILL.md`, before the
  watch loop and `/code-review` start: fetch, check `git log HEAD..origin/main`,
  and if the branch is behind, merge `main` in and push first. The existing
  "If the merge is rejected as behind `main`" section stays as the fallback.
- The same step mirrored in `PROCESS.md` where it describes opening a PR.
- Docs only. No code, no test.

## Not built

- No carry-forward script, no GitHub Action, no `strict: false`.

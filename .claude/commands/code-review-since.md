---
allowed-tools: Bash(gh issue view:*), Bash(gh search:*), Bash(gh issue list:*), Bash(gh pr comment:*), Bash(gh pr diff:*), Bash(gh pr view:*), Bash(gh pr list:*), Bash(gh api:*), Bash(git diff:*), Bash(git log:*), Bash(git rev-parse:*), Bash(git merge-base:*), Bash(cat:*)
description: Code review a pull request, scoped to changes since a specific commit or named checkpoint
argument-hint: <PR#> [<since-sha>|base|last-review|today|session]
disable-model-invocation: false
---

Review pull request `<pr>`, scoped to changes since `<checkpoint>` — not the
PR's full diff against its base branch.

Parse `$ARGUMENTS` yourself; do not rely on `$1`/`$2` substitution. Split on
whitespace: the first token is `<pr>`. Everything after it, trimmed, is
`<checkpoint>`; default to `last-review` if nothing follows.

## Resolving `<checkpoint>` to a SHA

Compute once, reuse for every preset: base branch via `gh pr view <pr>
--json baseRefName --jq .baseRefName`, then `merge_base=$(git merge-base
origin/<base> <head-sha>)`. Bound every preset that walks commit history to
`<merge_base>..<head-sha>`.

- **`base`** — `merge_base` itself.
- **`last-review`** (default) — the most recent commit in `git log
  <merge_base>..<head-sha> --format=%H`, walked newest-first, that has a
  successful `llm-review` status: `gh api
  repos/{owner}/{repo}/commits/<sha>/status --jq '.statuses[] | select
  (.context=="llm-review") | .state'`. If the walk reaches `merge_base`
  with no match, fall back to `base` and state that in the final comment.
- **`today`** — the parent of the first commit made today in range: `git
  log --since=midnight <merge_base>..<head-sha> --format=%H | tail -1`,
  then that commit's parent (`<sha>^`). If the range has no commits from
  today, fall back to `base` and state that in the final comment.
- **`session`** — read `.claude/session-start-sha` at the repo root. If
  missing, stop and report: "No session marker found. Run `git rev-parse
  HEAD > .claude/session-start-sha` at the start of your work session,
  then re-run this command."

A literal SHA is used as-is.

Verify `git merge-base --is-ancestor <resolved-sha> <head-sha>`. If it
fails, stop and report the problem — do not fall back silently. State the
checkpoint name (if a preset was used) and the resolved short-SHA at the
top of the final comment.

## Steps

1. Haiku agent: confirm the PR is open, not a draft, not too trivial to
   review, and does not already have a review covering the current head
   SHA. If any of those hold, stop.
2. One Opus agent, given the PR number, resolved SHA, and head SHA:
   a. List relevant CLAUDE.md files: root CLAUDE.md, plus any in
      directories touched by `git diff --name-only
      <resolved-sha>..<head-sha>`.
   b. Read `git log <resolved-sha>..<head-sha>` and `git diff
      <resolved-sha>..<head-sha>` — not `gh pr diff`, which returns the
      full base..head diff regardless of the resolved SHA.
   c. Review that diff for CLAUDE.md compliance and bugs. Skip nitpicks
      and false positives (list below).
   d. Where warranted, check git blame/history, prior PRs on the same
      files, or in-code comments.
   Return candidate findings (issue plus the reason flagged), excluding
   anything in the false-positive list. Do not assign a confidence score.
3. If step 2 found nothing, skip to step 5 and post "no issues found."
4. Otherwise, one Sonnet agent, given the full candidate list, the
   CLAUDE.md paths and PR number from step 2, and the resolved and head
   SHAs: re-check every candidate against its own `git diff
   <resolved-sha>..<head-sha>` — not `gh pr diff` — and score each 0-100:
   a. 0 — false positive, or pre-existing (including a line unchanged
      since the resolved SHA).
   b. 25 — possibly real but unverified; or a stylistic nitpick CLAUDE.md
      does not call out.
   c. 50 — verified real, but a nitpick or low-impact.
   d. 75 — verified real, will be hit in practice, impacts functionality,
      or is explicitly required by CLAUDE.md.
   e. 100 — certain, frequent, directly evidenced.
   Filter to score ≥ 75. If none remain, skip to step 5 and post "no
   issues found."
5. Haiku agent: repeat step 1's check against the current head SHA.
6. Post the result with `gh pr comment`: brief, no emojis, link and cite
   the relevant code/files/URLs, and state the checkpoint (preset name if
   used, plus resolved short-SHA) at the top.

## False positives (steps 2 and 4)

- Pre-existing issues, including a line unchanged as of the resolved SHA
  — not just relative to the PR's base branch.
- Not actually a bug.
- Nitpicks a senior engineer wouldn't raise.
- Anything a linter, typechecker, compiler, or CI would catch.
- General code quality issues not required by CLAUDE.md.
- Issues CLAUDE.md flags but the code explicitly silences.
- Intentional functional changes.
- Real issues on lines not modified between the resolved SHA and head.

## Notes

- Do not build or typecheck the app.
- Use `gh` and `git`, not web fetch.
- Make a todo list first.
- Cite and link every finding.
- Comment format, exactly (3-issue example; checkpoint `last-review`
  resolved to `abc1234`):

---

### Code review (scoped to changes since `last-review` = `abc1234`)

Found 3 issues:

1. <brief description of bug> (CLAUDE.md says "<...>")

<link to file and line with full sha1 + line range for context>

2. <brief description of bug> (some/other/CLAUDE.md says "<...>")

<link to file and line with full sha1 + line range for context>

3. <brief description of bug> (bug due to <file and code snippet>)

<link to file and line with full sha1 + line range for context>

🤖 Generated with [Claude Code](https://claude.ai/code)

<sub>- If this code review was useful, please react with 👍. Otherwise, react with 👎.</sub>

---

No-issues variant:

---

### Code review (scoped to changes since `last-review` = `abc1234`)

No issues found in the changes since `abc1234`. Checked for bugs and CLAUDE.md compliance.

🤖 Generated with [Claude Code](https://claude.ai/code)

---

- Link format: `https://github.com/<owner>/<repo>/blob/<full-sha>/<path>#L<start>-L<end>`.
  - Full SHA required — no `$(git rev-parse HEAD)` substitution; the
    comment is rendered as-is.
  - Repo name must match the repo being reviewed.
  - `#` before the line range; range is `L[start]-L[end]`.
  - At least 1 line of context on each side of the commented line (e.g.
    lines 5-6 → `L4-7`).

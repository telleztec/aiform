---
allowed-tools: Bash(gh issue view:*), Bash(gh search:*), Bash(gh issue list:*), Bash(gh pr comment:*), Bash(gh pr diff:*), Bash(gh pr view:*), Bash(gh pr list:*), Bash(gh api:*), Bash(git diff:*), Bash(git log:*), Bash(git rev-parse:*), Bash(git merge-base:*), Bash(cat:*)
description: Code review a pull request, scoped to changes since a specific commit or named checkpoint
argument-hint: <PR#> [<since-sha>|base|last-review|today|session]
disable-model-invocation: false
---

Provide a code review for the given pull request, but scoped to only the
changes introduced **since** a specific point (`<checkpoint>`, below) on
that PR's branch — not the PR's full cumulative diff against its base
branch. This exists for re-review passes: `/code-review <PR#>` (the
unscoped command) always diffs against the PR's base branch, so a second
or third pass re-examines every line from every earlier round too; this
command instead reviews only what changed since the checkpoint you name.

**Parse `$ARGUMENTS` yourself — do not rely on `$1`/`$2` substitution.**
(Confirmed unreliable in this environment: a real invocation with
arguments `24 last-review` expanded every literal `$1` in this file to
`last-review` — the *second* argument — and left every `$2` completely
unsubstituted. Treat that as a standing fact about this Claude Code
installation, not something to re-verify each time.) `$ARGUMENTS` is the
raw argument string. Split it on whitespace: the first token is the PR
number, call it `<pr>` for the rest of this command. Everything after it,
trimmed, is the checkpoint argument, call it `<checkpoint>`. If nothing
follows the PR number, `<checkpoint>` defaults to `last-review`.

## Resolving `<checkpoint>` to an actual SHA

`<checkpoint>` may be a literal git SHA, or one of these presets:

First, compute the PR's merge-base once and reuse it for every preset
below: get the base branch name (`gh pr view <pr> --json baseRefName --jq
.baseRefName`), then `merge_base=$(git merge-base origin/<base>
<head-sha>)`. **Every preset that walks commit history is bounded to
`<merge_base>..<head-sha>` — never further back into the base branch's own
history.** This matters concretely on this repo: since PRs here routinely
merge `main` into a feature branch (see `github-commit-process`), and
nearly every commit on `main` already carries a passing `opus-review`
status from its own original PR, an unbounded walk from a feature branch's
head would immediately find one of *those* and stop — silently resolving
to an unrelated, ancient checkpoint instead of correctly recognizing "this
PR has no review of its own yet" and falling back to `base`.

- **`base`** — `merge_base` itself, computed above. Equivalent to
  reviewing the PR's full cumulative diff — same universe as the unscoped
  `/code-review <pr>`, just expressed through this command.
- **`last-review`** (the default) — the most recent commit *strictly
  within this PR's own range* (`git log <merge_base>..<head-sha>
  --format=%H`) that already has a successful `opus-review` GitHub commit
  status (posted either by a completed code review or an explicit human
  skip — see this repo's `github-commit-process` skill). Walk that
  bounded list newest-first, checking each via `gh api
  repos/{owner}/{repo}/commits/<sha>/status --jq '.statuses[] | select
  (.context=="opus-review") | .state'`, stopping at the first `success`.
  This is the most useful checkpoint for a re-review pass: "what's changed
  since this PR last passed review." If the walk reaches `merge_base`
  without finding one (this PR has no review of its own yet), fall back
  to `base` and say so explicitly in the final review comment. Because the
  walk is bounded to the PR's own commits, this is normally a short list —
  no need for a separate batching optimization.
- **`today`** — the parent of the first commit made today *within this
  PR's own range*: `git log --since=midnight <merge_base>..<head-sha>
  --format=%H | tail -1`, then take that commit's parent (`<sha>^`). If
  that range has no commits from today, fall back to `base` and say so
  explicitly in the final review comment.
- **`session`** — reads `.claude/session-start-sha` at the repo root, a
  local, gitignored, one-line file holding a SHA that gets written at the
  start of a work session (`git rev-parse HEAD > .claude/session-start-sha`).
  If the file is missing, **stop and report** "No session marker found.
  Run `git rev-parse HEAD > .claude/session-start-sha` at the start of
  your work session, then re-run this command" — unlike `today`, there is
  no derivable fallback for a session boundary, so don't guess one.

Resolve `<checkpoint>` to an actual SHA using the rules above before
proceeding. Verify it's actually an ancestor of the PR's current head with
`git merge-base --is-ancestor <resolved-sha> <head-sha>` — if it isn't (a
typo'd literal SHA, or a preset that resolved to something on a different
branch), stop and report the problem rather than silently falling back to
a full-PR review. State which preset (if any) was used and the resolved
short-SHA at the top of the final review comment.

To do this, follow these steps precisely:

1. Use a Haiku agent to check if the pull request (a) is closed, (b) is a
   draft, (c) does not need a code review (eg. because it is an automated
   pull request, or is very simple and obviously ok), or (d) already has a
   code review from you covering the *current* head SHA (not an earlier
   one — a review posted against an older SHA does not make this PR
   ineligible, since new commits landed since). If so, do not proceed.
2. Use another Haiku agent to give you a list of file paths to (but not the
   contents of) any relevant CLAUDE.md files from the codebase: the root
   CLAUDE.md file (if one exists), as well as any CLAUDE.md files in the
   directories whose files changed **between the resolved SHA and the PR's
   current head** (`git diff --name-only <resolved-sha>..<head-sha>`, not
   the full PR diff).
3. Use a Haiku agent to view the pull request, and ask the agent to return
   a summary of the change **based on `git log <resolved-sha>..<head-sha>`
   and `git diff <resolved-sha>..<head-sha>`** (not `gh pr diff`, which
   always returns the full base..head diff regardless of the resolved
   SHA).
4. Then, launch 5 parallel Opus agents to independently code review the
   change, each explicitly told their diff is `git diff <resolved-sha>..
   <head-sha>`, not the PR's full diff. The agents should do the
   following, then return a list of issues and the reason each issue was
   flagged (eg. CLAUDE.md adherence, bug, historical git context, etc.):
   a. Agent #1: Audit the changes introduced since the resolved SHA to
      make sure they comply with the CLAUDE.md. Note that CLAUDE.md is
      guidance for Claude as it writes code, so not all instructions will
      be applicable during code review.
   b. Agent #2: Read the file changes between the resolved SHA and the
      PR's current head, then do a shallow scan for obvious bugs. Avoid
      reading extra context beyond those changes, focusing just on the
      changes themselves. Focus on large bugs, and avoid small issues and
      nitpicks. Ignore likely false positives.
   c. Agent #3: Read the git blame and history of the code modified since
      the resolved SHA, to identify any bugs in light of that historical
      context.
   d. Agent #4: Read previous pull requests that touched these files, and
      check for any comments on those pull requests that may also apply to
      the changes since the resolved SHA.
   e. Agent #5: Read code comments in the files modified since the
      resolved SHA, and make sure the changes comply with any guidance in
      the comments.
5. For each issue found in #4, launch a parallel Sonnet agent that takes the
   PR, issue description, and list of CLAUDE.md files (from step 2), and
   returns a score to indicate the agent's level of confidence for whether
   the issue is real or false positive. To do that, the agent should score
   each issue on a scale from 0-100, indicating its level of confidence.
   For issues that were flagged due to CLAUDE.md instructions, the agent
   should double check that the CLAUDE.md actually calls out that issue
   specifically. The scale is (give this rubric to the agent verbatim):
   a. 0: Not confident at all. This is a false positive that doesn't stand
      up to light scrutiny, or is a pre-existing issue (including one on a
      line last touched before the resolved SHA).
   b. 25: Somewhat confident. This might be a real issue, but may also be
      a false positive. The agent wasn't able to verify that it's a real
      issue. If the issue is stylistic, it is one that was not explicitly
      called out in the relevant CLAUDE.md.
   c. 50: Moderately confident. The agent was able to verify this is a
      real issue, but it might be a nitpick or not happen very often in
      practice. Relative to the rest of the changes since the resolved
      SHA, it's not very important.
   d. 75: Highly confident. The agent double checked the issue, and
      verified that it is very likely it is a real issue that will be hit
      in practice. The existing approach in the diff since the resolved
      SHA is insufficient. The issue is very important and will directly
      impact the code's functionality, or it is an issue that is directly
      mentioned in the relevant CLAUDE.md.
   e. 100: Absolutely certain. The agent double checked the issue, and
      confirmed that it is definitely a real issue, that will happen
      frequently in practice. The evidence directly confirms this.
6. Filter out any issues with a score less than 75. (Not 80 — the rubric
   above only ever produces one of the five discrete values 0/25/50/75/100,
   so an 80 cutoff would silently discard every "75: Highly confident"
   issue and only ever report a "100: Absolutely certain" one, defeating
   the rubric's own stated purpose for that tier.) If there are no issues
   that meet this criteria, **skip directly to step 8 and post the "no
   issues found" comment** — do not skip posting entirely; the format in
   the Notes section below covers exactly this case.
7. Use a Haiku agent to repeat the eligibility check from #1, to make sure
   that the pull request is still eligible for code review (no new commits
   landed while this ran that would make the reviewed range stale).
8. Finally, use the gh bash command to comment back on the pull request
   with the result. When writing your comment, keep in mind to:
   a. Keep your output brief
   b. Avoid emojis
   c. Link and cite relevant code, files, and URLs
   d. State explicitly, at the top of the comment, which checkpoint this
      review is scoped to (the preset name if one was used, plus the
      resolved short-SHA) — so a reader isn't misled into thinking earlier,
      already-reviewed commits were re-checked.

Examples of false positives, for steps 4 and 5:

- Pre-existing issues, including on a line that was already present
  (unchanged) as of the resolved SHA -- not just "pre-existing relative to
  the PR's base branch"
- Something that looks like a bug but is not actually a bug
- Pedantic nitpicks that a senior engineer wouldn't call out
- Issues that a linter, typechecker, or compiler would catch (eg. missing
  or incorrect imports, type errors, broken tests, formatting issues,
  pedantic style issues like newlines). No need to run these build steps
  yourself -- it is safe to assume that they will be run separately as
  part of CI.
- General code quality issues (eg. lack of test coverage, general security
  issues, poor documentation), unless explicitly required in CLAUDE.md
- Issues that are called out in CLAUDE.md, but explicitly silenced in the
  code (eg. due to a lint ignore comment)
- Changes in functionality that are likely intentional or are directly
  related to the broader change
- Real issues, but on lines that weren't modified between the resolved SHA
  and the PR's current head

Notes:

- Do not check build signal or attempt to build or typecheck the app.
  These will run separately, and are not relevant to your code review.
- Use `gh` to interact with GitHub (eg. to fetch a pull request, or to
  create inline comments) and `git` (for the SHA-scoped diff/log/blame),
  rather than web fetch.
- Make a todo list first.
- You must cite and link each bug (eg. if referring to a CLAUDE.md, you
  must link it).
- For your final comment, follow the following format precisely (assuming
  for this example that you found 3 issues, `<checkpoint>` was
  `last-review`, and it resolved to short SHA `abc1234`):

---

### Code review (scoped to changes since `last-review` = `abc1234`)

Found 3 issues:

1. <brief description of bug> (CLAUDE.md says "<...>")

<link to file and line with full sha1 + line range for context, note that you MUST provide the full sha and not use bash here, eg. https://github.com/anthropics/claude-code/blob/1d54823877c4de72b2316a64032a54afc404e619/README.md#L13-L17>

2. <brief description of bug> (some/other/CLAUDE.md says "<...>")

<link to file and line with full sha1 + line range for context>

3. <brief description of bug> (bug due to <file and code snippet>)

<link to file and line with full sha1 + line range for context>

🤖 Generated with [Claude Code](https://claude.ai/code)

<sub>- If this code review was useful, please react with 👍. Otherwise, react with 👎.</sub>

---

- Or, if you found no issues:

---

### Code review (scoped to changes since `last-review` = `abc1234`)

No issues found in the changes since `abc1234`. Checked for bugs and CLAUDE.md compliance.

🤖 Generated with [Claude Code](https://claude.ai/code)

---

- When linking to code, follow the following format precisely, otherwise
  the Markdown preview won't render correctly:
  https://github.com/anthropics/claude-cli-internal/blob/c21d3c10bc8e898b7ac1a2d745bdc9bc4e423afe/package.json#L10-L15
  - Requires full git sha
  - You must provide the full sha. Commands like
    `https://github.com/owner/repo/blob/$(git rev-parse HEAD)/foo/bar` will
    not work, since your comment will be directly rendered in Markdown.
  - Repo name must match the repo you're code reviewing
  - `#` sign after the file name
  - Line range format is `L[start]-L[end]`
  - Provide at least 1 line of context before and after, centered on the
    line you are commenting about (eg. if you are commenting about lines
    5-6, you should link to `L4-7`)

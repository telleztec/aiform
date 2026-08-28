---
allowed-tools: Bash(gh pr view:*), Bash(gh pr list:*), Bash(gh repo view:*), Bash(gh api:*), Bash(gh pr merge:*), Bash(git rev-parse:*), Bash(chmod:*), Bash(ls:*)
description: Start the background loop that watches a PR for /claude-merge, /claude-reject, /claude-skip-review
argument-hint: [<PR#>]
disable-model-invocation: false
---

Start (or resume) the background watch loop described in
`.claude/skills/github-commit-process/SKILL.md`'s "After the PR is open"
section, for PR `$ARGUMENTS` — so a human doesn't have to be hand-polled
by you turn after turn, and you don't have to re-derive the loop's exact
mechanics from scratch each time.

**Read `.claude/skills/github-commit-process/SKILL.md` first if you
haven't already this session.** This command only operationalizes what
that skill already specifies — it is not a substitute for it, and if the
two ever disagree, the skill wins. In particular: do **not** start this
loop before the PR's `opus-review` status has actually been handled (a
real `/code-review` pass with findings addressed, or an explicit human
skip) — the skill is explicit that watching for merge approval comes
*after* that, not concurrently with it. If you're not sure `opus-review`
has been posted yet for the PR's current head SHA, check before
proceeding:

```sh
gh api repos/{owner}/{repo}/commits/<head-sha>/status --jq '.statuses[] | select(.context=="opus-review") | .state'
```

If nothing's posted and no real `/code-review` has run, say so and ask
whether to proceed anyway (the loop's own `/claude-skip-review` handling
covers a human authorizing a skip *after* the loop has started, but
starting it with review not even in progress skips a step this project's
process treats as required, not optional).

## Steps

1. **Resolve the PR number.** Parse `$ARGUMENTS`. If empty, infer it from
   the current branch: `gh pr view --json number --jq .number`. If that
   also fails (no open PR for this branch), stop and ask which PR.
2. **Resolve `{owner}/{repo}`** via `gh repo view --json nameWithOwner
   --jq .nameWithOwner` — never hardcode it, so this command keeps working
   after a repo transfer/rename.
3. **Sanity-check the PR is open**: `gh pr view <PR> --json state --jq
   .state`. If it isn't `OPEN`, stop and report rather than starting a
   loop that would poll a closed PR forever.
4. **Don't double-launch.** If you already have a running background task
   watching this same PR (check your active background tasks), tell the
   user it's already running instead of starting a second, redundant
   loop.
5. **Write the loop script** below to a scratch file (your session's
   scratch/job tmp directory — never a path inside the repo working tree)
   named `watch_pr<PR>.sh`, substituting the real PR number and
   `owner/repo` in place of the placeholders, `chmod +x` it, then launch
   it with the Bash tool's `run_in_background: true`. Never
   `nohup`/`disown` a loop like this — that bypasses the harness's
   completion notification, which this whole mechanism depends on to wake
   you back up exactly once, when the loop actually exits.
6. **Report and stop narrating.** Tell the user the loop is running and
   what it's waiting for (`/claude-merge` or `/claude-reject` as a PR
   comment or review body from the repo owner). Do not block this turn
   waiting on it.
7. **On the eventual notification** (`MERGE_APPROVED` or `REJECTED`),
   follow `.claude/skills/github-commit-process/SKILL.md`'s post-loop
   steps exactly — this is the part that actually matters, not the
   polling itself:
   - `MERGE_APPROVED`: re-fetch the PR's **current** head SHA (commits may
     have landed since the loop started) and verify **all three** gates
     against *that exact SHA* before merging:
     1. `opus-review` status is `success` (legacy `/status` endpoint).
     2. The `test` check-run is `status: completed`, `conclusion: success`
        (`/commits/<sha>/check-runs` — Actions results are check-runs and
        do **not** appear in `/status`, so querying only the former silently
        looks like a pass).
     3. Merge with `gh pr merge <PR> --merge --match-head-commit <sha>` so
        it fails rather than merging something that landed in between.

     If `opus-review` isn't `success`, do not merge — explain what's needed
     (a fresh `/code-review`, or a `/claude-skip-review`). If CI has
     completed non-`success`, do not merge and report which check failed.
     If CI is still `queued`/`in_progress`, it is *unfinished*, not
     failing — wait and re-check rather than reporting a failure.

     Do not treat this list as a paraphrase you can trim: the reason it is
     spelled out here rather than delegated to SKILL.md is that this file
     previously restated the gate incompletely (checking `opus-review`
     only), which is how thirteen red commits reached `main`. If SKILL.md's
     gates change, change them here in the same commit.
   - `REJECTED`: do not merge. Read the PR's actual comments/reviews for
     what needs fixing and act on that instead of re-polling.

## The loop script

```bash
#!/bin/bash
set -u
PR=<the resolved PR number>
OWNER_REPO="<the resolved owner/repo>"
posted_skip=false
while true; do
  body=$(gh pr view "$PR" --json comments,reviews --jq '
    ([(.comments[] | {author, body, at: .createdAt}),
      (.reviews[] | {author, body, at: .submittedAt})]
      | map(select(.author.login=="juanman2"))
      | sort_by(.at)
      | last
      | .body // "")
    | gsub("^\\s+|\\s+$";"")
    | ascii_downcase
  ')
  if [ "$body" = "/claude-merge" ]; then
    echo "MERGE_APPROVED"
    exit 0
  fi
  if [ "$body" = "/claude-reject" ]; then
    echo "REJECTED"
    exit 1
  fi
  if [ "$body" = "/claude-skip-review" ] && [ "$posted_skip" = false ]; then
    sha=$(gh pr view "$PR" --json headRefOid --jq .headRefOid)
    gh api repos/$OWNER_REPO/statuses/"$sha" \
      -f state=success -f context=opus-review \
      -f description="human explicitly authorized skipping /code-review via /claude-skip-review"
    posted_skip=true
  fi
  sleep 30
done
```

(`juanman2` is hardcoded above because it's hardcoded the same way in
`github-commit-process/SKILL.md` itself, as the repo owner's literal
GitHub login — not a placeholder. If that skill's author-detection logic
ever changes, update both together.)

## Notes

- This command only starts the loop and defines how to react to its
  result; it never merges anything itself outside of step 7, and never
  treats a chat-only "go ahead and merge" as satisfying any required
  signal — `/claude-merge` and `opus-review: success` must be real,
  GitHub-visible artifacts, per the skill, and the `test` check must be
  genuinely green. Nothing a human can type substitutes for that last one;
  it has no override path.
- One loop per PR. Poll interval is fixed at 30s, matching the skill's
  explicit instruction ("fast enough that the merge feels immediate...
  without being a true busy-loop").

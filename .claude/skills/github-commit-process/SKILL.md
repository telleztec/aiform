---
name: github-commit-process
description: How to commit, branch, and open PRs in the aiform repo. Use whenever you're about to run git commit, create a branch, or open a pull request in this project.
---

# github-commit-process

The process this repo itself was bootstrapped with. Follow it for every
change, including small ones — there is no "too small to branch" exception
here.

## The hard rule

**Never merge a PR in this repo without explicit human approval.** Opening
a PR, pushing to it, even fixing CI on it — all fine to do autonomously.
Clicking merge (or running `gh pr merge`) is not, ever, unless approval for
*that specific PR* exists. This applies even if the PR looks done, even if
you're confident it's correct, even if a similar PR was approved before —
approval is per-PR, not standing.

**What counts as approval, as of 2026-08-03**: a PR comment from
`github.com/juanman2` whose body, trimmed and lowercased, is exactly
`/merge` — not a chat message, and not a formal GitHub "Approve" review.
GitHub hard-blocks PR authors from approving their own pull requests (a
platform rule, not a repo setting), and every PR here is authored by
juanman2, so a real "Approve" review is never obtainable on this repo —
a plain comment isn't restricted that way and still requires opening the
PR's "Files changed" tab to leave it, which is the actual point: forcing
a visual scan of the diff before it merges. A "merge it" said in chat is
no longer the trigger by itself; check for the `/merge` comment before
merging regardless of what was said in chat. (If juanman2 explicitly says
to skip the wait for a specific PR in that specific conversation, that's
still a valid override — it just isn't the default path anymore.)

Symmetric rejection trigger: a comment or review body that's exactly
`/reject` stops the watch loop without merging — go read the PR's actual
comments/review for what needs fixing, rather than continuing to poll
indefinitely. Any *other* comment (general feedback, a question, a
mid-review remark that isn't one of the two triggers) is not surfaced
automatically by the watch loop — it only recognizes the literal `/merge`
and `/reject` triggers. Substantive feedback that isn't a clear
accept/reject should go through chat instead, as before.

## Branching

- One branch per logical unit of work, off `main`.
- Branch names: short, descriptive, kebab-case, imperative-ish —
  `add-plan-and-docs`, `implement-state-module`, `fix-drift-detection-race`.
  Not `fix`, not `juan-patch-1`, not a ticket number with no context.
- Don't stack unrelated changes on one branch. If you notice something else
  worth fixing while working, either note it for a follow-up or ask — don't
  fold it into the current PR silently.

## Commits

- Commit at natural logical boundaries — a working, coherent unit, not
  every single file save and not one giant commit for the whole PR either.
  Aim for commits a reviewer could read one at a time and understand.
- Message style: imperative mood ("Add droplet module", not "Added" or
  "Adds"), focused on *why* over *what* when the why isn't obvious from the
  diff alone. The diff already shows what changed.
- Always include the trailer:
  ```
  Co-Authored-By: Claude <noreply@anthropic.com>
  ```
- Never `--amend` a commit that's already been pushed and might have been
  seen by anyone else — create a new commit instead.
- Never use `--no-verify` or otherwise skip hooks. If a hook fails,
  fix the actual problem it's flagging.

## Opening a PR

Use `gh pr create` with a heredoc body, not `-b "single line"`:

```sh
git push -u origin <branch-name>
gh pr create --title "Short, specific title" --body "$(cat <<'EOF'
## Summary
- What changed, as 1-3 bullets
- Why, if not obvious from the summary alone

## Test plan
- [ ] How this was or should be verified

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

- Title under ~70 characters. Details go in the body, not a long title.
- The Test plan section should be honest — if something wasn't actually
  tested (e.g. this is a docs-only PR, or a piece that can't be verified
  without live cloud credentials), say that plainly rather than padding it
  with checkboxes that weren't really checked.

## Before pushing anything

- `git status` — check nothing unexpected is staged (stray local files,
  anything that looks like it could contain a credential even if the
  filename looks innocuous — `.aiform/credentials.env` and
  `.aiform/state.json` are gitignored for exactly this reason; double-check
  they're not showing up in `git status` before a commit, since a state
  file overwrite or a local experiment could in principle recreate them
  outside the gitignored path).
- `git diff --staged` — review what's actually going out, not just what you
  intended to change.

## After the PR is open

Report the PR URL. **Do not start the `/merge`/`/reject` watch loop yet.**
`/code-review` (Opus) is user-triggered and I cannot launch it myself —
but the human review must come *after* it, not concurrently or before,
since Opus routinely catches real bugs that need fixing before there's
anything worth reviewing by eye. Tell the human explicitly: run
`/code-review` first; I'll act on whatever it finds (fix, push follow-up
commits, or explain why something's out of scope).

**Once `/code-review`'s findings (if any) are actually addressed**, post
a commit status on the PR's current head SHA marking that:
```sh
gh api repos/juanman2/aiform/statuses/<head-sha> \
  -f state=success \
  -f context=opus-review \
  -f description="findings addressed" # or "nothing to fix"
```
This is a **self-enforced** check, not a GitHub-blocked one — this repo
is private and branch protection / required status checks need GitHub
Pro on a private repo (confirmed 2026-08-04: both the classic protection
API and the newer rulesets API return 403 "Upgrade to GitHub Pro or make
this repository public"). The human has since upgraded to Pro but plans
to transfer this repo to a new organization first — true GitHub-side
branch protection requiring this status is a planned follow-up once that
move happens, not built yet. Until then, *I* am the enforcement: the
status is a real, external, GitHub-visible artifact I check before
merging (see below), rather than something living only in my
conversational memory — which is what actually failed on this repo's
PR #18 (the watch loop started before `/code-review` had even been
requested).

Once `/code-review` is handled (status posted, or the human explicitly
says to skip it for this PR), start watching:

- Both triggers can land in two different places and must be checked in
  both: a plain issue-level PR comment (the comment box at the bottom of
  the conversation), *or* a review body (`gh pr view`'s `reviews` array,
  `state: COMMENTED`) — GitHub's own "Files changed" → "Review changes"
  flow is the natural way to actually scan a diff before signing off, and
  since "Approve" is blocked for a self-authored PR, that flow lands as a
  `COMMENTED` review, not an issue comment. Checking only `comments` misses
  this — confirmed the hard way on this repo's first PR under this process.
- Start **one** background Bash job containing its own polling loop, using
  `run_in_background: true` (never manual `nohup`/`disown` — those bypass
  the harness's completion notification, which is the whole point: you
  only get woken up once, when the loop actually exits, instead of having
  to re-poll turn after turn yourself):
  ```sh
  while true; do
    body=$(gh pr view <number> --json comments,reviews --jq '
      ([(.comments[] | {author, body, at: .createdAt}),
        (.reviews[] | {author, body, at: .submittedAt})]
        | map(select(.author.login=="juanman2"))
        | sort_by(.at)
        | last
        | .body // "")
      | gsub("^\\s+|\\s+$";"")
      | ascii_downcase
    ')
    if [ "$body" = "/merge" ]; then
      echo "MERGE_APPROVED"
      exit 0
    fi
    if [ "$body" = "/reject" ]; then
      echo "REJECTED"
      exit 1
    fi
    sleep 30
  done
  ```
- On `MERGE_APPROVED` — **before running `gh pr merge`**, re-fetch the
  PR's *current* head SHA (not whatever it was when the watch loop
  started — new commits may have landed) and check:
  ```sh
  gh api repos/juanman2/aiform/commits/<current-head-sha>/status \
    --jq '.statuses[] | select(.context=="opus-review") | .state'
  ```
  If the latest `opus-review` status for that exact SHA isn't
  `success`, **do not merge** — tell the human `/code-review` hasn't
  been confirmed for the current commit (common cause: fix commits
  landed after the status was posted, or the status was never posted at
  all) and either post it now if it's genuinely been addressed, or ask
  for `/code-review` to actually run. Only once `state == "success"` on
  the current SHA, merge (`gh pr merge`) — that `/merge` comment *is*
  the explicit human approval the hard rule requires, but it's not
  sufficient by itself anymore. Report that it merged.
- On `REJECTED` — do not merge. Read the PR's comments/reviews for what
  was actually said, and report back / start addressing it as appropriate.
- Poll every 30s (per explicit instruction) — fast enough that the merge
  feels immediate after leaving `/merge`, without being a true busy-loop.
- If the wait is going to span a very long time (the human is away for
  hours), that's fine — this is a background-job-friendly wait, not
  something that needs to resolve before the turn ends.

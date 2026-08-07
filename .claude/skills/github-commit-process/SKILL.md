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

**What counts as approval, as of 2026-08-04**: **two independent things**,
both required — a `/claude-merge` signal, and a passing `opus-review` commit
status on the PR's current head SHA. Neither alone is sufficient.
Critically, **both signals are always external, GitHub-visible
artifacts — never something inferred from conversation history alone**,
including when either requirement is explicitly waived (see the two
override paths below). This is deliberate: relying on "I remember the
human said to skip it" is exactly the failure mode this whole mechanism
exists to replace — a long conversation, a context compaction, or a
fresh agent instance resuming the same PR can all silently lose a
chat-only override, either wrongly blocking an authorized merge or,
worse, wrongly proceeding on a misremembered one.

**The `/claude-merge` signal**: a PR comment or review body from
`github.com/juanman2`, trimmed and lowercased, exactly `/claude-merge` — not a
formal GitHub "Approve" review (GitHub hard-blocks PR authors from
approving their own pull requests, a platform rule; every PR here is
authored by juanman2, so a real "Approve" review is never obtainable).
A plain comment isn't restricted that way and still requires opening the
PR's "Files changed" tab to leave it — forcing a visual scan of the diff
before it merges. Override: if juanman2 explicitly says in *chat* to
merge without waiting for this GitHub signal, skip starting/polling the
watch loop and proceed straight to the merge-time check below — but this
override only ever waives the *polling step*, never the `opus-review`
status requirement (that has its own, separate, GitHub-based override —
see next).

**The `opus-review` status**: how it gets posted, and its own override
via a `/claude-skip-review` signal, are covered in "After the PR is open" below.

**Rejection**: a comment or review body that's exactly `/claude-reject` stops
the watch loop without merging — go read the PR's actual comments/review
for what needs fixing, rather than continuing to poll indefinitely. Any
*other* comment (general feedback, a question, a mid-review remark
that isn't `/claude-merge`/`/claude-reject`/`/claude-skip-review`) is not surfaced
automatically by the watch loop — it only recognizes those three literal
triggers. Substantive feedback that isn't a clear accept/reject should
go through chat instead, as before.

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

Report the PR URL. **Do not start the watch loop yet.** `/code-review`
(Opus) is user-triggered and I cannot launch it myself — but the human
review must come *after* it, not concurrently or before, since Opus
routinely catches real bugs that need fixing before there's anything
worth reviewing by eye. Tell the human explicitly: run `/code-review`
first; I'll act on whatever it finds (fix, push follow-up commits, or
explain why something's out of scope).

**How the `opus-review` status gets satisfied — always one of two
GitHub-visible events, never a chat-only decision:**

1. **A real `/code-review` pass.** Once its findings (if any) are
   actually addressed, post a commit status on the PR's *current* head
   SHA:
   ```sh
   gh api repos/{owner}/{repo}/statuses/<head-sha> \
     -f state=success \
     -f context=opus-review \
     -f description="findings addressed" # or "nothing to fix"
   ```
2. **An explicit human skip**, via a `/claude-skip-review` comment or review
   body from `github.com/juanman2` on the PR (same detection mechanism
   as `/claude-merge`/`/claude-reject` below) — e.g. for a docs-only change not worth
   an Opus pass. On seeing it, immediately post the *same* status,
   honestly:
   ```sh
   gh api repos/{owner}/{repo}/statuses/<head-sha> \
     -f state=success \
     -f context=opus-review \
     -f description="human explicitly authorized skipping /code-review via /claude-skip-review"
   ```

Both paths converge on the same artifact (an `opus-review: success`
status on a specific SHA), which is the point: the merge-time check
below only ever has to look at one thing, and "was review skipped" is
never something to infer from earlier in the conversation — a long
conversation, a context compaction, or a fresh agent instance resuming
this PR could all lose a chat-only override. This status is
**self-enforced**, not GitHub-blocked — this repo is private and branch
protection / required status checks need GitHub Pro on a private repo
(confirmed 2026-08-04: both the classic protection API and the newer
rulesets API return 403 "Upgrade to GitHub Pro or make this repository
public"). The human has since upgraded to Pro but plans to transfer this
repo to a new organization first — true GitHub-side branch protection
requiring this status is a planned follow-up once that move happens, not
built yet. Use the literal `{owner}/{repo}` placeholders in both `gh
api` calls above — they're filled in from the current directory's git
remote automatically, so this keeps working after the org transfer
without anyone needing to remember to edit this file.

Once `opus-review` is handled (either path above), start watching:

- All three triggers (`/claude-merge`, `/claude-reject`, `/claude-skip-review`) can land in
  two different places and must be checked in both: a plain issue-level
  PR comment (the comment box at the bottom of the conversation), *or* a
  review body (`gh pr view`'s `reviews` array, `state: COMMENTED`) —
  GitHub's own "Files changed" → "Review changes" flow is the natural
  way to actually scan a diff before signing off, and since "Approve" is
  blocked for a self-authored PR, that flow lands as a `COMMENTED`
  review, not an issue comment. Checking only `comments` misses this —
  confirmed the hard way on this repo's first PR under this process.
- Start **one** background Bash job containing its own polling loop, using
  `run_in_background: true` (never manual `nohup`/`disown` — those bypass
  the harness's completion notification, which is the whole point: you
  only get woken up once, when the loop actually exits, instead of having
  to re-poll turn after turn yourself). The loop also handles a
  `/claude-skip-review` that lands *after* watching starts (posting the status
  itself, inline, then continuing to poll for `/claude-merge`):
  ```sh
  posted_skip=false
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
    if [ "$body" = "/claude-merge" ]; then
      echo "MERGE_APPROVED"
      exit 0
    fi
    if [ "$body" = "/claude-reject" ]; then
      echo "REJECTED"
      exit 1
    fi
    if [ "$body" = "/claude-skip-review" ] && [ "$posted_skip" = false ]; then
      sha=$(gh pr view <number> --json headRefOid --jq .headRefOid)
      gh api repos/{owner}/{repo}/statuses/$sha \
        -f state=success -f context=opus-review \
        -f description="human explicitly authorized skipping /code-review via /claude-skip-review"
      posted_skip=true
    fi
    sleep 30
  done
  ```
  (Note: only the *latest* trigger comment counts, same as `/claude-merge` vs
  `/claude-reject` always did — if `/claude-skip-review` and `/claude-merge` are posted out
  of order, post `/claude-skip-review` first so its status lands before
  `/claude-merge` is seen as the latest comment.)
- On `MERGE_APPROVED` — **before running `gh pr merge`**, re-fetch the
  PR's *current* head SHA (not whatever it was when the watch loop
  started — new commits may have landed) and check:
  ```sh
  gh api repos/{owner}/{repo}/commits/<current-head-sha>/status \
    --jq '.statuses[] | select(.context=="opus-review") | .state'
  ```
  If the latest `opus-review` status for that exact SHA isn't
  `success` — this now covers *every* case, including an explicit skip,
  since that's always posted as a status too — **do not merge**: tell
  the human `/code-review` hasn't been confirmed for the current commit
  (common cause: fix commits landed after the status was posted) and
  either post it now if it's genuinely been addressed, ask for
  `/code-review` to run, or ask for `/claude-skip-review`. Otherwise, merge
  (`gh pr merge`) — the `/claude-merge` signal plus this status together *are*
  the explicit human approval the hard rule requires. Report that it
  merged.
- On `REJECTED` — do not merge. Read the PR's comments/reviews for what
  was actually said, and report back / start addressing it as appropriate.
- Poll every 30s (per explicit instruction) — fast enough that the merge
  feels immediate after leaving `/claude-merge`, without being a true busy-loop.
- If the wait is going to span a very long time (the human is away for
  hours), that's fine — this is a background-job-friendly wait, not
  something that needs to resolve before the turn ends.
- If the human explicitly says in *chat* to merge without waiting for
  the `/claude-merge` GitHub signal, skip starting/polling this loop and go
  straight to the `MERGE_APPROVED` step above — but the `opus-review`
  status check there still applies unconditionally; a chat-only remark
  never satisfies it by itself, only a real `/code-review` pass or a
  `/claude-skip-review` GitHub signal does.

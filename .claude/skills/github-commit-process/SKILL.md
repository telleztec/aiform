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

**What counts as approval, as of 2026-08-03**: an accepted GitHub PR review
from `github.com/juanman2` on that PR — not a chat message. A "merge it"
said in conversation is no longer the trigger by itself; check the PR's
actual review state before merging regardless of what was said in chat.
(If juanman2 explicitly says to skip the review-wait for a specific PR in
that specific conversation, that's still a valid override — it just isn't
the default path anymore.)

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

Report the PR URL, then start watching it for review approval — don't wait
for a follow-up chat message to prompt this.

- Start **one** background Bash job containing its own polling loop, so a
  single `run_in_background` completion notification is what wakes you up
  again, rather than manually re-polling turn after turn:
  ```sh
  while true; do
    state=$(gh pr view <number> --json reviews \
      --jq '[.reviews[] | select(.author.login=="juanman2")] | sort_by(.submittedAt) | last | .state // "PENDING"')
    if [ "$state" = "APPROVED" ] || [ "$state" = "CHANGES_REQUESTED" ]; then
      echo "$state"
      exit 0
    fi
    sleep 180
  done
  ```
- On `APPROVED` — merge immediately (`gh pr merge`), no further chat
  confirmation needed; that GitHub approval *is* the explicit human
  approval the hard rule requires. Report that it merged.
- On `CHANGES_REQUESTED` — stop, do not merge, and report what was
  requested so it can be addressed.
- Don't poll faster than every couple of minutes — a PR review is a
  human-timescale event, not something to busy-loop on.
- If the wait is going to span a very long time (the human is away for
  hours), that's fine — this is a background-job-friendly wait, not
  something that needs to resolve before the turn ends.

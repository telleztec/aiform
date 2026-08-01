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
Clicking merge (or running `gh pr merge`) is not, ever, unless the user
says so in that specific instance. This applies even if the PR looks done,
even if you're confident it's correct, even if the user previously approved
a similar PR — approval is per-PR, not standing.

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

Stop and report the PR URL. Wait for the human to review and decide on
merge — do not poll for approval, do not merge preemptively, do not assume
silence means approval.

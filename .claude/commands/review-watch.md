---
allowed-tools: Bash(gh pr view:*), Bash(gh pr list:*), Bash(gh repo view:*), Bash(gh api:*), Bash(gh pr merge:*), Bash(git rev-parse:*), Bash(chmod:*), Bash(ls:*)
description: Start the background loop that watches a PR for /claude-merge-approved or /claude-merge-rejected
argument-hint: [<PR#>]
disable-model-invocation: false
---

Start (or resume) the background watch loop described in
`.claude/skills/github-commit-process/SKILL.md`'s "After the PR is open"
section, for PR `$ARGUMENTS` — so a human doesn't have to be hand-polled by
you turn after turn, and you don't have to re-derive the loop's mechanics each
time.

**Read `.claude/skills/github-commit-process/SKILL.md` first if you haven't
this session.** This command only operationalizes what that skill specifies;
if the two ever disagree, the skill wins.

Start this loop **as soon as the PR is open**. The human's approval and the
`llm-review` gate are independent and may arrive in either order, so there is
nothing to wait for. Run `/code-review` in parallel with this, not before it.

## Steps

1. **Resolve the PR number.** Parse `$ARGUMENTS`. If empty, infer from the
   current branch: `gh pr view --json number --jq .number`. If that fails, stop
   and ask which PR.
2. **Resolve `{owner}/{repo}`** via `gh repo view --json nameWithOwner --jq
   .nameWithOwner` — never hardcode it, so this survives a transfer/rename.
3. **Check the PR is open**: `gh pr view <PR> --json state --jq .state`. If it
   isn't `OPEN`, stop and report rather than polling a closed PR forever.
4. **Don't double-launch.** If a background task is already watching this PR,
   say so instead of starting a second loop. If you are restarting after a
   rejection or a new commit, stop the old loop first.
5. **Write the loop script** below to your session's scratch/job tmp directory
   (never inside the repo working tree) as `watch_pr<PR>.sh`, substituting the
   real PR number and `owner/repo`, `chmod +x` it, then launch it with the Bash
   tool's `run_in_background: true`. Never `nohup`/`disown` — that bypasses the
   completion notification this whole mechanism depends on.
6. **Report and stop narrating.** Say the loop is running and what it waits
   for. Do not block the turn on it.
7. **On the eventual notification**, follow SKILL.md's post-loop steps — this
   is the part that matters, not the polling.

   - `MERGE_APPROVED`: re-read the current head SHA first — commits may have
     landed while the loop ran — then satisfy **all three gates on one and
     the same SHA**:
     1. `human-approval` — post it on **the SHA the loop was watching**, not
        on a newer head. The loop's watermark is that commit's date, so the
        trigger approves that commit and nothing after it. If head has moved,
        the approval is cleared: run the cosmetic check, and failing that ask
        for a fresh approval and restart. Stamping it onto a newer head
        launders an unapproved commit through a human artifact.
     2. `llm-review` is `success` — `/commits/<sha>/status`.
     3. The `test` check-run is `status: completed`, `conclusion: success` —
        `/commits/<sha>/check-runs`. Actions results are check-runs and do
        **not** appear in `/status`, which returns an empty `contexts` array
        for a green run and reads as a pass. Use each endpoint for its own
        gate; do not consolidate.

     Then merge with `gh pr merge <PR> --merge --match-head-commit <sha>`, so
     it fails rather than merging something that landed in between.

     If `llm-review` is missing, run `/code-review` yourself — that gate is
     yours to satisfy. If CI completed non-`success`, do not merge; report
     which check failed. If CI is `queued`/`in_progress`, or no run exists yet,
     it is *unfinished*, not failing — wait and re-check, but bound the wait
     and report `no test run was ever created` rather than looping forever on
     a run that will never appear.

     Do not treat this list as a paraphrase you can trim.

   - `REJECTED`: do not merge. Read the PR's comments and inline review for
     what was actually said, address it in a new commit, and start a fresh
     cycle — the new SHA clears all three gates. Restart this loop after
     pushing.

## The loop script

```bash
#!/bin/bash
set -u
PR=<the resolved PR number>
OWNER_REPO="<the resolved owner/repo>"
# Watermark on the HEAD COMMIT, not on loop start. A trigger older than the
# current head refers to code that no longer exists; a trigger newer than it
# counts even if posted before this loop began. Watermarking on loop start
# instead drops an approval left moments earlier, and leaves a rejected PR
# permanently unwatchable because the stale rejection stays "latest" forever.
SHA=$(gh pr view "$PR" --json headRefOid --jq .headRefOid)
SINCE=$(gh api repos/$OWNER_REPO/commits/"$SHA" --jq .commit.committer.date)
while true; do
  raw=$(gh pr view "$PR" --json comments,reviews 2>/dev/null || echo '{}')
  body=$(printf '%s' "$raw" | jq -r --arg since "$SINCE" '
    ([(.comments[]? | {author, body, at: .createdAt}),
      (.reviews[]?  | {author, body, at: .submittedAt})]
      | map(select(.author.login=="juanman2" and .at > $since))
      | sort_by(.at)
      | last
      | .body // "")
    | gsub("^\\s+|\\s+$";"")
    | ascii_downcase
  ')
  if [ "$body" = "/claude-merge-approved" ]; then
    echo "MERGE_APPROVED"
    exit 0
  fi
  if [ "$body" = "/claude-merge-rejected" ]; then
    echo "REJECTED"
    exit 1
  fi
  sleep 30
done
```

Note the JSON is fetched and piped to real `jq`: `gh pr view --jq` does **not**
accept `--arg` (that is a `gh api` flag), so passing the watermark inline
fails with `unknown flag: --arg`.

`juanman2` is hardcoded because it is hardcoded the same way in
`github-commit-process/SKILL.md`, as the repo owner's literal GitHub login. If
that skill's author detection changes, change it here in the same commit.

## Notes

- Triggers land in **two** places and both are checked: a plain issue-level PR
  comment, or a review body (`gh pr view`'s `reviews` array, `state:
  COMMENTED`). The "Files changed" → "Review changes" flow produces the latter,
  and since "Approve" is blocked on a self-authored PR that is the natural way
  to sign off. Checking only `comments` misses it.
- This command starts the loop and defines how to react to its result; it never
  merges outside step 7, and never treats a chat-only "go ahead" as satisfying
  any gate. **Never post `/claude-merge-approved` or `/claude-merge-rejected`
  yourself** — they are human triggers, and posting one manufactures your own
  approval.
- One loop per PR. Poll interval 30s.

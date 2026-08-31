---
name: github-commit-process
description: How to commit, branch, and open PRs in the aiform repo. Use whenever you're about to run git commit, create a branch, or open a pull request in this project.
---

# github-commit-process

The process this repo itself was bootstrapped with. Follow it for every
change, including small ones — there is no "too small to branch" exception
here.

## Historical Context
Extra historical context was removed from this file.  Do not examine the 
Github commit history on this repo in order to infer the rules for committing 
and merging work, follow the rules stated in this document exactly. If additional 
context is needed, stop and ask the human, don't guess the process.

## The hard rule

**Never merge a PR in this repo without explicit human approval.** Opening
a PR, pushing to it, even fixing CI on it — all fine to do autonomously.
Clicking merge (or running `gh pr merge`) is not, ever, unless approval for
*that specific PR* exists. This applies even if the PR looks done, even if
you're confident it's correct, even if a similar PR was approved before —
approval is per-PR, not standing.

**What counts as approval**: **three gates**, all required, all recorded as
commit statuses or checks **on the exact head SHA being merged**:

| Gate | Posted by | Means |
|---|---|---|
| `test` | GitHub Actions | CI is green |
| `llm-review` | you, the author LLM | head's content was reviewed and its findings resolved |
| `human-approval` | the watch loop | juanman2 posted `/claude-merge-approved` |

**The two reviews are order-independent.** The human may approve before the
LLM review runs, or after; either order is valid and both end in a merge. Do
not tell the human to wait for one before doing the other.

Critically, **all three are external, GitHub-visible artifacts — never
something inferred from conversation history**. A long conversation, a context
compaction, or a fresh agent instance resuming the same PR can all silently
lose a chat-only approval, either wrongly blocking an authorized merge or,
worse, proceeding on a misremembered one. If it isn't on the SHA, it didn't
happen.

**The `/claude-merge-approved` signal**: a PR comment or review body from
`github.com/juanman2`, trimmed and lowercased, either exactly
`/claude-merge-approved` or that followed by a waiver clause naming issues
(see "Closing more than one issue") — not a formal GitHub "Approve" review (GitHub
hard-blocks PR authors from approving their own pull requests, a platform
rule; every PR here is authored by juanman2, so a real "Approve" review is
never obtainable). A plain comment isn't restricted that way and still
requires opening the PR's "Files changed" tab to leave it — forcing a visual
scan of the diff before it merges.

**Rejection**: a comment or review body that's exactly
`/claude-merge-rejected` stops the watch loop without merging. Read the PR's
actual comments and inline review for what needs fixing, address it in a new
commit, and start a fresh cycle. Any *other* comment (general feedback, a
question, a mid-review remark) is not surfaced by the watch loop — it
recognizes only those two literal triggers. Substantive feedback that isn't a
clear accept/reject should go through chat.

### The restart rule: a new commit clears everything

All three gates are pinned to a SHA, so **any new commit — yours, the
human's, or one addressing review findings — mints a new SHA on which none of
them exist.** Every approval is therefore cleared automatically. There is no
separate bookkeeping to do and nothing to remember: if you pushed, the PR
needs all three gates again.

This is the whole restart mechanism. It covers the author making changes after
a review, the human pushing their own commits, and findings that turn out to
need code changes — one rule, no judgment.

**The one exception — cosmetic carry-forward.** `human-approval`, and only it,
may be re-posted onto a new SHA when the delta since the approved SHA is
*provably* cosmetic:

```sh
# Prints nothing iff every changed path is prose. Both ends pinned to real
# SHAs -- never local HEAD, which can lag or diverge from the PR head.
git diff --name-only <approved-sha> <pr-head-sha> \
  | awk '!/\.md$/ || /^\.claude\// || /^prompts\// || $0=="CLAUDE.md" || $0=="PROCESS.md"'
```

Empty output means cosmetic. **`.md` alone is not the test in this repo.**
`.claude/**`, `prompts/**`, `CLAUDE.md` and `PROCESS.md` are markdown that
agents execute — this very process lives in them — so a "docs-only" edit
there can rewrite the merge rules themselves. They are excluded above and
always require fresh approval.

Use `awk`, **not** `grep -qv '\.md$'` — `grep` on this machine is `ugrep`,
whose `-qv` does not invert the way GNU grep's does, and it reports "no
non-.md files" for a diff that plainly contains them. A misclassification
here carries a human approval onto a change they never saw, so verify the
check itself before trusting its answer.

Put the prior approved SHA in the new status's `description` so the
carry-forward is auditable rather than asserted. If the check doesn't pass
cleanly, it is not cosmetic — ask for re-approval instead of arguing the case.
A minor bug fix is a code change and does **not** qualify, however small.

`llm-review` never carries forward; it re-runs. The asymmetry is deliberate —
automate the cheap gate, protect the expensive one. Human attention is the
scarce resource here; re-running a review costs no round-trip at all.

### What the enforcement actually guarantees

`main` requires all three contexts via branch protection (`strict: true`,
`enforce_admins: true`). Be honest about what that buys: `llm-review` and
`human-approval` are posted **by you**, so requiring them catches *"the agent
forgot"*, not *"the agent misbehaves"* — an agent willing to skip a check
would equally post the status. Only `test` is enforced against an actively
wrong agent. Do not describe this setup as stronger than it is.

**Never post `/claude-merge-approved` or `/claude-merge-rejected` yourself.**
They are human triggers, and the watch loop converts the first into the
`human-approval` status — posting one would manufacture your own approval end
to end.

## Branching

- One branch per logical unit of work, off `main`.
- Branch names: short, descriptive, kebab-case, imperative-ish —
  `add-plan-and-docs`, `implement-state-module`, `fix-drift-detection-race`.
  Not `fix`, not `juan-patch-1`, not a ticket number with no context.
- Don't stack unrelated changes on one branch, and never fold one in
  silently. See "One issue, one PR" below for what to do with something you
  find mid-branch.

### One issue, one PR

**A PR closes at most one GitHub issue.** Not two related ones, not four
that happen to touch the same file, not "they're all onboarding papercuts."
If you are about to write `Closes #A, #B`, stop and split the branch.

The reasons are about review, not tidiness:

- **A reviewer can hold one change in their head.** Four issues in one diff
  means the human either reviews the largest one properly and skims the
  rest, or bounces the whole thing. Both are worse than four small reads.
- **Approval is all-or-nothing.** `/claude-merge-approved` is a single
  signal on a single SHA. Bundling means the human cannot accept three
  fixes and reject the fourth without rejecting all four — so a
  disagreement about one line blocks unrelated finished work.
- **A revert takes the bundle with it.** If one of four fixes turns out to
  be wrong, reverting it reverts the other three too.
- **Each issue gets its own review record.** `llm-review` on a bundled PR
  attests to a diff, not to an issue; nothing afterward says which issue
  was actually reviewed.

**A PR may close zero issues.** Process changes, refactors, doc fixes and
chores don't need an issue invented for them. The rule is a ceiling, not a
quota.

**If an issue is too big for one PR, split the issue, not the PR.** Two PRs
both claiming to fix #N leave #N in an ambiguous state — half-fixed, still
open, with no record of which half landed. File the second issue, say in
each what the other covers, and close each with its own PR. #76 and #87 are
such a split: the scaffold change and the driver-schema change need
different gates, since editing a driver changes its `sha256` and forces a
gate #1 re-review.

**Found something else mid-branch?** File it if it warrants an issue, or
ask — don't fold it in silently. A typo or a chore you fix in passing needs
no issue (see the zero-issue rule above); a defect somebody has to decide
about does.

**"Closes" means every closing keyword on the PR — body and commit messages
both.** `Closes #73` in the body plus `Fixes #74` in a commit closes two
issues and breaks this rule as surely as naming both in one line. Note also
that GitHub needs the keyword before **each** number: `Closes #73, #74`
closes only #73, and the rest stay open as fixed-but-unclosed.

### Closing more than one issue: the waiver

Sometimes one change genuinely resolves several issues — duplicates, or a
fix that incidentally closes another report. Splitting those apart is
artificial, and closing them silently is what this rule exists to stop.
The escape hatch is a **human waiver**, and it is not yours to grant:

1. **Say so in the PR description** — which issues, and why one change
   resolves all of them rather than being several changes in a trench coat.
2. **Tell the human you need a waiver.** Say it, in the conversation, when
   you open the PR. Do not leave it in the description for them to notice;
   they are the one being asked for something.
3. **The waiver arrives as an extended approval** naming the issues:
   `/claude-merge-approved issues 73, 74, 75`. A plain
   `/claude-merge-approved` approves the merge and grants **no** waiver.

Without a waiver, close one issue and link the others plainly (`see #81`)
for a follow-up PR. Never assume a waiver from a plain approval, and never
infer one from a conversation — same rule as every other gate here.

This is also the answer to `PROCESS.md` step 6's "one tightly-coupled pair"
(a module and the exceptions it raises, say). If that pair is two issues,
it needs a waiver like anything else, rather than a second exception with
its own boundary to argue about.

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

- Reference at most one issue with a closing keyword — see "One issue, one
  PR". To mention others without closing them, link them plainly (`see #81`).
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

Report the PR URL, then do **both** of these — they are independent and
neither waits on the other:

1. **Start the watch loop immediately** (`/review-watch <PR>`). The human may
   approve at any time, including before the LLM review has run.
2. **Run `/code-review <PR>` yourself**, then `/code-review-since <PR>` over
   each round of fixes until head is covered — see "Satisfying `llm-review`"
   below for the loop. You can launch both: they carry
   `disable-model-invocation: false`. Do not ask the human to trigger either.
   The one exception is `/code-review ultra`, which is user-triggered and
   billed — never launch that.

The reviewer must be **Opus 5 or newer, and never you**. An author reviewing
its own diff satisfies the letter of the gate and none of its purpose. The
mechanism: launch it as a subagent with an explicit `model` override rather
than inheriting yours. If you cannot select a different model, say so on the
PR instead of reviewing yourself.

### Satisfying `llm-review`

The status means the SHA's content **has been read by a reviewer**. Never post
it on a SHA containing code no pass has read — that is a false attestation
however small the change looks. Two placements, with different force:

- **On head, it is the gate**, and means read *and* resolved: every finding
  either fixed or explicitly deferred on the PR.
- **On an earlier SHA, it is history** — a review record, and the checkpoint
  `/code-review-since` walks back to. It gates nothing.

**Never post it on a SHA that is still head with findings open.** Head plus a
green CI plus an early human approval is a merge, so a premature `success`
there ships known-unfixed findings. Post on a reviewed SHA once it *stops*
being head; post on head only when its findings are resolved.

Because your own fix commits are code no pass has read, one round is usually
not enough. The loop:

1. Review head `R` — `/code-review <PR>` the first round,
   `/code-review-since <PR>` after, whose default `last-review` checkpoint
   resolves to the last SHA you posted on, so it reads only what is new.
2. Resolve every finding: fix it, or state on the PR why it is deferred.
3. **If that produced commits**, head is now `H`. Post `llm-review` on `R` —
   it has become history, and posting arms the checkpoint for the next round.
   Set `R = H` and go back to 1.
4. **If it produced no commits**, head is still `R` and its findings are
   resolved. Post `llm-review` on `R`. Done.

It terminates whenever a round produces no commits — including a round whose
findings are all deferred rather than fixed, which is why step 4 keys on
commits and not on the finding count. Head never carries the status while
anything is open, and every SHA the loop posts on has been read.

```sh
gh api repos/{owner}/{repo}/statuses/<reviewed-sha> \
  -f state=success \
  -f context=llm-review \
  -f description="/code-review pass; N findings addressed"   # or "nothing to fix"
# incremental rounds:
#   -f description="/code-review-since <R>; fixes reviewed, nothing further"
```

Say in the description what actually happened, including which model ran if it
wasn't the default and whether the pass was full or incremental. This status
is the durable record of what review this commit received; a description that
overstates it is worse than none.

Only head carrying `llm-review` satisfies the merge gate — the statuses on
earlier SHAs are review history and checkpoints, not gates.

If head moved for a reason you did not author, treat it exactly the same way:
`R..H` is unread, so review that delta before posting anything on `H`.

There is **no skip path.** If a review is genuinely impossible (model
unavailable), say so on the PR and stop — do not invent a bypass.

Use the literal `{owner}/{repo}` placeholders in every `gh api` call here —
they resolve from the current directory's git remote, so this survives an org
transfer with no edits.

### The watch loop

Triggers land in **two** places and both must be checked: a plain issue-level
PR comment, *or* a review body (`gh pr view`'s `reviews` array, `state:
COMMENTED`). GitHub's "Files changed" → "Review changes" flow is the natural
way to scan a diff before signing off, and since "Approve" is blocked on a
self-authored PR, that flow lands as a `COMMENTED` review, not a comment.
Checking only `comments` misses it.

Start **one** background Bash job with `run_in_background: true` — never
`nohup`/`disown`, which bypass the completion notification this depends on.

**Filter triggers by the head commit's timestamp, not by when the loop
started.** A trigger older than the current head refers to code that no longer
exists; a trigger newer than it counts, even if it was posted before the loop
began. Watermarking on loop start instead silently drops an approval the human
left moments earlier — and makes a PR unwatchable after a rejection, since the
stale rejection stays "latest" forever.

```sh
PR=<number>
SHA=$(gh pr view "$PR" --json headRefOid --jq .headRefOid)
SINCE=$(gh api repos/{owner}/{repo}/commits/"$SHA" --jq .commit.committer.date)
while true; do
  raw=$(gh pr view "$PR" --json comments,reviews 2>/dev/null || echo '{}')
  body=$(printf '%s' "$raw" | jq -r --arg since "$SINCE" '
    ([(.comments[]? | {author, body, at: .createdAt}),
      (.reviews[]?  | {author, body, at: .submittedAt})]
      | map(select(.author.login=="juanman2" and .at > $since))
      | sort_by(.at) | last | .body // "")
    | gsub("^\\s+|\\s+$";"") | ascii_downcase
  ')
  # Accepts a bare approval and the waiver form ("/claude-merge-approved
  # issues 73, 74"). An exact-equality test would silently ignore the latter.
  case "$body" in
    "/claude-merge-approved"|"/claude-merge-approved "*) echo "MERGE_APPROVED"; exit 0;;
    "/claude-merge-rejected"|"/claude-merge-rejected "*) echo "REJECTED"; exit 1;;
  esac
  sleep 30
done
```

`gh pr view --jq` does **not** accept `--arg` (that is a `gh api` flag), which
is why the JSON is fetched and piped to real `jq`. Poll every 30s.

A long wait is fine — this is a background job, not something to resolve
before the turn ends.

### On `MERGE_APPROVED`

**Post `human-approval` on the SHA the loop was watching — never on a newer
head.** The loop's watermark is that SHA's commit date, so the trigger it
caught approves *that* commit and nothing after it. If head has moved since,
the approval is **cleared**: run the cosmetic check, and if it doesn't pass,
ask for a fresh `/claude-merge-approved` and restart the loop. Re-reading head
and stamping the approval onto it would launder an unapproved commit through
a human artifact — the exact thing all three gates exist to prevent.

```sh
WATCHED_SHA=<the SHA the loop was started against>
SHA=$(gh pr view <number> --json headRefOid --jq .headRefOid)

# If these differ, STOP and resolve per the paragraph above before posting.
[ "$SHA" = "$WATCHED_SHA" ] || echo "head moved: approval does not cover $SHA"

gh api repos/{owner}/{repo}/statuses/"$SHA" \
  -f state=success -f context=human-approval \
  -f description="/claude-merge-approved by juanman2"

# llm-review — legacy status API
gh api repos/{owner}/{repo}/commits/"$SHA"/status \
  --jq '[.statuses[] | select(.context=="llm-review") | .state] | .[0] // "NONE"'

# test — check-runs API
gh api repos/{owner}/{repo}/commits/"$SHA"/check-runs \
  --jq '[.check_runs[] | select(.name=="test") | {status, conclusion}] | .[0] // "no run yet"'
```

Actions results are **check-runs**; the legacy `/status` endpoint does not
report them at all, so querying `/status` for CI returns an empty `contexts`
array and reads as a pass. The two queries hit different APIs deliberately —
do not consolidate them.

Then merge, pinned:

```sh
gh pr merge <number> --merge --match-head-commit "$SHA"
```

It fails rather than merging if anything landed between the checks and the
merge.

**CI has three outcomes, not two.** `queued`, `in_progress`, or `"no run yet"`
means *unfinished*, not failing — wait and re-check rather than reporting a
failure. But bound that wait: `"no run yet"` also covers runs that will never
appear (Actions disabled, quota exhausted, a SHA no trigger covers). Give up
after a few minutes and report `no test run was ever created for <sha>`.

**If `llm-review` is missing from head**, that gate is yours to satisfy, not
the human's: run the loop above — `/code-review-since <PR>` when an earlier
commit on the branch already carries the status, `/code-review <PR>` when none
does. **If CI completed non-`success`**, do not merge; say
which check failed and whether it is caused by this PR or pre-existing on
`main`. `/claude-merge-approved` approves the *change*; it is not a claim that
the build passes, and the human usually cannot see CI from the box they typed
it in.

### On `REJECTED`

Do not merge. Read the PR's comments and inline review for what was actually
said, address it in a new commit, and start a fresh cycle — the new SHA clears
all three gates, so the PR needs a new review and a new approval. Restart the
watch loop once you have pushed; the previous one has already exited, so with
no new loop nothing is listening.

### If the merge is rejected as behind `main`

That is `strict: true` working, not an error to force past. Update the branch,
which mints a **new head SHA** — so all three gates must be satisfied on it,
and the prior `/claude-merge-approved` does not carry over. If the update is a
mechanical merge or rebase with no content change, say so when asking for
re-approval rather than presenting it as a fresh review. Then restart the
watch loop.

### If the human says "just merge it" in chat

Skip the *polling* only. Go to the three **verification queries** above — not
to the `human-approval` post that precedes them; a chat remark is not the
trigger and never authorizes stamping that status.

`human-approval` still requires a real `/claude-merge-approved` on the PR. If
one already exists and is newer than the head commit, verify it yourself with
the same jq the loop uses, then post the status on the SHA it covers. If none
exists, ask for one — this override waives the waiting, never the gates, and
it is the path most in need of them since it skips the loop entirely.

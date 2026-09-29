# PR #139 UAT fix plan

Source: manual UAT of PR #139 (`aiform resource check/metrics/status`),
2026-09-18, in `~/infra-demo` against the real `aiform` DigitalOcean
account. 7 bugs filed (#157, #158, #160, #161, #162, #163, #164), plus
**#166**, found the same night while an agent was fixing #163 (not part
of the original UAT, but the same "apply/destroy confirmation flow"
cluster — see its row below). Two enhancement proposals also came out of
the original UAT (#156 — REPL/chat mode, #159 — opt-in data-plane health
check for the domain driver) but are **out of scope for this plan** —
they're feature work, not bugs, and track separately.

Default is one issue, one PR (`.claude/skills/github-commit-process/
SKILL.md`'s "One issue, one PR"). One exception below is pre-authorized
by the user in the planning conversation that produced this file:
**#158 and #161 may ship in a single PR**, because both fixes edit the
same `StatusReport` dataclass in `aiform/observability.py` — splitting
them would mean editing that dataclass's field list in two separate PRs,
the second rebasing around the first's addition, which is exactly the
"duplicating the same edit in two PRs" case `SKILL.md` names as the bar
for a waiver. Every other row is a normal single-issue PR.

**Every PR still needs the `/claude-merge-approved-multi` waiver runtime
step for the grouped row** — the human pre-authorized the *grouping* here,
but `SKILL.md`'s mechanics still apply: put a `## Waiver requested`
section at the top of that PR's description, tell the human it needs
the waiver when the PR opens, and get `/claude-merge-approved-multi`
(not a plain approval) before merging.

Each row below is written to be handed to a fresh session cold — read
the linked issue for full scope/rationale, follow this repo's normal
spec-first/test-first loop (`PROCESS.md`, `.claude/skills/tdd-workflow/
SKILL.md`) for the code change, then `.claude/skills/github-commit-process/
SKILL.md` for branch/PR/merge mechanics.

## ⚠️ Infrastructure blocker found tonight: #168 — fix ready, [PR #170](https://github.com/telleztec/aiform/pull/170)

**Status: PR open, all gates green (`test`/`llm-review`/`system-test`), awaiting human approval.** Branch `fix-power-off-poll-timeout-budget`, head `d11e727`. Bumped the shared `_poll_until` default from 45 attempts × 2s (90s) to 75 × 2s (150s) — ~38% margin over the observed 108.6s worst case. Verified live against real DigitalOcean infra (11 passed, 1 skipped, 0 failed), not just reasoned from history. Reviewed by Opus (not Fable) across 2 rounds; 2 blocking findings fixed (stale docs, a vacuous test), 2 cosmetic nits deferred. A review-watch loop is already running on this PR (task `bq4e4y2fe`), so a `/claude-merge-approved` will be caught automatically.

**#170 merged** (`9ebe653`, 2026-09-18T14:53:33Z, closes #168). #167 refreshed: merged `origin/main` in (head now `a6ad24f`), `llm-review` carried forward per human instruction (verified zero diff on #167's own files first — see PR comment), full local suite + ruff clean. **Live `system-test` re-run genuinely FAILED again** — same test, now timing out at 186s against the new 150s budget. Root-caused via DigitalOcean's own `GET /v2/actions` log (not a guess): the specific `power_off` action took **303s** on DO's side that run (vs ~11-14s for every other power action that night) — a real DO-side tail-latency outlier, not a bug in `_poll_until` and not a systemic slowdown. #170's own fix is NOT wrong — its live run hit normal-speed actions and passed for real. **#167 still needs**: a fresh live `system-test` run (hopefully hitting normal latency this time) once #171 (below) lands, and a fresh `/claude-merge-approved-multi` from the human regardless (human-approval never carries forward, only llm-review did here, and only because the diff check justified it).

## New: #171 — exponential backoff for `_poll_until`, in progress

Filed and being implemented in response to the #167 investigation above. A fixed 2s-interval poll forces a bad tradeoff: big enough to cover a rare 300s+ outlier means wastefully fast-polling for the ~95%+ of calls that finish in ~11-14s. [Issue #171](https://github.com/telleztec/aiform/issues/171), agent spawned (Sonnet implements / Opus reviews, no Fable), scoped deliberately to stay local to `drivers/digitalocean/compute.py` (not promoted to a shared cross-driver primitive yet — no second driver exists to justify that). Also deciding whether `create()`'s separate 180s override is still needed once a real backoff schedule exists. Not merged when done — reports back first.

## Infrastructure blocker found tonight: #168 (original notes, superseded by PR #170 above)

While landing PR #167, its live `system-test` run failed in a test helper
(`_power_off`) the PR never touched — DigitalOcean's power-off latency has
apparently drifted past the driver's 90s default poll budget (two runs
tonight both timed out around ~108s; this is the same budget #152 widened
60s→90s three days ago). **This blocks `system-test` for every future PR
that touches a runtime path** — i.e. most of the remaining rows below —
not just #167. Filed as
[#168](https://github.com/telleztec/aiform/issues/168), not fixed tonight:
its own scope note asks a real design question ("does the budget move
again, or does the test-only wait get decoupled from the real `apply`-path
default it currently shares") that's a human call, not a mechanical
one-line bump, and fixing it would cost another live suite run plus an
`llm-review` round against a tight Fable budget the night this was found.
**Recommend resolving #168 first thing**, before picking up any other row
below that touches a runtime path.

## Priority order

| # | Issue(s) | Theme | Severity | Files likely touched | Grouping note | Status |
|---|---|---|---|---|---|---|
| 1 | [#163](https://github.com/telleztec/aiform/issues/163) | A keystroke typed during the review-LLM call gets silently consumed as the `[y/N]` apply/destroy answer, bypassing the review gate | **High — safety** | `aiform/cli.py` (`_confirm`), `aiform/orchestrator.py` (`default_confirm`), a new regression test simulating pre-buffered stdin | Solo. Ship fast, isolated — don't let anything else's review slow this one down. | **✅ MERGED** — [PR #165](https://github.com/telleztec/aiform/pull/165), merge commit `4580750`, 2026-09-18T04:17:50Z. Collapsed `_confirm`/`default_confirm` into one implementation. |
| 2 | [#166](https://github.com/telleztec/aiform/issues/166) | Gate #2's review flags print only *after* `apply_plan()` returns — i.e. after the user already answered `[y/N]` — on **every** run, not just a mistimed one | **High — safety, same cluster as #163** | `aiform/cli.py` (`_print_apply_result`), `aiform/orchestrator.py` (`apply_plan`'s confirm/flags contract) | Solo — found mid-fix on #163, deliberately not folded into PR #165 per one-issue-one-PR. | **✅ MERGED** — [PR #180](https://github.com/telleztec/aiform/pull/180), merge commit `3802676`, 2026-09-20T04:50:19Z. Planned in plan mode, approved before implementation, per `PROCESS.md`'s gate. Added a second injectable `apply_plan()` callback, `on_review`, called with each review's own flags before the confirmation it precedes (both the batch and single-resource review points), rather than widening `ConfirmFn` or having the orchestrator format text itself. 2 Fable review rounds (round 1: 10 doc-accuracy findings, 4 fixed, 4 deferred — one filed as [#181](https://github.com/telleztec/aiform/issues/181); round 2: clean). Live `system-test`: 14 passed, 0 failed, 14m07s. |
| 3 | [#158](https://github.com/telleztec/aiform/issues/158) + [#161](https://github.com/telleztec/aiform/issues/161) | `resource check`/`status` JSON hides `provider`/`resource_type` and bakes `deployed`/`config` into prose instead of structured fields, unlike `resource metrics`'s JSON and `status`'s own `health` field | Medium-High — this is the closest thing aiform has to a scripting API | `aiform/observability.py` (`StatusReport`, `_check_json`, `_status_json`, `_status_for_entry`), `tests/test_observability.py`, `specs/driver_observability.md`, `specs/cli.md` | **Grouped — waiver required.** Same dataclass edit for both; see note above. | **✅ MERGED** — [PR #167](https://github.com/telleztec/aiform/pull/167), merge commit `0508c3d`, 2026-09-19T13:15:27Z, closes #158 and #161. Head `cc4b078` (after merging in #175/#177/#176/#179), all four gates green: `test` ✅, `llm-review` ✅ (independently re-verified by Fable 5.1 via `/code-review-since`, checkpoint `30e631b` — diff on this PR's own files confirmed empty, no findings), `system-test` ✅ (14 passed, 0 failed, 0 skipped, 13m02s, `system-test-20260919T063457Z.log` — clean run over the new SSH-first power-off path, no repeat of the earlier DO-side tail-latency outlier), `human-approval` ✅ (`/claude-merge-approved-multi`; a plain `/claude-merge-approved` arrived first and was correctly rejected by `merge_gate.py` since this PR closes two issues, then corrected). |
| 4 | [#164](https://github.com/telleztec/aiform/issues/164) | Review-orchestration flags are 60-90 word paragraphs with no brevity guidance in the prompt | Medium — undermines #163's fix in spirit: a gate nobody reads is as good as no gate | `prompts/review_plan.md`, optionally `aiform/models.py` (`PlanReviewFlag.concern` `Field(description=...)`) | Solo. Do this *after* #166 — no point shortening flags that don't appear before the prompt yet; no shared code either way, clean split. | **✅ MERGED** — [PR #184](https://github.com/telleztec/aiform/pull/184), merge commit `045c14f`, 2026-09-20T16:27:31Z. Planned in plan mode, approved before implementation. Two independent levers reaching the model: a brevity paragraph in `prompts/review_plan.md`, and a `description` on `PLAN_REVIEW_SCHEMA`'s `concern` property (mirrored in `PLAN.md`, per `specs/llm.md`'s verbatim-match requirement) — not a `Field(description=...)` on `aiform/models.py`'s `PlanReviewFlag`, since that model never becomes the JSON schema the API sees. Live-verified against a real droplet: the UAT's 60-90 word paragraphs became a two-sentence, ~33-word flag. 2 Fable review rounds (round 1: no code bugs, one overclaim in the PR's own test evidence fixed by narrowing the claim rather than the code, two low findings fixed; round 2: clean). Split out #182 (`[y/N]` implied default) and #183 (a pre-existing `move_to_trash` `FileNotFoundError` hit during live verification) rather than folding them in. |
| 5 | [#162](https://github.com/telleztec/aiform/issues/162) | Plan summary line (`Plan: N to create...`) prints identically for pure preview, awaiting confirmation, and auto-approved-and-executing | Medium — reproduced live user confusion, not just a theoretical read | `aiform/cli.py` (`_print_plan`) | Solo. | **✅ MERGED** — [PR #185](https://github.com/telleztec/aiform/pull/185), merge commit `962cfa2`, 2026-09-21T21:27:43Z. `_print_plan()` gains a keyword-only `yes` param, appending `(auto-approved via --yes, executing now)` to the tally line when `--yes` is set and the plan has real work to do (guarded against an all-`NO_OP` repeat run, caught by review). 2 Fable rounds found a real spec overclaim (the marker prints before gate #2 rules, so a blocked `--yes` destroy can still show "executing now" then abort — reworded to state only what the marker actually guarantees) and a same-line test gap, both fixed. A larger restructuring (fire the notice from inside `apply_plan()` at the exact skip point, fixing the timing issue at the root) was raised but not built — tracked as a possible follow-up, not blocking. |
| 6 | [#157](https://github.com/telleztec/aiform/issues/157) | `resource check`'s fleet coverage line reads as an error at both extremes (empty project, and a fully healthy fleet) | Low-Medium — first-run and steady-state UX | `aiform/observability.py` (`render_check`), `tests/test_observability.py` | Solo. | **✅ MERGED** — [PR #188](https://github.com/telleztec/aiform/pull/188), merge commit `704f443`, 2026-09-21T23:54:27Z. Empty fleet now prints `no resources tracked` (exit code unchanged); a populated fleet drops `"; N unsupported"` when the count is 0 and rewords "report health" to "reported a health verdict". JSON `coverage` shape untouched. 2 Opus review rounds (round 1: a spec gap and a missing PR closing-keyword, both fixed; round 2: clean, 3 non-blocking doc nits deferred). Live `system-test`: 14 passed, 0 failed. |
| 7 | [#160](https://github.com/telleztec/aiform/issues/160) | `resource metrics` text output drops sample labels (8 identical `cpu_seconds_total` rows) and carries a `gauge`/`counter` column that's redundant with the name today | Low-Medium — narrower blast radius, only the one multi-series family today | `aiform/observability.py` (`render_metrics`), `tests/test_observability.py` | Solo. | **✅ MERGED** — [PR #189](https://github.com/telleztec/aiform/pull/189), merge commit `d689ea5`, 2026-09-22T01:55:22Z. Decision revised twice in planning before implementation: the addendum's "keep the kind column" default was reversed to "drop it from text output only" (data model and JSON untouched), and the label-suffix format went from a rejected `key="value"` style to a human-readable bracketed name suffix (`cpu_seconds_total[idle]`) once it was confirmed the label is always one of 8 known CPU-time modes, not a per-vCPU index. 2 Opus review rounds (round 1 caught a real overclaim — `filesystem_free_bytes`/`filesystem_size_bytes` actually carry 3 labels each, not just `cpu_seconds_total`'s one — fixed by hardening the sort-by-key test and the docs; round 2 clean). Live `system-test`: 14 passed, 0 failed. Also fixed 3 more hardcoded `"gauge ..."` assertions in `tests/test_cli_resource.py` that the original file-list `grep` missed. |

## Out of scope for this plan (enhancements, not bugs)

- [#156](https://github.com/telleztec/aiform/issues/156) — interactive REPL / chat mode. Phase 1 (literal REPL) is implementable independently of anything above; phase 2 (plain English) is explicitly blocked on a repo-owner decision about a fifth model-tiering role.
- [#159](https://github.com/telleztec/aiform/issues/159) — opt-in data-plane DNS health check for the domain driver. This reopens an explicit "Out of scope" decision in `specs/driver_observability.md`; needs a design pass and a repo-owner call before any implementation PR is scheduled, not just a normal fix.

## Update 2026-09-18: the power-off timeout saga resolved differently than expected

Tonight's investigation into #168 (blocking `system-test`) went through
several rejected iterations before landing on the real fix:

- **#171/PR #172** (hardcoded exponential backoff) — built, reviewed,
  **closed, not merged**. Rejected as not reusable and not worth the
  review cost for a throwaway shape.
- **#174** (flat 7-minute widen) — filed as the interim fallback,
  **closed, not merged**. Superseded before landing once the SSH finding
  came in.
- **Root cause found**: DigitalOcean's `power_off` API action isn't an
  instant hard cut — it attempts a graceful signal first, forcing a hard
  stop only after ~5 minutes. A 10-run live diagnostic
  (`probes/digitalocean_compute_ssh_shutdown.py`) showed SSH-initiated
  in-guest shutdown reliably completes in 11.3-24.4s instead (9/9
  successful attempts), bypassing whatever's slow about DO's external
  signal delivery.
- **The real fix**: [#175](https://github.com/telleztec/aiform/issues/175)
  — SSH-initiated shutdown as the primary path, falling back to the
  existing API mechanism unchanged. Also closes #150 (droplet
  post-creation security) as a side effect, since it requires injecting
  a real SSH key into every created droplet by default. Went through a
  full planning-mode design session (approved plan preserved at the time
  in `/Users/juan/.claude/plans/shiny-bubbling-bear.md`), split generic
  SSH mechanics (`aiform/ssh.py`) from DigitalOcean-specific code
  (`drivers/digitalocean/compute.py`), explicitly scoped to this
  project's actual target user (see new PLAN.md Persona/Use Case
  sections). Implementation in progress as of this update.
- **New standing process gate**: PROCESS.md now requires a written plan
  + explicit human approval before any implementation begins — added in
  PR #173 (merged) after this same investigation initially jumped to
  implementation (#172) without one. See `PROCESS.md`'s "Before the
  loop: plan and get explicit approval" section.

**#157/#160/#162/#164/#166 remain blocked on `system-test`** until #175
lands and #167 gets its (now third) refresh.

## Update 2026-09-18, later: #175 (SSH-first power-off) ready for review

**PR #177** — https://github.com/telleztec/aiform/pull/177 — implements
issue #175 per the fully-approved plan session (design doc preserved at
the time in `/Users/juan/.claude/plans/shiny-bubbling-bear.md`). All
three gates green (`test`/`llm-review`/`system-test`), NOT merged.

**⚠️ Needs `/claude-merge-approved-multi`, not plain approval** — this PR
closes both #175 and #150 (the plaintext-root-password fix is an
inherent side effect of `create()` always injecting the managed key, per
the approved plan's own "not two separate features" framing). Waiver
section already in the PR body.

**Live-discovered findings beyond the original plan, all disclosed, not
silently folded in:**
- A freshly-uploaded DO account SSH key has ~9s of propagation lag
  before it's usable in a `create droplet` call (8 consecutive 422s
  observed, succeeded on the 9th) — fixed with a bounded retry.
- `TestSshFirstPowerOffLive::test_resize_uses_the_ssh_path` initially
  failed live (`no-ip-fallback` instead of `ssh-success`) — investigated
  and determined to be a genuine DigitalOcean characteristic (droplet
  reports `active` before its public v4 address attaches), not an aiform
  bug — corroborated by a pre-existing `DEGRADED` health verdict for
  exactly this case (`drivers/digitalocean/compute.py:655`, predates
  this PR). Fixed via a live-test-only wait-for-public-ipv4 guard, not a
  production code change.
- The same race hit `test_cli_observability.py`'s `resource check` step
  too (3 of 4 live runs) — filed as **#178** (the broader "should
  `create()`'s own poll predicate wait for public v4" question stays
  open), and the same guard applied there too, flagged before doing it.
- `tests/system/test_cli_observability.py`'s `_power_off` helper now
  calls `aiform.ssh.shutdown_via_ssh()` directly instead of the raw DO
  `power_off` action (falling back to the raw action if SSH doesn't pan
  out) — avoids the same ~300s DO-side outlier characterized earlier
  tonight (#154's "Learnings" section), decided as "option (c)" after
  ruling out a `shutdown`-then-`power_off` sequence empirically (tested
  live: `shutdown` alone didn't reliably complete either).
- 11 independent Opus review rounds total across the PR; caught a real
  subtle bug in the SSH timeout math (an early fix that truncated each
  connection attempt's timeout broke the "a timeout during SSH means the
  guest is shutting down" assumption the fallback logic depended on).

**Also merged today**: PR #176 (Persona/Use Case docs in README.md/
PLAN.md — went through a rejection + 5 inline review comments + 4
review-fix rounds before landing clean).

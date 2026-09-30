# Plan: #225 — refuse a paths-driven destroy that would orphan a dependent, plus the removal coverage #222 lacked

**Status: approved 2026-09-28. Both decisions confirmed by the repo owner: refuse by default with `--force`, and fix #216 and #225 together on PR #222.**

## Why this is on PR #222 rather than its own

The repo owner reviewed #222's live tests, found the update cases for referenced
objects uncovered, and asked for the edge-case review that produced #225. The
decision to fix it on #222 was theirs, taken with the alternatives (test-current-
behaviour-and-follow-up, or a separate PR) in front of them.

**#222 will therefore close #216 and #225, and needs
`/claude-merge-approved-multi`.** The honest case for not splitting is weak: the
two changes touch different modules and could land separately. The case for
keeping them together is that #225 was found *by* #222's review, and the tests
the owner asked for cannot assert correct behaviour until #225 is fixed — a
split would mean either landing tests that pin a defect, or landing a fix whose
tests arrive in a later PR. Rejecting the waiver and asking for a split is a
legitimate answer, and the split is clean if the owner prefers it: #225's fix
plus its tests in one PR, #216 as it stands today in the other.

## What the review established

Verified by reading, and the three load-bearing ones re-verified independently:

- `StateEntry.depends_on` **does** shrink on removal. All three writers
  (`orchestrator.py:585`/`:601-602`, `:1267`, `:177`) overwrite; none merge.
- That write lands at **plan** time (`state.save()`, `orchestrator.py:506`),
  before `apply_plan` is reached, so declining the confirm still rewrites it.
  `specs/resource_dependencies.md` states this deliberately.
- A stale persisted `depends_on` **cannot** block `plan create`:
  `_resolve_dependency_edges()` rebuilds edges from the `.aiform.md`, never from
  state. An earlier worry to the contrary was wrong.
- **The defect (#225):** `_build_destroy_plan_from_paths`
  (`orchestrator.py:873-915`) builds `node_keys` and `raw_edges` from the passed
  files only. Edges run *out of* the nodes being destroyed, never *into* them, so
  a dependent outside the run is never consulted.
- Nothing warns: `_warnings_for_uncovered()` is gated on `if not paths`.
- The LLM gate cannot see it: `build_plan_summary()` (`orchestrator.py:990-1001`)
  carries no `depends_on`.
- 2→1 **is** detected. `droplet_ids` is in `UNORDERED_FIELDS`
  (`firewall.py:118`) but `compare.unordered_equal` is a multiset compare, so
  `[111,222]` vs `[111]` differs. It costs one intent-orchestration call.

Coverage today: **nothing** at any layer covers a `depends_on`/reference list
shrinking by an element, a firewall with two referenced droplets, or a target
destroyed in a prior run. The nearest live test is 1→0 with a **literal int**,
so no dependency edge is exercised at all.

## Decision this plan asks for: refuse, with `--force`

**Recommended: refuse by default, `--force` to proceed, mirroring
`_resolve_dangling_targets`.**

That function already exists in the same producer and already establishes the
pattern and the message shape:

```
cannot destroy: {pairs} -- neither in this run nor tracked in state;
pass --force to drop these edges and destroy anyway
```

Reasons to follow it rather than invent something:

- **Consistency inside one code path.** A user who has met the dangling-edge
  refusal already knows this vocabulary and already knows `--force`. A second,
  differently-shaped hazard in the same function would be gratuitous.
- **`--force` already exists on `destroy` only** (`cli.py:867`), which is
  exactly the right surface — there is deliberately no `--force` on
  `create`/`apply`.
- **The failure is silent and its symptoms surface far from the cause.** The user
  learns about it later, from a `PlanBlockedError` on an unrelated-looking plan
  or from unexplained drift. A refusal puts the cost at the moment of the
  decision.

Rejected alternatives, so a reviewer need not re-litigate:

- **Warn and proceed.** Cheaper and breaks no existing script, but the repo has
  been bitten by warnings that scroll past; and the analogous condition three
  lines away refuses. Inconsistent.
- **Cascade — destroy the dependent too.** Never. Destroying a resource the user
  did not name is the exact class of action this project refuses to take
  implicitly.
- **Put `depends_on` in the LLM summary and let the model catch it.** Adds a
  token cost to every destroy for a check that is a set lookup, and makes a
  deterministic safety property depend on a model. `specs/orchestrator.md:1365`
  records keeping the summary minimal as deliberate.

**This is the decision the owner should confirm or overturn before any code is
written.** Warn-and-proceed is a defensible different answer; it is the owner's
call, not the reviewer's.

## The change

### 1. `aiform/orchestrator.py` — the reverse-dependent check

In `_build_destroy_plan_from_paths`, after `node_keys` is known: for every
resource tracked in `st.resources` that is **not** in `node_keys`, read its
persisted `StateEntry.depends_on` and collect any target that **is** in
`node_keys`. Each such pair is a dependent that would be orphaned.

Use the persisted `depends_on`, not the dependent's file: the dependent's file
may not exist, may not be in the run, and re-parsing every `.aiform.md` on disk
to answer this would be a new filesystem scan on the destroy path. The persisted
value is the already-unioned declared-plus-reference-derived set, which is
exactly the question being asked.

Route the refusal through the **same** mechanism as the dangling case so `--force`
and message shape are shared rather than duplicated — most likely by extending
`_resolve_dangling_targets` or adding a sibling beside it that returns warnings
under `--force` and raises `PlanBlockedError` otherwise. Decide between those two
while reading the function; do not duplicate the `--force` branch.

Name every offending pair, not just the first — `_resolve_dangling_targets`
already sets that precedent and has a test pinning it
(`test_dangling_error_names_every_offending_target`).

### 2. Scope guards

- **Only the paths-driven producer.** `_build_destroy_plan_from_state` is already
  correct; it reads the unioned `depends_on` for every entry and orders in
  reverse topologically. Do not touch it.
- **Do not touch the create path.** `_warnings_for_uncovered`'s `if not paths`
  gate is arguably wrong too, but that is a different question with a different
  blast radius. Not in scope; do not "improve" it in passing.
- **Do not add `depends_on` to `build_plan_summary()`.** Rejected above.

## Tests, red before green

### Offline, `tests/test_orchestrator.py` and `tests/test_cli.py`

Mirror the existing dangling-edge tests' structure — they are the closest
analogue and live beside where these belong.

1. `plan destroy droplet.aiform.md` raises `PlanBlockedError` when a tracked
   firewall's persisted `depends_on` names that droplet. **The #225 regression;
   must fail before the change.**
2. The error names **every** orphaned dependent when several depend on the target.
3. `--force` drops the check and warns once per pair, rather than raising.
4. A dependent that is **itself** in the run does not trigger it — destroying
   both together is the normal case and must stay silent.
5. A tracked resource whose `depends_on` does **not** name the target is
   unaffected.
6. The state-driven producer (no file args) is unchanged — an existing-behaviour
   test to catch a regression in the half not being modified.
7. `--yes` alone does not bypass the check, matching
   `test_force_drops_dangling_edge_and_warns_per_pair`'s CLI sibling.

### Offline, the removal coverage the owner asked for

8. 2→1 on `droplet_ids`: `diff_attributes` reports the shrink despite
   `droplet_ids` being in `UNORDERED_FIELDS`. Pins the multiset property *on this
   field* — `tests/test_compare.py` pins it generically, nothing pins it here.
9. `StateEntry.depends_on` shrinks from two targets to one when the file drops a
   reference. The only existing test is a 1→1 swap
   (`test_update_without_replace_persists_depends_on_in_place`); nothing covers a
   shrink.
10. `firewall.update()` receives the shortened `droplet_ids` as `desired` and its
    whole-object PUT body carries the shortened list.

### Live, `tests/system/test_cli_references.py`

One new test, the owner's scenario (1) and (2) in sequence against real
infrastructure. Reuse `unique_droplet_name`, `unique_firewall_name`,
`write_aiform_md`, `write_firewall_aiform_md`, `ensure_system_test_tag`,
`get_firewall_or_none`, `assert_cli_ok`, `teardown_tracked_resources`.

- Two droplets plus a firewall with `depends_on` on both **and** `droplet_ids`
  referencing both `provider_id`s. Apply. Assert the live firewall's
  `droplet_ids`, read back from DigitalOcean, holds both real ints.
  **This alone is new** — no live test has ever created a firewall with two
  referenced droplets.
- **Scenario (1):** rewrite the firewall to reference one droplet. Apply. Assert
  the live `droplet_ids` holds exactly the remaining id. This settles finding 8
  above — that DigitalOcean actually detaches on a whole-object PUT carrying the
  shorter list, which the driver's own comment marks **inferred** for
  `droplet_ids` and verified only for `tags`.
- Assert the firewall's persisted `depends_on` shrank too.
- **Scenario (2):** `plan destroy` the remaining droplet's file while the
  firewall still depends on it. Assert it is **refused**, and that both the
  droplet and the firewall are still live afterwards — the refusal must not be a
  partial apply.
- Then `--force` the same command and assert it proceeds and warns.
- Destroy everything.

**Assert end state, never the action label.** `categorize_diff()` is a real LLM
call, so asserting the word "update" would be flaky. Assert the live
configuration and the state file.

## Verification

- Full offline suite under `env -u DIGITALOCEAN_TOKEN`.
- `ruff format` / `ruff check` clean.
- **Live `system-test` re-runs.** This touches `aiform/**/*.py` and
  `tests/system/**`, so the green status on `c6a26e6` is invalidated and cannot
  be carried forward. Budget the ~15 minutes and the real droplets; this run
  creates four droplets across the suite rather than three.
- Read the log; do not trust a shell exit code behind a pipe.
- `telleztec-wordpress` is production and never in scope.
- Nothing under `drivers/` or `aiform/` may be edited while the live suite runs —
  `load_driver` re-execs from disk on every call.

## Pairing

Sonnet 5 authors, Opus 5 reviews, as on the rest of #222.

## Process

- #222 closes **#216 and #225**; the waiver bullet goes in the PR description's
  `## Plan` section with the repeated closing keyword, and the owner is told in
  conversation that the waiver is needed — not left to notice it.
- All four gates re-earned on the new head SHA. Any approval already posted is
  cleared by the new commits.
- This plan commits to `plans/fix-225-reverse-dependent-destroy.md`.

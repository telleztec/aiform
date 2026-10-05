# Plan: Phase 4, review round 1 on PR #268 (cosmetic fixes)

Point-in-time record, per `plans/README.md`. Committed on PR 4a's branch (`phase-4a-repair-dependents`).

## Scope

The repo owner's review of #268 at 1fccdd8:

1. Comments and docstrings describe what the code does, plus the minimum why. History ("was", "now", issue numbers as provenance) is removed from this PR's own lines, and from #269's lines in a separate commit on that branch. Strings, messages and behaviour are untouched.
2. `apply_plan`'s `for position, pr in enumerate(planned)` loop dispatches on `PlanAction` with `match`/`case`. The `if pr.repairs:` block moves above the match, ahead of the NO_OP check (it is not an enum arm and takes precedence). A repairing entry is always an UPDATE (`planner.repair_entry`), so the move changes no outcome. Behaviour is identical; the existing suite is the proof, no tests added.

## Deferred

- The two `tests/system/conftest.py` comments and the orchestrator's droplet vocabulary (neutral VM terms in neutral layers): filed as issue #270.

## Verification

Offline suite under `env -u DIGITALOCEAN_TOKEN -u ANTHROPIC_API_KEY`, `ruff check`, `ruff format --check`, and `pytest tests/system --collect-only`. No live run: the owner waived it because the change is cosmetic.

## Gates

Delta review only. The `system-test` and `llm-review` statuses are carried forward from the prior head. A fresh `/claude-merge-approved-multi` is required on each new head.

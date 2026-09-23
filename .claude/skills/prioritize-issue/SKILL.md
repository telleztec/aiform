---
name: prioritize-issue
description: Apply a priority label (P0-safety/P1-correctness/P2-usability/P3-cosmetic) to a single GitHub issue in this repo. Use right after filing a new issue, or when asked to triage/prioritize an existing one.
---

# prioritize-issue

Rubric and labels approved in issue #191, after an ad hoc `priority: high`
label was created to unblock a request, correctly pushed back on, and
reverted for having no written, approved basis. This skill is that basis.

One issue at a time, invoked deliberately — not a bulk/backfill tool. If
asked to triage a batch of existing issues, run this procedure once per
issue rather than trying to shortcut it across all of them at once.

## The rubric

Four tiers. Walk the tests **in this order — P0 first — and stop at the
first one that's met**, since safety wins ties:

- **P0 — Safety**: can cause an unintended destructive/irreversible action,
  real ongoing cost, or resource/data loss *without the user's informed
  consent* — regardless of how it was triggered (a bug, a race, an external
  kill). Test: *if this fires unnoticed, does something bad and
  hard-to-reverse happen, or does money/data leak silently?*
- **P1 — Correctness / process integrity**: not immediately destructive, but
  the tool's output, or a gate meant to catch mistakes, is provably wrong or
  non-functional. Test: *could someone make a bad decision trusting this, or
  does a safety mechanism not actually do its job?*
- **P2 — Usability**: confusing or misleading output that costs time/trust
  but doesn't risk a wrong action or bad data. Test: *does a human get
  confused or annoyed, without anything actually going wrong?*
- **P3 — Cosmetic / deferred**: documentation drift, nits, anything already
  explicitly deferred during a review round as non-blocking.

## Procedure

1. Read the issue: `gh issue view <n>` (title and body — for a not-yet-filed
   issue, use the drafted title/body directly).
2. Apply the rubric above and settle on exactly one tier.
3. Check for any `priority: *` label the issue already carries — there
   should be at most one, but check rather than assume:
   ```sh
   gh issue view <n> --json labels --jq '.labels[].name | select(startswith("priority: "))'
   ```
4. Set the label in **one** `gh issue edit` call — a `--remove-label` per
   stale label step 3 printed (if any), plus `--add-label` for the chosen
   tier, using the full label name every time:
   ```sh
   gh issue edit <n> \
     --remove-label "priority: <stale-tier, if any>" \
     --add-label "priority: P0-safety"   # or "priority: P1-correctness" / "priority: P2-usability" / "priority: P3-cosmetic"
   ```
   One call, not two — a remove followed by a separate add leaves a window
   where the issue briefly has no priority label at all if the second call
   fails.

For a newly-filed issue, do this immediately after `gh issue create` — as
part of filing, not a separate later pass.

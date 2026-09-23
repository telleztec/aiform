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
4. Act on what step 3 printed. `gh issue edit` resolves `--remove-label`
   and `--add-label` into a single final label set rather than applying
   them as ordered operations, so passing both for the **same** label nets
   to no label at all — don't do that:
   - **Nothing printed** — add the chosen tier:
     ```sh
     gh issue edit <n> --add-label "priority: P0-safety"   # full name, matching one of the four labels above
     ```
   - **The chosen tier was already printed** — nothing to do; already
     correctly labeled.
   - **A different tier was printed** — remove it and add the chosen one
     in one call, using full label names for both, so there's never a
     window with no priority label at all (unlike two separate calls,
     where the issue is briefly unlabeled if the second call fails):
     ```sh
     gh issue edit <n> \
       --remove-label "priority: <the different tier step 3 printed>" \
       --add-label "priority: P0-safety"   # full name, matching one of the four labels above
     ```

For a newly-filed issue, do this immediately after `gh issue create` — as
part of filing, not a separate later pass.

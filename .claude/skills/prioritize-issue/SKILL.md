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

- **P0 — Safety** (`priority: P0-safety`): can cause an unintended
  destructive/irreversible action, real ongoing cost, or resource/data loss
  *without the user's informed consent* — regardless of how it was triggered
  (a bug, a race, an external kill). Test: *if this fires unnoticed, does
  something bad and hard-to-reverse happen, or does money/data leak
  silently?*
- **P1 — Correctness / process integrity** (`priority: P1-correctness`): not
  immediately destructive, but the tool's output, or a gate meant to catch
  mistakes, is provably wrong or non-functional. Test: *could someone make a
  bad decision trusting this, or does a safety mechanism not actually do its
  job?*
- **P2 — Usability** (`priority: P2-usability`): confusing or misleading
  output that costs time/trust but doesn't risk a wrong action or bad data.
  Test: *does a human get confused or annoyed, without anything actually
  going wrong?*
- **P3 — Cosmetic / deferred** (`priority: P3-cosmetic`): documentation
  drift, nits, anything already explicitly deferred during a review round as
  non-blocking.

The parenthesized text after each tier is that tier's **exact, full** GitHub
label name — the only four `priority: *` labels this skill ever adds.

## Procedure

1. Read the issue: `gh issue view <n>` (title and body — for a not-yet-filed
   issue, use the drafted title/body directly).
2. Apply the rubric above and settle on exactly one tier.
3. Check for any `priority: *` label the issue already carries — there
   should be at most one, but check rather than assume:
   ```sh
   gh issue view <n> --json labels --jq '.labels[].name | select(startswith("priority: "))'
   ```
4. Compare the chosen tier's label (from the rubric above) against
   everything step 3 printed, and act in **one** `gh issue edit` call —
   never two, which leaves the issue briefly (or, done wrong, permanently)
   without any priority label between calls:
   - **Nothing printed** — add only:
     ```sh
     gh issue edit <n> --add-label "priority: P2-usability"
     ```
   - **Exactly the chosen tier's label, and nothing else** — already
     correctly labeled; nothing to do.
   - **Anything else** — a different tier, more than one label, or the
     chosen tier alongside another — `--remove-label` every printed label
     that is **not** the chosen tier (repeat the flag once per such label),
     plus `--add-label` for the chosen tier if it wasn't already among them.
     Never pass `--remove-label` and `--add-label` for the *same* label:
     `gh issue edit` resolves them into one final set rather than ordered
     operations, so removing and adding the same label nets to no label at
     all.
     ```sh
     gh issue edit <n> \
       --remove-label "priority: P1-correctness" \
       --remove-label "priority: P3-cosmetic" \
       --add-label "priority: P2-usability"
     ```

For a newly-filed issue, do this immediately after `gh issue create` — as
part of filing, not a separate later pass.

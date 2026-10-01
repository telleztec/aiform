# Plan: live tests for interrupt, retry, converge (#239)

Approved by the repo owner on 2026-10-01 through the decision page
https://claude.ai/artifact/PPAtxuBEZtW26zBL2q65z7 (doc `decisions/239`,
answered 2026-10-01T05:12:26Z, read back from the page database). Recorded
choices: `mech` inject, `scope` full, `ucf` A, `bug` xfail, `pair`
sonnet-opus, `approve` approved. The page predates the page-version field, so
the answer is matched by id and timestamp (published 05:09, answered 05:12).

Pairing: Sonnet 5 authors, Opus 5 reviews. Depends on #246, which is merged.

## Problem

Nothing proves against a real provider that aiform tells the user before it
removes something another resource still references (UC-F), or that a run
interrupted partway converges when re-run (UC-G).

## Decisions

- Interruption is injected in-process: after the real provider call for a
  chosen method and URL completes, the wrapper around `urllib.request.urlopen`
  (or `aiform.state.save`) raises `KeyboardInterrupt`. No SIGKILL test.
- All nine stages, parametrized so `-k C1` runs one:
  - Create: C1 provider accepted the create, state never saved; C2 mid-poll
    while the droplet is `new`; C3 droplet saved, firewall not yet created.
  - Update: U1 before the provider call; U2 provider accepted the update,
    state not saved; U3 between two resources in one apply.
  - Delete: D1 provider deleted, state not saved (UC-G a); D2 state saved,
    marker file still present (UC-G b); D3 mid multi-resource destroy.
- UC-F test 1: marker plus firewall file in one run, `plan create` refuses and
  the droplet still exists at the provider.
- UC-F step 2 (marker path alone orphans the firewall, #226): assert the
  desired refusal as `xfail(strict=True, reason="#226")`, with a separate
  assertion that pins the recovery that works today.
- A stage that exposes a real bug is `xfail(strict=True)` with the issue
  number. The new issue is filed from the use case, and the owner sees its
  text before it is filed. No fix to `aiform/` or `drivers/` in this PR.

## What each stage asserts

- The faulted run exits non-zero.
- The provider, queried by ids the test created, shows what the stage allows.
- Retry exits 0 and the provider matches the declared files: no duplicates, no
  orphans.
- A second run is a no-op, makes zero Anthropic calls, and a following
  `plan create` reports no changes.

## Safety

- Names come from `unique_droplet_name()` and `unique_firewall_name()`; the
  `aiform-system-test` tag exists up front.
- Out-of-band calls use only ids the test created. The production WordPress
  droplet is never listed or touched.
- Teardown deletes by recorded ids at the provider, not only through state.
- Updates use firewalls and domains (free). Droplets only where the async
  lifecycle is the point.
- No edits to `aiform/` or `drivers/` during a live run. Read
  `.aiform/testlog/`, never the exit code.

## Not built

- SIGKILL tests; fixes for any bug found.

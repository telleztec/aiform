# Plan: live fault tests as a stack of three PRs (follow-up to #239)

Approved by the repo owner on 2026-10-02 through the decision page
https://claude.ai/artifact/3BTHR2kQXWLXkNQWPwr3Ae (doc `plan/answers`, submitted
2026-10-02T16:46:59Z, verdict approve, read back from the page database).
Recorded choices: `t3` include, `droplet_id` assert, `issues` both, `pr1`
nothing (wait for #254 to land; it has, as `9538709`). The owner added in chat:
the id assertion must be generic, not driver specific, because aiform will be
multi-CSP.

Pairing: Sonnet 5 authors, Opus 5 reviews. No change to `aiform/` or `drivers/`.

## Stack

| PR | Fault | Base | Status |
|----|-------|------|--------|
| 1 (#254) | interrupt (Ctrl-C) | `main` | merged |
| 2 | timeout (client loses a race) | `main` | in progress |
| 3 | URL failures (reset, 500, 503, 429) | PR 2's branch | after PR 2's injector is reviewed |

- Merge order is 2, then 3, each only on the owner's `/claude-merge-approved`
  for that PR and head.
- Each PR carries its own `## Plan` section, closing keyword, issue (drafted
  from the use case, shown to the owner before filing) and its own live
  `system-test` on its head.

## Shared design

`fail_request(method, url_pattern, failure, *, provider_acts=True, occurrence=1)`
wraps `urllib.request.urlopen`. `failure` is a key into a small dispatch table;
PR 2 builds `timeout`, PR 3 only adds kinds. `provider_acts=True`: the real
request runs and the provider does the work, then the caller sees the failure
(the #253 shape, reached without an interrupt).

Every stage asserts: the faulted run exits non-zero; the error names the failed
operation, and contains the resource id when the provider returned one before
the failure; the provider holds only what the stage allows (ids the test
created); the retry exits 0 with one resource per declared name; a second run is
a no-op with zero Anthropic calls.

## Multi-CSP

- The injector names no provider. It matches method and URL pattern. Limit:
  every driver today calls `urllib` directly. An SDK-based driver (boto3) needs
  a different seam; noted, not built.
- A `ProviderProfile` (create request, poll request, resource-id reader, ledger
  list hook, a not-ready rewrite and the poll loop's name) keeps the stages
  from hard-coding DigitalOcean. DO is the only implementation; no registry.
- Id assertion, generic: when the provider returned a resource id before the
  failure, the error output contains it, read through the profile. When the
  failure precedes any id (the create POST), the stage asserts only that the
  error names the failed operation. A driver error that omits the id is a
  finding: `xfail(strict=True)` plus issue text to the owner, not a code change.

## PR 2: timeout

A worker thread sleeps `delay` (1.5 s) and then calls the real `urlopen`; the
caller waits `deadline` (1.0 s) and raises `TimeoutError`. `delay` > `deadline`
(validated), so the caller always loses and the provider still acts. The worker's
response is recorded on the `Fault`; the test joins the worker (bounded) and the
context manager joins it on exit even when the body raises.

- T1: the create POST loses the race; state has no entry.
- T2: the POST succeeds; the first poll GET loses the race.
- T3: every poll GET reports `new` and `time.sleep` is patched in the driver, so
  `_poll_until` runs all its polls in seconds and raises its real `TimeoutError`.

Expected, from the code and unverified: the retries duplicate the droplet, so
`xfail(strict=True, reason="#253")`. Anything else is reported before a marker
is chosen.

## PR 3: URL failures

Adds `reset`, `http_500`, `http_503` and `http_429` (`provider_acts=False`, the
control: the provider never saw the call, so the retry must make one droplet).
Matrix: create POST and first poll GET get all four; resize and delete get
`http_503` once each.

## Safety

Names from `unique_droplet_name()`, the `aiform-system-test` tag, teardown by
ledger id, the production droplet never listed or touched. After every live run
check `droplets`, `firewalls` and `domains` for leaks.

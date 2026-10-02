# specs/system_test_interrupt.md — `tests/system/test_cli_interrupt.py` and `tests/system/fault_injection.py`

## Purpose

Live proof, against real DigitalOcean, of two things nothing else
exercises (`specs/MULTI_RESOURCE_PRD.md`, UC-F and UC-G):

- **UC-G, interrupt and retry converge.** A create, update or delete cut
  off partway leaves the provider and `.aiform/state.json` in some
  in-between condition. Re-running the same command must end with the
  provider matching the declared files exactly, with no duplicate and no
  orphan, and a run after that must be a no-op.
- **UC-F, refuse to orphan a dependent.** Removing a resource another one
  still references is refused before anything is deleted, and the
  referenced resource is still there afterwards.

Until now both were established by reading the code. A mock cannot settle
either, because the mock encodes the same ordering assumption the
orchestrator does.

Plan and approval: `plans/interrupt-retry-live-tests.md` (#239). The timeout
and URL-failure follow-ups (a stack on the same injector):
`plans/fault-timeout-live-tests.md`.

## Interface

`pytest -m system tests/system/test_cli_interrupt.py`, excluded from the
default run by `pyproject.toml`'s `addopts`. Needs `ANTHROPIC_API_KEY` and a
`DIGITALOCEAN_TOKEN` with droplet and firewall scope; skips, never fails,
without them. One stage: `pytest -m system tests/system/test_cli_interrupt.py -k C1`.

`tests/system/fault_injection.py` is the injector. It imports nothing live,
so `tests/test_system_fault_injection.py` tests it offline in the default
run against a fake `urlopen`.

`tests/system/provider_ledger.py` is the teardown bookkeeping (`Ledger`): the
droplet and firewall ids and generated names the test has learned of, and the
exact-name matching that decides what teardown may delete. It does no I/O, so
`tests/test_system_interrupt_ledger.py` tests it offline. `conftest.py` gains
`list_droplets_tagged(token, tag)`, a `tag_name`-filtered listing, tested in
`tests/test_system_conftest.py`.

```python
class InjectedInterrupt(KeyboardInterrupt)

@dataclass
class Fault:
    fired: bool
    response: dict | None   # parsed JSON body of the faulted response
    seen: list[tuple[str, str]]   # every (METHOD, url) while installed

interrupt_after_request(method, url_pattern, *, occurrence=1, response_predicate=None)
interrupt_before_request(method, url_pattern, *, occurrence=1)
interrupt_after_state_save(predicate, *, occurrence=1)
```

Each is a context manager yielding a `Fault`.

```python
fail_request(method, url_pattern, failure, *, provider_acts=True, occurrence=1,
             join_timeout=10.0, **options)   # timeout: delay=1.5, deadline=1.0
Fault.worker_error: BaseException | None
Fault.join(timeout=10.0)    # RuntimeError if a late call is running or failed
```

`fail_request` makes the caller see `failure` instead of the response. `failure`
is a key into a small dispatch table (`_FAILURES`, kind -> handler and option
checker); `"timeout"` is the only one today and later kinds (URL failures) are
added there, not as classes. `**options` belong to the kind: `timeout` takes
`delay` and `deadline`, another kind takes none and is never given or checked
against them. Arguments are checked at the call: an unknown `failure`, an option
the kind does not know (`TypeError`), or `delay <= deadline` (`ValueError`)
raises before any block is entered.

`tests/system/provider_profile.py` holds `ProviderProfile`: the create request,
the poll request (each a `(method, url_pattern)` pair), `resource_id(body)`
(the id in a create or poll response, or `None`), `not_ready(body)` (a poll
body rewritten to report the resource not ready), `poll_loop` (the name of the
driver function whose sleeps a stage may skip), and `list_owned(token)` for
teardown. `DIGITALOCEAN` is the only instance; there is no registry. A stage's
own logic asks the profile and names no provider; the shared helpers it reuses
from the interrupt suite (`unique_droplet_name`, the ledger's `droplet_names`)
still carry DigitalOcean words. Tested offline in
`tests/test_system_provider_profile.py`.

## Behavior

### The injector

- It runs inside the process under test. The suite already drives
  `aiform.cli.main()` in-process, so wrapping `urllib.request.urlopen`
  (which every driver reaches as a module attribute) and `aiform.state.save`
  (which the orchestrator reaches as `state.save`) needs no subprocess.
- `InjectedInterrupt` subclasses `KeyboardInterrupt`, a `BaseException`. It
  passes through `except Exception` in `_call_driver`, `apply_plan` and
  `firewall.create`'s rollback, and through `cli._dispatch`, which catches
  only its handled exceptions. That is the same path a real Ctrl-C takes. A
  real one ends the process with exit 130; in-process, the test asserts
  `InjectedInterrupt` escaped `cli.main()`, which stands for that non-zero exit.
- `interrupt_after_request` calls the real `urlopen`, and when `method`
  equals the request's method and `url_pattern` (a regex, `re.search`)
  matches its URL, counts the match. On the `occurrence`th match whose
  response satisfies `response_predicate` (default: any), it raises. The
  provider has already acted: this is "accepted, caller never heard back".
- A matching call's body is read once. The parsed JSON is kept on
  `Fault.response`. A call that does not fire gets a replaying response
  object, so the driver's own `read()` still works and sees the same bytes.
  A call whose method or URL does not match is passed through untouched.
- `interrupt_before_request` raises on the `occurrence`th match *instead of*
  calling the provider.
- `interrupt_after_state_save` calls the real `state.save`, then raises on
  the `occurrence`th save whose state satisfies `predicate`.
- `fail_request(..., "timeout")` is the race the client loses. On the
  `occurrence`th match a worker thread sleeps `delay`, then calls the real
  `urlopen`; the caller waits `deadline` and raises `TimeoutError`. `delay` is
  longer than `deadline`, so the caller always loses and, with
  `provider_acts=True`, the provider still acts: the #253 shape, without an
  interrupt. The worker's body is parsed into `Fault.response` when it
  arrives (so a ledger can learn the id), and any exception it hits lands in
  `Fault.worker_error`. With `provider_acts=False` no worker runs and the real
  `urlopen` is never called. `join()` is bounded and raises `RuntimeError` if a
  worker is still running or if its call failed (a late call that never reached
  the provider means the provider did not act, so the stage has not reproduced
  the shape it names). Leaving the block joins every worker, after restoring
  `urlopen`, even when the body raised. When the body raised, its exception
  propagates and a join failure is attached to it as a note, so a failing
  assertion is never replaced by "a late call is still running". The join
  failure names the late request (method and URL) and says to look for the
  resource by name. A worker still running at that point cannot be stopped:
  its request may yet reach the provider after teardown, and a resource it
  creates is caught only by teardown's by-name match; the session sweep skips
  anything younger than 60 minutes, so it catches a leak only on a later run.
  The injected `TimeoutError` is an `Exception` (unlike
  `InjectedInterrupt`), so a caller that catches it carries on: the driver's
  `_poll_until` timeout is swallowed in the SSH power-off path
  (`drivers/digitalocean/compute.py`), so a stage must not inject there and
  expect a failure.
- `interrupt_*` and `fail_request` fire at most once. Afterwards they pass
  everything through, so `finally` blocks that make provider calls are not cut
  off a second time. `rewrite_responses` rewrites every match.
- Installing is scoped: the original `urlopen` and `save` are restored on
  exit, on every path, so the retry and the test's own provider queries run
  unpatched.
- A request that raises (an `HTTPError`) propagates unchanged and never
  counts as a match.
- A request is described by `Request.get_method()` and `full_url`, or by the
  bare string for a `urlopen("https://...")` call.

### The stages

Nine stages in three parametrized tests, so `-k C1` selects one. Every stage
asserts, in this order:

1. The faulted run raised `InjectedInterrupt`, and `Fault.fired` is true. A
   stage whose injection point was never reached fails with that message
   rather than passing vacuously.
2. The provider, queried by ids the test recorded, shows what the stage
   allows (below).
3. Re-running the same command exits 0 and the provider matches the declared
   files: exactly one object per declared name, no extras.
4. A second run (`plan apply --yes --verbose`) is a no-op: every remaining
   resource prints `= <key>: no-op`, zero to create, update and destroy, and
   `verbose_call_count == 0`. A following `plan create --verbose` agrees, with
   no `Warning:` line (a resource tracked with no file is a warning).

| Stage | Where it is cut | Provider allowed to show |
|---|---|---|
| C1 | after `POST /v2/droplets` returns, before state is saved | the droplet exists, state does not track it |
| C2 | after a `GET /v2/droplets/{id}` that reports a status other than `active` | the droplet exists and is still provisioning or just active |
| C3 | after state saves the droplet, before the firewall is created | the droplet exists and is tracked, no firewall by that name |
| U1 | before `PUT /v2/firewalls/{id}` | the old rules |
| U2 | after that `PUT` returns, before state is saved | the new rules, state holds the old |
| U3 | after state saves the first of two edited firewalls | one firewall new, the other old |
| D1 | after `DELETE /v2/droplets/{id}` returns, before state is saved | gone or going, still tracked |
| D2 | after state drops the droplet, before the marker file moves to the trash | gone or going, not tracked, marker still on disk |
| D3 | after state drops the firewall, before the droplet delete (two markers, firewall depends on the droplet) | firewall gone, droplet live and tracked |

The create and delete stages use droplets because the asynchronous
provisioning and teardown is what is under test. The update stages use
firewalls, which are free, and only the rules change.

### Expected results

C1 and C2 **fail** today, confirmed live (#253). `create()` has no idempotency
key and no lookup by name, and state is written only after `create()` returns,
so a retry cannot know the first droplet exists and POSTs a second. The tests
assert the desired behaviour (one droplet per declared name) under
`xfail(strict=True, reason="#253...")`. The other stages converge, confirmed
live.

### Timeout stages

`tests/system/test_cli_timeout.py`, four stages in one parametrized test
(`-k T2` selects one). A real timeout cannot be waited for, so the caller
loses a race: `fail_request(..., "timeout")` lets the real request reach the
provider and raises `TimeoutError("timed out")` at the caller, the message
urllib itself gives, so nothing from the request leaks into aiform's error.
Every stage asserts, in order:

1. The faulted run exits non-zero and its stderr names the failed operation
   (`driver failed during create`).
2. The provider holds exactly one resource under the declared name, found by
   name and not by the response the test injected, and the id in the
   provider's response (read through `ProviderProfile.resource_id`) is that
   resource's id. State tracks nothing.
3. T2 and T3 only: the error names that id. T1 asserts the operation name only,
   because no response was ever read. T4 is the control and replaces steps 2
   and 3: the provider holds nothing under the name, because the request never
   reached it, and the injector recorded no response.
4. The retry exits 0 and leaves exactly one resource per declared name; the
   duplicate is raised as `RetryDuplicatesResource`, which is what the
   `xfail(strict=True, raises=RetryDuplicatesResource, reason="#253")` marker
   accepts, so any other failed assertion is not hidden by it.
5. A second run is a no-op with zero Anthropic calls.

| Stage | Where the caller loses | Helper |
|---|---|---|
| T1 | the create `POST` | `fail_request` |
| T2 | the first poll `GET` | `fail_request` |
| T3 | every poll reports the resource not ready until the poll budget is spent | `rewrite_responses` + `skip_driver_sleeps` |
| T4 | the create `POST`, which the provider never receives (`provider_acts=False`) | `fail_request` |

`rewrite_responses(method, url_pattern, rewrite)` hands the real response body
to `rewrite` and returns the result in its place; T3 passes the profile's
`not_ready`. `skip_driver_sleeps(functions)` makes `time.sleep` return at once
for calls made directly by a function named in `functions` (T3 passes the
profile's `poll_loop`) in a module named `aiform_driver_*`, so the driver's own
poll loop runs to exhaustion in seconds. A driver's other waits, such as the
key-propagation backoff in create, still sleep: skipping them would let T3 die
on a provider error instead of the exhausted poll budget. This patches the shared `time.sleep` and filters on caller, not a
driver module attribute as the plan worded it, because `load_driver()` execs
the driver afresh on every call and a module-level patch would not survive.

The runner and teardown shared with the interrupt suite live in
`tests/system/live_support.py`.

Live results, 2026-10-02 (rerun on head `d38fb35` after review: 3 xfailed, no
XPASS, no leaks):

- T1, T3: duplicate observed live, `xfail` against #253.
- T2: fails at step 3. The error is
  `digitalocean.compute driver failed during create: timed out` with no
  droplet id: a poll `GET` that times out says only "timed out", and the
  driver holds the id in a local. The retry also duplicates (observed with the
  step 3 assertion relaxed locally). T2 is `xfail(strict=True)` on
  `ErrorOmitsResourceId` alone; once the error carries the id it reaches the
  duplicate check, raises `RetryDuplicatesResource`, and fails strictly until
  its marker becomes the #253 one. The issue for the missing id is awaiting
  owner approval of its text.
- T4 (live, 2026-10-02): passes. A create that times out without reaching the
  provider leaves nothing there and nothing in state; the retry makes exactly
  one droplet and the second run is a no-op. It shows the duplicate in T1 and T3
  comes from the accepted create and not from the timeout itself.

### UC-F

- **Refusal.** A droplet and the firewall that references it are applied.
  The droplet's file is renamed `AIFORM-DELETE-*` while the firewall's file
  stays. `plan create` and `plan apply --yes` both exit 2 with an `Error:` on
  stderr naming both keys and saying the target is marked for deletion. Then,
  via the provider and by the recorded id, the droplet still exists; state
  still tracks both.
- **Marker path alone** (#226). Handing `plan apply` only the marker path
  skips the dependency check, because that check reads the other file. The
  desired refusal is `xfail(strict=True, reason="#226")`. A separate test
  pins the recovery that works today: after the droplet is gone, `plan create`
  refuses and names both keys; removing the dead reference from the firewall's
  file lets `plan apply` converge; and a following run is a no-op.

### Teardown

- Every droplet and firewall id the test learns of is recorded as it is
  learned: from state after each step, and from the response body of the
  faulted `POST`. Teardown deletes those ids directly at the provider, with
  `destroy_droplet_or_shout` for droplets.
- A droplet whose creation the test interrupted, and then retried, has no
  state entry. Names are the only handle, so teardown also looks up droplets
  carrying `aiform-system-test` by the **exact** names this test generated and
  deletes those ids. It never lists the account without that tag, and never
  deletes a name the test did not generate.
- The ordinary path, `plan destroy --all --deployment default --yes`, runs
  first when a state file exists. The id-based pass is independent of it, per
  `specs/system_test.md`'s rule that a backstop must not depend on what it
  backs up.
- The production droplet is never listed, queried or touched: it carries
  neither the tag nor a generated name.
- The `aiform-system-test` tag is created up front. The session sweeps in
  `conftest.py` remain the last backstop.

## Edge cases / errors

- **C2 depends on seeing a non-`active` status.** DigitalOcean reports `new`
  for a while after the POST, and the first poll normally lands inside it. If
  a droplet reaches `active` before the first poll, the injection point is
  never reached, `Fault.fired` is false, and the stage fails with that
  message instead of passing without having tested anything.
- **A real interruption during `plan apply` also skips `finally` blocks it
  has not reached.** Nothing here relies on cleanup inside aiform; the
  injector raises where a signal would.
- **Retry costs real Anthropic calls** (a destroy is reviewed by gate #2,
  an edit is categorized). Only the second run's zero is asserted.
- **A droplet delete is asynchronous**, so "gone" is polled with
  `wait_until_droplet_gone`, never checked once.

## Out of scope

- **SIGKILL or a killed subprocess.** In-process injection reaches the same
  points deterministically; killing a process at a precise point is not
  reproducible.
- **A non-urllib driver.** The injector wraps `urllib.request.urlopen`, which
  every driver reaches today. A driver on an SDK with its own HTTP stack (boto3)
  needs a different seam; not built.
- **Moving the injector out of `tests/system/`.** It stays beside the suite that
  uses it. Using it from a default-run test would be a move, taken when there
  is a second user.
- **Fixing anything found.** A stage that exposes a real bug is marked
  `xfail(strict=True)` with its issue number (C1 and C2: #253). No change to
  `aiform/` or `drivers/` in this work.
- **Domains.** They add nothing the firewall update stages do not, and need the
  zone parent.
- **Re-proving the CLI, gates or drivers.** `test_cli_digitalocean.py`,
  `test_cli_firewall.py` and `test_cli_references.py` own that.

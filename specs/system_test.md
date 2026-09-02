# specs/system_test.md — `tests/system/test_cli_digitalocean.py`

## Purpose

Every existing test in `tests/` (including
`tests/drivers/test_digitalocean_compute.py`) runs against a mocked
`urllib.request.urlopen` and a fake Anthropic client — `specs/digitalocean_compute.md`
names this explicitly under "Out of scope": *"A live integration test
against DO's real API with a real `DIGITALOCEAN_TOKEN` ... this spec and
the hand-written test suite ... are both built and validated against
mocked `urllib.request.urlopen`, not a live DO account."* `PLAN.md` §6
independently names the same gap for the driver-generation path: *"the
generation process includes a system test ... that verifies the
generated operations actually work against the real CSP API."*

This spec covers that gap for the curated driver that exists today. It
is not a driver-level test — it drives the whole system through
`aiform/cli.py`'s actual command surface (`init`, `plan create`, `plan
apply`, `plan refresh`, `plan destroy`) exactly as a real user would,
against real DigitalOcean and Anthropic APIs, using
`drivers/digitalocean/compute.py` as the one resource under test. It is
the automated form of `PLAN.md` §9's MVP walkthrough, extended with the
update/replace/destroy steps §9 doesn't cover, plus the explicit
zero-Anthropic-call verification CLAUDE.md's implementation-order note
asks for ("actually verify that with `--verbose` logging or a request
counter, don't just assume it") — `_CountingClient` and the `"[verbose]
{n} Anthropic API call(s) made"` line already implemented in `aiform/cli.py`
(`_report_verbose_calls`) are exactly that counter; this suite is its
first real caller against a live API.

Once mechanism 2 (`aiform driver create`, `PLAN.md`'s "Driver curation")
is built, the "system test" §6 describes for a freshly drafted
driver is a variant of this same suite pointed at that driver instead of
the curated one — not a separate concept.

## Interface

- **Location**: `tests/system/test_cli_digitalocean.py`, in a new
  `tests/system/` directory — mirrors the existing `tests/drivers/`
  grouping, and leaves room for a second provider's system test later
  without cluttering `tests/`'s root. **Naming note**: this spec's own
  filename, `specs/system_test.md`, deliberately doesn't follow
  `specs/README.md`'s strict per-module mirroring rule
  (`drivers/digitalocean/compute.py` → `specs/digitalocean_compute.md`)
  — that rule assumes one spec maps to one implementation module, and
  this spec instead covers a cross-cutting test suite that exercises
  `cli.py`, `orchestrator.py`, and the compute driver together end to
  end, with no single module to mirror. Named for what it is rather than
  mechanically flattening the test file's own path.
- **Invocation**: a pytest marker, `@pytest.mark.system`, registered in
  `pyproject.toml`'s `[tool.pytest.ini_options]` with `markers = ["system: ..."]`
  and excluded from the default run via `addopts = "-m 'not system'"`.
  Plain `pytest` (what `.github/workflows/tests.yml` runs today) never
  collects or executes this suite. Explicit invocation only:
  `pytest -m system tests/system/`.
- **Gating fixture**: a session-scoped `autouse` fixture in
  `tests/system/conftest.py` that calls `pytest.skip(...)` (not a
  failure) unless *both* `ANTHROPIC_API_KEY` and `DIGITALOCEAN_TOKEN`
  are set in the environment. This keeps the suite runnable by anyone
  with credentials without a separate flag, while a contributor without
  a DO account, or CI with no secrets configured, skips cleanly instead
  of failing.
- **Isolation**: every test runs inside a `tmp_path`-backed working
  directory (`monkeypatch.chdir(tmp_path)`), so `.aiform/state.json` and
  `.aiform/credentials.env` never touch the real repo checkout or collide
  across parallel runs. `tmp_path` isolation is local-only, though — it
  says nothing about DigitalOcean's own droplet namespace, which every
  concurrent run shares. Each fixture's base name (`"aiform-system-test-lifecycle"`,
  `"aiform-system-test-bad-token"`, `"aiform-system-test-ssh-keys"`) is
  passed through `tests/system/conftest.py`'s `unique_name()` before use,
  appending a `%Y%m%dT%H%M%SZ` timestamp (matching `scripts/run_system_tests.py`'s
  own log-filename convention) plus a short random suffix, so two runs
  overlapping in DO's droplet list never collide on name and a leaked
  droplet's age is legible from its name alone without needing to cross-
  reference `state.json` or the sweep script's own timestamp field.
- **Driving the CLI**: calls `aiform.cli.main([...])` in-process (same
  entry point `tests/test_cli.py` already uses), not a subprocess — so
  stdout/stderr capture and exit codes are asserted the same way the
  existing CLI tests do, and `--verbose`'s call-count line
  (`aiform/cli.py:197`) is directly assertable.
- **Cost/cleanup fixture**: a fixture that yields control to the test
  body inside a `try`/`finally`, and in the `finally` clause runs
  `aiform plan destroy --yes` (or, if nothing was ever created, a
  no-op) against whatever `state.json` exists in `tmp_path` at that
  point — so a droplet is torn down even when an assertion mid-test
  raises. Its scope must match how Behavior's ordered sequence
  (cases 1–9) is implemented: since those cases deliberately share one
  `tmp_path` and one tracked droplet across the whole sequence (see
  Behavior), that entire sequence is **one pytest test function**, with
  one function-scoped instance of this fixture wrapping it — not
  several chained test functions each with their own instance, which
  would tear the droplet down after the first one finishes and break
  every case after it. Case 10 (the bad-token path) is independent and
  gets its own separate test function with its own instance of this
  fixture. This is the primary cleanup path, but not the only one — see
  "Orphan cleanup (leaked resources)" below for what covers the case
  where even this fixture doesn't get to run.
- **Fixture params bound cost**: the `.aiform.md` fixture used
  throughout requests DigitalOcean's cheapest available droplet size
  (`s-1vcpu-512mb-10gb` or equivalent at time of writing — verify
  against DO's current size list if this 404s) and a region/image pair
  known cheap and available; a droplet under test never runs longer
  than one test's duration.

## Behavior

Cases 1–9 are one ordered sequence within a single test function,
sharing one `tmp_path` and one tracked droplet — deliberately
sequential, not independent per-case, because each step depends on
state the previous one created (see Interface's fixture-scope note for
why this must not be split across multiple test functions). Case 10 is
independent, in its own test function with its own `tmp_path`.

1. **`aiform init`** — scaffolds `.aiform/`, `.gitignore` entries, and
   `examples/compute.aiform.md`; with both real env vars set, prints
   `✓` for both credential checks. **`init` does make live API calls**
   (`specs/cli.md`'s preflight): a free `GET /v1/models` for Anthropic,
   and `GET /v2/account` plus `GET /v2/droplets?per_page=1` for
   DigitalOcean. All are read-only and unbilled, so this step costs
   nothing and creates nothing — but it means a rejected token now
   surfaces **here**, at step 1, rather than at step 2.

   This reverses the note that stood here previously ("don't expect
   `init` to catch a bad token"), which described the presence-only
   check that `specs/cli.md` replaced. A `✓` on this step is now
   evidence the token authenticates, not merely that it is set.

   Note also that `✓` for DigitalOcean carries a detail — the account
   email, or `authenticated (scoped token)` for a token that cannot read
   the account — so an assertion on this line must not expect it to end
   after the variable name.
2. **First `plan create`** (fresh project, no `state.json` yet) — per
   `PLAN.md` §9 step 2: gate #1 (`code-review-model`) fires to
   trust-on-first-use the curated driver's on-disk hash, since no state
   entry yet exists to short-circuit it; plan output shows one `create`
   action; exit code `0`.
3. **`plan apply --yes`** — a separate CLI invocation, which per
   `aiform/cli.py`'s `_cmd_plan_apply` re-runs `orchestrator.build_create_plan()`
   from scratch before applying — so gate #1 fires **again** here, a
   second time, since step 2 never wrote a resource entry to state (only
   an applied resource's `StateEntry` records a trusted driver hash) and
   there is still nothing to short-circuit it. Don't assert a combined
   call count of `1` across steps 2–3; assert `>= 1` in each step
   individually instead, and note in the test that the `code_review`
   record actually persisted into state.json is from this step's
   review, not step 2's (step 2's result is discarded — `plan create`
   never persists a driver trust record on its own, only a
   resource-creating `apply` does). Beyond that: executes a real
   `driver.create()` (see the note below on its actual DO-call
   footprint); assert the printed result includes an `id`; assert
   `.aiform/state.json` now has one resource entry carrying
   `driver.sha256` and a `code_review` record; assert
   `.aiform/state.json.backup` exists (written before
   the overwrite, per CLAUDE.md's state-handling rule). From this point
   on — every case after this one — the driver's hash is trust-cached
   against this resource's state entry, so gate #1 does not fire again
   for the rest of this sequence (see case 7's note).

   Note: `driver.create()` itself is no longer a single HTTP request —
   per `specs/digitalocean_compute.md`'s `create()` Behavior section, it
   now polls `GET /v2/droplets/{id}` until the droplet reaches
   `status: "active"` before returning, so this step also blocks for as
   long as that convergence takes (bounded by the driver's own poll
   timeout). Don't assert a DO-call count of exactly `1` for this step.
4. **Second `plan create`, file unchanged** — the concrete proof of the
   zero-Anthropic-call no-op guarantee (`PLAN.md` §9 step 4, CLAUDE.md's
   "must make zero Anthropic API calls" rule): run with `--verbose` and
   assert stderr contains exactly `"[verbose] 0 Anthropic API call(s) made"`;
   assert the plan reports `no-op`; assert exactly one DO `read()` call
   was made (refresh-before-diff still happens mechanically, per
   CLAUDE.md's "refresh before diff" rule — only the *LLM* call count is
   zero, not the DO call count). This case's fixture deliberately omits
   `ssh_keys` from `params` — see case 11 below for why that's not
   incidental, and for the separate, dedicated case that exercises the
   realistic (`ssh_keys`-configured) path where this guarantee is
   already known not to hold.
5. **`plan refresh`** — no `.aiform.md` parsing, no LLM calls at all.
   **Not verifiable via `--verbose`**: `refresh`/`show` are dispatched
   through `aiform/cli.py`'s `_PLAIN_PLAN_DISPATCH`, which never
   constructs a `_CountingClient` or calls `_report_verbose_calls` —
   there is no `"[verbose] ..."` line to assert for this command at all
   (unlike `create`/`apply`/`destroy`). The zero-LLM-call property here
   is structural (nothing in `refresh_state()`'s code path can reach
   `llm.py`), not something this test asserts via output; instead
   assert state's attributes match a direct `read()` against the live
   droplet.
6. **In-place update (size only)** — edit the fixture's `.aiform.md` to
   change only `size` to a different valid size; `plan create` shows an
   `update` action (not `create`/`destroy`); `plan apply --yes` drives
   `drivers/digitalocean/compute.py`'s resize path (power_off → resize
   `disk: false` → power_on) against the real API; assert the droplet's
   size actually changed (direct `GET`) and its `id` is unchanged
   (proves this was an in-place update, not a replace).
7. **Forced replace (region or image)** — edit `.aiform.md` to change
   `region` or `image` (both in `LIKELY_REPLACE_FIELDS`); `plan create`
   flags it as a likely-replace; `plan apply --yes` triggers gate #2
   (`review-orchestration-model`, since this is destructive) before
   executing; assert the old droplet is actually gone (direct `GET` →
   `404`) and a new `id` is recorded in state; assert `--verbose`'s
   Anthropic call count is `>= 1` for this run — the `intent-orchestration-model`'s
   diff categorization (correcting an earlier draft of this spec, which
   wrongly attributed that to "gate #1") plus gate #2's
   `review-orchestration-model` both fire here. Gate #1 does **not**
   fire in this case: the driver's hash is already trust-cached against
   this resource's state entry from case 3's apply (see case 3's note),
   so `ensure_driver_trusted()` short-circuits before any review call.
   Capture this case's new `id` — needed by case 9, after case 8 removes
   it from state.
8. **`plan destroy --yes`** — plans and applies destroy of the tracked
   resource; gate #2 fires unconditionally (`PLAN.md` §7: destroy is
   "100% subject to gate #2 by definition") — assert `--verbose`'s
   Anthropic call count is `>= 1` for this run specifically (this is the
   test's only direct evidence that gate #2 actually ran for a plain
   destroy, rather than merely inferring it from the droplet being
   gone, which a regression that silently skipped `review_plan()` for a
   destroy-only plan would still produce); assert the droplet is
   actually gone (direct `GET` → `404`); assert the resource's
   `.aiform.md` file has moved into `.aiform/trash/`; assert the entry is
   removed from `state.json`.
9. **Idempotent delete** — using the `id` captured in case 7 (state no
   longer has it after case 8's destroy removed the entry), directly
   instantiate `drivers.digitalocean.compute.Driver` and call
   `delete(id, credentials)` again against the now-gone `id`; assert it
   returns `None` and raises nothing (`404` treated as success, per
   `aiform/driver.py`'s own contract docstring — "the single most
   important behavior" `specs/digitalocean_compute.md` calls out for
   this driver).
10. **Bad-token failure path** — with `DIGITALOCEAN_TOKEN` overridden to
    an obviously invalid value for one isolated test function (its own
    `tmp_path`, not reusing case 1–9's tracked resource): `plan apply`
    fails with a clear, non-crashing error; assert no `state.json`
    entry was written for the failed resource and no droplet was
    created (nothing to tear down for this case — the cleanup fixture's
    `finally` clause should find an empty or absent `state.json` and
    no-op cleanly); assert the invalid token's literal value does not
    appear anywhere in captured stdout/stderr — `PLAN.md` §5 step 3
    requires a bad-credential failure to name "enough of the CSP's own
    error to diagnose it — never the credential's value itself," and
    this suite's own stdout/stderr can land in CI logs, so this is a
    real check, not a formality.
11. **`ssh_keys` configured: the zero-Anthropic-call no-op guarantee
    holds even here** — a separate, isolated test function (its own
    `tmp_path` and tracked droplet, not sharing cases 1–9's), whose
    fixture sets a real `ssh_keys` value in `params`. Create and apply
    it, then refresh and run a second `plan create` the same way case 4
    does. **This case originally existed to document the opposite** — a
    real, then-unfixed gap where `read()`'s inability to recover
    `ssh_keys` (write-only on DO's side) produced a non-empty diff, and
    a real `intent-orchestration-model` call, on every subsequent `plan`
    for any `ssh_keys`-configured resource. That gap is now closed
    (`drivers/digitalocean/compute.py`'s `NON_DIFFABLE_FIELDS`,
    `specs/digitalocean_compute.md`), so this case now asserts the same
    thing case 4 does: `[verbose] 0 Anthropic API call(s) made` and a
    `no-op` plan, this time with `ssh_keys` configured — proving the
    guarantee holds universally, not just for case 4's fixture (which
    happens not to set `ssh_keys`). Kept as its own case, not folded
    into case 4, specifically so a regression that reintroduces this gap
    (e.g. `NON_DIFFABLE_FIELDS` accidentally dropped, or a future field
    with the same write-only shape added without the same exclusion)
    starts failing here immediately, the same role this case always
    played — just checking the opposite condition now that the gap it
    watches for has actually been fixed.

## Orphan cleanup (leaked resources)

The Interface's teardown fixture is the primary defense, but it only
runs if the test *process* survives long enough to reach its `finally`
clause. A CI runner timeout/OOM kill, a `SIGKILL`, a crashed machine, or
a bug in the fixture itself all bypass it — each one leaves a real,
billable droplet running with nothing left in this suite to clean it
up. This section is the backstop for exactly that case.

### Design principle: the backstop must not depend on the code under test

The fixture's `finally` clause calls `aiform plan destroy`, which goes
through `orchestrator.py` and `drivers/digitalocean/compute.py`'s
`delete()` — the same code this suite exists to exercise. If *that*
code is what's broken (the actual failure mode a leak is often evidence
of), routing the backstop through it too means one bug disables both
layers at once. So the backstop is required to use a **separate
implementation, calling DigitalOcean's API directly** — no `import
aiform.orchestrator`, no `import drivers.digitalocean.compute`, no
`aiform plan destroy` subprocess. Deliberate code duplication of the
handful of lines needed to list/delete a droplet, traded for the
backstop actually being independent.

### Mechanism: a standalone sweep script

`scripts/sweep_system_test_droplets.py` — outside `tests/` entirely
(it's an ops tool, not something pytest should ever collect):

```python
def list_tagged_droplets(
    token: str, tag: str
) -> list[dict]: ...  # GET /v2/droplets?tag_name={tag}, urllib.request, no aiform import


def sweep(
    token: str,
    tag: str = "aiform-system-test",
    min_age_minutes: int = 60,
    dry_run: bool = False,
) -> list[dict]:
    ...  # filters list_tagged_droplets() to created_at older than the
    # threshold, DELETEs each match directly unless dry_run,
    # returns what was (or would be) removed


def main(argv: list[str] | None = None) -> int:
    ...  # CLI: --dry-run, --min-age-minutes, --tag; reads
    # DIGITALOCEAN_TOKEN from env the same way the driver does;
    # prints a summary; exits non-zero if anything was swept
```

- **`min_age_minutes=60` default**: this suite never legitimately runs
  more than a few minutes, so a threshold well above that means the
  sweep can never race a still-in-progress, healthy run — while still
  catching a real leak before it accrues much cost.
- **Runs on its own schedule**, independent of whatever triggers the
  system-test suite: a separate GitHub Actions workflow
  (`.github/workflows/sweep-system-test.yml`, `schedule` +
  `workflow_dispatch`, `DIGITALOCEAN_TOKEN` as a repo secret). This
  matters specifically because the scenario that produces a leak (the
  system-test workflow crashing, being disabled, or its own trigger
  breaking) is exactly the scenario where relying on that same workflow
  to also schedule cleanup would fail to fire.
- **A non-empty sweep is always treated as a bug report**, not routine
  maintenance: the workflow run fails loudly / posts a non-empty
  summary whenever it actually deletes something, since every hit means
  the primary teardown fixture (or the suite itself) failed to clean up
  after itself somewhere.
- The sweep script's own logic (age filtering, tag matching) gets
  ordinary mocked unit tests (`tests/test_sweep_system_test_droplets.py`,
  mocking `urllib.request.urlopen` the same way
  `tests/drivers/test_digitalocean_compute.py` does) — through the
  normal `PROCESS.md` spec-first loop as its own small pass, not part
  of this system-test suite's own spec.

### Tag reliance: what's guaranteed today vs. deferred

The sweep only reaches droplets that actually carry the
`aiform-system-test` tag, so the whole mechanism depends on that tag
reliably being present.

- **Works today, no aiform changes needed.** `PARAM_SCHEMA` already
  accepts an optional `tags` field, and `create()` echoes back whatever
  `params["tags"]` was, sent straight through to DO on `POST
  /v2/droplets` (`specs/digitalocean_compute.md`'s Behavior section).
  This suite's own `.aiform.md` fixture sets `tags: ["aiform-system-test"]`
  explicitly — since the suite owns and fully controls its own fixture,
  that's a real, sufficient guarantee for *this* suite specifically.
  This is why the sweep above can be designed and built now, not
  blocked on anything new.
- **Not a general guarantee — a real, separate gap.** Nothing in
  `aiform/driver.py`'s `ResourceDriver` contract or `orchestrator.py`
  forces *any* aiform-created resource to carry an identifying tag —
  `tags` is just one more optional key a user's own `.aiform.md` may or
  may not set (`additionalProperties: True`, no default, no orchestrator
  involvement). So this sweep, as designed, only ever reaches resources
  whose own config happened to request the tag. That's fine for this
  suite's fixture, but it is not a general safety net — it would not
  catch a leak from, say, a hand-run `aiform plan apply` against a
  `.aiform.md` that never set `tags`.
- **Project-wide automatic resource marking: `specs/resource_tagging.md`.**
  A guarantee that every resource any driver creates carries a fixed,
  aiform-owned marker tag (`aiform-managed`), transparently, regardless
  of what the user's file specifies — two concrete base-class helper
  methods a driver calls from its own `create()`/`read()`/`update()`
  that keep the marker invisible to the orchestrator's diff engine.
  This has real value beyond testing (e.g. answering "what has aiform
  ever created in this account," independent of a `state.json` that
  could itself be lost or corrupted). Note this spec is also,
  separately, a deliberately minimal first slice of `PLAN.md` §10's own
  pre-existing "Resource tagging convention" entry — that entry's
  long-term target is a fuller, structured tag format, which
  `specs/resource_tagging.md` explicitly reconciles with rather than
  silently duplicating (see that spec's Purpose section). Until it's
  implemented, this suite's own explicit fixture tag (above) is what
  the sweep relies on — sufficient to ship this spec's mechanism now,
  not a reason to block on the general feature.

## Edge cases / errors

- **Never treat a DO/Anthropic transient error as suite flakiness to
  retry away.** Neither `drivers/digitalocean/compute.py` nor `aiform/llm.py`
  builds in retry logic today (`PLAN.md`'s own openAPI-not-SDK
  rationale explicitly defers retry/failover decisions to the
  orchestrator, which doesn't implement them yet either) — so a real
  `5xx`/`429` mid-suite must surface as a real, visible test failure,
  not be silently retried by the test itself. If this happens
  repeatedly, it's a signal that `orchestrator.py`/the driver need
  retry handling, not that the test needs a retry loop bolted on.
- **DO fixture drift.** If DO deprecates the region/size/image the
  fixture requests (a `422` on `create()` unrelated to anything this
  suite is testing), that's a fixture-staleness issue to fix in
  `tests/system/conftest.py`'s fixture constants, not a driver or
  orchestrator bug — don't conflate the two when triaging a failure
  here.
- **Leaked droplets from a killed/crashed test process** — see "Orphan
  cleanup (leaked resources)" above for the full mechanism (an
  independent, non-aiform sweep script keyed on the
  `aiform-system-test` tag this suite's fixture sets).
- **Real dollar and token cost.** This suite creates a real (if
  minimal) billable droplet and makes real `code-review-model`/
  `review-orchestration-model`/`intent-orchestration-model` calls at
  Opus/Sonnet pricing (`PLAN.md` §10's pricing note). It must not run on
  every PR the way the rest of `tests/` does via `tests.yml` — run it on
  a schedule (e.g. nightly) or `workflow_dispatch` in a separate,
  opt-in CI workflow with `DIGITALOCEAN_TOKEN`/`ANTHROPIC_API_KEY`
  configured as repo secrets, never on the default `pull_request`/`push`
  triggers.
- **Ordering dependency.** Because cases 2–9 share one droplet, a
  failure partway through (e.g. case 6's resize) leaves later cases
  unable to run meaningfully — the suite should report this as one
  ordered scenario failing at a named step, not N independent failures,
  so triage isn't misled into thinking unrelated cases regressed.

## Out of scope

- **Implementing `specs/resource_tagging.md`'s mechanism** — the
  project-wide "every aiform-created resource carries an aiform-owned
  marker tag" guarantee named in "Orphan cleanup (leaked resources)"
  above and specified in full (including its relationship to `PLAN.md`
  §10's pre-existing tagging-convention entry) in that spec. Not
  implemented as part of this spec: this suite's own sweep only needs
  the tag its own fixture already sets, which works without it.
- **The sweep script's own test suite**
  (`tests/test_sweep_system_test_droplets.py`) and the scheduled GH
  Actions workflow that runs it — both named in "Orphan cleanup" above,
  built as their own small `PROCESS.md` pass, not part of implementing
  this spec's `tests/system/test_cli_digitalocean.py`.
- **The generated-driver variant of this same system test** (`PLAN.md`
  §6: run only after gate #1 approves a freshly generated driver, as
  part of that driver's own generated test suite) — not designed here;
  blocked on mechanism 2 (`aiform driver create`) being built at all
  (`PLAN.md` §10, "Self-service driver creation is not implemented").
- **A second provider or resource kind.** MVP is `digitalocean`/`compute`
  only (`PLAN.md` §10, "Only one resource kind is implemented") — this
  suite tests exactly that one `(provider, resource)` pair.
- **Concurrency/locking behavior.** `PLAN.md` §10 already names "single
  local state file, no locking, no multi-user story" as a known,
  deferred gap — this suite runs one sequential scenario, never two
  concurrent `apply`s against the same state.
- **`aiform driver create`/`refresh`/`publish`** — not implemented yet
  (`PLAN.md` §7 marks these "not yet implemented"); nothing here
  exercises them.
- **Observability/status-URL, structured logging** — both named in
  `PLAN.md` §10 as "planned, not yet designed"; this suite only asserts
  on today's stdout/stderr/exit-code/`--verbose` surface.

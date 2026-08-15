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

Once mechanism 2 (on-the-fly driver generation, `PLAN.md`'s "Driver
curation") is wired into `plan`/`apply`, the "system test" §6 describes
for a freshly generated driver is a variant of this same suite pointed
at the generated driver instead of the curated one — not a separate
concept. That variant is not designed here; see Out of scope.

## Interface

- **Location**: `tests/system/test_cli_digitalocean.py`, in a new
  `tests/system/` directory — mirrors the existing `tests/drivers/`
  grouping, and leaves room for a second provider's system test later
  without cluttering `tests/`'s root.
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
  across parallel runs.
- **Driving the CLI**: calls `aiform.cli.main([...])` in-process (same
  entry point `tests/test_cli.py` already uses), not a subprocess — so
  stdout/stderr capture and exit codes are asserted the same way the
  existing CLI tests do, and `--verbose`'s call-count line
  (`aiform/cli.py:197`) is directly assertable.
- **Cost/cleanup fixture**: a function-scoped fixture that yields
  control to the test body inside a `try`/`finally`, and in the
  `finally` clause runs `aiform plan destroy --yes` (or, if the test
  failed before any resource was ever created, a no-op) against
  whatever `state.json` exists in `tmp_path` at that point — so a
  droplet is torn down even when an assertion mid-test raises. This is
  the single most important property of this suite: every entry point
  leaves zero running droplets behind, regardless of pass/fail.
- **Fixture params bound cost**: the `.aiform.md` fixture used
  throughout requests DigitalOcean's cheapest available droplet size
  (`s-1vcpu-512mb-10gb` or equivalent at time of writing — verify
  against DO's current size list if this 404s) and a region/image pair
  known cheap and available; a droplet under test never runs longer
  than one test's duration.

## Behavior

Each bullet is one test case, run in the order below within a single
test (or a small ordered chain of tests sharing one `tmp_path` and one
tracked droplet) — this suite is deliberately sequential, not
independent per-case, because each step depends on state the previous
one created:

1. **`aiform init`** — scaffolds `.aiform/`, `.gitignore` entries, and
   `examples/compute.aiform.md`; with both real env vars set, prints
   `✓` for both credential checks. (Note: `_cmd_init`'s check is
   presence/resolvability only, per `aiform/cli.py:240-247` — it does
   not itself make a live API call. This suite's later steps are what
   first exercise the token for real; if DO ever rejects the token,
   that surfaces at step 2, not step 1 — don't expect `init` to catch a
   bad token.)
2. **First `plan create`** (fresh project, no `state.json` yet) — per
   `PLAN.md` §9 step 2: gate #1 (`code-review-model`) fires exactly
   once to trust-on-first-use the curated driver's on-disk hash; plan
   output shows one `create` action; exit code `0`.
3. **`plan apply --yes`** — executes a real `driver.create()` (one DO
   API call per `specs/digitalocean_compute.md`'s "exactly one API
   call"); assert the printed result includes an `id`; assert
   `.aiform/state.json` now has one resource entry carrying `driver.sha256`
   and a `code_review` record; assert `.aiform/state.json.backup` exists
   (written before the overwrite, per CLAUDE.md's state-handling rule).
4. **Second `plan create`, file unchanged** — the concrete proof of the
   zero-Anthropic-call no-op guarantee (`PLAN.md` §9 step 4, CLAUDE.md's
   "must make zero Anthropic API calls" rule): run with `--verbose` and
   assert stderr contains exactly `"[verbose] 0 Anthropic API call(s) made"`;
   assert the plan reports `no-op`; assert exactly one DO `read()` call
   was made (refresh-before-diff still happens mechanically, per
   CLAUDE.md's "refresh before diff" rule — only the *LLM* call count is
   zero, not the DO call count).
5. **`plan refresh`** — no `.aiform.md` parsing, no LLM calls at all
   (assert `--verbose` shows `0` again); state's attributes match a
   direct `read()` against the live droplet.
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
   Anthropic call count is `>= 1` for this run (both gate #1's
   diff-categorization and gate #2 fire here).
8. **`plan destroy --yes`** — plans and applies destroy of the tracked
   resource; gate #2 fires unconditionally (`PLAN.md` §7: destroy is
   "100% subject to gate #2 by definition"); assert the droplet is
   actually gone (direct `GET` → `404`); assert the resource's
   `.aiform.md` file has moved into `.aiform/trash/`; assert the entry is
   removed from `state.json`.
9. **Idempotent delete** — with the resource already destroyed in step
   8, directly instantiate `drivers.digitalocean.compute.Driver` and call
   `delete(id, credentials)` again against the now-gone `id`; assert it
   returns `None` and raises nothing (`404` treated as success, per
   `aiform/driver.py`'s own contract docstring — "the single most
   important behavior" `specs/digitalocean_compute.md` calls out for
   this driver).
10. **Bad-token failure path** — with `DIGITALOCEAN_TOKEN` overridden to
    an obviously invalid value for one isolated sub-test (a fresh
    `tmp_path`, not reusing the tracked resource above): `plan apply`
    fails with a clear, non-crashing error; assert no `state.json`
    entry was written for the failed resource and no droplet was
    created (nothing to tear down for this case — the cleanup fixture's
    `finally` clause should find an empty or absent `state.json` and
    no-op cleanly).

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
- **Resource tagging for orphan recovery.** Every droplet this suite
  creates gets a recognizable tag or name prefix (e.g.
  `aiform-system-test`) distinct from anything a real user's droplets
  would use, so that if the teardown fixture itself never runs (process
  killed, machine crash mid-suite), a human or a separate scheduled
  sweep job can find and remove orphaned droplets by that tag — the
  in-test `finally` clause is the primary cleanup mechanism, this is
  the backstop for when it doesn't fire.
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

- **The generated-driver variant of this same system test** (`PLAN.md`
  §6: run only after gate #1 approves a freshly generated driver, as
  part of that driver's own generated test suite) — not designed here;
  blocked on mechanism 2 being wired into `plan`/`apply` at all
  (`PLAN.md` §10, "Self-service driver creation is not implemented").
  When that lands, it likely reuses this suite's fixtures/teardown
  pattern against a generated driver module instead of the curated one,
  rather than inventing a second mechanism.
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

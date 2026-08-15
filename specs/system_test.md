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
  the primary cleanup path, but not the only one — see "Orphan cleanup
  (leaked resources)" below for what covers the case where even this
  fixture doesn't get to run.
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
def list_tagged_droplets(token: str, tag: str) -> list[dict]:
    ...  # GET /v2/droplets?tag_name={tag}, urllib.request, no aiform import

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
- **Proposed follow-up subtask (not designed here): project-wide
  automatic resource marking.** A guarantee that every resource any
  driver creates carries a fixed, aiform-owned marker tag (e.g.
  `aiform-managed`) transparently, regardless of what the user's file
  specifies — most naturally as `orchestrator.py` merging that marker
  into `params` before calling `driver.create()`, so it's one
  provider-agnostic seam rather than something every driver has to
  remember to do itself. This has real value beyond testing (e.g.
  answering "what has aiform ever created in this account," independent
  of a `state.json` that could itself be lost or corrupted), which is
  why it's proposed as its own spec (`specs/resource_tagging.md`, not
  written in this PR) rather than folded into this one. Open questions
  that spec would need to resolve: orchestrator-level vs. per-driver
  placement; how it composes with a future driver whose CSP doesn't
  support tagging on a given resource kind at all; and whether the
  marker should carry more than "aiform made this" (e.g. which
  project/state file, for multi-project cleanup). Until that lands,
  this suite's own explicit fixture tag (above) is what the sweep
  relies on — sufficient to ship this spec's mechanism now, not a
  reason to block on the general feature.

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

- **`specs/resource_tagging.md`** — the project-wide "every aiform-created
  resource carries an aiform-owned marker tag" guarantee proposed in
  "Orphan cleanup (leaked resources)" above. Named here as a concrete
  follow-up subtask, not designed in this spec: this suite's own sweep
  only needs the tag its own fixture already sets, which works without
  it.
- **The sweep script's own test suite**
  (`tests/test_sweep_system_test_droplets.py`) and the scheduled GH
  Actions workflow that runs it — both named in "Orphan cleanup" above,
  built as their own small `PROCESS.md` pass, not part of implementing
  this spec's `tests/system/test_cli_digitalocean.py`.
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

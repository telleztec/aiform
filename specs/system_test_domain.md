# specs/system_test_domain.md — `tests/system/test_cli_domain.py`

## Purpose

The domain-driver analogue of `specs/system_test.md`. That spec covers
`drivers/digitalocean/compute.py` end to end against the real API; this
one does the same for `drivers/digitalocean/domain.py`, whose other tests
are all mocked.

It exists because `specs/digitalocean_domain.md` named the gap for
itself: a live system test was listed under "Out of scope" as a
follow-up, on the grounds that it was the only way to settle that spec's
"recalled, not verified" items — the trailing-dot round trip and
`TXT` quoting, which mocked tests can only assert against an assumption
they share with the driver — and that the per-type required-field table
was "the one remaining item for the live system test." Those entries have
since been updated to point here.

This suite is that follow-up. Its point is **not** to re-prove the CLI,
orchestrator, gates or state machinery — `specs/system_test.md`'s suite
already does that, and duplicating it here would double the cost for no
new information. What this suite exists for is the set of assumptions
about DigitalOcean's DNS API that `domain.py` hardcodes and **a mock
cannot falsify**, because the mock encodes the same assumption:

- the dotted-write / dotless-read asymmetry (`_to_wire_record`);
- `TXT` `data` stored verbatim, quoted or unquoted;
- which fields each record type actually requires (`_TYPE_EXTRA_FIELDS`);
- whether `read()` round-trips to a stable zero-diff — i.e. whether
  `UNORDERED_FIELDS = ["records"]` holds against DO's real ordering;
- whether `create()`'s rollback really leaves no orphan zone behind, and
  whether a name-already-taken 422 really *doesn't* trigger it.

## Knowledge-confidence

Following `specs/digitalocean_compute.md`'s and
`specs/digitalocean_domain.md`'s convention of separating verified from
recalled facts.

**Verified live** by a disposable-zone probe before this spec was
written (one zone, 4 calls, no cost, deleted in a `finally` — the same
pattern `specs/digitalocean_domain.md` used for its own authoring):

- **A zone whose name is a subdomain of an existing zone on the same
  account is accepted** — `POST /v2/domains` with
  `systest-probe-….telleztec.com` returned 201 while `telleztec.com`
  was already hosted on that account. Zones are independent objects, not
  a hierarchy; the parent's own records are untouched. This is what makes
  this suite's naming scheme viable at all.
- **DigitalOcean lowercases the stored zone name.** A requested
  `systest-probe-20260904T000759Z-718b80.telleztec.com` came back as
  `systest-probe-20260904t000759z-718b80.telleztec.com`. See "Zone naming"
  below — this is load-bearing for the sweep, not a cosmetic detail.
- **Zone lookup is case-insensitive.** `GET /v2/domains/{original mixed
  case}` returned 200 and reported the folded name. So `read(id)` with
  the user's own mixed-case `name:` still resolves, and since `read()`
  returns the `id` it was passed (`domain.py`) rather than the API's
  spelling, no phantom `id` diff arises.
- **A fresh zone's auto-created records** are exactly what
  `_filter_managed()` assumes: one `SOA` at `@` whose `data` is the zone
  TTL (`'1800'`, not a nameserver string), and three `NS` records at `@`
  for `ns1`/`ns2`/`ns3.digitalocean.com`, **dotless**.
- **The account's API budget** is 5,000 requests/hour
  (`ratelimit-limit: 5000`), and a full run of this suite costs roughly
  **80 DO calls** — measured, not estimated: two consecutive full runs
  plus the probes above consumed 174 of the hour's budget. Under 2% per
  run, so the ceiling is not a design constraint; see Edge cases on why a
  429 must still fail loudly regardless.
- **Wall-clock**: a full run is about **7½ minutes**, nearly all of it
  Anthropic latency rather than DO's.
- **The token now carries `domain` scope.** `GET /v2/domains` returned
  200, not the 403 `specs/digitalocean_domain.md` recorded during the
  driver's authoring. That prerequisite is cleared, but the suite still
  probes rather than assumes it — see case 1.

**Settled by the suite's own first green run** (case 6), which is what it
was built to do — these were `specs/digitalocean_domain.md`'s last
"recalled, not verified" items:

- **The per-type required-field table is correct.** One record of each of
  `A`, `AAAA`, `CAA`, `CNAME`, `MX`, `NS`, `SRV`, `TXT`, carrying exactly
  the fields `_TYPE_EXTRA_FIELDS` claims each needs and nothing more, was
  accepted by DigitalOcean and converged to a stable `no-op`. No field the
  table calls required is in fact optional in a way that breaks a write,
  and none it omits is in fact demanded.
- **`TXT` `data` is stored verbatim, quotes included** — a value whose own
  text is `"v=spf1 include:_spf.example.com -all"`, embedded quotes and
  all, read back byte-identical.
- **The dotless canonical form round-trips.** `CNAME`/`MX`/`NS`/`SRV`/`CAA`
  targets written without a trailing dot came back without one, so
  `_to_wire_record()`'s wire-boundary dot is both necessary and
  sufficient.
- **`AAAA` addresses are not renormalized.** A compact `2001:db8::1` was
  returned in the same form rather than expanded — worth stating, because
  an expanding API would have produced a permanent phantom diff and the
  driver has no `AAAA` normalization to absorb it.
- **`CAA` `issue` and `issuewild` coexist** at one name with identical
  `data` (case 6). Note the narrower claim: no case *edits* a CAA record,
  so "reconciliation touches neither when the other changes" is not
  directly verified. What case 7c does verify is adjacent and cheaper —
  every record's DO id survives an edit to a different record, the CAA
  pair included.

**Recalled, not verified**: nothing this spec now depends on.

## Interface

Mirrors `specs/system_test.md`'s decisions rather than reinventing them.
Reused unchanged from `tests/system/conftest.py`: the `@pytest.mark.system`
marker (already registered in `pyproject.toml` and already excluded via
`addopts = "-m 'not system'"`), the `tests/system/` location, in-process
`cli.main([...])` invocation, the `project_dir` fixture
(`tmp_path` + `monkeypatch.chdir`), the session-scoped
`_require_live_credentials` skip, the three-layer credential-redaction
machinery (`RedactedSecret` / `_redact_resolved_credentials` /
`pytest_runtest_makereport`), `live_token()`, `unique_name()`, and
`teardown_tracked_resources`.

Two helpers **move** into `tests/system/conftest.py` from
`test_cli_digitalocean.py` rather than being duplicated, and are renamed
to drop the now-misleading module-private underscore: `_assert_ok` →
`assert_cli_ok`, `_count_driver_reads` → `count_driver_reads`. Both carry
load-bearing rationale — why a bare `assert code == 0` loses the only
diagnostic, and why wrapping the statically imported `Driver` class does
not work — and a second copy of either would drift from it.

**No new marker, no new runner** —
`scripts/run_system_tests.py` already runs `pytest -m system tests/system/`,
which picks this file up with no change.

What is new:

- **Location**: `tests/system/test_cli_domain.py`. Same
  naming-note caveat as `specs/system_test.md`: this spec's filename
  doesn't mechanically flatten a module path, because it covers a
  cross-cutting suite, not one implementation module.
- **Zone naming** — `unique_zone_name(label)` in
  `tests/system/conftest.py`:

  ```python
  SYSTEM_TEST_ZONE_PREFIX = "systest-"
  SYSTEM_TEST_ZONE_PARENT = "telleztec.com"


  def unique_zone_name(label: str) -> str:
      stem = unique_name(SYSTEM_TEST_ZONE_PREFIX.rstrip("-"))
      return f"{stem}-{label}.{SYSTEM_TEST_ZONE_PARENT}".lower()
  ```

  yielding e.g. `systest-20260904t000759z-718b80-lifecycle.telleztec.com`.
  Three properties, each load-bearing:

  - **Lowercased at generation.** DO folds the case of a stored zone name
    (verified above), and `unique_name()` embeds `%Y%m%dT%H%M%SZ` with an
    uppercase `T`/`Z`. Lowercasing here makes *requested == stored*, so
    any direct comparison between a name this suite generated and the one
    DO reports back is exact, and nothing downstream has to remember to
    casefold.

    Be precise about what this does **not** rescue, since an earlier draft
    of this spec claimed more: the sweep's age filter would have been fine
    either way. `datetime.strptime` matches format literals
    case-insensitively, so `%Y%m%dT%H%M%SZ` parses `...t000759z` without
    complaint — verified against the interpreter after review flagged the
    claim. The lowercasing is hygiene that removes a class of
    requested-vs-stored mismatch, not a fix for a live `ValueError`.
  - **A fixed, literal `systest-` prefix**, so the timestamp always
    starts at a known offset and the sweep's match is an exact prefix
    test, never a glob.
  - **A subdomain of a zone the account already owns.** Chosen over an
    unowned name: DigitalOcean does not verify domain ownership, so both
    work and both cost nothing, but this keeps every name the suite
    creates inside a namespace the operator actually controls. The
    tradeoff, taken deliberately, is that `telleztec.com` appears in the
    same `GET /v2/domains` listing the sweep reads — see "Orphan cleanup".
- **`write_domain_aiform_md()`** — the `resource: domain` parallel to
  `write_aiform_md()` that `specs/digitalocean_domain.md` predicted would
  be needed. Signature:

  ```python
  def write_domain_aiform_md(
      project_dir: Path,
      *,
      name: str,
      records: list[dict],
      filename: str = "domain.aiform.md",
  ) -> Path: ...
  ```

  Serializes `records` to the `params.records` YAML block. `records` is
  passed through verbatim, in the caller's order and with the caller's
  exact field set — a helper that normalized or reordered would defeat
  cases 6 and 7d, which exist precisely to observe what the driver does
  with a given spelling and ordering.
- **Direct-API helpers** (no `aiform` import, per the "backstop must not
  depend on the code under test" principle): `get_domain_or_none()`,
  `list_domain_records()`, `list_domains()` (paginated —
  `GET /v2/domains` defaults to `per_page=20`), `create_domain_directly()`,
  `delete_domain_directly()`, and `wait_until_domain_gone()`.
  `wait_until_domain_gone()` mirrors `wait_until_droplet_gone()`'s
  contract exactly — including *returning* the still-live object rather
  than raising, so a caller's assertion never puts the live token into
  pytest's assertion-introspection output — even though DO's zone delete
  appears synchronous. A suite that races a provider's convergence fails
  a destroy that in fact worked, and the poll costs nothing on the happy
  path.
- **Cost**: no billable resource at all. DO bills nothing for DNS zones,
  so unlike the droplet suite the only real cost is Anthropic tokens.
  That is what makes it affordable to assert convergence (an unchanged
  re-plan reporting `no-op`) after *every* mutation rather than once.

## Behavior

### Sequence A — full lifecycle

Cases 1–9 are one ordered sequence in a **single test function**, sharing
one `tmp_path` and one tracked zone, for the same reason
`specs/system_test.md`'s cases 1–9 are: each step depends on state the
previous one created, and splitting them across test functions would give
each its own teardown instance and destroy the zone after the first.

1. **Domain-scope preflight, then `aiform init`.** Probe
   `GET /v2/domains?per_page=1` directly; on **401 or 403**, `pytest.skip`
   naming the missing `domain` scope — the same clean-skip shape
   `test_ssh_keys_configured_no_op_guarantee_holds` uses for a keyless
   account. Then `init`, asserting `[✓]` for both credentials.

   The probe is **not** redundant with that `[✓]`. `aiform/cli.py`'s
   `_check_droplet_scope` probes `GET /v2/droplets` only, so a
   droplet-scoped token earns a green check and then fails at the first
   domain `apply` — `specs/digitalocean_domain.md` calls this out
   explicitly. The assertion on the DO line must also not expect it to end
   after the variable name: it carries the account email, or
   `authenticated (scoped token)`.

   Treating **401** as a skip alongside 403 is a deliberate widening with
   a real cost: an outright invalid token skips three of this suite's four
   tests with a message blaming scope, where the compute suite would fail
   loudly at `init`. Accepted because the alternative — skipping on 403
   only — turns a token whose scoping DigitalOcean happens to report as
   401 into a confusing hard failure, and case 10 already covers the
   invalid-token path deliberately. The lifecycle test asserts `init`'s
   `[✓]` immediately after, so a wholly dead token still surfaces there.
2. **First `plan create`** — gate #1 (`code-review-model`) fires for
   `domain.py`'s own sha256, since no state entry trusts it yet. Assert
   `+ digitalocean.domain.<zone>: create` and a `--verbose` Anthropic call
   count `>= 1`. As in the compute suite, do not assert a combined count
   across this case and the next: `plan create` persists no driver-trust
   record, so gate #1 fires again in case 3.
3. **`plan apply --yes`** — creates the real zone and its records. Assert
   the state entry carries `id` / `driver.sha256` / `driver.code_review`,
   and that `.aiform/state.json.backup` exists (CLAUDE.md's
   state-handling rule). Then the two live checks this case exists for:
   - **The dot asymmetry.** The `_FQDN_TYPES` records in this case's
     fixture (`CNAME` and `MX`; `NS`, `SRV` and `CAA` arrive in case 6)
     are written **dotless** in the `.aiform.md`. Assert the raw
     `GET /v2/domains/{zone}/records` payload returns them dotless too,
     and that state's `attributes["records"]` matches what was written,
     field for field. This is `_to_wire_record()`'s entire justification,
     and the assumption a mock necessarily shares with the driver.
   - **SOA and apex NS are filtered.** Assert the raw payload *does*
     contain an `SOA` and at least one apex `NS` — the fixture asserts
     presence, not the specific `ns{1,2,3}.digitalocean.com` triple, so
     it does not break if DigitalOcean changes its nameserver set — and
     that state's `records` contains neither, per `_filter_managed()`.
4. **Second `plan create`, file unchanged** — `[verbose] 0 Anthropic API
   call(s) made`, `= <key>: no-op`, and exactly one driver `read()`
   (`count_driver_reads()`, which wraps the instance
   `orchestrator.load_driver()` returns rather than the statically
   imported class).

   **This is the single highest-value assertion in the suite.** It is the
   zero-diff invariant measured against reality, and it folds in `TXT`
   verbatim storage, the dot round-trip, DO's own record ordering versus
   `UNORDERED_FIELDS`, TTL rectification and `CAA` `tag` handling all at
   once. A mock cannot make this assertion mean anything, because the mock
   returns whatever the driver's author believed DO returns.
5. **`plan refresh`** — assert state's attributes match a direct live
   read. Not verifiable through `--verbose`: `refresh`/`show` dispatch
   through `_PLAIN_PLAN_DISPATCH`, which never builds a `_CountingClient`,
   so there is no `[verbose]` line to assert for this command. The
   zero-LLM-call property here is structural, exactly as in
   `specs/system_test.md`'s case 5.
6. **One record of every supported type at once.** Rewrite the file with
   one each of `A`, `AAAA`, `CAA`, `CNAME`, `MX`, `NS`, `SRV`, `TXT`,
   carrying exactly the fields `_TYPE_EXTRA_FIELDS` claims each type
   requires; apply; then re-plan and assert `no-op`.

   **This is the case that settles the spec's one openly unverified
   item.** A field the table calls required but DO rejects, or one DO
   requires that the table omits, surfaces here as a 422. Deliberately
   includes three shapes chosen for what they can break:
   - a `TXT` whose `data` contains embedded quotes — settles "stored
     verbatim, quoted or unquoted";
   - `CAA` `issue` **and** `issuewild` at the same name with **identical
     `data`**, differing only in `tag` — the exact pair that broke
     `_reconcile_set_path` before it paired on the whole projected record
     (`domain.py`'s own comment records the regression);
   - a delegated-subdomain `NS` (`name != "@"`) pointing at a DO
     nameserver, which `_is_do_managed_ns` must **not** filter out, since
     only the apex is DO-managed.
7. **In-place edits.** Each is followed by an unchanged re-plan asserting
   `no-op`, so every case proves convergence rather than just the
   immediate effect — affordable here only because zones are free.
   - **7a, single-valued PUT.** Change the apex `A` record's `data`.
     Assert via a direct records `GET` that the record's **DO record id is
     unchanged** and its data changed. The surviving id is what proves
     `_reconcile_single_valued` issued a `PUT` rather than a
     delete/create pair; asserting only the new value would pass either way.
   - **7b, ttl-only on a set-path type.** Change only one `MX` record's
     `ttl`. Same assertion, for the `_key_without_ttl` pairing branch.
   - **7c, add and remove.** Add a second `MX`, drop one `TXT`. Snapshot
     **every** managed record's DO id before the apply and assert each one
     except the removed `TXT` is unchanged after — the POST and DELETE
     touched only what changed. Checking just the edited record's sibling
     would let a regression that delete/recreated, say, the `CAA` pair on
     every update pass 7a–7c and the convergence re-plans alike, since
     each of those only looks at the record it edited.

     This case proves nothing about DO's silent TTL rectification, and
     should not claim to: both `MX` records are written at the same `ttl`
     (as `_validate_ttl_consistency` requires), so there is nothing to
     rectify and such an assertion could not fail for that reason.
   - **7d, pure reorder.** Shuffle the `records` list with no semantic
     change.

     **The load-bearing assertion is deterministic, not the plan output.**
     Reordering rewrites the file, so `planner.py`'s short-circuit is
     skipped and `categorize_diff()` runs against an *empty* diff — which
     makes a `no-op` line whatever the `intent-orchestration-model`
     answered. It could report `no-op` with `UNORDERED_FIELDS` removed
     (two semantically identical lists) and could spuriously report
     `update` with it present. This spec's own rule for cases 11 and 12 —
     an assertion must not be able to pass or fail on a model's wording —
     applies here too, and applied to the one case that is supposed to
     prove `UNORDERED_FIELDS` end to end.

     So assert against live `read()` output directly:
     `diff_attributes(live, desired, unordered_fields=UNORDERED_FIELDS)`
     is empty, **and** `diff_attributes(live, desired)` without it is
     not. The second half is what shows `UNORDERED_FIELDS` is doing the
     work rather than the two orderings happening to coincide. The
     `plan create` `no-op` assertion stays as the end-to-end half.

     It must **not** assert a zero Anthropic call count. `planner.py`'s
     short-circuit fires only when the diff is empty **and** the
     `.aiform.md` sha256 is unchanged:

     ```python
     if not diff and state_aiform_md_sha256 == current_aiform_md_sha256 and not drifted_missing:
     ```

     **`UNORDERED_FIELDS` buys a no-op plan, not a free one**; the
     zero-call guarantee is keyed on the file, not on the diff, and cases
     4 and 7's convergence re-plans are where it is asserted. An earlier
     draft of this case asserted 0 calls here and failed against a plan
     that was, correctly, already a no-op.
   - **7e, `records: []`.** Empty the list, apply, re-plan → `no-op`. The
     driver spec calls this "a stable no-op state, not a perpetual diff";
     only a live `read()`, which must return `[]` after filtering the
     SOA/NS records DO keeps, can confirm it.
   - Across every *applying* step of 7 (7a–7c, 7e), assert
     `(likely replace)` appears **nowhere**. 7d applies nothing — it
     asserts a `no-op`, which already excludes a replace.
     `LIKELY_REPLACE_FIELDS` is empty for this driver, and this is the
     domain analogue of the compute suite's issue-#77 guard: no record
     edit may ever propose tearing the zone down. As there, `--yes` cannot
     mask a regression — `apply_plan()`'s mid-apply `Replace …?`
     confirmation is not skippable by it, so a driver that forced a
     replace fails the apply outright on a non-TTY rather than quietly
     recreating the zone.
8. **`plan destroy --yes`** — gate #2 fires unconditionally (`PLAN.md` §7:
   destroy is "100% subject to gate #2 by definition"). Assert a
   `--verbose` Anthropic call count `>= 1` **for this invocation
   specifically** — that is the only direct evidence gate #2 ran, since a
   regression that silently skipped `review_plan()` for a destroy-only
   plan would still leave the zone gone. Then assert the zone 404s, the
   `.aiform.md` moved into `.aiform/trash/`, and the entry is gone from
   `state.json`.
9. **Idempotent delete** — instantiate `drivers.digitalocean.domain.Driver`
   directly and call `delete(<zone>, credentials)` against the now-gone
   zone; assert it returns `None` and raises nothing. 404-as-success is
   `aiform/driver.py`'s own contract requirement.

### Independent test functions

10. **Bad token.** Same shape as `specs/system_test.md`'s case 10: a bogus
    `DIGITALOCEAN_TOKEN`, `plan apply` exits 2 with an `Error:` line, no
    state entry is written, and the literal token value appears in
    neither stdout nor stderr. That last assertion is real rather than a
    formality — `PLAN.md` §5 step 3 requires a bad-credential failure to
    name enough of the CSP's error to diagnose it and never the
    credential's value, and this suite's output lands in
    `.aiform/testlog/`.
11. **A name already taken is neither adopted nor rolled back.** Create a
    zone *directly* via `urllib`, then call `Driver().create()` against
    that same name. Assert it raises `HTTPError` with status **422**, and
    that the pre-existing zone is **still live afterwards**. `create()`'s
    rollback must not fire on a `POST /v2/domains` that failed because the
    name was taken — otherwise aiform deletes a zone it did not create.
    The driver spec states this as a hard requirement ("never triggers
    rollback … rather than having aiform adopt — or delete — a zone it did
    not create"); it is a destructive-behavior claim, and no mock can
    settle it. A CLI-level `plan apply` runs alongside for the
    exits-cleanly and tracks-nothing assertions, making no claim about
    where it failed. Cleanup deletes the zone directly, not through aiform.
12. **Rollback leaves no orphan zone.** Provoke a real record-level 422
    *after* the zone is created, by calling `Driver().create()` with an
    `A` record whose `data` is not an IP address — a `str`, so
    `_validate_record` passes it, and DO rejects it. Assert the
    `HTTPError` is a **422** and that the zone then **404s**: the rollback
    in `create()` actually ran, leaving nothing live and untracked. This
    is the one case whose failure mode *is* a leaked zone, so it also
    carries its own direct-API `finally` sweep on top of the assertion.

    Both cases drive the driver rather than the CLI deliberately — see
    Edge cases, "A failed apply is not evidence the path under test ran".

## Orphan cleanup (leaked resources)

`specs/system_test.md`'s design keys its sweep on the `aiform-system-test`
tag. **That does not transfer**: DigitalOcean has no tagging API for
domains, which `specs/digitalocean_domain.md` already records as
impossible rather than merely unimplemented. The zone *name* is the only
handle, which is why the naming scheme above is part of the safety
mechanism and not just cosmetics.

Two layers, same division of labour as the droplet suite:

- **Primary**: `teardown_tracked_resources`, reused unchanged. It runs
  `plan destroy --yes` against whatever `state.json` exists and is
  resource-kind agnostic, so it already covers the tracked zone. Cases 11
  and 12 deliberately create zones *outside* aiform's state, so each also
  carries its own direct-API `finally`.
- **Backstop**: a session-scoped `autouse` fixture whose teardown lists
  `GET /v2/domains` directly — paginated, since that endpoint defaults to
  `per_page=20` and this account hosts real zones too — and deletes a zone
  only when **all three** hold:
  1. its name starts with the literal `systest-` prefix, **and**
  2. its name ends with `.telleztec.com`, **and**
  3. the `%Y%m%dt%H%M%Sz` timestamp parsed out of the name is at least
     `SWEEP_MIN_AGE_MINUTES` (60) old.

  Condition 3 is an **age threshold, not "older than this session
  started"**. The latter looks equivalent and is not: it would delete the
  live zones of a *concurrent* run that began a minute earlier, which is
  precisely the race the threshold exists to prevent. Same value and same
  reasoning as `specs/system_test.md`'s droplet sweep — a run never
  legitimately lasts more than a few minutes, so an hour cannot overlap a
  healthy one.

  Conditions 1 and 2 are independent guards, and `telleztec.com` itself
  fails the first — so the production zone is excluded twice over, not
  once. A zone whose name doesn't parse is **skipped, never deleted**:
  the failure mode of a name this suite doesn't recognize must be a leak
  someone notices, not a deletion of something it didn't create. Per
  `specs/system_test.md`'s "the backstop must not depend on the code under
  test", this imports no `aiform` module and issues raw `urllib` calls.

  The fixture is `autouse` across all of `tests/system/`, so it also runs
  on a **compute-only** session, where the token may legitimately carry no
  `domain` scope. Listing therefore warns rather than raises on any
  failure — a 403 there must not turn a green droplet run into a
  teardown error, since this is a best-effort backstop for a leak that
  has already happened and must never be the thing that fails an
  otherwise-passing run.

  **`zone_created_at()` is unit-tested in the default `pytest` run**
  (`tests/test_system_conftest.py`), not only under `-m system`. It is
  the function that decides what gets deleted from a live account holding
  a production zone, so leaving its only coverage behind live credentials
  would mean the one piece of code that can destroy production DNS is
  exercised solely by the suite it exists to clean up after. Same
  reasoning as `specs/conftest.md`'s extraction of
  `find_leaked_credential()` as a separately-testable pure matcher. The
  cases pin both guards independently — removing either one turns the
  suite red — and assert that whatever `unique_zone_name()` emits,
  `zone_created_at()` can claim back, since drift between those two
  silently strands every leaked zone. `write_domain_aiform_md()` is
  covered there too, round-tripping through a real `yaml.safe_load` —
  including the quote-carrying `TXT` value, whose live assertion would be
  worthless if the fixture mangled it on the way out.

A non-empty sweep is a bug report, not routine maintenance: it warns
loudly, because every hit means the primary teardown failed somewhere.

## Edge cases / errors

- **Never retry a transient DO/Anthropic error away.** Neither the driver
  nor `aiform/llm.py` implements retry, so a real `5xx`/`429` mid-suite
  must surface as a visible failure. If it recurs, that is evidence the
  orchestrator needs retry handling — not that this test needs a retry
  loop. (The 5,000/hour budget verified above makes rate-limiting an
  unlikely cause; roughly 80 calls per run, measured.)
- **Ordering dependency.** Cases 2–9 share one zone, so a failure partway
  through leaves later cases unable to run meaningfully. Report it as one
  ordered scenario failing at a named step — hence the `assert_cli_ok(code,
  captured, step)` helper, which surfaces the `Error:` line `cli.main()`
  writes to stderr before returning 2. Without it a failed multi-minute
  run logs `assert 2 == 0` and nothing else.
- **A failed apply is not evidence the path under test ran**, and the
  categorization step is not deterministic. Cases 11 and 12 assert on a
  zone's fate after a `create()` that must fail — but exit 2 with an
  `Error:` line is equally what local validation, a declined gate #2 or a
  credential failure produce, and in each of those `create()` is never
  entered while the zone's state is trivially satisfied anyway.

  This is not hypothetical. Driven through `plan apply`, case 12 failed
  one run with `categorization returned 'update' but no state entry is
  tracked for it` — the `intent-orchestration-model` labelled a brand-new
  resource an update, so the plan was blocked before `create()` ran. The
  run before it had passed. Every assertion the case then made would have
  been satisfied by that outcome too, so a regression removing `create()`'s
  rollback could have gone unnoticed for runs at a time.

  Both cases therefore drive `Driver().create()` **directly** and assert
  the `HTTPError`'s status is `422`, the same way case 9 drives `delete()`.
  A test whose subject is a driver method must not be able to pass or fail
  on an LLM's wording. Case 11 keeps a CLI-level apply alongside it for the
  clean-error-and-tracks-nothing assertions, but makes no claim about
  *where* that apply failed. See "Out of scope" on the categorization
  flakiness itself, which is aiform's bug rather than this suite's.
- **Zone-name case.** Covered under "Zone naming" — and note what it is
  *not*: `strptime` matches format literals case-insensitively, so the
  sweep's age filter parses either spelling and the lowercasing is
  hygiene rather than a fix for a live error.
- **A droplet-scoped token** skips the suite rather than failing it (case
  1), the same way a keyless account skips the compute suite's case 11.

## Out of scope

- **Local-validation rejections** — a written trailing dot, `ip_address`,
  a wrong scalar type, duplicate records, an apex `NS` at a DO
  nameserver. All raise `ValueError` before any HTTP call, so the live API
  adds no information a mock doesn't; `tests/drivers/test_digitalocean_domain.py`
  already covers them and remains the right place.
- **A standalone `scripts/sweep_system_test_domains.py`** plus its mocked
  unit tests and a scheduled GH Actions workflow — the domain counterpart
  to the droplet sweep `specs/system_test.md` specs and that is itself
  still unimplemented. Its own `PROCESS.md` pass; the in-suite backstop
  above is what ships now.
- **Extending `aiform init`'s preflight to probe domain scope.** A real
  gap (`specs/digitalocean_domain.md` names it), but it touches
  `specs/cli.md` and several `tests/test_cli.py` assertions. Case 1's
  probe covers this suite's own need.
- **`aiform init` scaffolding a `domain.aiform.md` example** — already
  its own deferred item in `specs/digitalocean_domain.md`.
- **Pagination beyond one page of records.** `fetch_all_pages` requests
  `per_page=200`; a zone with more than 200 records would cost real time
  to build and prove little that `tests/drivers/test_digitalocean_common.py`
  and `specs/digitalocean_pagination.md` don't already cover. The sweep's
  own `GET /v2/domains` pagination *is* in scope, because this account
  really does hold other zones.
- **The categorization defect this suite surfaced — fixed, not deferred.**
  Running case 12 through `plan apply` produced, on one run and not the
  next, `PlanBlockedError: … categorization returned 'update' but no state
  entry is tracked for it`: the `intent-orchestration-model` labelling a
  brand-new resource an `update`, which `orchestrator.py` then correctly
  refused. It later hit the lifecycle sequence at case 2 as well, failing
  a first `plan create` outright.

  That was issue &#35;117, fixed in PR &#35;118 — `orchestrator.py` now plans an
  untracked or drifted-missing resource as `create` deterministically,
  without consulting the model, so the guess that could only ever be wrong
  is no longer made. This suite is what found it, which is the whole
  argument for running against a live API rather than a mock: the mock
  answered whatever its author believed.

  Cases 11 and 12 still drive `Driver().create()` directly, and that is
  now a deliberate scoping choice rather than a workaround. Their subject
  is `create()`'s rollback semantics, and routing them through the CLI
  would put an LLM call back on the path to an assertion that has nothing
  to do with one. The lifecycle sequence exercises the normal
  categorization path throughout.
- **Concurrency/locking.** `PLAN.md` §10's known deferred gap; this suite
  runs one sequential scenario.

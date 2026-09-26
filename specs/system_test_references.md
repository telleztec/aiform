# specs/system_test_references.md — live system test for cross-resource references (`tests/system/test_cli_references.py`)

Companion to `specs/system_test.md` (droplets), `specs/system_test_domain.md`
(DNS) and `specs/system_test_firewall.md`. The feature under test is
`specs/resource_references.md`, Phase 2 of `MULTI_RESOURCE_PRD.md` (#215).

## Purpose

Prove against the real DigitalOcean API that a reference to a value the
provider assigns at create time — a droplet's `ipv4_address` — flows into
another resource's `params` in a **single** `apply`, and that the value
published is the one the droplet actually got.

## Why this one is billable

Unlike the domain suite, which creates only free DNS zones, this test creates a
real droplet. That is not incidental, it is the point: `ipv4_address` is
assigned by DigitalOcean at create time and cannot be predicted or fixtured. A
mock would have to invent the value, and would therefore encode the same
assumption the code under test makes. `specs/system_test.md`'s cost discipline
applies — never on a `pull_request`/`push` trigger.

## What it settles that unit tests cannot

- A plan can be **built and applied in one pass** while the referenced value
  does not yet exist anywhere, against a provider that assigns it
  asynchronously. The unit tests prove this against a fake whose `create()`
  returns instantly.
- The address the zone publishes is the address the droplet got. Both sides are
  read back **from DigitalOcean**, not from `.aiform/state.json` — state
  agreeing with itself proves nothing about what the provider was told.
- A second `plan` over the applied pair is a clean no-op at **zero Anthropic
  calls**, measured with `--verbose` and `verbose_call_count()`. This is the property
  the whole design is built around, and offline it can only be shown against a
  fake client.
- `aiform resource status` does not report a resolved reference as drift, which
  is the failure `observability.py` would exhibit without resolving first.

## What it deliberately does NOT test

**Ordering.** Phase 1 orders by resource *key*, and `digitalocean.compute.*`
sorts before `digitalocean.domain.*` whether or not an edge exists — so a
droplet/zone pairing cannot distinguish a reference-derived edge from the plain
`sorted()` drain, and a test claiming otherwise would be passing vacuously. That
property is pinned in `tests/test_orchestrator.py`, with a dependent whose key
sorts *before* its target (`aaa-01` depending on `zzz-01`). What this suite
proves is value flow, which no ordering accident can fake.

`specs/resource_dependencies.md` advises naming files so alphabetical order
contradicts the required order. That advice is about *filenames*, and it does
not apply here for the reason above; the filenames are chosen for legibility.

## Steps

One test, five steps, in order:

1. Write both `.aiform.md` files — a droplet via `write_aiform_md()`, named by
   `unique_droplet_name()` so the sweep can reclaim it (see Cleanup), and a zone
   via `write_domain_aiform_md()` whose
   single A record's `data` is
   `${digitalocean.compute.<droplet>:ipv4_address}`. Then `plan create`, and
   assert the unresolved reference prints **verbatim with no annotation** — no
   "known after apply".
2. `plan apply --yes`, once, for both.
3. Read the droplet's `ipv4_address` from state, then read the zone's A records
   from DigitalOcean and assert exactly one, that its `data` equals that
   address, and that no `${` survived to the provider.
4. `plan create --verbose` again: a no-op at zero Anthropic calls, and the
   **resolved value** now printed where the reference used to be. The flag goes
   **after** the subcommand — `aiform -v plan create` silently leaves verbose
   off (issue #134), which would make `verbose_call_count()` raise on a missing
   `[verbose]` line and fail the run *after* the droplet had been billed. The
   no-op is asserted per resource (`= <key>: no-op`), not as the bare substring
   "no-op", which the always-printed summary tally contains regardless.
5. `resource status <zone>` reads `in sync` — asserted positively rather than
   as the word "drift" being absent, so it pins the verdict rather than the
   wording of its opposite — then `plan destroy --yes` and wait for the zone to
   be gone.

## Cleanup

Both objects are reclaimable if the run is interrupted (#141), but only because
the droplet is named through **`unique_droplet_name()`**. `is_sweepable_droplet()`
requires three signals and the first is the `SYSTEM_TEST_DROPLET_PREFIX` name
prefix — which is deliberately *not* a prefix of the compute suite's own
`aiform-system-test-droplet*`, so the tag alone is not enough and a droplet
named that way has **no** sweep backstop. An earlier draft of this suite named
it that way and claimed the tag covered it; it did not. The zone side needs no
such care: `unique_zone_name()` produces exactly what `zone_created_at()`
parses.

`teardown_tracked_resources` is still the first line of defence; the sweeps
matter only when the process dies before it runs.

The record's `data` is emitted through `write_domain_aiform_md()`, which
serializes each record as a JSON object — valid YAML **flow** style. That
matters: a reference inside `[ ]`/`{ }` must be quoted, and `json.dumps()`
quotes it for free. An unquoted reference in flow style is a `ParserError`, per
`specs/resource_references.md`'s YAML table.

## Out of scope

- **Integer-typed reference targets** — the firewall's `droplet_ids`. Not
  supported yet; see `specs/resource_references.md`'s "Out of scope" and its
  issue.
- **A reference to a target being replaced.** The withheld-target path
  (`volatile`/`replaced`) is covered offline in `tests/test_orchestrator.py`;
  provoking a real replace here would mean a second droplet create for a
  property that has no provider-specific behaviour to settle.
- **Fan-in and chains.** Same reasoning: the graph shapes are Phase 1's engine
  and are pinned offline.

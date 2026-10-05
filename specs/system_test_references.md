# specs/system_test_references.md — live system test for cross-resource references (`tests/system/test_cli_references.py`)

Companion to `specs/system_test.md` (droplets), `specs/system_test_domain.md`
(DNS) and `specs/system_test_firewall.md`. The feature under test is
`specs/resource_references.md`, Phase 2 of `MULTI_RESOURCE_PRD.md` (#215).

## Purpose

Prove against the real DigitalOcean API that a reference to a value the
provider assigns at create time flows into another resource's `params` in a
**single** `apply`, and that the value published is the one the provider
actually gave — two cases, one test each:

1. A droplet's `ipv4_address` flowing into a DNS A record's `data` (the
   original, string-valued case).
2. (#216) A droplet's `provider_id` flowing into a firewall's `droplet_ids`,
   the first **integer**-typed reference target Phase 2's syntax reaches.

(#224) A further test, `TestTwoDropletsInOneRuleLive`, proves that a firewall
rule admitting **two** droplets by reference inside `sources.droplet_ids`
applies and re-plans clean.

A third test proves a related but distinct claim against the same real API:
(#225) that `aiform plan destroy <file>` refuses to destroy a droplet a
tracked firewall still depends on — via a reference, via a declared
`depends_on:`, or both — unless `--force`, and that a forced destroy really
does drop the edge from the firewall's persisted state rather than leaving
it dangling. `specs/resource_dependencies.md`'s "Reverse dependents on a
paths-driven destroy" section is the mechanism; this is its only live
coverage.

## Why this one is billable

Unlike the domain suite, which creates only free DNS zones, this module
creates real droplets — one per reference test, since `ipv4_address` and
`provider_id` are each assigned by DigitalOcean at create time and cannot be
predicted or fixtured. A mock would have to invent the value, and would
therefore encode the same assumption the code under test makes. The second
test additionally creates a firewall, since settling #216 live needs a
resource that actually consumes an integer-typed reference — a firewall
attached only via `droplet_ids: []` proves nothing about the type
DigitalOcean accepted.

**The third test, `TestReverseDependentDestroyRefusalLive`, is the most
expensive in this file: two droplets plus a firewall referencing both.**
Neither droplet is skippable — the refusal being tested is specifically
about a *surviving* dependent, so the test needs one droplet to destroy and
a second the firewall keeps depending on throughout, to tell "no longer
orphaned because the edge was dropped" apart from "no longer orphaned
because nothing depends on anything any more." It also settles, live and
as a side effect, that DigitalOcean actually detaches a droplet from a
firewall's `droplet_ids` on a whole-object PUT carrying the shorter
list — a claim the driver's own comment marks `INFERRED`, verified so far
only for `tags`.

Each test gates on exactly the scope it needs, independently — not a
shared module-wide requirement: `test_one_apply_publishes_the_droplets_real_address`
skips on missing `domain` scope or an unowned zone parent
(`_skip_without_domain_scope()`); `test_droplet_ids_reference_publishes_the_droplets_provider_id`
and `TestReverseDependentDestroyRefusalLive`'s test both skip on missing
`firewall` scope (`_skip_without_firewall_scope()`) and need neither
`domain` scope nor the zone parent. This was a module-scoped autouse
fixture until the #216 review round found it meant a firewall-scoped token on
an account without the zone parent silently skipped *both* tests, including
the one that needs neither precondition — a droplet-only token still earns a
green `aiform init` check and then fails at the first firewall apply, same
gap `specs/digitalocean_domain.md` records for zones. `specs/system_test.md`'s
cost discipline applies — never on a `pull_request`/`push` trigger.

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
- (#216) That DigitalOcean **actually accepts** the integer a `:provider_id`
  reference resolves to for `droplet_ids`, and hands that same integer back
  rather than a stringified copy of it — read from the live firewall, not
  from state. A mock can only assert the value aiform sent; it cannot show
  what DigitalOcean does with it, which is exactly what the first test above
  does not settle for an integer-typed target.
- (#225) That destroying a droplet a real, tracked firewall still depends on
  is genuinely refused — `code == 2`, both resources still live and tracked
  afterward — and that `--force` genuinely proceeds: the droplet is deleted
  on DigitalOcean, and the firewall's **persisted** `depends_on` no longer
  names it. A unit test can assert `_reverse_dependents()` found the pair and
  `_prune_dependents_on()` rewrote the list; it cannot show that the
  subsequent state-driven cleanup destroy (`plan destroy --all`,
  run without `--force`) actually succeeds afterward rather than
  hitting the identical refusal again for a droplet the user just
  deliberately destroyed. It also settles, as a side effect, that
  DigitalOcean detaches a droplet from `droplet_ids` on a shorter
  whole-object PUT — `INFERRED` in the driver's own comment, previously
  verified only for `tags`.

## What it deliberately does NOT test

**Ordering.** Phase 1 orders by resource *key*, and `digitalocean.compute.*`
sorts before both `digitalocean.domain.*` and `digitalocean.firewall.*`
whether or not an edge exists — so no pairing in this file (droplet/zone,
droplet/firewall, or the third test's two-droplets/firewall) can
distinguish a reference-derived edge from the plain `sorted()` drain on the
*create* side, and a test claiming otherwise would be passing vacuously.
The third test's destroy-refusal assertions are a different claim, about
whether a destroy is *permitted* at all rather than what order it runs
in — the ordering caveat here doesn't cover them. That
property is pinned in `tests/test_orchestrator.py`, with a dependent whose key
sorts *before* its target (`aaa-01` depending on `zzz-01`). What this suite
proves is value flow, which no ordering accident can fake.

`specs/resource_dependencies.md` advises naming files so alphabetical order
contradicts the required order. That advice is about *filenames*, and it does
not apply here for the reason above; the filenames are chosen for legibility.

## Steps

Three tests, each gating independently on the scope it needs (see "Why this
one is billable"). `test_one_apply_publishes_the_droplets_real_address` (the
ipv4_address/domain case) — skipped if the token lacks `domain` scope or the
account doesn't own the system-test zone parent — five steps:

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
5. `resource status <zone>` reads `in sync`, asserted positively *in addition
   to* the word "drifted" being absent — the positive form pins the verdict
   rather than the wording of its opposite, and the negative one catches a
   renamed verdict that still reports drift. Then `plan destroy --all --deployment default --yes` and wait
   for the zone to be gone.

`test_droplet_ids_reference_publishes_the_droplets_provider_id` (#216, the
provider_id/firewall case), four steps — skipped outright if the token lacks
`firewall` scope:

1. Write a droplet's `.aiform.md` and a firewall's, the firewall's
   `droplet_ids` holding one reference string,
   `${digitalocean.compute.<droplet>:provider_id}`, not a literal. Then `plan
   create`, and assert the unresolved reference prints verbatim.
2. `plan apply --yes`, once, for both.
3. Read the droplet's `provider_id` from state and assert it is a real `int`;
   read the firewall back from DigitalOcean and assert its `droplet_ids`
   equals `[provider_id]` — settling that the provider itself accepted and
   returned the integer, not merely that aiform sent one.
4. `plan create --verbose` again: a no-op at zero Anthropic calls for both
   resources. Then `plan destroy --all --deployment default --yes`.

`TestReverseDependentDestroyRefusalLive`'s single test (#225) — skipped
outright if the token lacks `firewall` scope; two droplets and a firewall
referencing both:

1. Write two droplets and a firewall whose `droplet_ids` references both
   `provider_id`s **and** whose `depends_on:` frontmatter declares both keys
   — exercising the union `_dependency_targets()` computes, not only the
   half a reference alone would. `plan create` then `plan apply`; assert
   the firewall's persisted `depends_on` is both keys, sorted, and the live
   firewall's `droplet_ids` matches both `provider_id`s.
2. Rewrite the firewall to reference and declare only the first droplet,
   dropping the second out of both halves of the union at once — this is
   what lets the rest of the test tell "still depended on" apart from "no
   longer depended on by anything." `plan apply`; assert the live
   `droplet_ids` shrank to one and the persisted `depends_on` did too. This
   is the live proof that DigitalOcean actually detaches on a whole-object
   PUT carrying the shorter list.
3. `plan destroy <survivor-file> --yes`, **no** `--force`: assert `code ==
   2`, `"Error:"` on stderr naming both the firewall and the droplet, and
   that both are still tracked and still live afterward — the refusal must
   not be a partial apply.
4. The same command **with** `--force`: assert success, `"Warning:"` on
   stdout naming the firewall, the droplet gone from state and from
   DigitalOcean, and — the point of the whole test —
   `tracked.resources[firewall_key].depends_on == []`: the forced destroy's
   warning about "dropping the edge" is checked against the persisted
   state, not just its wording. Then a final, unforced `plan destroy --all --deployment default --yes`
   cleans up what remains and must succeed — it would instead hit
   the identical refusal a second time, against a droplet the user just
   deliberately destroyed, if the pruning above were only cosmetic.

`TestTwoDropletsInOneRuleLive`'s single test (#224) — skipped outright if the
token lacks `firewall` scope; two droplets and a firewall:

1. Name the two droplets, sort the names, and write a firewall whose one rule's
   `sources.droplet_ids` references the **later-keyed** droplet first, then the
   earlier. Droplets are created in key order and DigitalOcean's ids ascend, so
   the resolved list is `[higher, lower]`: unsorted, deterministically, which
   is what the removed sorted-list check refused partway through apply.
2. `plan create`, then `plan apply --yes`. Assert it succeeds, and that the
   live firewall's `sources.droplet_ids`, read back from DigitalOcean, holds
   exactly the two `provider_id`s (compared sorted, since the provider owns
   the order it returns).
3. `plan create --verbose` again: `no-op` for all three resources at zero
   Anthropic calls. This is the half a unit test cannot settle, because it
   needs DigitalOcean's real read-back to compare equal to the unsorted
   desired list.
4. `plan destroy --all --deployment default --yes`.

Written and spec'd in the PR that fixed #224 but **not run** by its author: the
coordinating session runs live suites serially because they share the
deployment `default`.

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
matter only when the process dies before it runs. The second and third
tests' droplets are all named the same way, through `unique_droplet_name()`
(the third names two, `"revdep-a"`/`"revdep-b"`), for the same reason;
every firewall (`unique_firewall_name()`, `SYSTEM_TEST_FW_PREFIX`) has its
own session sweep, `_sweep_leaked_system_test_firewalls()`
(`tests/system/conftest.py:1180`), keyed on that prefix plus the
`aiform-system-test` tag plus an age floor — cleaner than the droplet/zone
sweeps since a firewall carries tags and `created_at` directly, so nothing
needs parsing back out of a name. The third test's firewall must carry that
tag *before* creation, unlike a droplet's tag (auto-created on demand), so
it calls `ensure_system_test_tag()` up front — a 422 `"tag ... does not
exist"` would otherwise make the whole scenario order-dependent on the
droplet side effect happening to run first.

The record's `data` is emitted through `write_domain_aiform_md()`, which
serializes each record as a JSON object — valid YAML **flow** style. That
matters: a reference inside `[ ]`/`{ }` must be quoted, and `json.dumps()`
quotes it for free. An unquoted reference in flow style is a `ParserError`, per
`specs/resource_references.md`'s YAML table.

## Out of scope

- **A reference to a target being replaced.** The withheld-target path
  (`volatile`/`replaced`) is covered offline in `tests/test_orchestrator.py`;
  provoking a real replace here would mean a second droplet create for a
  property that has no provider-specific behaviour to settle.
- **Fan-in and chains.** Same reasoning: the graph shapes are Phase 1's engine
  and are pinned offline.

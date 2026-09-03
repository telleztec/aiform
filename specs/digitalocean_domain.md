# specs/digitalocean_domain.md — `drivers/digitalocean/domain.py`

## Purpose

Manage a DigitalOcean DNS zone **and the records inside it** as a single
aiform resource, hand-authored through `PROCESS.md`'s loop (mechanism 1 —
`PLAN.md`'s "Driver curation"), never generated.

This is the second curated driver and the first resource kind other than
`compute`.

### Required `PLAN.md` §10 cross-references

`PROCESS.md` step 1 requires checking §10 for pre-existing entries on this
topic before writing a new spec, and explicitly implementing, narrowing, or
extending them rather than silently ignoring one. Three entries apply:

- **"Only one resource kind is implemented."** That entry names `network` and
  `load_balancer` as kinds the vocabulary anticipates; `domain` is not on that
  list, so this spec **extends** it. It also states: *"Adding a second kind or
  a second provider is expected to require zero orchestrator changes, but that
  claim is untested until it actually happens."* This driver is that test. The
  acceptance criterion is concrete and checkable: **no file under `aiform/` may
  change.** If one must, that is a finding about the architecture to be
  flagged, not a change to make quietly.
- **"No dependency graph."** That entry's canonical example is *"a DNS record
  referencing that IP"* — precisely this resource kind. This spec **implements
  the narrowed version**: record `data` is always a literal the user types.
  There is no way to reference a `compute` resource's `ipv4_address`, and this
  spec does not add one. Changing a droplet's address means editing the A
  record's `data` by hand. That entry is also why zone and records are one
  resource rather than two kinds — see "Why one resource kind" below.
- **"Resource tagging convention"** / `specs/resource_tagging.md`. DigitalOcean
  domains have **no tagging API at all**, so this driver adopts neither
  `_tags_for_create()` nor `_tags_for_attributes()`, and `PARAM_SCHEMA` has no
  `tags` key. That spec anticipated exactly this: *"A CSP/resource kind with no
  tagging primitive at all … not an error, not a degraded mode, the guarantee
  just doesn't extend to that `(provider, resource)` pair."* Stated here
  explicitly rather than left silent, because it is the first real data point
  on whether that design generalizes — and it carries a real consequence: any
  sweep or audit built on the `aiform-managed` marker has **no signal** for
  domains created by aiform. `specs/system_test.md`'s orphan-cleanup sweep
  would need a different identification strategy for zones.

### Why one resource kind, not `domain` + `dns_record`

Because there is no dependency graph. File discovery is
`sorted(cwd.glob("*.aiform.md"))` (`orchestrator.discover_files()`), so with two
kinds nothing would guarantee a zone is created before the records inside it,
and a destroy would have no defined ordering either. One kind makes zone and
records atomic, needs no cross-resource references, and keeps the whole feature
within one driver, one spec, and one test suite.

## Interface

Per `PLAN.md` §4's contract, unchanged. Class attributes:

```python
PARAM_SCHEMA = {
    "type": "object",
    "properties": {
        "records": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [
                            "A",
                            "AAAA",
                            "CAA",
                            "CNAME",
                            "MX",
                            "NS",
                            "SRV",
                            "TXT",
                        ],
                    },
                    "name": {"type": "string"},
                    "data": {"type": "string"},
                    "ttl": {"type": "integer"},
                    "priority": {"type": "integer"},
                    "port": {"type": "integer"},
                    "weight": {"type": "integer"},
                    "flags": {"type": "integer"},
                    "tag": {"type": "string"},
                },
                "required": ["type", "name", "data", "ttl"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["records"],
    "additionalProperties": True,
}
LIKELY_REPLACE_FIELDS = []
NON_DIFFABLE_FIELDS = []
UNORDERED_FIELDS = ["records"]
```

`LIKELY_REPLACE_FIELDS` is empty because **nothing** about a zone forces a
replace: every record change is applicable in place. `NON_DIFFABLE_FIELDS` is
empty because `read()` recovers every managed field from the API.

`UNORDERED_FIELDS = ["records"]` is the load-bearing one, and this driver is
the second consumer of the mechanism `specs/unordered_fields.md` added (the
first being `compute.py`'s `tags`). A DNS zone's records are a set: the order
DigitalOcean happens to list them in carries no meaning, and neither does the
order a user writes them. Without this declaration, `planner.diff_attributes()`
would compare the two lists as ordered sequences and report a permanent diff
the moment they disagreed — see "Record order is free" below for why that is
not something a user can be asked to work around.

Note this is the first `UNORDERED_FIELDS` entry whose elements are **dicts**
rather than strings, which is why `aiform/compare.py` sorts by a canonical
serialization rather than by the elements themselves: `sorted()` on a list of
dicts raises `TypeError`.

`credentials` is `{"DIGITALOCEAN_TOKEN": "<token>"}` (`config.PROVIDER_TOKEN_ENV_VARS`),
sent as `Authorization: Bearer`. Base URL `https://api.digitalocean.com/v2`.
HTTP is `urllib.request` only — see `specs/digitalocean_compute.md`'s "HTTP
client convention"; `prompts/review_driver.md` item 9 makes a violation
blocking.

The logger name is the literal `"aiform.driver.digitalocean.domain"`, never
`__name__`: `load_driver()` execs the module under a synthetic name that isn't
a descendant of the `aiform` logger, so `getLogger(__name__)` produces a logger
whose output silently reaches neither sink. `driver_gen.py`'s
`_logger_name_reasons()` rejects it outright.

Record listing goes through `drivers/digitalocean/_common.py`'s
`fetch_all_pages()` — see `specs/digitalocean_pagination.md`.

### Per-type field requirements

| Type | Required | Optional |
|---|---|---|
| `A`, `AAAA`, `CNAME`, `NS`, `TXT` | `type`, `name`, `data`, `ttl` | — |
| `MX` | + `priority` | — |
| `SRV` | + `priority`, `port`, `weight` | — |
| `CAA` | + `flags`, `tag` | — |

A field not listed for a type is rejected — an `A` record with `priority` is a
user error, and accepting it would mean `read()` (which returns only the type's
own fields) diffs against it forever.

`SOA` is deliberately absent from the enum: DigitalOcean creates it
automatically and, in its own words, it is *"unavailable as an individual
record resource."*

## The zero-diff invariant

**This is the property the whole design serves, and the one most likely to be
broken by a plausible-looking change.**

`planner.diff_attributes()` compares `records` as a multiset, element by
element, because this driver declares it in `UNORDERED_FIELDS`. Each element is
still compared **whole** — one mismatched key in one record makes that record a
different element, which marks the entire `records` value changed. `CLAUDE.md`
requires a repeat `plan` on unchanged input to make **zero** Anthropic API
calls. Therefore:

> Every record `read()` returns must match a record the user wrote in
> `.aiform.md` — **same keys, same values, same types** — one for one, with no
> record left over on either side. Their *order* is free; nothing else is.

Anything less doesn't merely look untidy: every `plan` bills an
`intent-orchestration-model` call and shows a phantom diff, and every `apply`
rewrites records that were already correct.

Three mechanisms hold the invariant:

1. **`records` is declared `UNORDERED_FIELDS`**, so the planner compares it as
   a multiset. Record order in the user's file is therefore free — see below.
2. **`read()` returns DO's values verbatim** for `name` and `data`. It does
   **not** try to reverse DO's normalizations.
3. **Non-canonical input is rejected**, loudly, before any API call.

### Why `read()` does not un-normalize

DigitalOcean rewrites what it stores: `data` on `CNAME`/`MX`/`NS`/`SRV` is
expanded to a fully-qualified name with a trailing dot, so a POST of
`data: "www"` on zone `example.com` comes back as `www.example.com.`.

The tempting fix — strip the trailing dot in `read()`, and expand relative
names to match — requires reimplementing DO's expansion rules locally and
keeping them correct forever. It cannot work in general anyway: `"@"`,
`"www"`, and `"www.example.com."` all round-trip to the same stored value, so
no reverse mapping can know which of the three the user typed.

So the canonical form is **whatever DigitalOcean stores**, and the user writes
that: `data` for `CNAME`/`MX`/`NS`/`SRV` must be fully qualified **with the
trailing dot**. Anything else is rejected at validation time with a message
naming the exact expected value (see Edge cases), rather than silently
producing a diff that never converges.

`name` needs no such rule — DO returns it relative (`www`, `@`), which is what
a user writes.

### Record order is free, and `read()` sorts anyway

**A user may list records in any order.** `UNORDERED_FIELDS = ["records"]`
makes `planner.diff_attributes()` compare the list as a multiset
(`specs/unordered_fields.md`), so element order never produces a diff.

An earlier draft of this spec instead required the user's file to be written in
one canonical sort order, and documented the resulting permanent phantom diff
as an accepted ergonomic cost. That was the wrong trade and it is worth
recording why, because the reasoning generalizes to every future driver: it
assumed a CSP's list ordering is a stable property one can design around. It
isn't. A hash-set-backed store returns elements in an order determined by each
element's hash and the table's capacity, so adding one record can rehash and
reorder the ones already there — stable at three records, unstable at
twenty-one, with no API change and no warning. Requiring the *user* to
compensate for that pushes an unfixable CSP-side non-guarantee onto the person
least able to observe it. Closing #110 in the generic diff layer first was the
correct order of work, and this driver is its second consumer.

`read()` still returns records sorted, by the tuple

```python
(type, name, data, str(priority), str(port), str(weight), str(flags), str(tag))
```

with absent fields as `""` — all components strings, so the sort is total and
never raises comparing `None` to `int`. But this is now **cosmetic, not
load-bearing**: it keeps `state.json` from churning on every refresh if DO's
own ordering wobbles, and makes `aiform show` output readable. Correctness no
longer depends on it, and a future change that drops the sort would produce
noisy state diffs rather than a broken plan.

**Duplicate records remain rejected** (see Edge cases), which matters
here: multiset comparison would otherwise report a duplicated record as a
genuine diff forever, since DO stores each record once. Rejecting at validation
time is the fix, exactly as `specs/unordered_fields.md` prescribes for the
equivalent duplicate-tag case in `compute.py`.

## Behavior

### `create(name, params, credentials)`

1. `POST /v2/domains` with body `{"name": name}` — nothing else. The
   `ip_address` convenience field is **not supported**; see Edge cases.
2. For each record in `params["records"]`, in the user's given order,
   `POST /v2/domains/{name}/records` with the type's fields.
3. Return `{"id": name, **attributes}` where `attributes` comes from the same
   normalize/filter/sort pipeline `read()` uses, built from a fresh `read()`
   rather than from the POST responses — so `create()` and `read()` cannot
   drift apart.

**`id` is the domain name.** DigitalOcean keys domains by name; there is no
numeric domain id. `_pop_id()` treats it as an opaque string, so this is
contract-legal, and it makes `read(id)` and `delete(id)` straightforward.

**Rollback on partial failure.** If any record POST fails *after* step 1
created the zone, `delete` the zone and re-raise the original error, so
`create()` is atomic. Without this, a half-built zone would be live and
untracked: the orchestrator only writes state on success, so the next `plan`
would try to create the zone again and get a 422.

Rollback applies **only to a zone this call created**. If step 1 itself fails
because the zone already exists, nothing is deleted — destroying a
pre-existing zone the user did not ask aiform to manage would be catastrophic
and unrecoverable. The rollback delete is best-effort: if it also fails, raise
a `RuntimeError` naming both the original error and the failed cleanup, so the
orphan is reported rather than hidden (mirroring `compute.py`'s
resize-then-restore compounding-failure handling).

### `read(id, credentials)`

1. `GET /v2/domains/{id}` — a 404 raises `ResourceNotFoundError`, per
   `prompts/review_driver.md` item 10. Any other non-2xx propagates.
2. `fetch_all_pages(fetch, f"{BASE}/domains/{id}/records", "domain_records")`.
3. Filter, project, sort (below).
4. Return `{"id": id, "ttl": <zone ttl>, "records": [...]}`.

`zone_file` is deliberately **excluded** from attributes: it embeds the SOA
serial, which changes on every zone edit, so storing it would churn
`state.json` on every apply and bloat it with a full BIND file. The zone's
read-only `ttl` is kept because it is small, stable, and informative in
`aiform show` — and harmless to the diff, which only compares keys present in
`desired`.

**Filtering — the destructive-diff guard.** Creating a zone auto-creates an
SOA record and three apex `NS` records pointing at
`ns1`/`ns2`/`ns3.digitalocean.com`. These are DO-managed. Surfaced unfiltered,
they appear in `read()` but not in the user's file, so the diff reads as
"delete the zone's nameservers" — and `apply` would execute it, breaking
resolution for the entire domain. `read()` therefore drops:

- every record of type `SOA`; and
- every `NS` record whose `name` is `@` **and** whose `data` ends in
  `.digitalocean.com.`

Both conditions are required for the NS case. A user's own delegated-subdomain
`NS` record (`name != "@"`) stays managed, and so would an apex `NS` pointing
somewhere other than DO's nameservers — narrow filter, so nothing the user
actually asked for is silently dropped.

**Projection.** Each surviving record keeps only the fields its type declares
(table above). This drops DO's `id` and the explicit `null`s it returns for
`priority`/`port`/`weight`/`flags`/`tag` on types that don't use them — none of
which a user writes, and every one of which would otherwise diff forever.

### `update(id, current, desired, credentials)`

1. **Refuse a non-`records` diff first, before mutating anything.** Any key
   other than `records` differing raises `DriverUpdateNotSupported` with those
   keys in `unsupported_fields`. This satisfies `PLAN.md` §4's ordering
   requirement trivially — the only raise happens before the first API call.
2. Validate the desired records (same checks as `create()`), still before any
   mutation, so a malformed value can't leave the zone half-edited. Raises
   `ValueError`, never `DriverUpdateNotSupported` — a bad value is not a
   replace-worthy diff, and converting it into one would destroy a live zone
   over a typo. Same reasoning as `compute.py`'s `_reject_malformed_values()`.
3. Reconcile the record set (below).
4. Return a fresh `read()`.

**`update()` must essentially never raise `DriverUpdateNotSupported`.** Every
records change is applicable in place via the record endpoints, and the
orchestrator answers that exception by **destroying and recreating the
resource** — here, deleting an entire live DNS zone. `prompts/review_driver.md`
item 4 names over-broad refusal as a *blocking* issue for exactly this reason.
A zone's `name` cannot change: it is the state key, so a rename is a different
resource, never an update.

**Reconciliation.** Pair `current` against `desired`:

- **Identity.** Group both sides by `(type, name)`. Where a group holds exactly
  one record on each side, those two are the same record — apply differences
  with `PUT /v2/domains/{id}/records/{record_id}`, including a changed `data`.
  This keeps the common single-valued cases (`A`, `CNAME`) as in-place edits
  rather than a delete/create pair, which would open a brief resolution gap.
- Otherwise (multi-record groups such as `MX`, `TXT`, `NS`) identity is
  `(type, name, data)`: matched pairs differing only in `ttl`/`priority`/etc.
  are `PUT`; unmatched `desired` records are `POST`; unmatched `current`
  records are `DELETE`.
- **Order: `PUT`, then `POST`, then `DELETE`.** Updates and additions land
  before removals so the zone is never missing a record it will have again.
  No conflict arises from adding before deleting, because the single-valued
  case — where a duplicate would be rejected — is handled by `PUT` above.

`record_id` comes from the live listing, so reconciliation needs the unprojected
records internally even though `read()` returns them projected.

### `delete(id, credentials)`

`DELETE /v2/domains/{id}`. A 404 returns `None` (success) — idempotent per
`prompts/review_driver.md` item 3. Deleting a zone removes its records; no
per-record cleanup is needed.

## Edge cases / errors

- **Knowledge-confidence.** Following `specs/digitalocean_compute.md`'s
  convention of separating verified from recalled facts:
  - **Verified** against `digitalocean/openapi` during this spec's authoring:
    the record field set and their nullability (`models/domain_record.yml`);
    `ip_address` being **write-only** and *"automatically generates an A record
    pointing to the apex domain"*, plus `ttl`/`zone_file` being read-only and
    the SOA being *"automatically created and unavailable as an individual
    record resource"* (`models/domain.yml`); every endpoint and method
    (`domains_*.yml`); `per_page` defaulting to **20**, max 200
    (`shared/parameters.yml`); and `links.pages.next` as an absolute URI absent
    on the final page (`shared/pages.yml`).
  - **Recalled and reasoned, NOT verified** — treat as the likeliest failure
    points and confirm in the live system test (see Out of scope): the
    per-type required-field table; the exact trailing-dot behavior for each of
    `CNAME`/`MX`/`NS`/`SRV`; whether DO quotes or escapes `TXT` `data` on read;
    and the precise shape of the auto-created apex `NS` records.
  - **Known-wrong upstream, do not copy.** `models/domain_record_types.yml`'s
    `required` lists mark `NS`, `TXT`, and `SRV` as requiring `flags` and
    `tag`. That contradicts the field descriptions in `domain_record.yml`,
    which document both as CAA-only. The table in this spec follows the field
    descriptions. This is a concrete reminder that a vendor OpenAPI document is
    evidence, not ground truth.
- **`data` not fully qualified** for `CNAME`/`MX`/`NS`/`SRV` (no trailing dot):
  `ValueError` before any API call, naming the record and the expected form.
  This is the single most likely user error and, left unchecked, produces a
  permanently non-converging plan rather than a visible failure. See "Why
  `read()` does not un-normalize".
- **`ip_address` is not a supported param.** DigitalOcean accepts it on
  `POST /v2/domains` as a convenience that auto-creates an apex `A` record, but
  it is **write-only** — `read()` could never recover it, so supporting it would
  mean a `NON_DIFFABLE_FIELDS` entry carried forward forever *and* two different
  ways to express the same apex address, one of them invisible to the diff. The
  driver never sends it. A user who sets it gets a `ValueError` naming the
  explicit apex `A` record to write instead — not silent ignoring, which would
  leave them believing an unmanaged record was managed.
- **A record type outside the enum** (including `SOA`): `ValueError` naming the
  type and the supported set.
- **A field not valid for its record type** (e.g. `priority` on an `A` record):
  `ValueError`. Accepting it would guarantee a permanent diff, since `read()`
  returns only the type's own fields.
- **`records: []`** is valid and means "an empty zone" — the zone still exists,
  with only its DO-managed SOA/NS records, which `read()` filters out. `read()`
  correctly returns `records: []`, so this is a stable no-op state, not a
  perpetual diff.
- **`records` not a list, or an element not a dict**: `ValueError` before any
  API call. Nothing upstream validates against `PARAM_SCHEMA` — despite
  `driver.py`'s docstring claiming the orchestrator does, it does not, a gap
  `tests/drivers/test_digitalocean_compute.py` already records — so values
  arrive exactly as YAML parsed them.
- **Duplicate records** (identical on all projected fields) in the user's file:
  `ValueError`. DO would accept some duplicates, but they make the
  reconciliation pairing ambiguous and the diff unstable.
- **A 422 from `POST /v2/domains`** because the zone already exists is
  **not** converted into anything softer, and never triggers rollback. It
  propagates, so the user learns the name is taken rather than having aiform
  adopt — or delete — a zone it did not create.
- **Zone deleted out of band** between refresh and apply: `read()` raises
  `ResourceNotFoundError`, the orchestrator marks `drifted_missing`, and the
  plan proposes a recreate. Standard path, no special handling.
- **Rate limiting / 5xx** propagate as `urllib.error.HTTPError`, never
  converted to `DriverUpdateNotSupported` (`prompts/review_driver.md` item 4).

## Out of scope

- **Referencing another resource's attributes** — e.g. an A record pointing at
  a `compute` resource's `ipv4_address`. `PLAN.md` §10's "No dependency graph",
  quoted above; this driver implements the narrowed, literal-value version.
- **A live system test.** `tests/system/` is its own module with its own spec
  (`specs/system_test.md`) and `PROCESS.md` is one module per PR. Worth filing
  as a follow-up, and cheap to run: DigitalOcean bills nothing for DNS zones,
  unlike the droplet suite. It is also the only way to settle the
  "recalled, not verified" items above — particularly the trailing-dot and
  `TXT`-quoting behavior, which unit tests can only assert against a mock that
  encodes the same assumption. Its `write_aiform_md()` helper hardcodes
  `resource: compute` and would need a parallel.
- **`aiform init` scaffolding a `domain.aiform.md` example.** `cli.py` writes
  exactly one example today; a second touches `specs/cli.md` and several
  `tests/test_cli.py` assertions. Separate, small PR.
- **Marker tagging** — impossible here; see the §10 cross-reference above.
- **Zone-level settings** (default `ttl` on create, `zone_file` import/export).
  `ttl` is read-only on the domain object, and bulk zone-file import is a
  different interaction model from record-by-record reconciliation.
- **Reverse DNS / PTR records**, which DigitalOcean manages per-droplet, not
  through the domain API.
- **Tolerating a non-canonical record order or a bare `data` value** by
  normalizing the user's file. The driver validates and rejects; it never
  rewrites the user's input, and never edits `.aiform.md`.

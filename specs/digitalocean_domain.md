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
    "additionalProperties": False,
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

### `data` is written dotless; the driver adds the dot the API demands

**Verified against the live API**, after two earlier versions of this section
were wrong in opposite directions. DigitalOcean is asymmetric here, and that
asymmetry is the whole difficulty:

| | Form |
|---|---|
| What DO **requires** on `POST`/`PUT` | **with** a trailing dot — a dotless or relative value returns `422 "Data needs to end with a dot (.)"` |
| What DO **stores and returns** on `GET` | **without** it — `POST`ing `target.example.com.` reads back as `target.example.com` |

Applies to `CNAME`, `MX`, `NS`, `SRV`, **and `CAA`** — the same five types
DigitalOcean's own Terraform provider special-cases. `"@"` is exempt in both
directions: it is sent as `@` and stored as `@`.

**The canonical form is therefore the dotless FQDN** — `mail.example.com` —
because that is what `read()` returns, and:

- **Validation rejects a written trailing dot**, naming the dotless form to
  write. Not pedantry: `read()` returns dotless, so a user writing the dotted
  form would get a permanent phantom diff on that record forever.
- **Validation rejects a relative target** (a bare label with no dot at all),
  which DO also rejects — so this is a clearer error, earlier, not a new rule.
- **The driver appends the trailing dot at the wire boundary only**, in the
  one helper that builds a `POST`/`PUT` body. Everything else — validation,
  duplicate detection, reconciliation identity, `read()`'s projection — works
  in the dotless canonical form.
- **`read()` returns `data` verbatim.** DO already returns dotless, so there is
  nothing to strip, and keeping `read()` free of normalization is what makes
  the zero-diff invariant checkable by inspection.
- The DO-managed-NS filter still matches
  `data.rstrip(".").endswith(".digitalocean.com")` — belt and braces, cheap,
  and correct if DO ever changes.

**Why "accept either spelling" cannot work, and why that generalizes.** The
version of this section immediately before this one tried to accept both forms
and normalize internally. That is structurally impossible here, for a reason
worth stating in general terms because it constrains **every** future driver:

> `planner.diff_attributes()` compares `read()`'s output against the user's
> **raw** `params`. There is no hook for a driver to normalize either side of
> that comparison. So a driver can only ever have **one** writable spelling of
> a value that converges to zero-diff, and it must be exactly the spelling
> `read()` returns.

A driver that wants to accept a second spelling has only one honest option:
reject it, with a message naming the canonical one. See
`specs/driver.md`'s addendum, where this is recorded as a general rule rather
than left to be rediscovered per driver.

`name` needs no rule of either kind — DO returns it relative (`www`, `@`),
which is what a user writes.

### How this was gotten wrong twice, and what caught it

Kept because the pattern generalizes past this driver (see #114):

The first version asserted DO stores `data` **with** a trailing dot, marked in
Knowledge-confidence as "recalled, not verified". That label was accurate and
completely inert. From that one fact: the DO-managed-NS filter tested for
`.digitalocean.com.` and therefore never matched, so `apply` would have
**deleted the zone's own nameservers** — the precise failure the filter exists
to prevent — and validation demanded a form DO does not store, so every
hostname-typed record would have diffed forever while the user was forbidden
from writing the value that converges. Two guards meant to catch each other,
both defeated by the same wrong belief.

**The 74 tests did not catch it**, and structurally could not: the fake
`urlopen` fixtures returned dotted `data` because that is what the author
believed, so the tests confirmed the driver handled the wrong universe
correctly. A hand-written fake cannot falsify the model it was built from.

The second version — "accept either form" — was written after two vendor
sources (DO's OpenAPI examples, DO's Terraform provider) contradicted the
first. It was still wrong, because no document revealed the input/output
asymmetry above. Only a live request did: two throwaway zones, ~15 API calls,
zero cost, deleted in a `finally`.

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

- every record of type `SOA` — verified live to be present in the listing as
  `{type: "SOA", name: "@", data: "1800"}`, its `data` being the zone TTL
  rather than a hostname; and
- every `NS` record whose `name` is `@` **and** whose `data`, with any trailing
  dot removed and lowercased, ends in `.digitalocean.com`.

The dot-insensitive and case-insensitive comparison is deliberate. DigitalOcean
returns these **without** a trailing dot (`ns1.digitalocean.com`, verified
live), and an earlier version of this filter anchored on `.digitalocean.com.`
**with** the dot — so it never matched, and `apply` would have deleted the
zone's own nameservers. Normalizing before comparing costs nothing and removes
the whole class of near-miss.

Both conditions are required for the NS case. A user's own delegated-subdomain
`NS` record (`name != "@"`) stays managed, and so would an apex `NS` pointing
somewhere other than DO's nameservers — narrow filter, so nothing the user
actually asked for is silently dropped.

**Projection.** Each surviving record keeps only the fields its type declares
(table above). This drops DO's `id` and the explicit `null`s it returns for
`priority`/`port`/`weight`/`flags`/`tag` on types that don't use them — none of
which a user writes, and every one of which would otherwise diff forever.

### `update(id, current, desired, credentials)`

1. Validate `desired` (the same checks `create()` runs — including the
   unknown-top-level-key rejection below), before any mutation, so a malformed
   value can't leave the zone half-edited. Raises `ValueError`.
2. Diff **only `records`**, the single key `PARAM_SCHEMA` declares. Mirrors
   `compute.py`'s `diff_fields`, which likewise iterates
   `PARAM_SCHEMA["properties"]` rather than everything in `desired`.
3. Reconcile the record set (below).
4. Return a fresh `read()`.

**`update()` never raises `DriverUpdateNotSupported`, and that is correct** —
stated plainly rather than left as a branch nobody can reach. The orchestrator
answers that exception by **destroying and recreating the resource**, which
here means deleting an entire live DNS zone and every record in it.
`prompts/review_driver.md` item 4 makes over-broad refusal a *blocking* issue
precisely because of that consequence. Every records change is applicable in
place through the record endpoints, so no records diff is ever replace-worthy.
A zone's `name` cannot change either: it is the state key, so a rename is a
different resource, not an update.

**An unknown top-level param is a `ValueError`, not a
`DriverUpdateNotSupported`** — and an earlier draft of this spec got this
wrong, in a way worth recording because it is the exact trap the review
checklist describes. That draft said "any key other than `records` differing
raises `DriverUpdateNotSupported`". Since `orchestrator.apply_plan()` calls
`update(id, state_entry.attributes, pr.desired_params, ...)` with
`desired_params` being the raw `params:` block, the only way such a diff can
arise is a user typing a key this driver doesn't support. Under that draft, a
stray `ttl:` in a `.aiform.md` file would have **destroyed and recreated the
user's DNS zone**. Rejecting the input is the proportionate answer; destroying
a zone over a typo is not. Caught while writing this module's tests, when the
contract had to be stated concretely enough to assert on.

Accordingly `PARAM_SCHEMA` sets `additionalProperties: False` at the top level.
Nothing upstream enforces `PARAM_SCHEMA` (`driver.py`'s docstring claims the
orchestrator validates against it; it does not), so the driver performs this
check itself — the schema records the intent, the validation enforces it.

**Reconciliation.** Group both sides by `(type, name)`, then pick per group:

- **Single-valued path** — `PUT /v2/domains/{id}/records/{record_id}`. Taken
  when the record's type is in `{A, AAAA, CNAME}` **and** the group holds at
  most one record on each side. The two records are then the same record, and a
  changed `data` is an edit: `PUT` avoids the brief resolution gap a
  delete-then-create pair would open on the name a user is most likely to be
  resolving.
- **Set path** — identity is the **whole projected record**, paired by
  `aiform.compare.canonical_key`. Records equal on every projected field are
  already correct and produce no call; unmatched `desired` records are `POST`;
  unmatched `current` records are `DELETE`. A `PUT` is used only where a
  desired and a current record match on every field *except* `ttl`, which is
  the one edit worth doing in place rather than as a delete/create pair.

  **Identity is not `(type, name, data)`.** An earlier implementation used
  that, and it silently lost records whenever two legitimately share a `data`
  value while differing elsewhere — which is ordinary, not exotic:

  - **CAA**, the standard Let's Encrypt setup: `issue` and `issuewild` for the
    same CA differ only in `tag`. Adding the second one produced *no* call at
    all (a diff that could never converge); removing it rewrote the **wrong**
    record, leaving two `issue` entries and the `issuewild` still live.
  - **SRV**, the same target on two ports: differs only in `port`. Same
    outcome.

  Matching on the full record makes both fall out correctly with no per-type
  special-casing, and needs no new comparison rule — `canonical_key` already
  exists for exactly this, from `specs/unordered_fields.md`.
- **Order: `PUT`, then `POST`, then `DELETE`.** Updates and additions land
  before removals so the zone is never missing a record it will have again.
  Adding before deleting cannot conflict, because the one case where a
  duplicate would be rejected — a single-valued name — is handled by `PUT`.

**Both conditions on the single-valued path are load-bearing**, and an earlier
draft of this spec had only the second, describing the rule as purely
count-based ("where a group holds exactly one record on each side"). Corrected
after implementation, where the two rules diverged on a real test case:

- **The type condition.** Under a purely count-based rule, a lone `TXT` record
  whose `data` changed would be `PUT`. But for `TXT`/`MX`/`NS`/`SRV`/`CAA` the
  `data` *is* the identity — several values at one name is the ordinary case —
  so changing it means one value removed and another added, not one value
  edited. Worse, the count-based rule makes the same edit behave differently
  depending on unrelated state: adding a second `TXT` record would silently
  change how the first one is updated.
- **The count condition.** Multiple `A` (or `AAAA`) records at one name is
  ordinary round-robin DNS, so the type alone cannot imply single-valued. When
  such a group holds more than one record on either side, it falls to the set
  path. `CNAME` is the only genuinely single-valued type here — a `CNAME` must
  be the only record at its name — but it needs no special case, since a
  correct zone never has two.

`record_id` comes from the live listing, so reconciliation needs the unprojected
records internally even though `read()` returns them projected. That listing
goes through the **same** `fetch_all_pages()` call `read()` uses, with the same
`per_page`: a reconciliation that paged differently from the read that produced
the diff could match against a truncated set and delete records it simply never
saw — the identical failure `specs/digitalocean_pagination.md` exists to
prevent, one layer down.

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
  - **Verified against the live API** by a disposable-zone probe (two zones,
    ~15 calls, no cost, deleted in a `finally`), after two of these shipped
    wrong:
    - `POST`/`PUT` **require** a trailing dot on `data` for `CNAME`/`MX`/`NS`/
      `SRV`/`CAA` (`422 "Data needs to end with a dot (.)"`); `GET` returns it
      **without**. `CAA` belongs in that set — an earlier version of this spec
      omitted it.
    - The auto-created apex `NS` records are `ns1..ns3.digitalocean.com`, no
      trailing dot.
    - `SOA` **is** returned in the records listing, as
      `{type: "SOA", name: "@", data: "1800"}` — so the filter is required, and
      its `data` is the zone TTL rather than a nameserver string.
    - `TXT` `data` is stored **verbatim**, quoted or unquoted — no `TXT`
      normalization anywhere.
    - `"@"` is stored as `"@"`, exempt from the dot rule in both directions.
    - `CAA` `issue` and `issuewild` for the same CA coexist at one name with
      **identical `data`**, differing only in `tag` — which is why
      reconciliation identity must be the whole record.
    - DigitalOcean **silently rectifies** mismatched TTLs within an RRset:
      adding a second `A` at the same name with `ttl: 3600` changed the
      existing record from `1800` to `3600`. Hence the local rejection.
    - Zone names using RFC 2606 reserved TLDs (`.invalid`, `.test`) are
      rejected with a 422, so a probe or system-test zone needs a real TLD.
  - **Verified by the live system test** (`tests/system/test_cli_domain.py`,
    case 6), which is what closed the last gap here. The per-type
    required-field table was previously "recalled and reasoned, NOT verified" —
    the authoring probe exercised the supported types but never enumerated
    each field's necessity. That suite writes one record of *every* supported
    type carrying exactly the fields this table claims it needs and nothing
    more, applies it, and requires the re-plan to be a stable `no-op`. It
    passes, so no "required" field here is in fact optional in a way that
    breaks a write, and none is missing. The same run also confirmed `TXT`
    verbatim storage (embedded quotes included), the dotless round trip for
    every `_FQDN_TYPES` type, and that `AAAA` addresses are **not**
    renormalized from their compact form — the last of which would otherwise
    have produced a permanent phantom diff, since this driver has no `AAAA`
    normalization to absorb one.
  - **On how these got verified at all.** The DigitalOcean token this project
    used had **no `domain` scope** — the probe returned 403 until a broader
    token was issued. That is a prerequisite for the live system test and for
    any real user of this driver, and worth stating in user-facing docs:
    `aiform init`'s credential preflight checks droplet access only, so a
    droplet-scoped token earns a green check and then fails at the first
    domain `apply`.

- **A non-canonical `data` spelling** for `CNAME`/`MX`/`NS`/`SRV`/`CAA` is a
  `ValueError` before any API call, naming the form to write instead. Two
  spellings are rejected, for different reasons:
  - **A written trailing dot** (`data: "mail.example.com."`). DigitalOcean
    requires that dot on the wire but returns the value without it, so `read()`
    yields the dotless form and a user writing the dotted one would diff
    forever. The driver adds the dot itself at the wire boundary.
  - **A bare relative label** (`data: "www"`, no dot at all). DigitalOcean
    rejects this too, with a 422; catching it locally is a clearer error,
    earlier, not a rule of aiform's own invention.

  `"@"` is accepted as the apex and is exempt from both checks — it is sent and
  stored unchanged. See "`data` is written dotless; the driver adds the dot the
  API demands".
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
- **A field of the wrong scalar type**: `ValueError`. `ttl`/`priority`/`port`/
  `weight`/`flags` must be `int` (and **not** `bool`, which is an `int`
  subclass in Python); `type`/`name`/`data`/`tag` must be `str`. Nothing
  upstream enforces `PARAM_SCHEMA`, so `ttl: "1800"` — a perfectly ordinary
  quoted YAML scalar — otherwise reaches the API as a string, and comes back
  from `read()` as the integer `1800`, diffing forever and re-`PUT`ting on
  every apply. Mirrors `compute.py`'s `_reject_malformed_values()`, which
  exists for the same reason. This check also removes an unhashable-value
  crash in the duplicate detection below, which would otherwise raise a bare
  `TypeError` on `data: [...]`.
- **Records sharing `(type, name)` with different `ttl` values**: `ValueError`.
  DNS requires every record in an RRset to share a TTL (RFC 2181 §5.2), and
  DigitalOcean **silently rectifies** a mismatch rather than rejecting it — so
  two `A` records at `@` with different TTLs come back with a value the user
  never wrote, and diff forever. Rejecting locally turns a silent
  non-convergence into an immediate, explainable error. (DigitalOcean's own
  Terraform provider raises a warning diagnostic for the same case.)
- **A user-written apex `NS` record pointing at DigitalOcean's own
  nameservers**: `ValueError`. `read()` filters these out as DO-managed, so a
  user who copies them out of the control panel into their `.aiform.md` gets a
  record that is permanently "missing" — re-`POST`ed on every apply, and on
  the very first `create()` the resulting 422 triggers the zone rollback and
  deletes the zone. Rejecting with a message explaining that DO manages these
  automatically closes a trap that would otherwise look like aiform losing the
  user's records.
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
- **A live system test.** No longer deferred — it exists, as
  `tests/system/test_cli_domain.py`, specced in `specs/system_test_domain.md`
  and built as its own `PROCESS.md` pass after this driver shipped. It is what
  settles the "recalled, not verified" items above — the per-type
  required-field table, the trailing-dot round trip and `TXT` quoting, none of
  which a mock can answer, since the mock encodes the same assumption the
  driver does. The `write_aiform_md()` parallel that entry predicted is
  `write_domain_aiform_md()` in `tests/system/conftest.py`.
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

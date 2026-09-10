# specs/digitalocean_firewall.md — `drivers/digitalocean/firewall.py`

## Purpose

A curated `ResourceDriver` for DigitalOcean cloud firewalls: a named set
of inbound/outbound rules, optionally applied to droplets by id or by
tag.

Every behavioral claim below was established by running
`probes/digitalocean_firewall.py --mutate` against a live account and is
cited to the transcript that established it. Where a claim is recalled
rather than observed, it says so.

## Interface

`PLAN.md` §4's resource module contract, as `aiform/driver.py` fixes it.
Class attributes:

```python
LIKELY_REPLACE_FIELDS = []  # every field is applicable in place; see Behavior
NON_DIFFABLE_FIELDS = []  # read() recovers every managed field
UNORDERED_FIELDS = ["inbound_rules", "outbound_rules", "droplet_ids", "tags"]
```

`PARAM_SCHEMA` accepts exactly `inbound_rules`, `outbound_rules`,
`droplet_ids` and `tags`, with `additionalProperties: False` at both the
top level and inside each rule. `additionalProperties: False` follows
`domain.py`, not `compute.py`'s `True` — issue #108 records that the
permissive case silently drops unknown `params` keys and permanently
defeats the zero-LLM-call guarantee.

Each rule requires `protocol`, `ports`, `action`, and `sources`
(inbound) or `destinations` (outbound). A target object accepts
`addresses`, `droplet_ids`, `tags`, `load_balancer_uids` and
`kubernetes_ids`.

`UNORDERED_FIELDS` is declared as an assumption, not a probe result: a
collection's order-stability is a property a single live observation
cannot establish, so it is reasoned about rather than measured
(`specs/unordered_fields.md`, "Why there is no live ordering probe").

## Behavior

- **`create()`** POSTs the whole firewall in one call, then **polls
  until DigitalOcean has actually applied the rules** before returning
  `read()`'s projection. An unattached firewall is `"succeeded"` in the
  create response itself (`02-create-unattached-firewall...`), so the
  common case returns on the first read having slept not at all. An
  attached one is `"waiting"`, with a `pending_changes` entry per
  droplet, for tens of seconds (`attach/02-`, `attach/04-`).

  Returning at that point would report success about a firewall that is
  not yet filtering anything. State would still converge — `read()`
  drops both fields — so this is not about diffs: it is that for a
  *firewall*, "aiform said done" must not precede "the rules are in
  force", because the user's next action assumes protection that does
  not exist yet. `status: "failed"` raises at once rather than waiting
  out the ceiling; exhausting it raises too, saying the firewall exists
  but may not be filtering.
- **`read()`** GETs the firewall and returns
  `{"id", "name", "inbound_rules", "outbound_rules", "droplet_ids",
  "tags"}`. A 404 raises `ResourceNotFoundError`.
  `status`, `created_at` and `pending_changes` are dropped: they are
  server-set and churn, so storing them would rewrite `state.json` on
  every refresh — the same reason `domain.py` excludes `zone_file`.
  `name` is kept even though it is not a `PARAM_SCHEMA` key, because
  `update()`'s whole-object PUT requires it and `update()` is handed only
  `id`, `current` and `desired`. Carrying it in `current` costs nothing:
  it is stable (a rename is not exposed), and `planner.diff_attributes()`
  iterates `desired` only, so an extra key in `read()`'s output can never
  produce a diff. The alternative — an extra GET inside `update()` — buys
  nothing.
- **`update()`** is a **single `PUT`**, verified to be a whole-object
  replace: a firewall created carrying a tag (`24-`), PUT with the
  `tags` key omitted (`25-`), reads back with `tags: []` (`26-`). Note
  this is established for `tags` only. The same is *inferred* for
  `droplet_ids` -- one PUT, one replace semantics -- but never observed,
  because no probe attaches a droplet, so every transcript carries
  `droplet_ids: []` on both sides of the call.

  An
  earlier probe appeared to show this but could not: it edited a
  firewall whose tags were already empty, so "reset" and "left alone"
  were indistinguishable. That is the same mistake
  `specs/digitalocean_domain.md` §"How this was gotten wrong twice"
  describes -- a confidence label attached to an unchecked belief -- and
  it was caught in review, not by the probe. One atomic call
  makes `driver.py`'s ordering invariant — never raise
  `DriverUpdateNotSupported` after mutating anything — trivially true,
  because validation and any refusal happen strictly before the single
  mutation. A half-applied firewall is therefore structurally
  impossible. `domain.py` reconciles across many calls only because
  DigitalOcean has no whole-zone PUT; a firewall has one, so the
  `/rules`, `/tags` and `/droplets` sub-endpoints are not used.
  Because an omitted key is reset rather than preserved, **every**
  managed field is required in `params` and sent on every write: an
  omitted key is invisible to `planner.diff_attributes()` (it iterates
  `desired` only), so allowing omission would let an unrelated edit
  silently clear `tags` or detach every droplet, with no plan line
  naming it.
- **`update()` never raises `DriverUpdateNotSupported`.** Every
  `PARAM_SCHEMA` field is expressible in the PUT body, and `name` is
  aiform's state key so it never reaches `update()` as a diff. (A rename
  *is* accepted by the API — `14-can-a-firewall-be-renamed...` — but is
  deliberately not exposed.)
- **`delete()`** DELETEs and treats 404 as success
  (`30-`/`31-delete-the-same-firewall-again`: 204 then 404).

### Values the driver rejects rather than normalizes

`aiform/planner.py`'s `diff_attributes()` compares `read()`'s output
against the user's raw `params` with no hook for a driver to normalize
either side. DigitalOcean silently rewrites several inputs on store, so
each of these would otherwise produce a **permanent phantom diff** — the
user writes one thing, `read()` forever returns another. Following
`domain.py`'s trailing-dot precedent, each is rejected with the storable
spelling named in the error, so a user is never left holding a value
that can never converge:

| Written | DigitalOcean stores | Transcript |
|---|---|---|
| `ports: 22` (int) | `"22"` (string) | `04-` |
| `protocol: "TCP"` | `"tcp"` | `05-` |
| `ports: "all"` | `"0"` | `10-` |
| `ports` omitted on an icmp rule | `"0"` | `09-` |

The same guard applies **inside** a rule's `sources`/`destinations`:
`droplet_ids` entries must be ints and every other target list must hold
strings. Nothing upstream enforces `PARAM_SCHEMA` — no part of `aiform/`
runs a JSON-schema validator, it is grounding shown to the model — so the
driver's own validation is the only guard, and a string droplet id would
otherwise be coerced on store exactly as an int port is. An empty target
sub-list (`{"addresses": []}`) is refused for the same reason an empty
target object is.

An **unsorted** target list is rejected for a different reason, and it is
not about rewriting on store. `unordered_equal()` is top-level only: a
rule reaches it through `canonical_key()`, which serializes any list
nested inside it positionally (`specs/unordered_fields.md`). So
`UNORDERED_FIELDS` frees the order of `inbound_rules` but not the order
of `sources.addresses` *within* a rule, and DigitalOcean does not promise
to return one as written — its own Terraform provider models all five
target keys as sets. Since `diff_attributes()` reads `params` raw, the
driver cannot sort that side; it requires the written list sorted, naming
the sorted spelling, and `_project_rule()` sorts what it reads back. Both
sides canonical is what makes the comparison converge at all.

Not rejected, because DigitalOcean stores them verbatim: a bare address
`"1.2.3.4"` is **not** expanded to `/32` (`06-`), and an IPv6 range is
returned unchanged (`07-`).

`ports: ""` is rejected as a separate case: it is a `str` and it is not
`"all"`, so every other check passes it through. What DigitalOcean stores
for it is **unprobed** — probe `09` establishes only that an *omitted*
icmp `ports` becomes `"0"`. Rejecting is the conservative reading.

### `action` is required on every rule

DigitalOcean adds `"action": "allow"` to every rule it returns, even
though the field appears nowhere in the published `firewall_rule_base`
schema (`02-`, `03-`). Since rules are compared as whole dicts inside a
list, a rule the user wrote without `action` can never equal the rule
`read()` returns. `action` is therefore **required** in every rule —
the same treatment `domain.py` gives `ttl`, and for the same reason.

## Edge cases / errors

- **At least one rule is required.** A firewall with both rule lists
  empty is rejected by the API with 422 `"must have at least one rule"`
  (`08-`). Validated locally so the failure is a `ValueError` before any
  call, not a 422 after one.
- **Referenced tags must already exist.** Creating a firewall naming an
  unknown tag fails 422 `"tag <name> does not exist"`, both in the
  firewall's own `tags` and inside a rule's `sources.tags` (`19-`,
  `20-`). This driver does **not** auto-create tags. Note this
  contrasts with droplet *creation*, which does auto-create.

  This does **not** settle the open question
  `specs/digitalocean_compute.md` leaves standing. That question is about
  `POST /v2/tags/{name}/resources` — *assigning* an existing tag to a
  droplet — which no probe here touches. Different endpoint, different
  operation. The two findings are related but not the same, and treating
  a near-miss as a confirmation is the failure this whole process exists
  to prevent.
- **Unknown referents 422 with a readable message**: `"droplet does not
  exist"` (`21-`), `"load balancer does not exist"` (`22-`). Folded into
  the raised error by `_fold_do_error_into_exc`, as `domain.py` does.
- **A malformed id 404s**, exactly as a well-formed but absent uuid does
  (`27-`, `28-`) — the same behavior `compute.py` records for a
  malformed droplet id, so `read()` needs no special 422 branch.
- **An empty `sources: {}` is accepted by the API** (202, stored as `{}`
  — `18-`) and produces a rule that matches no traffic. The driver
  rejects it: in a *firewall*, a rule that silently does nothing is a
  security-relevant footgun, and nothing legitimate needs it. See Open
  questions.
- **No `DriverUpdateNotSupported` classification.** `PLAN.md` §10's
  HTTPError entry asks the `(400, 422)` "genuinely rejected vs.
  transient" allowlist to generalize once a second or third driver
  exists. This is the third — and it **narrows** the entry rather than
  extending it: since `update()` provably never raises
  `DriverUpdateNotSupported`, this driver needs no rejected-vs-transient
  classification at all, and every `HTTPError` propagates as a genuine
  error. That is a third data point for whatever shared mechanism the
  entry eventually designs, not a silent divergence from it.

## Out of scope

- **Attaching to droplets is exercised live, by one test that pays for
  it.** Every probe uses `droplet_ids: []`, and unattached firewalls are
  free and carry no traffic, which is what made this resource safe to
  characterize live at all. But that left `droplet_ids` the one managed
  field with no live evidence behind it, so
  `tests/system/test_cli_firewall.py`'s `TestAttachedToARealDroplet`
  creates the cheapest droplet DigitalOcean sells, attaches, asserts
  convergence, detaches, and destroys it — about two minutes of hourly
  billing. Cleanup is guaranteed twice over: the `throwaway_droplet`
  fixture destroys it in a `finally`, and a session sweep catches one a
  crash left behind.
- **`droplet_ids` can only hold literal integers.** There is no
  cross-resource reference mechanism — `PLAN.md` §10, "No dependency
  graph" — so a firewall cannot say "the droplet aiform just created".
  Note also the type asymmetry: `compute.py` stringifies a droplet id
  (`"id": str(droplet["id"])`) while this API takes and returns
  integers.
- **Pagination is not used.** Rules and `droplet_ids` are embedded in the
  single firewall object rather than being separate paginated
  collections, and `read()` is one GET, so
  `drivers/digitalocean/_common.py`'s `fetch_all_pages` is deliberately
  not reached for here.
- **`AIFORM_MANAGED_TAG`** (`specs/resource_tagging.md`) stays
  unimplemented. Firewalls do carry tags, unlike domains, so this is the
  first resource where that mechanism would apply — but it is its own
  module and its own PR.

## Knowledge-confidence

*Verified live by a disposable-resource probe* — 31 probes, all against
free unattached firewalls (plus one throwaway tag), every created
resource deleted by the session's context manager on the way out;
transcripts in `probes/transcripts/digitalocean_firewall/`:

- create returns `succeeded` immediately when unattached (`02-`)
- `action` is server-added and absent from the published schema (`02-`, `03-`)
- int ports, uppercase protocols, `"all"`, and omitted icmp ports are all
  silently rewritten (`04-`, `05-`, `09-`, `10-`)
- bare addresses and IPv6 ranges are stored verbatim (`06-`, `07-`)
- zero rules is a 422 (`08-`)
- PUT is a whole-object replace: an omitted `tags` key is reset to `[]`,
  demonstrated on a firewall that actually had one (`24-`, `25-`, `26-`)
- tags must pre-exist, in both positions (`19-`, `20-`)
- 404 covers both absent and malformed ids (`27-`, `28-`)
- delete is idempotent: 204 then 404 (`30-`, `31-`)

The `waiting → succeeded` and `pending_changes` transitions for an
**attached** firewall are **observed**, in probe session
`digitalocean_firewall_attach` (7 probes, one throwaway droplet):

- Attaching returns `202` with `"status": "waiting"` and
  `pending_changes: [{"droplet_id": …, "status": "waiting",
  "removing": false}]` (`attach/02-`). The unattached case returns
  `succeeded` immediately (`digitalocean_firewall/02-`), so the two
  differ exactly as recalled.
- Detaching shows the same shape with `"removing": true`
  (`attach/05-`), and the create path and the `update()` path behave
  identically (`attach/06-`).
- **Convergence is slower than it looks.** A read 20s after attaching
  was *still* `waiting` (`attach/04-`); `succeeded` was first seen 20s
  after a later edit (`attach/07-`). Tens of seconds, not a moment, and
  not a figure DigitalOcean promises.

`TestAttachedToARealDroplet` still asserts the *consequence* rather than
the transition — `read()` returns neither field, so a re-plan is a no-op
whatever state DO is in mid-convergence. Pinning the timing would make a
live test flaky against a number nobody guaranteed.

## Resource graph

Edges from `firewall` to other resource kinds, as observed:

| Field | → kind | Id type | Must pre-exist? | Evidence |
|---|---|---|---|---|
| `tags` | tag | name string | **yes** — 422 | `19-` |
| `sources.tags` / `destinations.tags` | tag | name string | **yes** — 422 | `20-` |
| `droplet_ids`, `sources.droplet_ids` | droplet | **integer** | **yes** — 422 | `21-` |
| `sources.load_balancer_uids` | load balancer | uuid string | **yes** — 422 | `22-` |
| `sources.kubernetes_ids` | k8s cluster | uuid string | assumed yes | not probed |

Every edge that was probed requires its referent to already exist, which
makes a firewall a pure *consumer* of other resources — it is a leaf in
any future dependency graph, never a thing others point at. Whether a
reference silently shrinks when its referent is deleted is **not yet
probed**; doing so needs a disposable droplet or tag and is worth adding.

### `apply` returns before the rules are in force

`create()` and `update()` return as soon as DigitalOcean accepts the
write, which — for an **attached** firewall — is tens of seconds before
`status` reaches `succeeded` (`attach/02-`, `attach/04-`). Convergence
of aiform's *state* is unaffected, and deliberately so: `read()` drops
`status` and `pending_changes`, so a re-plan is a no-op either way.

But "aiform reported success" and "this firewall is filtering traffic"
are not the same instant, and for a firewall specifically that gap has a
direction that matters: rules a user believes are protecting a droplet
are not yet protecting it. Nothing here establishes how long that window
is in general, or what a droplet does with traffic during it — no probe
in this repo sends any.

**Decided: the driver polls.** `create()` and `update()` wait for
`status: "succeeded"` with `pending_changes` empty before returning, so
`apply` cannot report done while a firewall is still being applied. The
feared cost does not materialise for the common case — an unattached
firewall is already `succeeded` on the first read, so the loop returns
on attempt 1 having never slept — and `PLAN.md` §5's zero-LLM-call
short-circuit is about `plan`, not `apply`, so the cost of waiting is
wall clock rather than tokens. Progress is logged rather than silent.

## Open questions

- **`action: "deny"` is accepted and stored** (202, echoed back as
  `"deny"` — `12-`), even though DigitalOcean documents cloud firewalls
  as allow-only with an implicit default deny. Whether a stored `deny`
  rule is actually *enforced*, ignored, or means something else is
  unverified, and could not be settled without attaching to a live
  droplet and generating traffic. The driver round-trips faithfully what
  the API accepts rather than second-guessing it; a user writing `deny`
  should not assume it does anything. **Flagged for a human decision:**
  restrict the enum to `allow` until enforcement is established.
- **Rejecting empty `sources: {}` is a judgment call**, not an API
  constraint — DigitalOcean accepts it. Worth confirming the stricter
  behavior is wanted.

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

- **`create()`** POSTs the whole firewall in one call and returns
  `read()`'s projection. It does **not** poll. An unattached firewall
  comes back `"status": "succeeded"` in the create response itself
  (`02-create-unattached-firewall...`), so there is nothing to converge —
  unlike `compute.py`, whose droplet is genuinely `new` for a while.
  A firewall attached to droplets does pass through `waiting`; aiform
  never creates one attached (see Out of scope), so that path is
  unexercised — recalled, not verified.
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

Not rejected, because DigitalOcean stores them verbatim: a bare address
`"1.2.3.4"` is **not** expanded to `/32` (`06-`), and an IPv6 range is
returned unchanged (`07-`).

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
  contradicts droplet creation, which does auto-create — and it closes
  the open question `specs/digitalocean_compute.md` left standing
  ("the tag-must-exist-first behavior is read off DigitalOcean's
  published OpenAPI responses, not observed live"). It is now observed.
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

- **Attaching to droplets is expressible but never exercised live.**
  `droplet_ids` is in `PARAM_SCHEMA` and goes out in the PUT/POST body
  like any other field, but every probe uses `droplet_ids: []`. (There is
  no firewall system test yet — `tests/system/` covers droplets and
  domains only. Both prior drivers shipped one, and the domain spec
  credits the live path with catching what unit tests could not, so this
  is a real gap, tracked as its own follow-up rather than silently
  omitted.) Unattached firewalls are free and carry no traffic,
  which is what makes this resource safe to characterize live at all.
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

*Recalled, not verified* — the `waiting → succeeded` and
`pending_changes` transitions for an **attached** firewall. Only
reachable by attaching to a real droplet, which this work deliberately
never does. Mitigated by `read()` not returning `status` or
`pending_changes` at all, so a mid-`waiting` read produces no diff
either way.

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

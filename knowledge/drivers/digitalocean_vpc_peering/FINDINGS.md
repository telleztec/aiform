# digitalocean_vpc_peering — findings

Each cites the transcript that established it, in
`probes/transcripts/digitalocean_vpc_peering/`. Promotion into `knowledge/` needs
a **second** observation (`specs/driver_creation.md`, "How the loop learns").

No peering driver exists and none is being written. This session was run to test
whether `specs/resource_dependencies.md`'s model can order a resource whose whole
content is a relationship between two others — a shape the driver set has no
example of. **It can**, with no new concept; see "What a peering is" below. The
findings worth keeping turned out to be the mundane provider facts rather than the
modelling ones.

Free — VPCs and peerings cost nothing and no droplet was created, so this session
is cheap to re-run.

**Status: knowledge only.** By the repo owner's decision, nothing here enters
`specs/resource_dependencies.md` until there are clear use cases and requirements
for peering. These are observations about a provider, not a proposal. A future
session that identifies the use cases is what promotes them.

## The headline: two relationship kinds on one resource, two different statuses

A VPC refuses deletion for two different reasons, and **the reasons are
distinguishable only by reading the message**:

| Blocker | Status | `id` | `message` | Session |
|---|---|---|---|---|
| a member droplet | `409` | `conflict` | `Can not delete VPC with members` | `digitalocean_vpc_member/10` |
| a peering | **`412`** | **`unknown`** | `Can not delete VPC with peerings` | `10` |

Three consequences, and the first is the one that matters:

1. **A referential refusal cannot be recognised by status code**, not even within
   one provider and one resource type. Code matching on `409` would miss the
   peering case entirely. Anything that wants to pre-empt or explain these has to
   match the message, which is a far weaker contract.
2. **DigitalOcean's documentation says `403`** for VPC deletion refusals
   (`vpcs_delete`, quoted in `digitalocean_vpc_member/FINDINGS.md`). Observed: `409`
   in one case and `412` in another. The documented status is wrong in **two
   different ways**, which retires "the documented signature tells you the status"
   as a usable assumption for this provider.
3. **The error `id` degrades**: `conflict` is a usable handle, `unknown` is not.
   So the more specific refusal carries the less specific machine-readable field.

I predicted `409` on the strength of the members case. Wrong, and wrong in the
useful direction: had it matched, the session would have supported a false
generalisation.

## Resource-specific (stay here)

- **Both VPCs must pre-exist** (`04`):
  `404 {"id": "not_found", "message": "VPC ID 00000000-… not found"}`. Consistent
  with every other probed DigitalOcean edge — `droplet.vpc_uuid` 404s,
  `firewall.droplet_ids` 422s. A peering cannot be created half-dangling.

- **`vpc_ids` comes back reordered** (`05` vs `08`). Sent
  `[b07339a1…, 9cf0a263…]`; returned `[9cf0a263…, b07339a1…]`. So a future driver
  **must** put `vpc_ids` in `UNORDERED_FIELDS`, or `diff_attributes()` reports a
  phantom diff on every re-plan and the resource never converges — exactly the trap
  `specs/unordered_fields.md` exists for. This is not a stability assumption: the
  reorder was observed on the one call that could show it.

- **The relationship is readable from the VPC side** (`09`).
  `GET /v2/vpcs/{id}/peerings` returns the peering. So for peering DigitalOcean
  answers **both** directions — unlike a firewall, where membership lives on the
  firewall and a droplet cannot be asked what protects it
  (`digitalocean_vpc/FINDINGS.md`). Second data point that reverse-edge
  readability is a property of the **relationship kind**, not of the provider.

- **The lifecycle is asynchronous at both ends, and there is no terminal status**
  (`05`, `11`). Create returns `202` with `status: PROVISIONING`, reaching `ACTIVE`
  on the first poll. Delete returns `202` with `status: DELETING`, and the resource
  then **404s** rather than settling into a `DELETED` status. This session's poll
  waited for `DELETED` and never saw it — 24 polls of `404`. A driver must poll for
  `404`, not for a terminal state.

## Promotion candidates

| Finding | Would become | Evidence | Held because |
|---|---|---|---|
| A referential-integrity refusal cannot be identified by HTTP status; the status varies by relationship kind within one resource | `csp/digitalocean/refusal-status-varies-by-kind` | `10`, plus `digitalocean_vpc_member/10` | **Has its two observations.** Held only because `knowledge/` is not built (`scripts/knowledge.py` has no implementation) |
| A documented error status is not evidence about the actual status | `probing/documented-status-is-not-evidence` | `10`, plus `digitalocean_vpc_member/10` | Also has two observations now, and they are independent: `403` documented, `409` and `412` observed |
| A collection returned by the provider may be reordered relative to the request, so any such field needs `UNORDERED_FIELDS` | `driver/provider-reorders-collections` | `05`/`08` | Needs a second **provider** for a `driver/` claim; `firewall.droplet_ids` is same-provider |
| Deletion completes as a `404` rather than a terminal status | `csp/digitalocean/delete-ends-in-404` | `11` | Needs a second DigitalOcean resource family with an async delete. Droplet delete behaves this way too (`digitalocean_vpc/09`) but was not probed *as* this question |

## What a peering is, relative to its two VPCs

The session's reason for existing. Written after the repo owner pushed back on a
first draft of this section that overreached — the corrections are marked, because
the overreach is more instructive than the conclusion.

**It depends on both VPCs, and that is nearly the whole story for provisioning.**
Both must pre-exist (`04`), and neither can be deleted while it exists (`10`). The
edges are `peering → VPC A` and `peering → VPC B`: both directed, both toward
targets that already exist, no cycle. A topological sort gives A, B, then the
peering; the reverse gives the peering first — **and the `412` proves that reverse
order is required rather than merely tidy.**

So the existing Phase 1 machinery orders this correctly with no new concept. That
is the useful finding: a resource shape that looked novel turns out to be a
resource with two dependencies.

**Retracted: that the symmetry breaks the DAG.** A first draft argued that because
`vpc_ids: [a, b]` means what `[b, a]` means, a symmetric relationship "has no
natural representation" in a directed graph, and cited
`specs/resource_dependencies.md`'s open "one graph, or two?" question. That is
wrong. The symmetry is between the two **VPCs** — a fact about what a peering
*means*. It never enters `depends_on`, where the only edges are peering→VPC, and
those are ordinary directed ones. The provisioning graph is a plain DAG.

What the symmetry does cost is concrete and small: the provider reorders `vpc_ids`
(`05` vs `08`), so a driver needs it in `UNORDERED_FIELDS`. That is the whole
consequence.

**Demoted to a note: that a peering is a "reified edge" with its own lifecycle.**
True — a peering can be created and destroyed without touching either VPC, which no
other relationship in the driver set allows, since a droplet cannot change VPC
without changing the droplet. But it changes *nothing* about create or destroy
ordering, which is what the model is for. Recorded as an observation, not as a
model concept.

**Demoted to a note: the operational fan-out.** Every member of A can reach every
member of B because the peering exists, so destroying it stops traffic for resources
whose configuration never mentions it. Real, and it is the already-deferred UC-D
blast-radius case. Peering makes the radius larger without changing whether that
capability gets built.

**Not aiform's to model: the `ip_range` non-overlap requirement.** Two peered VPCs
must not have overlapping ranges. DigitalOcean enforces it, so an apply fails
loudly; there is nothing `aiform` can add by re-checking a predicate the provider
already owns.

**Cross-account peering needs no new concept either.** DigitalOcean permits a
peering between VPCs in two different accounts, and the owner's point is that
`aiform` has nothing to model that with. Correct — and
`specs/resource_dependencies.md`'s Scope section already says so: *"Cross-deployment
orchestration is not a deferred item; it is not a thing this model has."* A peer in
another account is outside the deployment, so `_resolve_dependency_edges()` raises
`PlanBlockedError` — "neither a file in this run nor a resource tracked in state."
Refusing to model what it cannot see is the right answer. So this is an **instance
of an existing declared boundary**, not a gap.

## Not probed

- **Cross-account peering.** DigitalOcean permits a peering between VPCs in two
  different accounts, which breaks the assumption that every edge endpoint lives in
  one state file — see **#201**. Scoped out by the repo owner; this session has one
  account.
- **Peering two VPCs with overlapping ranges.** A compatibility constraint rather
  than a dependency, and provoking it needs a deliberately colliding VPC, which
  DigitalOcean may refuse at VPC-create time instead — a different finding that
  would need its own session to attribute correctly.
- **Cross-region peering.** Both VPCs here were `sfo3`. Whether a peering may span
  regions is unknown, and it matters: a peering that spans regions is the first
  resource in the driver set not bound to one.
- **Whether a peering can be updated.** Only create and delete were exercised. If
  `vpc_ids` is immutable, a driver's `update()` must raise
  `DriverUpdateNotSupported` rather than attempt one.
- **Peering a VPC with itself**, and peering the same pair twice. Both are
  plausible 4xx cases and neither was asked.

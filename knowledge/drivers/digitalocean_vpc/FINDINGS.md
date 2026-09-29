# digitalocean_vpc — findings

Each cites the transcript that established it, in
`probes/transcripts/digitalocean_vpc/`. Promotion into `knowledge/` needs a
**second** observation — one is an anecdote — and what counts as the second
depends on the category (`specs/driver_creation.md`, "How the loop learns").

**No VPC driver exists and none is being written.** This session was run to
settle dependency *edges* for `specs/resource_dependencies.md`'s dependency
model, which `specs/driver_creation.md` licenses explicitly: *"Probe dependency
edges — a relationship between resources the documentation does not state, or
that the implementation does not make obvious."* Every edge in the current
driver set is firewall→droplet, which survives its target; a VPC is the
aligned case, and a model validated only against the survivable one is
validated against the example that misleads.

This session's step 07 was **misdesigned** and its transcripts are kept for
that reason — see the last resource-specific finding. `digitalocean_vpc_member`
is the corrected session.

## Promotion candidates

| Finding | Would become | Evidence | Held because |
|---|---|---|---|
| An edge is not readable from live state until the dependent converges — a just-created resource does not yet report the parent it was created into | `csp/digitalocean/edges-lag-creation` | `05`, `06` | `csp/` claim — needs a second DigitalOcean resource family with a parent reference; `firewall.droplet_ids` is set by the *dependent* at create time so it does not test the same thing |
| A provider may hold a reference to a resource it knows is deleted, and report the holder as fully settled | `csp/digitalocean/stale-reference-reports-healthy` | `13` | `csp/` claim — needs a second reference-holding resource family. `sources.droplet_ids` inside a rule is the obvious second case and was not probed |
| The reverse-edge query ("what points at me") is answerable from the parent for some resource kinds and only from the child for others | `driver/reverse-edge-asymmetry` | `06`, `13` | `driver/` claim — needs a second **provider**, so blocked until a non-DigitalOcean driver exists |

## Resource-specific (stay here)

- **A droplet naming a nonexistent `vpc_uuid` is refused `404 not found`**
  (`01`) — it is not auto-created, and the droplet is not created either. So
  `droplet.vpc_uuid` is an implied edge of the same shape as
  `firewall.droplet_ids`: the target must pre-exist. Contrast `tags`, where
  droplet creation *does* auto-create — a compute-side fact recorded in
  `specs/digitalocean_compute.md`, not in the firewall probes, which established
  the firewall side (a firewall's `tags` 422 on a tag that does not exist).
  Either way, "must the referent pre-exist" cannot be answered once for a
  provider and reused.

- **`GET /v2/vpcs/{id}/members` exists and answers `200`** (`03`, `06`) — so
  for a VPC the provider itself answers "what depends on me". A firewall's
  membership lives on the *firewall* (`droplet_ids`), so a droplet cannot be
  asked what protects it. The same logical relationship is readable from
  opposite ends depending on resource kind.

- **A member is identified by URN, not by the id the forward edge uses**
  (`digitalocean_vpc_member/09`, recorded here because it bears on this
  session's `06`). The member entry carries
  `"urn": "do:droplet:89a3a30d-…"` — a UUID unrelated to the droplet's
  numeric id. So the same droplet has three identifiers: the int
  `604558243` that `droplet_ids` takes, the string `"604558243"` that
  `StateEntry.id` holds, and a URN for VPC membership. **A provider's
  reverse-edge answer therefore cannot be joined to aiform's state without
  an identifier aiform does not record.**

- **A firewall keeps a deleted droplet's id in `droplet_ids`, and reports
  itself as converged** (`13`). After `DELETE /v2/droplets/{id}` returned
  204 and a subsequent `GET` returned 404, the firewall read back:

  ```
  droplet_ids:     [604557432]
  status:          "succeeded"
  pending_changes: []
  ```

  **Verified, and narrower than a first reading of it.** What `13` establishes
  is that the dead id is listed *while the firewall reports itself converged* —
  so the firewall's own status is useless as a signal that a member is gone,
  since `succeeded` with empty `pending_changes` is exactly what a healthy
  firewall reports. That much is solid and is what the driver has to cope with.

  **It does not establish that the entry is permanent, and the timing is why.**
  The read at `13` is **12 seconds** after the `DELETE` at `09`
  (`04:46:07.1` vs `04:45:55.0`). The droplet never reached `active` — created
  `04:45:51`, still `new` at `05`, deleted at `:55` — and `08` shows the
  firewall created at `:54` with `status: "waiting"` and a **pending change to
  add that same droplet**, `removing: False`. So `13` caught that pending attach
  resolving with the id retained, not a settled steady state.

  **This is step 07's defect in a different place** — the session diagnosed a
  raced convergence there and then read `13` the same way. Whether DigitalOcean
  reaps a dead id on a slower sweep is **unknown**: `inferred` at best, and the
  probe that would settle it is below.

- **Step 07 was misdesigned, and the failure is itself the finding.** It
  deleted the probe VPC while a droplet created into it was notionally
  inside, and got `204`, which read as a refutation of the owner-reported
  "a VPC can only be deleted if it has no member resources attached". It was
  not: `05` shows the droplet at `status: "new"` with **no `vpc_uuid` key at
  all** — absent, not null; the `None` in the console output was the probe's own
  `.get()` — and
  `06` shows `members: []`, `total: 0`. The VPC was empty as far as
  DigitalOcean was concerned. **A probe that races convergence measures the
  race, not the rule** — the corrected session waits and gets `409`.

## Not probed, deliberately

- **Whether a stale `droplet_ids` entry is permanent.** The session's own read at
  `13` cannot answer it, for the timing reasons above. The probe that would:
  create a droplet and **wait for `active`**; attach a firewall and **wait for
  its `status` to reach `succeeded` with `pending_changes: []` on a live
  member**, so the baseline is a genuinely converged firewall; `DELETE` the
  droplet and poll it to 404; then read the firewall **immediately, at one
  minute, and at ten**, recording each. Three recorded reads separate "the id
  survives convergence" from "the id survives indefinitely", and only the second
  supports the word *permanent*. Cheap — one droplet of the smallest size, one
  firewall, both free or near-free — and it is the read `#232` rests on.
- **Whether a `default` VPC can be deleted.** DigitalOcean documents that it
  cannot — its `vpcs_delete` description says *"the default VPC for a region can
  not be deleted"* — and a passing result would be a destroyed region default,
  which is not a finding worth the blast radius. Recorded as documented and
  unprobed rather than attempted. Note `digitalocean_vpc_member` showed the
  documented *status* for the sibling case to be wrong, so the documentation is
  not evidence about what this would return.
- **What happens to a droplet whose VPC is deleted mid-provisioning.** Step 07
  created exactly that situation and the session did not ask; the droplet went
  on to delete normally at `09`. Whether it fell back to the region default or
  kept a dangling `vpc_uuid` is unknown.
- **How a region's default VPC comes to exist.** Recorded as **owner-reported and
  consistent with observation**, after this file first guessed, then over-corrected
  to "unknown", then landed here.

  The account: *DigitalOcean generates a default VPC named `default-<region slug>`
  the first time resources are used or provisioned in that region.*
  Owner-reported, and consistent with three things observed independently — the
  naming convention (`default-nyc3`, `default-sfo3`), DigitalOcean's own "all
  applicable resources are placed into the default VPC network unless otherwise
  specified", and `digitalocean_vpc_default`'s finding that an unplaced droplet
  lands in the default.

  **The `created_at` timestamps are not usable evidence, in either direction, and
  two earlier drafts of this bullet used them both ways.** The first cited them as
  support ("the dates match when droplets were first made in those regions"); the
  second cited them as refutation ("`default-sfo3` postdates this repo's earliest
  `sfo3` droplet by 51 minutes"). Both were unsound for one reason: **this token
  has pointed at more than one DigitalOcean team**, each with its own per-region
  default, and no archived droplet records which team it was created against.
  System-test logs show `sfo3` droplets from `2026-08-20`, weeks before either
  VPC's `created_at`, so the dates cannot be reconciled without team creation dates
  nobody has.

  A related confusion is dissolved by the same fact. An earlier draft flagged
  `b57810f7-ac82-4428-906d-baca2a95553d` — named by droplet `589098829`,
  `telleztec-wordpress` — as a mysterious **third** VPC. It is not: it is the
  production team's own `default-sfo3`, owner-reported. There were never three
  VPCs; there were two teams, each with a per-region default. See
  `knowledge/drivers/digitalocean_vpc_default/FINDINGS.md` for why that makes
  `aiform`'s `provider.resource_type.name` key ambiguous across teams.

  Do not re-derive a timeline from those dates. Either take the owner-reported
  mechanism, or settle it the one clean way: **provision a resource into a region
  that currently has no VPC and watch what appears.** That permanently adds a
  default to a third region, so it is a deliberate decision rather than a side
  effect of a probe.

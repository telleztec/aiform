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
  droplet creation *does* auto-create (`digitalocean_firewall` probes `19`,
  `20`), which is why "must the referent pre-exist" cannot be answered once
  for a provider and reused.

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

  This settles the question `specs/digitalocean_firewall.md` carried as **not
  yet probed** and regrades it `verified`. Two consequences: DigitalOcean does
  **not** self-heal a stale reference, so there is real repair work for the
  issue that proposes doing it; and the firewall's own status is useless as a
  signal, since `succeeded` with empty `pending_changes` is exactly what a
  healthy firewall reports.

- **Step 07 was misdesigned, and the failure is itself the finding.** It
  deleted the probe VPC while a droplet created into it was notionally
  inside, and got `204`, which read as a refutation of the owner-reported
  "a VPC can only be deleted if it has no member resources attached". It was
  not: `05` shows the droplet at `status: "new"` with `vpc_uuid: None`, and
  `06` shows `members: []`, `total: 0`. The VPC was empty as far as
  DigitalOcean was concerned. **A probe that races convergence measures the
  race, not the rule** — the corrected session waits and gets `409`.

## Not probed, deliberately

- **Whether a `default` VPC can be deleted.** A passing result is a destroyed
  region default, which is not a finding worth the blast radius. Recorded as
  unprobed rather than attempted.
- **What happens to a droplet whose VPC is deleted mid-provisioning.** Step 07
  created exactly that situation and the session did not ask; the droplet went
  on to delete normally at `09`. Whether it fell back to the region default or
  kept a dangling `vpc_uuid` is unknown.
- **Whether two default VPCs observed at recon time were provider-created.**
  `default-nyc3` and `default-sfo3` both carry `default: true` and
  `created_at` dates matching when droplets were first made in those regions,
  which is suggestive, not evidence. Nothing in this session created them and
  nothing probed how they came to exist.

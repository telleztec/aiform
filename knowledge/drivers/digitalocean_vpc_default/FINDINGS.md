# digitalocean_vpc_default — findings

Each cites the transcript that established it, in
`probes/transcripts/digitalocean_vpc_default/`. Promotion into `knowledge/`
needs a **second** observation (`specs/driver_creation.md`, "How the loop
learns").

One question the other two VPC sessions could not answer, because both created
their droplets with an explicit `vpc_uuid`: **where does a droplet go when
nobody says which VPC?** It matters because
`drivers/digitalocean/compute.py` never sends `vpc_uuid` — it is absent from
`PARAM_SCHEMA` and from the create body — so this is what every droplet
`aiform` has ever created did.

## Resource-specific (stay here)

The region default is what `specs/resource_dependencies.md` calls an **intrinsic
resource** — one the provider creates on the user's behalf, that no configuration
requested and that cannot be deleted.

- **A droplet created with no `vpc_uuid` lands in the region's `default: true`
  VPC** (`01`, `06`, `07`). The create body was exactly `compute.py`'s shape:
  `name`, `region`, `size`, `image`, `backups`, `monitoring`, `tags`, and no VPC
  field. The droplet converged reporting
  `vpc_uuid: 850b2e38-568b-4071-9eaf-0997a8edb547`, which `01` had already
  identified as `default-sfo3`. Confirmed from both ends: `07` shows the default
  VPC listing the droplet as a member.

  This regrades `specs/resource_dependencies.md`'s claim that `aiform`'s
  droplets sit in a VPC they never named from **inferred** — it had rested on
  DigitalOcean's "all applicable resources are placed into the default VPC
  network unless otherwise specified" plus the absence of the field in our code
  — to **verified**.

- **`vpc_uuid` appears before the droplet is `active`, and is absent rather
  than null before that.** Polls, 15s apart: `new` with no key, then `new` with
  the key set, then `active`. Consistent with `digitalocean_vpc_member`'s
  observation of the same sequence for an explicitly-placed droplet, so the
  lag is a property of droplet creation rather than of VPC assignment.
  **These polls were `record=False` and left no transcript**, so nothing here
  may be graded `verified` on their strength.

- **The member entry identifies the droplet by URN again** (`07`):
  `do:droplet:2b489d41-1bde-4bd1-bbcc-ecd8d2a47df7`, while the droplet's own id
  was `604721972`. Second independent observation of the namespace mismatch
  first seen in `digitalocean_vpc_member/09` — see the promotion note below.

- **A region's default VPC name is not unique across teams, and collides with
  certainty.** Owner-reported and corroborated by this repo's own archive: the
  owner states WordPress lives in `default-sfo3`, and
  `digitalocean_compute_monitoring`'s transcript shows droplet `589098829`
  (`telleztec-wordpress`) reporting
  `vpc_uuid: b57810f7-ac82-4428-906d-baca2a95553d`. The VPC named `default-sfo3`
  on the token's *current* team is `850b2e38-568b-4071-9eaf-0997a8edb547`. So two
  distinct VPCs share the name `default-sfo3`, one per team.

  This is not an ordinary name clash. Every team gets a `default-<region>`, so the
  collision is **guaranteed** rather than possible. And `aiform`'s resource key is
  `provider.resource_type.name` (`models.py`'s `parse_dependency_key`) with no
  account or team component — `grep` finds no notion of either in `models.py` or
  `state.py`. A future `network` driver would therefore key the owner's production
  and development VPCs identically as
  `digitalocean.network.default-sfo3`.

  Related to **#201**, which frames deployment identity around a destroy-all run
  in the wrong directory. This is the same gap reached from the provider side, and
  it is sharper: the ambiguity is in the key itself rather than in which state file
  is loaded, and this project has *already* repointed its token from one team to
  another mid-flight. Worth naming on #201 rather than filing again.

## Promotion candidates

| Finding | Would become | Evidence | Held because |
|---|---|---|---|
| A provider silently places a resource into a container the request never named, and that container cannot be deleted | `csp/digitalocean/implicit-containment` | `06`, `07` | `csp/` claim — needs a second DigitalOcean resource family that acquires a container it did not ask for. Load balancers and managed databases are the obvious candidates and neither has a driver |
| The reverse-edge query answers in a URN namespace the forward edge never uses | `csp/digitalocean/member-urn-mismatch` | `07`, plus `digitalocean_vpc_member/09` | **This now has its two observations** and is the strongest promotion candidate across the three VPC sessions. Held only because `knowledge/` itself is not built (`scripts/knowledge.py` has no implementation) |

## What this does and does not settle about the defaults' origin

It settles that the region default is the **sink** for resources that do not
name a VPC. It does **not** settle how the two `default: true` VPCs on this
team came to exist.

It does, however, dissolve one part of the earlier confusion.
`knowledge/drivers/digitalocean_vpc/FINDINGS.md` recorded that an archived droplet
named a "third VPC the current token cannot see" and treated that as deepening the
mystery. It does not: `b57810f7` is the **production team's** `default-sfo3`, per
the owner. There was never a third VPC — there were two teams, each with its own
per-region default, which is exactly what DigitalOcean's model produces.

Two things narrow it, neither conclusive:

- The repo owner reports there is **no way to delete them**, which matches
  DigitalOcean's documented *"the default VPC for a region can not be
  deleted"*.
- DigitalOcean's "How to Set a Default VPC" page says every region containing
  resources *has* a default but never says how one arises.

So a default is undeletable and is where unnamed resources go. Whether it is
created by the provider on first use, or is simply the first VPC a user made
being promoted and then locked, is untested. **Probing it means creating a
resource in a region that has no VPC at all** and watching what appears —
cheap, and worth doing before any VPC driver is written, but it permanently
adds a default to a third region and so should be a deliberate decision rather
than a side effect.

## Not probed

- Whether the assignment can be changed after creation (DigitalOcean documents
  a VPC move for some resource types; no probe here).
- Whether a non-default VPC in the same region is ever chosen without being
  named. One observation says the default wins; nothing tests a tie.

# digitalocean_vpc_member — findings

Each cites the transcript that established it, in
`probes/transcripts/digitalocean_vpc_member/`. Promotion into `knowledge/`
needs a **second** observation (`specs/driver_creation.md`, "How the loop
learns").

This session exists because `digitalocean_vpc`'s step 07 raced convergence and
so measured the race rather than the rule. It is separate rather than appended
because a re-run restarts the sequence at 01 and would overwrite that
session's transcripts, which carry the race finding —
`digitalocean_firewall_attach.py` sets the same precedent against
`digitalocean_firewall.py`.

One question, asked properly: **does DigitalOcean refuse to delete a VPC that
still has a live member?** Owner-reported as yes. Now verified.

## Promotion candidates

| Finding | Would become | Evidence | Held because |
|---|---|---|---|
| A provider enforces referential integrity on delete with `409 conflict`, not `403` — so "the provider will refuse" is not a licence to guess the status | `csp/digitalocean/referential-delete-is-409` | `10`, `14` | `csp/` claim — needs a second DigitalOcean resource family that refuses a delete for the same reason; no other in-repo resource has a member collection |
| A parent-child edge becomes visible in the API **before** the child reports itself ready, so "wait for active" is stricter than the edge requires | `csp/digitalocean/membership-precedes-active` | polls 1-3 | `csp/` claim — one resource family, one run. Timing findings are also the class `specs/driver_creation.md` warns against over-reading |

## Resource-specific (stay here)

- **The empty requirement holds, and the refusal is `409`** (`10`):

  ```
  DELETE /v2/vpcs/{id}  ->  409
  {"id": "conflict", "message": "Can not delete VPC with members"}
  ```

  with a converged droplet inside. The same call after the droplet was
  destroyed returned `204` (`14`), so the refusal is about membership and not
  about the VPC. **This is the first verified existentially-coupled edge in
  the repo** — every other edge in the driver set survives its target.

  My prediction was `403`. It is `409`, and the difference matters: `409
  conflict` is a state conflict, which is the honest shape for "this is
  refusable now and permitted later", whereas `403` would have implied a
  permissions problem. A design that pre-empts this refusal has to match on
  something; the status alone is now known rather than assumed.

- **Membership becomes visible before the droplet is active** (polls 1-3, 15s
  apart):

  | poll | droplet `status` | droplet `vpc_uuid` | VPC `members` |
  |---|---|---|---|
  | 1 | `new` | `None` | 0 |
  | 2 | `new` | set | 1 |
  | 3 | `active` | set | 1 |

  So the edge is readable from both ends while the droplet is still `new`.
  Waiting for `active` is sufficient but stricter than necessary; waiting for
  nothing at all is what `digitalocean_vpc`'s step 07 did, and it is not
  enough.

- **A member is identified by URN, in a namespace the forward edge never
  uses** (`09`):

  ```json
  "members": [{"urn": "do:droplet:89a3a30d-8b38-432b-bb42-1ef403ea227c",
               "name": "aiform-system-test-vpcmem-probe-…",
               "created_at": "2026-09-29T04:50:49Z"}]
  ```

  The droplet's own id was `604558243`. The URN carries an unrelated UUID, and
  there is no numeric id in the member entry at all. Joining this answer back
  to aiform's state requires matching on `name`, which is not an identity
  aiform treats as stable, or recording the URN, which no driver does. **The
  provider's reverse-edge answer is not directly usable.**

## Consequences for the dependency model

Recorded here because the spec cites this session for them:

1. **Containment is a real, verified relationship kind** on DigitalOcean, and
   it behaves differently from reference: the parent refuses to go while
   children exist. A model with one relationship type cannot express both.
2. **The provider's refusal arrives at apply time.** aiform knowing the edge
   lets it refuse at *plan* time instead, before any mutation — which is the
   argument for aiform's own graph being worth maintaining even where the
   provider also enforces.
3. **But aiform cannot lean on the provider's graph**, because of the URN
   mismatch above. Whatever aiform knows about relationships, it has to know
   from its own records.

## Not probed

- Whether a `default` VPC refuses deletion for the same reason, or at all.
  Deliberately not attempted; see `digitalocean_vpc`'s findings.
- Whether `409` is also the answer for a VPC holding a non-droplet member
  (load balancer, database). No driver for either exists.
- Whether the refusal message is stable enough to match on. One observation.

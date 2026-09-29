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
still has a live member?** DigitalOcean's own `vpcs_delete` description already
said yes — *"a VPC can only be deleted if it does not contain any member
resources"* — so this started `documented`, not guessed. The probe was worth
running anyway, and the reason is the finding: **the documented status is
wrong.**

## Promotion candidates

| Finding | Would become | Evidence | Held because |
|---|---|---|---|
| A documented HTTP status can be wrong, so a documented signature is not evidence about status | `probing/documented-status-is-not-evidence` | `10` | `probing/` rule — needs one more case of a documented status being contradicted. This one is unusually clean: the docs promise `403` and the API returns `409` |
| A provider enforces referential integrity on delete with `409 conflict` | `csp/digitalocean/referential-delete-is-409` | `10`, `14` | `csp/` claim — needs a second DigitalOcean resource family that refuses a delete for the same reason; no other in-repo resource has a member collection |
| A parent-child edge becomes visible in the API **before** the child reports itself ready, so "wait for active" is stricter than the edge requires | `csp/digitalocean/membership-precedes-active` | **none — polls were `record=False`** | Cannot be promoted at all until re-run with recording on. Listed so the observation is not lost, not as a candidate ready to promote. Timing findings are also the class `specs/driver_creation.md` warns against over-reading |

## Resource-specific (stay here)

- **The documented status is wrong: documented `403`, observed `409`** (`10`).
  DigitalOcean's `vpcs_delete` description promises *"a 403 Forbidden error
  response"* for both a default VPC and one with members. The refusal itself is
  as documented; the status is not:

  ```
  DELETE /v2/vpcs/{id}  ->  409
  {"id": "conflict", "message": "Can not delete VPC with members"}
  ```

  with a converged droplet inside. The same call after the droplet was
  destroyed returned `204` (`14`), so the refusal is about membership and not
  about the VPC. **This is the first edge in the repo whose parent the provider
  will not release** — every other edge in the driver set survives its target.
  Note the narrower claim: what is established is the refusal, not that a droplet
  would break without its VPC. The provider prevents that state, so it cannot be
  observed.

  My prediction of `403` came from the documentation, not from a guess — which
  is what makes this worth recording. `409 conflict` is also the more honest
  shape: a state conflict, refusable now and permitted later, where `403` reads
  as a permissions problem. Any code that recognised this refusal by status
  would have matched the wrong one had it trusted the docs.

- **Membership becomes visible before the droplet is active** (polls 1-3, 15s
  apart):

  | poll | droplet `status` | droplet `vpc_uuid` | VPC `members` |
  |---|---|---|---|
  | 1 | `new` | **key absent** | 0 |
  | 2 | `new` | set | 1 |
  | 3 | `active` | set | 1 |

  Note "key absent", not `None`: the droplet record has no `vpc_uuid` field at
  all at poll 1 (see `digitalocean_vpc`'s transcript `05`). The `None` in this
  session's console output was `droplet.get("vpc_uuid")` supplying it. A driver
  author must code for a missing key.

  So the edge is readable from both ends while the droplet is still `new`.
  Waiting for `active` is sufficient but stricter than necessary; waiting for
  nothing at all is what `digitalocean_vpc`'s step 07 did, and it is not enough.

  **These polls were `record=False` and left no transcripts**, so none of this
  paragraph may be graded `verified` — `specs/driver_creation.md` is explicit
  that a `verified` entry must name a transcript. Recording the polls would fix
  it and cost nothing; a future run should.

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

1. **Containment is a real relationship kind** on DigitalOcean, documented by
   the provider and confirmed here, and it behaves differently from reference:
   the parent refuses to go while children exist. A model with one relationship
   type cannot express both. Note what is *not* established: no probe observed a
   droplet after losing its VPC, because the provider prevents that state, so
   "the dependent cannot survive" is inferred from the refusal rather than
   measured.
2. **The provider's refusal arrives at apply time.** aiform knowing the edge
   lets it refuse at *plan* time instead, before any mutation — which is the
   argument for aiform's own graph being worth maintaining even where the
   provider also enforces.
3. **But aiform cannot lean on the provider's graph**, because of the URN
   mismatch above. Whatever aiform knows about relationships, it has to know
   from its own records.

## Not probed

- Whether a `default` VPC refuses deletion. DigitalOcean documents that it
  cannot be deleted at all; deliberately not attempted, because a passing result
  is a destroyed region default. Note the documented status for that case is the
  same `403` this session showed to be wrong for the members case, so the
  documentation is not evidence about what it would actually return.
- Whether `409` is also the answer for a VPC holding a non-droplet member
  (load balancer, database). No driver for either exists.
- Whether the refusal message is stable enough to match on. One observation.

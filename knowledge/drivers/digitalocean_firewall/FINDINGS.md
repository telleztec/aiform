# digitalocean_firewall — findings

Each cites the transcript that established it, in
`probes/transcripts/digitalocean_firewall/`. A finding is promoted into
`knowledge/` only on a **second** observation — one is an anecdote — and
what counts as the second depends on the category
(`specs/driver_creation.md`, "How the loop learns"): a `csp/` claim
needs two resources on this provider, a `driver/` claim needs two
*providers* and so cannot be settled before Gate #3, and a `probing/`
rule needs two cases of the method paying off.

## Promotion candidates

| Finding | Would become | Evidence | Held because |
|---|---|---|---|
| DigitalOcean coerces scalar types on store rather than rejecting (int port → `"22"`, `"TCP"` → `"tcp"`, `"all"` → `"0"`) | `csp/digitalocean/scalar-coercion` | `04`, `05`, `10` | `csp/` claim — needs a second DigitalOcean resource; promotable at the next driver |
| A server-added field absent from the published OpenAPI schema is a phantom-diff source | `driver/server-added-fields` | `02`, `03` | `driver/` claim — needs a second **provider**, so blocked until Gate #3 (AWS) |
| To learn whether an omitted field is *reset* or *left alone*, the resource must first hold a non-empty value | `probing/reset-vs-unchanged` | `24`, `25`, `26` | `probing/` rule — needs one more case of it paying off; same provider is fine |

## Resource-specific (stay here)

- An unattached firewall returns `status: succeeded` immediately, so
  `create()` must not poll (`02`).
- A firewall must have at least one rule; both lists empty is 422 (`08`).
- Referenced tags must pre-exist, in both `tags` and `sources.tags`
  (`19`, `20`) — unlike droplet creation, which auto-creates. This
  settles an open question in `specs/digitalocean_compute.md`.
- A malformed id 404s exactly as an absent one does (`27`, `28`), so
  `read()` needs no separate 422 branch.
- `PUT` is a whole-object replace: an omitted `tags` key is reset to `[]`
  (`24`, `25`, `26`). Inferred but **not observed** for `droplet_ids`,
  which no probe attaches.
- Bare addresses and IPv6 ranges are stored verbatim (`06`, `07`).

## Open

- `action: "deny"` is accepted and stored (`12`) though DigitalOcean
  documents firewalls as allow-only. Whether it is *enforced* cannot be
  settled without attaching to a live droplet and generating traffic.

# digitalocean_resource_tags — findings

Each cites the transcript that established it, in
`probes/transcripts/digitalocean_resource_tags/`. One observation each:
nothing here is promoted to `knowledge/`.

Run to settle #249 (the deployment name and an aiform marker on every provider
resource). Free resources only, no droplets; every created resource was deleted
(`cleanup` lines in the run's output; the throwaway tag is gone at `31`).

## Resource-specific (stay here)

- **A firewall cannot carry a DigitalOcean tag.** `POST /v2/tags/{tag}/resources`
  with `resource_type: "firewall"` answers `204` on a firewall that has no tags
  at all (`13`), yet the firewall still reads `tags: []` (`14`) and the tag's
  `resources` breakdown never gains a firewall entry (`29`: only `droplets`,
  `images`, `volumes`, `volume_snapshots`, `databases`). A firewall's own
  `tags` field is a selector for droplets, not a label on the firewall
  (`09`/`10`). So the firewall fallback applies: the deployment name goes in
  the name.
- **The `204` is not evidence the attach worked.** The same attach to a droplet
  id that does not exist also answers `204` (`07`), and the tag then counts that
  phantom droplet (`10`, `29`). A driver must read back, not trust the status.
- **A domain cannot carry a tag either.** Attaching with `resource_type:
  "domain"` is `404` (`16`), and a domain record has only `name`, `ttl` and
  `zone_file`, no `tags` key (`17`).
- **A firewall name rejects `_`** (`23`: `422 invalid name`) and `:` (`21`), and
  accepts `.` (`24`), uppercase (`25`), a leading digit (`26`) and exactly 255
  characters (`27`, `28`: 256 is `422`). A deployment name may contain `_`
  (`^[a-z0-9][a-z0-9_-]{0,62}$`), so the name fallback maps `_` to `-`; that
  mapping is lossy (`a_b` and `a-b` give the same name).
- **A tag name keeps its case and is case-sensitive.** `Aiform:TagProbe-...-UPPER`
  is created as sent (`03`) and `GET` by its lowercased name is `404` (`04`). A
  reserved-name check on user tags therefore needs no case folding.
- **A tag name may contain `:` unescaped** in a path and a body (`01`, `02`);
  a space or `/` is `400`, not `422` (`05`, `06`).
- **A second TXT record at a zone apex rectifies the first one's TTL.** A user
  TXT at ttl 1800 reads back as 300 after the marker TXT is added at ttl 300
  (`18`, `19`, `20`). A TXT value `aiform:prod` round-trips verbatim (`19`,
  `20`). So the marker must be created with the TTL of any existing apex TXT
  (or the user's TXT must be reconciled), or the user's TXT shows as drift.

## Open

- **Not probed: tag behavior on droplet create and delete.** Both need a
  droplet, which bills. Droplet tag auto-creation on create is taken from the
  existing driver's documented behavior (`_ensure_tag_exists` runs first), and
  what happens to a tag when its last resource is deleted is **not observed**:
  only that a tag with zero resources persists until deleted is observed
  (`02`).

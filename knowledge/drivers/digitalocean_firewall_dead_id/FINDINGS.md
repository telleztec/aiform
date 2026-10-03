# digitalocean_firewall_dead_id — findings

Each cites the transcript that established it, in
`probes/transcripts/digitalocean_firewall_dead_id/`. One observation is an
anecdote; nothing here is promoted into `knowledge/`
(`specs/driver_creation.md`, "How the loop learns").

Issue #265. No driver changed. The session measures how long a firewall keeps a
deleted droplet's id in `droplet_ids`.

## Resource-specific (stay here)

- The baseline was converged before the `DELETE`: droplet `active`, firewall
  `succeeded`, `pending_changes: []`, the droplet listed (`09`).
- The droplet read `404` 12s after its `DELETE` (`13`).
- The dead id was still listed 12s after the `DELETE`, by id and in the list
  (`14`, `16`).
- The dead id was gone 42s after the `DELETE` (`17`) and still gone at 72s, by id
  and in the list (`18`, `20`).
- The firewall reported `status: succeeded` and `pending_changes: []` in every
  read (`09`, `14` to `20`).
- The reap happened in (12s, 42s]. This run cannot narrow it: no read was taken
  between 12s and 42s.
- The session's `count=27` in `AUDIT.log` is 24 real HTTP calls plus 3 sequence
  numbers with no request behind them.

## Not established

- Whether a `PUT` removing the dead id is accepted. The id had left, so it was
  never sent.
- Whether a `PUT` that keeps a dead id is accepted or refused. This is what
  aiform's desired state would send.
- `sources.droplet_ids` inside a rule.
- A tag-targeted firewall.
- Stability across runs.
- Any horizon past 72s.

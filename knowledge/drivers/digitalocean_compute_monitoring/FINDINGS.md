# digitalocean_compute_monitoring — findings

Each cites the transcript that established it, in
`probes/transcripts/digitalocean_compute_monitoring/`. Promotion into
`knowledge/` needs a **second** observation — one is an anecdote — and
what counts as the second depends on the category
(`specs/driver_creation.md`, "How the loop learns").

This session probed `/v2/monitoring/*`, which no earlier session had
touched at all: every prior transcript in this repo is of `/v2/droplets`
or `/v2/firewalls`.

## Promotion candidates

| Finding | Would become | Evidence | Held because |
|---|---|---|---|
| An endpoint can answer `401 unauthorized` for a resource that does not exist, not `404` — so a 401 is not evidence about the *token* | `csp/digitalocean/401-means-maybe-gone` | `14`, `15` | `csp/` claim — needs a second DigitalOcean resource family with a monitoring-style endpoint; nothing else in the repo has one yet |
| A metrics endpoint returns `200` with an empty series for a resource that exists but has never reported, rather than an error | `driver/absent-telemetry-is-not-an-error` | `23`, `24` | `driver/` claim — needs a second **provider**, so blocked until a non-DigitalOcean driver exists |
| Probing the *absence* case (a resource with no agent) is worth a mutating probe even when the present case is fully documented | `probing/probe-the-empty-case` | `23`, `24`, `25` | `probing/` rule — needs one more case of it paying off; same provider is fine |

## Resource-specific (stay here)

- **A nonexistent or malformed `host_id` answers `401 unauthorized`**
  (`14`, `15`) — never `404`, unlike `GET /v2/droplets/{id}`, which
  404s (`drivers/digitalocean/compute.py`'s `read()` relies on that).
  The consequence is structural: **`metrics()` cannot distinguish "the
  droplet is gone" from "the token is wrong"**, so it must not map 401
  to `ResourceNotFoundError` — doing so would report a live droplet as
  deleted every time a credential was bad. It lets the error propagate
  instead, and `observability.collect()` records it as a failure to
  observe. This is why `specs/driver_observability.md`'s
  "`metrics()` raises `ResourceNotFoundError`" row is unreachable for
  this driver.
- **The monitoring series steps every 120 seconds, and the newest point
  was 64–96s old** across two runs (`12`). So a reading this command
  prints is up to ~3.5 minutes stale and already averaged by
  DigitalOcean over its own step. Sub-2-minute resolution is
  unavailable no matter how often the command runs — a property of the
  provider, not of the transport. Confirms as *observed* what
  `specs/driver_observability.md` carried as *recalled*.
- **`cpu` is a family of 8 series labelled by `mode`**
  (`idle`, `iowait`, `irq`, `nice`, `softirq`, `steal`, `system`,
  `user`), cumulative and rising between reads (`02`, and the recon run
  before it). Not one series — a driver mapping endpoint→sample
  one-to-one gets this wrong.
- **Every series carries a `host_id` label** (`01`–`10`), which is
  identity the output already prints beside the samples. The driver
  strips it, so a future exporter can stamp its own without colliding.
- **`filesystem_free` and `filesystem_size` carry `device`, `fstype`
  and `mountpoint`** (`06`, `07`). Those are *not* identity — they are
  what tells two filesystems apart — and must survive.
- **`bandwidth` needs `interface` and `direction`**; without them it is
  `400` (`11`), and with them it returns a rate rather than a state
  (`17`–`20`). It is the only metric in the family not addressable by
  `host_id` alone, so a driver looping uniformly over metric names earns
  one `400` for free. Omitted from the driver's first slice for that
  reason plus the four extra requests it costs.
- **Values are JSON strings, timestamps are ints** (`01`). `"2063577088"`,
  not `2063577088`.
- **Omitting `start`/`end` is `400`** with `failed to parse start time`
  (`13`) — both are genuinely required, not defaulted.
- **An undefined metric name is `404`** (`16`), distinguishable from the
  401s above, so a typo in a metric name fails differently from a bad
  host.
- **A droplet reports `status: "new"` with `features: ["droplet_agent"]`
  seconds after creation** (`25`) — so `health()` must treat `new` as
  DEGRADED (provisioning), not FAILING, and `metrics()` must treat an
  empty series as normal rather than broken.
- The droplet object carries `status`, `locked`, `features`, `region`,
  `size_slug` and `networks` (`21`) — enough for a verdict and its
  observations without widening `read()`.

## Open

- Whether `cpu`'s cumulative counters reset on reboot was **not
  probed**: it needs a reboot, which is a mutation on a droplet that
  must then be waited on, and the answer does not change the driver's
  behaviour — the repo owner decided a reset is a handled condition
  rather than a disqualifier (see `specs/driver_observability.md`'s
  counter-honesty amendment), so `cpu_seconds_total` ships as a
  `COUNTER` either way. Worth probing if that decision is ever revisited.
- Whether a *stopped* droplet's agent keeps reporting, stops, or reports
  zeroes. `health()` answers "is it off" from the droplet object, so the
  driver does not depend on this — but an operator reading `metrics` on
  a powered-off droplet will meet whichever it is, and nothing here
  says which.
- Whether `filesystem_*` returns more than one series on a droplet with
  several mounts. Only one was observed (`06`, `07`), on a droplet with
  a single filesystem, so the multi-series path is exercised by the
  driver's label handling but not by evidence.

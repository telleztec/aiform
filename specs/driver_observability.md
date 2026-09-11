# specs/driver_observability.md — runtime health and metrics (`driver.py`/`scan.py`/`models.py`/`cli.py`)

**Naming note**: like `specs/resource_tagging.md` and
`specs/unordered_fields.md`, this filename deliberately doesn't follow
`specs/README.md`'s per-module mirroring rule. It is one capability spread
across the contract (`specs/driver.md`), a new module (`aiform/scan.py`), the
models (`specs/models.md`), the CLI (`specs/cli.md`) and the driver-generation
validator (`specs/driver_gen.md`). Named for the feature so it is discoverable
from any of them, with each cross-referencing it.

Closes #131.

**Build status.** Nothing here is built. This spec defines the contract; the
implementation lands in later PRs, per-module, and this file is the acceptance
criteria they are written against. `aiform/driver.py` and every driver are
deliberately untouched by the PR that adds this file.

## Purpose

Give a driver two optional, read-only methods for answering the day-2
questions `read()` cannot: **is this resource working**, and **what are its
counters and gauges**. Then sweep them across every tracked resource with
`aiform scan`, in a format Prometheus or Grafana can consume.

## Relationship to `PLAN.md` §10's "Observability" entry

`PROCESS.md` step 1 requires a spec in this space to implement, narrow or
extend §10's existing entry rather than silently work around it — a rule that
exists because `specs/resource_tagging.md` shipped without checking §10 and
cost a reconciliation PR.

This **extends** it. The two are genuinely different mechanisms, and §10 has
been edited to say so:

| | §10 Observability | This spec |
|---|---|---|
| Subject | a *formation* — one plan/apply run | a *resource* — one live thing |
| Question | what's planned/applying/succeeded/failed, when | is it functional, what are its counters |
| Shape | a status URL, CI-run-status-page-like | a stateless CLI sweep, scrape-shaped |
| Lifetime | spans one run | runs indefinitely, on an interval |

`specs/driver_creation.md`'s "Out of scope" draws the same line for its own
build-time audit log — "related in spirit, unrelated in mechanism." Neither
this spec nor §10's entry subsumes the other, and building one does not
discharge the other.

## Why not `read()`

`read()` already fetches live attributes for a tracked resource, so the
cheapest-looking design is to poll it. That is wrong in three independent
ways, and the reasons are the design constraints for everything below:

1. **`read()` writes state.** `orchestrator.refresh_resource()` persists its
   output, and `PLAN.md` §3's refresh mechanism writes `.aiform/state.json`
   immediately, even on a bare `plan`. A dashboard polling every 15s would
   churn the state file and rewrite `last_refreshed_at`, changing what a later
   `plan` means.
2. **There is no locking** (`PLAN.md` §10). A poll racing a real `apply` can
   corrupt state.
3. **`read()`'s shape is constrained to be diffable** against the user's raw
   `params`. Health is not an attribute anyone declares in `.aiform.md`, and a
   CPU gauge that appeared in the attribute dict would be a permanent,
   non-empty diff — which permanently defeats the zero-LLM-call short-circuit
   for that resource, exactly the failure `specs/unordered_fields.md` exists to
   fix.

So these are separate methods with separate return types on a separate command
that never writes state — not a second caller of `read()`.

## Interface

### `aiform/driver.py`

One new exception, and two new **concrete** (non-abstract) methods on
`ResourceDriver`:

```python
class CapabilityNotSupported(Exception):
    def __init__(self, capability: str, reason: str):
        self.capability = capability
        self.reason = reason
        super().__init__(f"{capability}: {reason}")


class ResourceDriver(ABC):
    ...

    def health(self, id: str, credentials: dict[str, str]) -> HealthReport:
        raise CapabilityNotSupported("health", "this driver does not implement health()")

    def metrics(self, id: str, credentials: dict[str, str]) -> list[Sample]:
        raise CapabilityNotSupported("metrics", "this driver does not implement metrics()")
```

**Concrete, not `@abstractmethod`** — deliberately. Making either abstract
would break every existing driver at instantiation time and force a resource
that cannot answer to write a stub anyway. This mirrors how
`specs/resource_tagging.md`'s `_tags_for_create`/`_tags_for_attributes` are
concrete: a driver opts in by overriding, not by setting a flag.

`CapabilityNotSupported` lives in `driver.py` alongside
`DriverUpdateNotSupported`, not in `exceptions.py`. It is raised by the base
class itself, so it is part of the contract rather than a general-purpose
error — and putting it in `exceptions.py` would re-create the stale `PLAN.md`
§1 claim that `specs/driver.md` has been flagging about
`DriverUpdateNotSupported` since that module was written.

**The parameter lists are contract, not style.** `driver_gen.py`'s AST
validator does exact list equality on `[arg.arg for arg in method.args.args]`,
so both are `["self", "id", "credentials"]` — identical to `read()` and
`delete()`. A driver renaming `id` to `resource_id` fails validation.

### `aiform/models.py`

```python
class HealthStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    FAILING = "failing"
    UNKNOWN = "unknown"


class HealthReport(BaseModel):
    status: HealthStatus
    summary: str
    observations: dict[str, str] = Field(default_factory=dict)


class MetricKind(str, Enum):
    COUNTER = "counter"
    GAUGE = "gauge"


class Sample(BaseModel):
    name: str
    kind: MetricKind
    value: float
    labels: dict[str, str] = Field(default_factory=dict)
```

### `aiform/scan.py` (new module)

```python
@dataclass
class ResourceScan:
    resource_key: str
    provider: str
    resource_type: str
    name: str
    id: str
    health: HealthReport | None  # None iff health_unsupported is set
    health_unsupported: str | None  # the CapabilityNotSupported reason
    samples: list[Sample]
    samples_unsupported: str | None
    errors: list[str]


def scan_resources(
    paths=None, *, state_path=state.DEFAULT_STATE_PATH
) -> tuple[list[ResourceScan], float]:
    """Sweep every tracked resource. Returns the scans and elapsed seconds.
    Reads state; never writes it. Makes zero Anthropic API calls."""


def render_text(scans: list[ResourceScan], elapsed: float) -> str: ...
def render_json(scans: list[ResourceScan], elapsed: float) -> str: ...
def render_prometheus(scans: list[ResourceScan], elapsed: float) -> str: ...
def write_atomically(text: str, path: Path) -> None: ...
```

### `aiform/cli.py`

```
aiform scan [--format text|json|prometheus] [--output PATH]
            [--state-file PATH] [FILE.aiform.md ...]
```

`scan` is a **top-level** command, not an `aiform plan` subcommand. It neither
plans nor applies anything, and `plan`'s subcommands all share `--state-file`
semantics that include writing state — which this must never do.

## Behavior

### What `health()` may look at

**Control plane only.** `health()` asks the CSP what it believes about the
resource. It must **not** originate traffic toward the resource itself — no TCP
connect to a droplet's port, no DNS resolution against a record the driver
manages.

The reason is that a data-plane check makes the verdict a property of *where
aiform happens to be running*, not of the resource. A correctly-configured
firewall between the operator's laptop and the droplet would report `failing`,
and the same resource would report `ok` from inside the VPC. A dashboard whose
red/green depends on which machine ran the scrape is worse than no dashboard.

Be honest about the cost of this choice: a control-plane `health()` cannot tell
you `sshd` is up, or that the application is serving. It tells you the CSP has
not noticed anything wrong. That is a real limit, not a temporary one — see
Out of scope.

### The four health states

| Status | Means | Example |
|---|---|---|
| `OK` | the CSP says the resource is doing its job | droplet `status == "active"` with a public v4 assigned |
| `DEGRADED` | working, but not fully | firewall `status == "succeeded"` with a non-empty `pending_changes` |
| `FAILING` | the CSP says it is not working, or it is gone | droplet `status == "off"`; a 404 |
| `UNKNOWN` | aiform could not find out | timeout, 5xx, auth failure |

**`UNKNOWN` is why there are four states and not two.** A timeout is aiform
failing to observe, not evidence the resource is broken. Collapsing it into
`FAILING` would page somebody every time aiform's own network hiccuped, and
after the third false page nobody trusts the alert.

A driver returns `OK`/`DEGRADED`/`FAILING` from what it read. It does **not**
return `UNKNOWN` for its own failures — it lets the exception propagate, and
`scan_resources()` converts it. A driver that catches its own timeout and
returns `UNKNOWN` is hiding the error text that would say what went wrong.

### What `metrics()` returns

A flat list of `Sample`. The driver supplies a **bare** name — `memory_bytes`,
not `aiform_memory_bytes` and not `digitalocean_droplet_memory_bytes`. The
renderer adds the `aiform_` prefix and the identity labels.

`Sample` has deliberately **no `unit` field and no timestamp**:

- The unit lives in the name, per Prometheus convention (`_bytes`,
  `_seconds`), and a `COUNTER`'s name must end in `_total`. A separate `unit`
  field would be a second source of truth the two renderers could disagree
  about.
- The scrape time is the right timestamp, and node_exporter's textfile
  collector rejects explicit ones.

**Counter honesty.** `COUNTER` is only for a value the CSP itself documents as
cumulative and monotonic over the resource's lifetime. aiform never derives a
counter by differencing two reads — `scan` is stateless and holds no history to
difference against, by construction. A value the CSP resets on reboot is not a
counter. When in doubt, `GAUGE`: a wrong gauge reads as noise, a wrong counter
makes `rate()` produce a plausible, silently false number.

### Rules both methods must follow

These are what make it safe to call them on a 15-second interval. They are
checklist items in `prompts/review_driver.md`, which since #119 is the only
review a driver's imports and call shapes ever get.

1. **Read-only.** `GET`/`HEAD` against the CSP control plane and nothing else.
   No `POST`/`PUT`/`PATCH`/`DELETE`. No side effect that creates or modifies
   anything — not a tag, not an alert subscription, not a temporary resource.
2. **Control plane only**, per the section above.
3. **No state write.** Neither method may touch `.aiform/state.json` or its
   backup.
4. **Zero LLM calls, always**, on both paths. `tests/conftest.py`'s
   `forbid_llm_client` fixture asserts this mechanically.
5. **Bounded.** The driver bounds its own HTTP calls; the contract's target is
   ≤5s per resource. *This is not mechanically enforced* — aiform cannot bound
   a synchronous `urllib` call without threads, so the requirement is stated
   and `/code-review` checks it. Said plainly here rather than implied to be
   guaranteed.
6. **No identity labels.** A driver does not set `provider`, `resource_type`,
   `name` or `id` in `Sample.labels`; the renderer stamps those. A driver that
   sets one has its samples dropped for that resource (see Edge cases). This
   means a driver cannot misspell or omit an identity label, and cannot smuggle
   a credential into one.

### `scan_resources()`

1. Load state. Iterate `st.resources.values()`, filtered to the resources
   matching `paths` when given — the same walk `orchestrator.refresh_state()`
   and `build_destroy_plan()` already do over every tracked resource.
2. Cache drivers by `(provider, resource_type)` and credentials by provider,
   matching `refresh_state()`'s existing caching.
3. Per resource, call `health()` then `metrics()`, each guarded independently:
   one being unsupported or raising does not skip the other.
4. Return the scans and the elapsed wall-clock seconds. **Never writes state.**

**Partial failure never aborts the sweep.** Everything is caught per resource
and per method. One driver raising means that resource reports `UNKNOWN` and
the rest still render — a single broken driver must not blank the dashboard.

### Rendering

`render_prometheus()` emits, for each resource with a health verdict:

```
# HELP aiform_resource_up Whether the CSP reports this resource as working.
# TYPE aiform_resource_up gauge
aiform_resource_up{provider="digitalocean",resource_type="compute",name="web-01",id="123456789"} 1
aiform_memory_bytes{provider="digitalocean",resource_type="compute",name="web-01",id="123456789"} 2.147483648e+09
# HELP aiform_scan_duration_seconds Wall-clock seconds for the whole sweep.
# TYPE aiform_scan_duration_seconds gauge
aiform_scan_duration_seconds 0.83
```

`aiform_resource_up` is `1` for `OK` and `DEGRADED`, `0` for `FAILING`, and
**absent** for `UNKNOWN`. An absent series is how Prometheus already expresses
"no observation"; emitting `0` would assert a failure aiform did not observe,
which is the `UNKNOWN`-vs-`FAILING` distinction thrown away at the last step.

`DEGRADED` mapping to `1` is a deliberate loss: `up` is binary, and a degraded
resource is still serving. The distinction survives in `text` and `json`
output, and a driver that wants it alertable should emit its own gauge.

Label values are escaped per the exposition format (`\\`, `"`, `\n`). `HELP`/
`TYPE` are emitted once per metric name, before its first sample — repeating
them makes the file invalid, which matters because one malformed line makes the
textfile collector discard the **whole file**.

`--output PATH` writes via a temporary file in the same directory plus
`os.replace()`. The textfile collector reads the directory continuously and
will happily parse a half-written file; atomicity is a requirement of that
integration, not a nicety.

### Scrape cost

Each swept resource costs one or more CSP API requests. N resources scraped
every I seconds costs roughly `N × calls × 3600 / I` requests/hour against a
token whose DigitalOcean limit is 5000/hour — 10 resources making 2 calls each
at a 15-second interval is 4800/hour, which is already most of the budget.

The spec states the arithmetic; `--verbose` logs the sweep duration. There is
deliberately **no per-driver cost-declaration attribute** — that is a knob for
a fleet three drivers cannot yet produce, and `CLAUDE.md` is explicit about not
building for scenarios that can't happen yet.

## Edge cases / errors

- **`CapabilityNotSupported`** from either method is caught per resource and
  rendered as `unsupported: <reason>`. Never an error, never a non-zero exit.
  In `prometheus` output the resource simply contributes no series for that
  capability.
- **`ResourceNotFoundError`** from `health()` renders as `FAILING`, summary
  `"resource not found"`, `up 0`. It does **not** mark drift: `scan` cannot
  write state, and a vanished resource should page someone now. The next `plan`
  is what records it.
- **Any other exception** from either method renders as `UNKNOWN` for health,
  or drops that resource's samples, with the exception text appended to
  `ResourceScan.errors` and logged at WARNING. The sweep continues.
- **A driver-supplied label colliding** with an identity label drops that
  resource's samples and appends an error. Deliberately not a silent overwrite,
  which would hide a driver bug, and deliberately not fatal to the sweep, which
  would let one bad driver blank every panel. `aiform_resource_up` is still
  emitted from `health()`, which is unaffected.
- **A `COUNTER` whose name does not end in `_total`** is an error for that
  sample, dropped with a message naming the driver. The name is the only place
  the type is expressed to Prometheus, so a mismatch here is a wrong dashboard,
  not a cosmetic one.
- **A non-finite `value`** (`NaN`, `±Inf`) is dropped with an error. The
  exposition format has spellings for these, but a driver producing one almost
  always means a division by a zero-valued denominator it did not check.
- **No tracked resources at all** — `scan` prints an empty result and exits 0.
  A scrape of an empty formation is not an error.
- **`--output` to an unwritable path** fails loudly and exits non-zero. Unlike
  a per-resource failure, there is no partial result worth salvaging.
- **A driver file missing** for a tracked resource raises `PlanBlockedError`
  from `load_driver()`; caught, recorded as that resource's error, sweep
  continues.

## Out of scope

- **Data-plane reachability** — TCP connect, HTTP request, DNS resolution
  against the resource itself. Excluded for the reason given under "What
  `health()` may look at": it makes the verdict a property of aiform's network
  location rather than of the resource. Reopening this needs a design pass that
  answers where the check runs from, not just a new method.
- **A long-running `/metrics` exporter.** `aiform scan` is a one-shot command.
  A daemon Prometheus scrapes directly would be this repo's first inbound
  socket and first server dependency, with auth, TLS and lifecycle all
  undesigned. It belongs with `PLAN.md` §10's "Centralized server support",
  which names the direction without committing to an architecture.
- **Historical storage.** aiform holds no time series. `scan` is stateless; the
  scrape target stores history. This is also why a driver may not derive a
  counter by differencing.
- **Alerting rules, dashboards, or Grafana provisioning.** aiform emits;
  configuring what reads it is the operator's.
- **Health on the `apply` path.** It is tempting to reuse `health()` for
  post-create readiness — `drivers/digitalocean/firewall.py` already has
  `_wait_until_active`, and `compute.py` has `_poll_until`. Deliberately not
  done: convergence-during-apply and steady-state health are different
  questions (one is "has it settled yet", the other "is it still working"), and
  making `apply` depend on an *optional* method would mean a driver declining
  `health()` silently loses its readiness wait. Revisit only with
  `PLAN.md` §10's "Timeout/retry/failover orchestration" entry, which owns the
  polling story.
- **Implementing either method for any driver.** Per-driver work, each needing
  its own probe session with recorded transcripts per
  `specs/driver_creation.md` — the endpoints `metrics()` calls are frequently
  ones no existing driver has probed.
- **`PLAN.md` §10's formation status URL.** Extended and cross-referenced
  above, not implemented here.

## Knowledge-confidence

*Verified by reading the code this contract has to fit* —

- that `driver_gen.py`'s validator is a whitelist of required things, so an
  extra method passes validation silently and an optional one needs its own
  `OPTIONAL_METHOD_PARAMS` check rather than an `EXPECTED_METHOD_PARAMS` entry
  (which would make it required)
- that `refresh_state()` and `build_destroy_plan()` already contain the
  walk-every-tracked-resource loop `scan_resources()` mirrors
- that `cli.py`'s `_dispatch()` hardcodes `if args.command == "init"` and
  otherwise reads `args.plan_command`, so a new top-level command needs an
  explicit branch or it `AttributeError`s
- that `prompts/review_driver.md` is the only review a driver gets, since #119

*Inferred, not verified* — that the three-state-to-binary `up` mapping is the
right loss, that ≤5s is the right per-resource target, and that the
`observations` dict stays small enough to render. All three need one real
driver implementation and should be edited after it.

*Recalled, not verified* — the textfile collector's exact tolerance for
malformed input and for explicit timestamps. Confirm against
node_exporter's documentation before `render_prometheus()` is written, not
after.

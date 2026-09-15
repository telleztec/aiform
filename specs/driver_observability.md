# specs/driver_observability.md — runtime health and metrics (`driver.py`/`observability.py`/`models.py`/`cli.py`)

**Naming note**: like `specs/resource_tagging.md` and
`specs/unordered_fields.md`, this filename deliberately doesn't follow
`specs/README.md`'s per-module mirroring rule. It is one capability spread
across the contract (`specs/driver.md`), a new module (`aiform/observability.py`), the
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
counters and gauges**. Then expose them through `aiform resource` — three
verbs (`check`, `metrics`, `status`) for an operator at a terminal, with
`metrics --format prometheus` over the whole fleet doubling as the scrape a
Prometheus or Grafana pipeline consumes.

## Use cases

The three an operator actually has, and what each one needs from the contract.
They are the reason the surface is three verbs rather than one sweep.

### 1. I just deployed this — is it up, and is it doing anything?

```
$ aiform resource check web-01
ok  digitalocean.compute.web-01  active, public v4 203.0.113.10

$ # ...apply some load...
$ aiform resource metrics web-01
up                       1
gauge  memory_bytes      2.147e+09
gauge  cpu_percent       41.2
```

Two things this demands that a fleet sweep does not. **`check` is an
assertion, so its exit code carries the verdict** — `0` for `OK`, non-zero
otherwise — because the natural next thing anyone writes is `aiform resource
check web-01 && ./smoke-test.sh`. And **`metrics` is read twice, by eye,
minutes apart**, to see a number move under load; its default format is
therefore aligned text, not exposition format.

### 2. Someone says it's down

```
$ aiform resource check web-01
failing  digitalocean.compute.web-01  status is "off"

$ aiform resource status web-01
deployed    2026-09-10T14:02:11Z, id 123456789
live        present
config      in sync with examples/web.aiform.md
health      failing — status is "off"
```

The diagnostic order matters: `check` answers *is it working*, `status`
answers *is what I deployed still what is there*. They fail independently. A
resource can be `failing` while perfectly in sync (someone powered it off), or
`ok` while drifted (someone resized it by hand). Collapsing them into one
verdict would lose exactly the distinction this case needs — and it is why
`status` exists rather than being folded into `check`.

`status` is also the only one of the three that works when the resource is
**gone**: `check` reports `failing`, but `status` says whether aiform ever
deployed it and when, which is what tells you this was a deletion rather than
a crash.

### 3. What is the deployment status of this resource?

`aiform resource status <name>` again, and the answer has four independent
parts — deployed, live, config, health — because any of them can be the
surprising one. `aiform plan show` is the neighbouring command and is
deliberately different: it prints **stored** state for everything with zero
API calls, and cannot tell you whether the record is still true.

**What `status` must not do: write state.** It reads live to answer "is the
record still true", and the temptation is to save what it learned. It doesn't,
for the same reason `metrics` doesn't — an inspection command that mutates the
record makes the next `plan` mean something different because you looked.
`plan refresh` is the command that reconciles; `status` only reports.

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

## How `health()` relates to `read()`

A driver's `health()` **may** call its own `read()` and classify the result,
when `read()` returns enough — `compute.py`'s does; `firewall.py`'s does not,
having projected `status` away. What a driver may **not** do is widen `read()`
to make that work: `read()` returns what is worth storing, and status fields
are excluded from it precisely because they churn.

These stay separate methods on separate commands because `read()`'s return is
an attribute dict with no room for a verdict, and because every caller that
refreshes also writes state, which these commands must never do. Full
reasoning: Decisions, "Why not `read()`".

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

### `aiform/observability.py` (new module)

```python
@dataclass
class ResourceReading:
    resource_key: str
    provider: str
    resource_type: str
    name: str
    id: str
    health: HealthReport | None  # None when declined, or never reached
    health_unsupported: str | None  # the CapabilityNotSupported reason
    samples: list[Sample]
    samples_unsupported: str | None
    errors: list[str]


@dataclass
class Collection:
    readings: list[ResourceReading]
    elapsed_seconds: float
    errors: list[str]  # family-level only; per-sample goes on the ResourceReading


@dataclass
class StatusReport:
    """`aiform resource status`. Four independent answers; any one can be
    the surprising one, so none is folded into another."""

    deployed: str | None  # last_applied_at + id, or None if not in state
    live: str  # "present" | "missing on the provider" | an error
    config: str  # "in sync with <path>" | "<n> fields drifted" | "no source file found"
    health: HealthReport | None
    health_unsupported: str | None


def resolve_name(name: str, st: State) -> str:
    """A `name:` frontmatter value -> the one matching state key.

    Raises ValueError naming what is tracked when nothing matches, and
    listing the candidates when more than one does. Never guesses --
    two matches can be a droplet and the firewall in front of it."""


def collect(*, keys=None, state_path=state.DEFAULT_STATE_PATH) -> Collection:
    """Read health and metrics for tracked resources: exactly those in `keys`, or every one when
    `keys` is None (no `<name>` given). Reads state; never writes it. Makes zero
    Anthropic API calls."""


def status_for(key: str, *, state_path=state.DEFAULT_STATE_PATH) -> StatusReport:
    """The four answers for one resource. Composes a state lookup, a live
    read(), diff_attributes() against the discovered .aiform.md, and
    health(). Adds no driver method of its own. Writes no state."""


# The three verbs render a list -- one entry when <name> was given, every
# tracked resource when it was not. render_check also returns the exit
# code, since the aggregate rule that produces it lives in one place.
def render_check(readings: list[ResourceReading], fmt: str) -> tuple[str, int]: ...
def render_metrics(readings: list[ResourceReading], fmt: str) -> str: ...
def render_status(reports: list[StatusReport], fmt: str) -> str: ...
def write_atomically(text: str, path: Path) -> None: ...
```

### `aiform/cli.py`

```
aiform resource check   [<name>] [--format text|json] [--state-file <path>]
aiform resource metrics [<name>] [--format text|json|prometheus]
                        [--output <path>] [--state-file <path>]
aiform resource status  [<name>] [--format text|json] [--state-file <path>]
```

Notation is `PLAN.md` §7's: `<lower-case>` in angle brackets is a placeholder,
everything else is typed literally.

**`resource` is a noun with its own verb lifecycle**, matching `aiform driver`
— `PLAN.md` §10 states that shape explicitly for drivers ("deliberately a
separate command surface from `plan`"), and the same reasoning applies: none
of these plans or applies anything.

The fleet sweep is `metrics` with no `<name>`, not a verb of its own — see
Decisions, "Three verbs, not four".

### The three verbs are not interchangeable

| Verb | Question | Driver method | Exit code means |
|---|---|---|---|
| `check` | is it working *now*? | `health()` | **the verdict**: 0 iff every verdict is `OK` |
| `metrics` | what are its numbers? | `metrics()` | the command ran |
| `status` | is what I deployed still there, and still what I declared? | `health()` + state + a live `read()` | the command ran |

`metrics` calls `health()` as well as `metrics()`, in both scopes:
`aiform_resource_up` is itself a metric and is the series an alert rule fires
on, and emitting it from the same pass keeps `up` and the gauges on one
timestamp.

**`check`'s exit code is the one exception in this whole surface**, and it is
deliberate. Everywhere else — `metrics`, `status` — a non-zero exit means
*aiform could not answer*, never *the answer was bad*; `metrics` exits 0 on
a `FAILING` resource precisely so a cron wrapper does not page on one bad
scrape. `check` inverts that because it exists to be used as an assertion
(`aiform resource check web-01 && ./smoke-test.sh`), and an assertion that
exits 0 when the thing is down is useless. `UNKNOWN` and an unsupported
`health()` both exit non-zero too, for a named resource: neither is evidence
that resource is fine.

### What `check` returns besides its exit code

`HealthReport` carries three fields and `check` renders all of them, but not
all of the time:

- `status` and `summary` — always, one line per resource.
- `observations` — a flat `str -> str` map of what the driver actually saw:
  `{"status": "off", "locked": "false", "last_action": "power_off"}`. In
  `--format text` these print **indented under the line, and only when the
  verdict is not `ok`**. In `--format json` they are always present.

The conditional is deliberate. The gate use case (`check && ./smoke-test.sh`,
or a fleet check in CI) wants one line per resource and silence when
everything is fine; the diagnosis use case is by definition the one where the
verdict is bad, which is exactly when the detail appears. No flag is needed to
switch between them because the two cases never overlap.

```
$ aiform resource check db-01
failing  digitalocean.compute.db-01  status is "off"
    status        off
    locked        false
    last_action   power_off
```

**A driver is what decides whether this is useful.** `observations` is
free-form by design — the contract cannot know which fields matter for a
resource kind it has never seen — so a driver that returns an empty map
produces a `check` with no diagnostics at all, and that is legal. The
review checklist asks for it to be populated and bounded; nothing enforces
either.

**All three take an optional `<name>`**, and omitting it means every tracked
resource. `check`'s aggregate rule is the only one that needed deciding, and
it is specified under Exit codes below. The short version: a gate that passes
because it checked nothing is the failure mode worth designing against, so
that case is exit 2, not exit 0.

`status` composes rather than adding a fourth driver method. Its four lines come
from the state entry (deployed), a live `read()` (live), `diff_attributes()`
against the discovered `.aiform.md` (config), and `health()` (health) — every
one already specified elsewhere. It writes no state, per use case 3.

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
`collect()` converts it. A driver that catches its own timeout and
returns `UNKNOWN` is hiding the error text that would say what went wrong.

### What `metrics()` returns

A flat list of `Sample`. The driver supplies a **bare** name — `memory_bytes`,
not `aiform_memory_bytes` and not `digitalocean_droplet_memory_bytes`. The
renderer adds the `aiform_` prefix and the identity labels.

**`MetricKind` is `COUNTER|GAUGE` and nothing else**, so there are no native
percentiles — no `histogram_quantile()` over a latency bucket. Adding
histograms means a new `MetricKind` and a driver that can produce bucket
boundaries; nothing here forecloses that and nothing here provides it. Named
because "why can I not chart p99" is the first question this answers.

`Sample` has deliberately **no `unit` field and no timestamp**:

- The unit lives in the name, per Prometheus convention (`_bytes`,
  `_seconds`), and a `COUNTER`'s name must end in `_total`. A separate `unit`
  field would be a second source of truth the two renderers could disagree
  about.
- The scrape time is the right timestamp, and node_exporter's textfile
  collector rejects explicit ones.

**Provider values may already be stale and pre-averaged.** DigitalOcean's
monitoring endpoints return a *time series*, not an instantaneous reading, so
a driver takes its most recent point — which is minutes old and already
averaged by the provider. Sub-minute resolution is therefore not available for
such a metric no matter how often the command runs, and that is a property of
the provider's API, not of how the output is transported. A driver should not
paper over it by resampling.

**Counter honesty.** `COUNTER` is only for a value the CSP itself documents as
cumulative and monotonic over the resource's lifetime. aiform never derives a
counter by differencing two reads — these commands are stateless and hold no history to
difference against, by construction. A value the CSP resets on reboot is not a
counter. When in doubt, `GAUGE`: a wrong gauge reads as noise, a wrong counter
makes `rate()` produce a plausible, silently false number.

### Rules both methods must follow

These are what make them safe to call repeatedly.

**Where they are actually enforced.** `PROCESS.md`'s PR-time `/code-review` —
and nothing else, for every driver that exists today. `prompts/review_driver.md`
is loaded only by `llm.review_driver()`, whose only caller is
`driver_gen.py:generate_driver()`, which no code path calls; and #119 removed
gate #1 from `plan`/`apply` entirely. `CLAUDE.md` states this plainly. Item 12 was still added to that prompt, so
the rules are in place for the future `aiform driver create` flow — but a curated driver's only gate is the human-launched review at PR
time, and a spec that implies otherwise invites someone to rely on a check
that never runs.

1. **Read-only.** `GET`/`HEAD` against the CSP control plane and nothing else.
   No `POST`/`PUT`/`PATCH`/`DELETE`. No side effect that creates or modifies
   anything — not a tag, not an alert subscription, not a temporary resource.
2. **Control plane only**, per the section above.
3. **No state write.** Neither method may touch `.aiform/state.json` or its
   backup.
4. **Zero LLM calls, always**, on both paths. `observability.py`'s tests must request
   `tests/conftest.py`'s `forbid_llm_client` fixture explicitly — it is
   **not** autouse (unlike the two credential/logger fixtures beside it), so a
   test that forgets it asserts nothing. Note also what it does not cover: it
   patches `llm.build_client`, so a driver constructing `anthropic.Anthropic()`
   directly inside `metrics()` walks straight past it. That case is caught by
   `/code-review` at PR time — **not** by `driver_gen.py`'s
   `_imports_anthropic` AST check, which has no caller either, per the
   enforcement note above.
5. **Bounded — with one permitted exception.** The target is ≤5s per
   resource. Every existing driver already bounds each call —
   `compute.py`, `domain.py` and `firewall.py` all set
   `REQUEST_TIMEOUT_SECONDS = 30` and pass it to `urlopen` — so the hazard is
   not an unbounded call, it is a **30-second** one: a `health()` that reuses
   the driver's existing `_request()` helper silently gets a bound six times
   the target, and ten such resources put a sequential sweep well past a
   run that a human is waiting on.

   **The exception:** a `health()` that delegates to `self.read()` (permitted
   above, and the reason it is permitted is there) inherits that 30s bound and
   has no way to shorten it without changing `read()`'s signature. That is
   allowed. A `health()` issuing its own request should pass a shorter timeout
   explicitly. A reviewer applying this rule must not reject the first shape
   for failing the second's standard.

   *Not mechanically enforced.* `collect()` calls each driver
   synchronously and cannot interrupt a `urlopen` already in flight without
   threads, and there is no whole-sweep deadline. Nothing stops the next
   the next sweep starting before the last finished. Stated plainly rather than
   implied to be guaranteed; a real bound belongs with `PLAN.md` §10's
   "Timeout/retry/failover orchestration" entry, which owns this for every
   driver call rather than just these two.
6. **No identity labels.** A driver does not set `provider`, `resource_type`,
   `name` or `id` in `Sample.labels`; the renderer stamps those. A driver that
   sets one has **that sample** dropped (see Edge cases). This
   means a driver cannot misspell or omit an identity label, and cannot smuggle
   a credential into one.

### `collect()`

1. Load state. Sweep the entries named by `keys`, or every entry in
   `st.resources` when `keys` is `None`.
2. Cache drivers by `(provider, resource_type)` via
   `orchestrator.load_driver()` — not a private copy of it, so these commands and
   `plan` can never disagree about which driver file they loaded — and
   credentials by provider. This mirrors `refresh_state()`'s caching shape;
   it does not call `refresh_state()`, which saves state.
3. Per resource, call `health()` then `metrics()`, each guarded independently:
   one being unsupported or raising does not skip the other.
4. Return a `Collection`. **Never writes state.**

`Collection` is a dataclass rather than a widening tuple because `errors`
carries findings that have no `ResourceReading` to attach to: a metric family
rejected because two drivers gave the same name different `MetricKind`s
belongs to neither driver alone.

### Scope: name it, or don't

`collect()` takes `keys` — a list of state keys, or `None` meaning every
tracked resource. `<name>` resolves to one key through `resolve_name()`;
omitting it passes `None`. All three verbs share this, so scope behaves
identically across the noun group.

**There is no `--all` flag.** Omitting `<name>` already means everything, which
is what `plan refresh`, `plan show` and `plan create` do with no arguments, so
a flag saying the same thing would be a second spelling of one meaning — the
thing `specs/driver.md`'s "one writable spelling per value" addendum warns
against for driver fields, applied to the CLI. See Decisions, "No `--all`
flag".

All three verbs take `--format text|json`; only `metrics` adds `prometheus`,
since only metrics have an exposition format. `--output` stays on `metrics`
alone: it exists for the textfile collector's atomicity requirement, not as a
general write-to-file convenience, and shell redirection covers the rest.

`render_check` returns its exit code alongside its text rather than having the
CLI re-derive it from `HealthStatus`. The mapping is the one place in this
surface where an exit code carries a verdict, and deriving it twice is how the
two copies drift.

### Partial failure never aborts the sweep

Everything is caught per resource and per method. One driver raising means that
resource reports `UNKNOWN` and the rest still render — a single broken driver
must not blank the dashboard. Concretely, for each way a resource can fail:

| Failure | `health` | `health_unsupported` | `samples` | `errors` |
|---|---|---|---|---|
| `health()` declines (`CapabilityNotSupported`) | `None` | the reason | `metrics()` still attempted | unchanged |
| `health()` raises `ResourceNotFoundError` | `HealthReport(FAILING, "resource not found")` | `None` | `metrics()` still attempted | unchanged |
| `health()` raises anything else | `HealthReport(UNKNOWN, summary=<exception text>)` | `None` | `metrics()` still attempted | the exception text |
| `metrics()` declines | untouched | untouched | `[]`, `samples_unsupported` set | unchanged |
| `metrics()` raises `ResourceNotFoundError` | untouched | untouched | `[]` | unchanged |
| `metrics()` raises anything else | untouched | untouched | `[]` | the exception text |
| Driver file missing (`PlanBlockedError`) | `None` | `None` | `[]` | the error |
| Credentials unresolvable | `None` | `None` | `[]` | the error |
| A returned `Sample` fails validation | untouched | untouched | the surviving samples | one entry per rejected sample |

Every row that records an error also logs it at `WARNING` on the `aiform`
logger, matching `refresh_resource()`'s handling of a drifted resource. A row
whose `errors` column says "unchanged" is a normal outcome, not a failure —
declining a capability is not an error anywhere in this spec.

`ResourceNotFoundError` gets its own rows because "raises anything else" would
otherwise classify it `UNKNOWN`, contradicting the `FAILING` mapping below. It
is not recorded in `errors`: the resource being gone is the finding, not a
failure to observe.

**`metrics()` is still attempted after `health()` reports the resource gone**,
per step 3's "guarded independently". One hole is worth naming, because it
is the one shape in which a vanished resource leaves the scrape silently: if
`health()` *declines* the capability and `metrics()` then raises
`ResourceNotFoundError`, the resource has no `up` series to go to zero and no
error recorded, so it simply stops appearing. A driver that declines `health()`
therefore cannot report its own disappearance at all — an argument for
implementing `health()` even where the verdict is thin, and a limitation stated
here rather than discovered from a dashboard that quietly lost a row.

`UNKNOWN` is represented as a real `HealthReport`, not as `health=None`, so
`render_prometheus()` can tell it from a decline and from a never-reached
driver without consulting `errors`. `health=None` therefore means only "the
method was never called"; the `health_unsupported` field distinguishes a
deliberate decline from a failure before the call.

**A missing credential is per-resource here, not fatal.** This is a deliberate
divergence from `refresh_state()`, which lets `PlanBlockedError` abort the
whole run. A sweep covering two providers must still report the one whose token
is present — aborting everything because an unrelated provider's token expired
is precisely the "one broken thing blanks the dashboard" failure above.

`ResourceNotFoundError` is handled for **both** methods, not just `health()`:
from `metrics()` it means the same thing, and the resource contributes no
samples rather than an error, since `health()` has already reported `FAILING`
for it.

### Rendering

**Group by metric family, not by resource.** The exposition format wants every
sample of one metric name contiguous, with its `# TYPE` line once, immediately
before the group. Iterating resources and emitting each one's samples inline
scatters a family across the file; node_exporter's parser tolerates that, but
`promtool check metrics` flags it, and a **second `# TYPE` line for the same
name is a hard parse error**. So `render_prometheus()` collects all samples,
buckets them by rendered name, and emits one group per name.

```
# HELP aiform_resource_up Whether the CSP reports this resource as working.
# TYPE aiform_resource_up gauge
aiform_resource_up{provider="digitalocean",resource_type="compute",name="web-01",id="123456789"} 1
aiform_resource_up{provider="digitalocean",resource_type="firewall",name="web-fw",id="aaa-bbb"} 1
# TYPE aiform_memory_bytes gauge
aiform_memory_bytes{provider="digitalocean",resource_type="compute",name="web-01",id="123456789"} 2.147483648e+09
# HELP aiform_scan_duration_seconds Wall-clock seconds for the whole sweep.
# TYPE aiform_scan_duration_seconds gauge
aiform_scan_duration_seconds 0.83
```

`# TYPE` is emitted for every family, including driver-supplied ones — that is
what `Sample.kind` is *for*.

`# HELP` is emitted **only** for aiform's own two families, whose text this
spec fixes (`aiform_resource_up`: "Whether the CSP reports this resource as
working."; `aiform_scan_duration_seconds`: "Wall-clock seconds for the whole
sweep."). `Sample` carries no help text, so driver metrics get `TYPE` and no
`HELP`. That is legal, and it is the honest option: the alternative is a
`help` field on `Sample` that every driver would fill with a restatement of
the name.

### `aiform_resource_up`

`1` for `OK` and `DEGRADED`, `0` for `FAILING`, and **absent** for `UNKNOWN`.
An absent series is how Prometheus already expresses "no observation"; emitting
`0` would assert a failure aiform did not observe, which is the
`UNKNOWN`-vs-`FAILING` distinction thrown away at the last step.

**No `up` series at all** when `health` is `None` — the capability was
declined, the driver file was missing, or credentials would not resolve. Same
reasoning as `UNKNOWN`: aiform has no observation to report, and `0` would
assert one. An implementation emitting `0` on a credentials failure would page
on an expired token rather than surfacing it as the configuration problem it
is. All three `health=None` rows in the failure table above land here.

`DEGRADED` mapping to `1` is a deliberate loss: `up` is binary, and a degraded
resource is still serving. The distinction survives in `text` and `json`
output, and a driver that wants it alertable should emit its own gauge.

### Validation before a line is written

One malformed line makes the textfile collector discard the **whole file**, so
a driver's mistake has to be caught before it is written. `collect()`
does the catching — see the note at the end of this section on why it is not
the renderer — and these are the rules it applies. They are phrased against the
rendered `aiform_<name>`, which the validator therefore computes:

- **Metric name** — the rendered `aiform_<name>` must match
  `[a-zA-Z_:][a-zA-Z0-9_:]*`. A driver returning `cpu%` or `disk-free` is
  dropped with an error naming the driver, not written out to poison the file.
- **Label names** — must match `[a-zA-Z_][a-zA-Z0-9_]*`. Label *values* are
  arbitrary UTF-8, escaped for `\\`, `"` and `\n`.
- **`COUNTER` whose name does not end in `_total`** — dropped with an error.
- **Non-finite value** (`NaN`, `±Inf`) — dropped with an error. The format has
  spellings for these, but a driver producing one has almost always divided by
  an unchecked zero.
- **Two drivers emitting the same bare name with different `MetricKind`** — one
  `TYPE` line cannot carry both. The whole family is dropped with an error
  naming both drivers, rather than silently picking one and mistyping the
  other's samples.

**No timestamps, ever.** node_exporter does not merely ignore an explicit
timestamp — it treats one as an error and **skips the entire file**
(`prometheus/node_exporter#1284`). This is why `Sample` has no timestamp field
rather than an optional one.

**Where this runs: `collect()`, not a renderer.** Specifying it under
prometheus alone would let `cpu%` through unvalidated in JSON, reporting the
same driver bug differently depending on a flag. Each `Sample` is validated once, on the way out of the sweep; all three
renderers receive the same already-validated set.

**Where a rejection is recorded follows what it belongs to.** A per-sample
rejection — a bad name, a mistyped counter, a non-finite value, a colliding
label — goes in that resource's `ResourceReading.errors`, because there is a
resource that produced it and an operator asking "why is `web-01` missing
`memory_bytes`" looks there. Only a finding with no resource to attach to goes
to the top level, and since the file-path selector was removed there is exactly
one: a metric family rejected because two drivers gave the same name different
`MetricKind`s, which belongs to neither driver alone.

### `--output`: where the file goes, and who owns it

**The filename is never derived from `<name>`.** `aiform resource metrics
web-01 --output /var/lib/node_exporter/web-01.prom` writes that path because
you typed it, not because the resource is called `web-01`. One invocation
produces one file, containing whatever that invocation covered.

That matters because the tempting deployment — one `.prom` per resource,
named after it — is a trap under this design. aiform never deletes the file
it wrote, so destroying a resource leaves its file behind, serving stale
series until a human notices. The whole-fleet form has no such problem: a
destroyed resource simply stops appearing in the next write of the one file.
**Prefer a single `aiform.prom` written by `aiform resource metrics --output
...` with no `<name>`.** Per-resource files are legal, and their cleanup is
then yours.

**There is no default path, and aiform never invents one.** The collector's
directory is a deployment decision — `/var/lib/node_exporter/`,
`/var/lib/prometheus/node-exporter/`, something else entirely — and guessing
wrong fails in the worst way available: the write succeeds, nothing reads the
file, and the dashboard stays empty with no error anywhere. Without
`--output`, output goes to stdout; with it, the operator has stated the path.

**The file is an export, not state.** This is the distinction that answers
most of the lifecycle questions at once:

| | `.aiform/state.json` | the `--output` file |
|---|---|---|
| Is it a record? | yes — losing it loses track of real resources | no — a projection of a moment |
| Does aiform read it back? | every run | never |
| Durability required | backed up before every overwrite | **none** |
| Who may delete it | nobody, casually | anyone, any time |
| Cost of losing it | severe | nothing; rerun the command |

So: **no durability requirement at all.** Delete it and the next run recreates
it. It is not backed up (unlike state, which gets `.backup` before every
overwrite), not versioned, and never read back — aiform is write-only here.
A reader that wants history is Prometheus, which already has it.

**A stale file is served as current, indefinitely.** Whatever reads the file
has no way to know the command stopped running, so a broken collection
pipeline renders as a flat, healthy dashboard rather than as an outage. This
is a hazard of handing metrics to a transport through a file at all, not of
any particular transport: the file's mtime is the only freshness signal, and
something has to watch it. node_exporter publishes
`node_textfile_mtime_seconds` for this; other transports expose an equivalent.
Stated here rather than left to be discovered, because the failure is silent
and the symptom is a dashboard that looks fine.

**aiform does not manage the file beyond writing it.** It never deletes it,
never rotates it, never prunes stale ones, and never cleans up after a
resource is destroyed. One consequence worth stating because it is the one
that bites: after `plan destroy`, the resource simply stops appearing in the
next write — its series go stale in Prometheus and age out by that system's
retention. But if the *command itself* stops running, the last file persists
and is served as current forever, which is the failure mode described under
"Wiring it to Grafana". Neither is aiform's to fix; both are the operator's to
monitor.

**A missing parent directory is an error, not something to create.**
`--output /var/lib/nod-exporter/aiform.prom` (note the typo) exits 2 naming
the directory. `mkdir -p` here would write metrics into a directory nothing
reads, which is the silent-failure case above wearing a different hat.

**Permissions are the umask's business, and the content is not secret but is
inventory.** aiform does not `chmod` the file. What lands there — resource
names, provider IDs, metric values — is readable by anyone who can read the
collector's directory, which is typically world-readable. No credential is in
it, and none may be: `observations` and labels come from drivers bound by the
same never-log-a-credential rule as everything else. But a reader of that
directory learns the shape of the infrastructure, which is worth knowing
before pointing `--output` somewhere broadly readable.

**Concurrent writers: last one wins, silently.** `os.replace()` is atomic, so
a reader never sees a partial file, but two `aiform resource metrics` runs
writing one path means one run's data is discarded with no error. There is no
locking — the same position `PLAN.md` §10 takes for `state.json`, and for the
same reason. A cron job and a human running it by hand at the same moment is
the realistic case, and it is harmless precisely because the file has no
durability requirement.

### stdout, and using these commands in a pipe

**Without `--output`, every format goes to stdout, and stdout is a clean
stream.** That is a contract, not an accident: it is what makes
`aiform resource metrics --format json | jq '.resources[].health.status'`
work.

| Channel | Carries |
|---|---|
| stdout | the rendered report, and nothing else |
| stderr | log lines (`log.py`'s `--verbose`-gated echo), the `.prom` suffix warning, and error messages |
| `.aiform/logs/aiform-<ts>.log` | the always-on file sink, unaffected |

Three consequences worth pinning, because each is a way a pipeable command
usually goes wrong:

- **Nothing diagnostic is ever interleaved into stdout.** `check`'s coverage
  line (`2 of 3 resources report health; 1 unsupported`) is part of the
  *report*, so in `--format text` it goes to stdout with the rest; in
  `--format json` it is a field in the document, never a stray line that
  would make the output unparseable. A warning is not part of the report and
  goes to stderr in both.
- **`BrokenPipeError` must be handled, not raised.** `aiform resource metrics
  --format prometheus | head -20` closes the pipe early, and the default
  Python behavior is a traceback on stderr plus a non-zero exit — for a
  command whose whole point is to be piped. Treat a closed stdout as a
  successful, complete run.
- **A pipe discards `check`'s exit code**, which is the one place in this
  surface where the exit code is the answer. `aiform resource check | tee
  log` exits with `tee`'s status, so the gate silently always passes. This is
  shell semantics, not something aiform can fix; the spec records it because
  `check` is specifically built to be used in `&&` chains, and a reader who
  pipes it for logging would lose exactly the thing they were relying on.
  `set -o pipefail`, or `${PIPESTATUS[0]}`, or do not pipe the gate.

### Atomic write mechanics

`write_atomically(text, path)` creates a temporary file **in the destination
directory** — not `/tmp`, since `os.replace()` is only atomic within one
filesystem — writes, flushes, and replaces.

Two details that decide whether the integration works at all:

- **The destination must end in `.prom`** for the prometheus format. The
  collector globs `*.prom` and nothing else, so `--output .../aiform.txt`
  produces silence rather than an error. `metrics` warns when `--format
  prometheus` is written to a path with any other suffix.
- **The temporary file must not match `*.prom`**, or the collector reads it
  mid-write — the entire failure atomicity exists to avoid. Use
  node_exporter's own documented shape: `aiform.prom.<pid>` renamed onto
  `aiform.prom`, never `aiform.tmp.prom`. The `<pid>` is not decoration:
  two concurrent runs sharing one temp name corrupt each other's write
  before either rename happens.
- **A failed write removes its temporary file.** An exception between create
  and replace otherwise leaves `aiform.prom.31415` in the collector's
  directory forever — invisible to the collector, since it does not match the
  glob, and therefore never noticed.

`--output` is accepted with every `--format`, not just `prometheus`: the
JSON output is polled by its consumer too, and a half-written JSON file is
just as unparseable.

### What Prometheus requires, and what aiform does not decide

**aiform produces a payload; it does not decide how that payload reaches
Prometheus.** Prometheus pulls over HTTP, and aiform serves no HTTP endpoint,
so something between the two always carries it. What that something is — a
node_exporter textfile collector reading `--output`, a Pushgateway, a
collector agent, an aiform exporter that does not exist yet, a bastion host
running the command on a schedule — is a deployment decision this spec
deliberately does not make. Different answers suit different networks, and
committing to one here would bake an architecture into a driver contract.

What the spec *does* fix is the payload, so that any of those transports has
something correct to carry:

- `--format prometheus` emits text exposition format: `# TYPE` per family,
  `aiform_`-prefixed names, base units in the name, `_total` on counters,
  **no timestamps**, samples grouped by family.
- `aiform_resource_up` is the alerting series — `1` for `ok`/`degraded`, `0`
  for `failing`, absent for `unknown`.
- The identity labels are what make it chartable: `provider`,
  `resource_type`, `name` and `id` become Grafana template variables and
  legend fields, so one panel covers a fleet with
  `aiform_memory_bytes{resource_type="compute"}` and a `$name` selector.

An endpoint Prometheus scrapes directly must additionally answer `200` with
`Content-Type: text/plain; version=0.0.4; charset=utf-8` (or negotiate
OpenMetrics, which requires a trailing `# EOF`). Nothing in aiform emits those
headers, because nothing in aiform serves HTTP — that belongs to the exporter
in Out of scope.

**`aiform resource metrics` is a verification tool and a payload source, not
the scrape target.** Running it by hand is how an operator confirms a driver
reports what they expect before wiring anything; `--output` is how a transport
that reads files gets the same bytes.

## Edge cases / errors

Per-resource failures are tabulated under "Partial failure never aborts the
sweep" above; per-sample validation under "Validation before a line is
written." What remains:

- **`ResourceNotFoundError`** from `health()` renders as `FAILING`, summary
  `"resource not found"`, `up 0`. It does **not** mark drift: these commands cannot
  write state, and a vanished resource should page someone now. The next `plan`
  is what records it.
- **A driver-supplied label colliding** with an identity label drops **that
  sample**, not the resource's whole set — matching how every other validation
  failure is scoped. Deliberately not a silent overwrite, which would hide the
  driver bug. `aiform_resource_up` comes from `health()` and is unaffected.
- **No tracked resources at all** — `metrics` prints an empty result and exits 0.
  A scrape of an empty formation is not an error.
- **`.aiform/state.json` missing** — empty result, exit 0. Not an error, and
  `metrics` adds no `exists()` check of its own: `state.load()` already returns an
  empty `State()` for a missing path, no other command treats that as a
  failure, and a fresh project's cron scrape should report nothing rather than
  fail until the first `apply`.
- **`.aiform/state.json` malformed** — exits 2. This one genuinely raises
  (`json.loads`, or Pydantic validation), and a scrape reporting zero resources
  because state failed to parse is worse than one that fails loudly.
- **`--output` to an unwritable path, or one whose parent directory does not
  exist** exits 2, naming the directory. aiform does not create it: a `mkdir
  -p` here writes metrics into a directory nothing reads, which fails silently
  rather than loudly.
- **`--output` where a previous run left a temporary file** — overwritten
  without comment. The temp name carries the writing process's pid, so a
  stale one belongs to a dead process and is not evidence of a live writer.

### The per-resource verbs

`check`, `metrics` and `status` all take an **optional** resource `<name>` —
the `name:` frontmatter field, not the full `<provider>.<resource_type>.<name>`
state key. The key is an implementation detail of state; the name is what the
operator wrote in the file and what they will type. Omitting it means every
tracked resource, matching `plan refresh`/`show`/`create`.

- **A `<name>` matching no state entry** — exit 2, naming the name and listing
  what *is* tracked. Only reachable when a name was given; omitting it cannot
  fail this way. Not exit 0 with "not deployed": for `check` that would
  assert health on a resource aiform has never heard of, and a typo'd name is
  overwhelmingly the likelier cause than a genuine question about something
  undeployed. `status` is the exception in spirit but not in code — see below.
- **A `<name>` matching more than one state entry** (the same name under two
  providers or resource types) — exit 2, listing the full keys that matched
  and asking for one. Never a guess: the two could be a droplet and the
  firewall in front of it, and checking the wrong one answers confidently
  about the thing you did not ask about.
- **`status` on a resource whose `.aiform.md` cannot be found** — the
  `config` line reports "no source file found", not "in sync". The other three
  lines are still answered. Silence on a drift question reads as *no drift*,
  which is the opposite of what is known.
- **`status` on a resource that is gone** (`ResourceNotFoundError`) — `live`
  reports "missing on the provider", `config` is skipped (there is nothing to
  diff against), and `deployed` still reports when aiform last applied it.
  That last line is the point of the command in this case: it distinguishes a
  resource that was deleted from one that never existed.
- **`check <name>` where that driver does not implement `health()`** — exit 2
  with the decline reason. It is not a passing check; aiform has no verdict to
  give. In the fleet form that resource is listed as `unsupported` and does not
  fail the aggregate — the question there is "is anything I can assess
  unhealthy", and one unassessable resource does not invalidate the others'
  verdicts. Exit 2 returns only when *no* resource produced a verdict.

### Exit codes

A scrape runs from cron or a systemd timer, where the exit code is the only
signal anyone sees, so it is specified rather than left to the implementer.

**`check` is the exception** and is tabulated separately below, because its
exit code answers the health question rather than reporting whether aiform
could answer it.

For `metrics` and `status`:

| Code | When |
|---|---|
| 0 | the command ran — **including** when resources reported `FAILING`, `UNKNOWN`, or an unsupported capability |
| 2 | the command could not run: malformed state, an unwritable `--output` or one whose parent directory is missing, an unknown `--format`, or a `<name>` that is unknown or ambiguous |

**0 on `FAILING` is the important one.** The resource's health belongs in the
metrics, where an alert rule evaluates it with history and a `for:` duration —
not in the exit code, where a cron wrapper turns one bad scrape into a page.
An implementation returning 1 on any `UNKNOWN` would page on exactly the
transient blip the four-state design exists to absorb. There is no code 1 for
these three.

For `check` — the only command whose exit code answers the question rather
than reporting whether it could answer:

| Code | Named `<name>` | No name (every resource) |
|---|---|---|
| 0 | the verdict is `OK` | at least one verdict was produced **and** every verdict is `OK` |
| 1 | the verdict is `DEGRADED`, `FAILING` or `UNKNOWN` | any verdict is `DEGRADED`, `FAILING` or `UNKNOWN` |
| 2 | no verdict: name unknown or ambiguous, `health()` declined, unreadable state | no verdict from **any** resource: nothing tracked, or not one driver implements `health()` |

Three things this rule decides, each of which could reasonably have gone the
other way:

- **A declining driver does not fail the aggregate.** It is listed as
  `unsupported` and skipped. The alternative — any decline fails the gate —
  makes the fleet form unusable today, when no driver implements `health()` at
  all, and would keep it unusable until every driver did.
- **But "nothing answered" is exit 2, not exit 0.** This is the important half
  of the previous point. Leniency about *some* resources declining must not
  become a gate that passes because it assessed nothing, which is exactly what
  `0` would mean on a fleet where every driver declines. A coverage line
  (`2 of 3 resources report health; 1 unsupported`) prints either way, so
  partial assessment is visible rather than inferred from an exit code that
  cannot express it.
- **`UNKNOWN` fails.** A resource aiform could not reach is not a resource
  known to be healthy. This is the opposite of what the prometheus rendering
  does with `UNKNOWN` — there it emits no `up` series at all, letting the next
  scrape answer — and the asymmetry is deliberate: a scrape can afford to say
  nothing and try again in fifteen seconds, a script about to run the next
  deploy step cannot.

`DEGRADED` exiting 1 while `aiform_resource_up` renders it as `1` is not an
inconsistency. The gauge answers "is it serving" for an alert rule with
history behind it; `check` answers "is this fully healthy" for a script that
is about to do something next, and a half-attached firewall is not a green
light for the deploy step after it. `UNKNOWN` exits 1 for the opposite reason
to the gauge omitting it: a scrape can afford to say nothing and let the next
one answer, a script gating on it cannot.

### `render_json` shape

A consumer contract, so it is fixed here rather than left to the renderer:

```json
{
  "elapsed_seconds": 0.83,
  "errors": ["dropped family 'queue_depth': compute says gauge, firewall says counter"],
  "resources": [
    {
      "resource_key": "digitalocean.compute.web-01",
      "provider": "digitalocean", "resource_type": "compute",
      "name": "web-01", "id": "123456789",
      "health": {"status": "ok", "summary": "active, public v4 assigned",
                 "observations": {"status": "active"}},
      "health_unsupported": null,
      "samples": [{"name": "memory_bytes", "kind": "gauge",
                   "value": 2147483648.0, "labels": {}}],
      "samples_unsupported": null,
      "errors": ["dropped sample 'cpu%': name is not a valid metric name"]
    }
  ]
}
```

Names are **bare** here, as the driver returned them — unlike the prometheus
rendering, which prefixes and stamps identity labels. A JSON consumer already
has the identity fields beside the samples, and duplicating them into every
label map would be noise.

The top-level `errors` array carries only what has no `ResourceReading` to hang
on — today just a family-level rejection from validation. A per-sample
rejection has a resource, so it appears in that resource's own `errors`
instead. Without the top-level array a family rejection would exist only in
the log, and a JSON consumer would see a short `resources` list with no
indication anything was dropped. `render_metrics` in `text` form prints the
same list; in `prometheus` form it cannot, so it logs it at `WARNING` — an exposition file
has no channel for prose, and inventing an `aiform_scan_errors` counter would
be a metric nobody asked for.

**The `.prom` suffix warning is not one of these.** `--output foo.txt` with
`--format prometheus` is known to be wrong before the sweep runs, from the
arguments alone, so `cli.py` warns on stderr and never reaches `Collection`.

## Out of scope

- **Data-plane reachability** — TCP connect, HTTP request, DNS resolution
  against the resource itself. Excluded for the reason given under "What
  `health()` may look at": it makes the verdict a property of aiform's network
  location rather than of the resource. Reopening this needs a design pass that
  answers where the check runs from, not just a new method.
- **A long-running `/metrics` exporter.** `aiform resource metrics` is one-shot.
  A daemon Prometheus scrapes directly would be this repo's first inbound
  socket and first server dependency, with auth, TLS and lifecycle all
  undesigned. It belongs with `PLAN.md` §10's "Centralized server support",
  which names the direction without committing to an architecture.

  **A flag that prints the URL Prometheus should scrape belongs there too,
  and cannot exist before it.** aiform serves no endpoint, so it has no URL
  to name: the address Prometheus hits belongs to whatever transport carries
  the payload — node_exporter, a Pushgateway, an agent — and aiform neither
  chooses nor knows it. A flag printing a guess would be worse than none. Once
  the exporter exists it owns an address, and printing that is a reasonable
  feature *of the exporter*.
- **Prescribing a transport** — how the payload reaches Prometheus is a
  deployment decision. See Decisions, "aiform does not prescribe a transport".
- **Historical storage.** aiform holds no time series. These commands are stateless; the
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
- that `refresh_state()` and `build_destroy_plan()` contain the
  walk-every-tracked-resource loop `collect()` mirrors
- that `refresh_resource()` itself writes nothing — every `state.save()` is in
  `refresh_state()`, `build_create_plan()` or `apply_plan()`
- that `planner.diff_attributes()` iterates `desired.items()`, so an extra key
  in `current` can never diff — and that `compute.py` and `firewall.py` both
  already rely on this
- that `firewall.py`'s `read()` projection deliberately drops `status` and
  `pending_changes`, which is why its health verdict cannot come from `read()`
- that every existing driver already bounds its calls at
  `REQUEST_TIMEOUT_SECONDS = 30`, so the hazard is an inherited 30s bound, not
  an unbounded call
- that `forbid_llm_client` is opt-in rather than autouse, and covers only the
  `llm.build_client` seam
- that `cli.py`'s `_dispatch()` hardcodes `if args.command == "init"` and
  otherwise reads `args.plan_command`, and that `main()` reads `args.verbose`
  before dispatching
- that `prompts/review_driver.md` is reached by no code path, so `/code-review`
  at PR time is the only gate these rules have

*Verified against node_exporter's own documentation* — that the textfile
collector globs `*.prom` only, that an explicit timestamp is an error which
skips the whole file (`prometheus/node_exporter#1284`), and that temp-plus-
rename is the documented write pattern.

*Inferred, not verified* — that the three-state-to-binary `up` mapping is the
right loss, that ≤5s is the right per-resource target, that per-resource
credential failure (rather than `refresh_state()`'s abort-everything) is right,
and that `observations` stays small enough to render. All four need one real
driver implementation, and this spec should be edited after it.

*Recalled, not verified* — that a second `# TYPE` line for one metric name is a
hard parse error rather than a tolerated duplicate. The renderer groups by
family regardless, so nothing depends on the distinction; confirm it only if
that grouping is ever relaxed.

## Decisions

Design decisions and the reasoning behind them, kept together at the end
rather than inline. A reader who wants to know *what to build* can stop at
Out of scope; a reader — or a model — who needs to know *why it is this way*,
before changing it, reads here.

**On format**: the standard instrument for this is an Architecture Decision
Record (Michael Nygard's form — context, decision, consequences — usually one
file per decision under `docs/adr/`). This project has no ADR tree, and
spinning one up for a single spec would scatter the reasoning further from the
contract it explains, so these live here in ADR-ish shape. If a second spec
needs the same thing, that is the moment to promote them to real ADRs.

### Why not `read()`

**Decision.** `health()` and `metrics()` are separate driver methods on
commands that never write state, rather than a second caller of the existing
`read()` refresh path.

**Reasoning.** A driver's `read()` deliberately drops the fields health needs.
`firewall.py`'s `_project()` excludes `status` and `pending_changes` because
storing them would rewrite `state.json` on every refresh — and those two are
exactly what its health verdict is made of. Separately, every existing caller
that refreshes calls `state.save()`, and there is no locking (`PLAN.md` §10),
so a command polled for observation would churn state and can race an `apply`.

**Not a reason, though two earlier drafts said so.** That an extra attribute
would cause a permanent diff: `diff_attributes()` iterates `desired.items()`,
so a key present only in `current` can never diff — `compute.py` already
returns `status` and `ipv4_address` that way. And that `read()` itself writes
state: `refresh_resource()` writes nothing; the `save()` calls are in its
callers. Recorded because the false version ruled out delegating to `read()`,
which is now explicitly allowed.

### Three verbs, not four

**Decision.** `check`, `metrics`, `status`. An earlier draft had a fourth,
`scan`, for the fleet sweep.

**Reasoning.** `metrics <name> --format prometheus` already emitted exposition
format for one resource, so `scan` was the same operation at a different
scope. Folding it into `metrics` with no name meaning *all* removed a
surface, and removed the file-path selector with it — a third way of saying
what naming a resource, or not, already said.

### No `--all` flag

**Decision.** Omitting `<name>` means every tracked resource. There is no
`--all`.

**Reasoning.** `plan refresh`, `plan show` and `plan create` all operate on
everything when given no arguments, and `plan destroy` does too while being
destructive; a read-only command needing a flag to do what its siblings do by
default is the inconsistency. Keeping `--all` as a synonym would be a second
spelling of one meaning — what `specs/driver.md`'s "one writable spelling per
value" addendum warns against for driver fields, applied to the CLI.

### `check`'s exit code carries the verdict; nothing else's does

**Decision.** `check` exits 0 only when every verdict is `OK`. `metrics` and
`status` exit 0 whenever they ran.

**Reasoning.** `check` exists to be written as `aiform resource check web-01
&& ./smoke-test.sh`, and an assertion that exits 0 when the thing is down is
useless. The others are reports: a `FAILING` resource is an answer, and a
wrapper that treats it as a command failure pages on one transient blip.

Two consequences that must travel together. A driver declining `health()` is
listed but does not fail the aggregate — otherwise the fleet form is unusable
until every driver implements `health()`. But *nothing* answering is exit 2,
not 0, so leniency never becomes a gate that passes having assessed nothing.

`UNKNOWN` fails the gate while being *absent* from the `up` gauge. Not an
inconsistency: a scrape can afford to say nothing and let the next one answer;
a script about to run the next deploy step cannot.

### aiform does not prescribe a transport

**Decision.** The spec fixes the payload and says nothing about how it reaches
Prometheus.

**Reasoning.** Prometheus pulls over HTTP and aiform serves none, so something
always carries it — a textfile collector, a Pushgateway, an agent, a bastion
host on a timer, an exporter that does not exist yet. An earlier draft wrote
one of those into the spec as *the* path. It was a legitimate deployment, but
committing to it baked an architecture into a driver contract, and two of the
four caveats it carried were consequences of that assumption rather than
properties of this design.

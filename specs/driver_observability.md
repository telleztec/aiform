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
`metrics` over the whole fleet when you omit the name. Feeding a monitoring
system continuously is a separate project — `PLAN.md` §10, "Metrics pipeline
integration".

## Use cases

The three an operator actually has, and what each one needs from the contract.
They are the reason the surface is three verbs rather than one sweep.

### 1. I just deployed this — is it up, and is it doing anything?

```
$ aiform resource check web-01
ok  digitalocean.compute.web-01  active, public v4 203.0.113.10

$ # ...apply some load...
$ aiform resource metrics web-01
gauge  memory_bytes  2147483648
gauge  cpu_percent   41.2
```

Two things this demands that a fleet sweep does not. **`check` is an
assertion, so its exit code carries the verdict** — `0` for `OK`, non-zero
otherwise — because the natural next thing anyone writes is `aiform resource
check web-01 && ./smoke-test.sh`. And **`metrics` is read twice, by eye,
minutes apart**, to see a number move under load; its default format is
therefore aligned text.

### 2. Someone says it's down

```
$ aiform resource check web-01
failing  digitalocean.compute.web-01  status is "off"

$ aiform resource status web-01
deployed  2026-09-10T14:02:11Z, id 123456789
live      present
config    in sync with examples/web.aiform.md
health    failing — status is "off"
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
| Shape | a status URL, CI-run-status-page-like | a command a human runs |
| Lifetime | spans one run | answers about right now |

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
`delete()`. A driver renaming `id` to `resource_id` would be rejected by the
`OPTIONAL_METHOD_PARAMS` check, which `specs/driver_gen.md` specifies and
`aiform/driver_gen.py` does not yet implement.

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

    resource_key: str  # the fleet form heads each resource's rows with this
    name: str
    deployed: str | None  # last_applied_at + id, or None if not in state
    live: str  # "present" | "missing on the provider" | an error
    config: str  # "in sync with <path>" | "<n> fields drifted: a, b" | "no source file found" | "not applicable: resource is gone"
    health: HealthReport | None
    health_unsupported: str | None


def resolve_name(name: str, st: State) -> str:
    """A `name:` frontmatter value -> the one matching state key.

    Raises ValueError naming what is tracked when nothing matches, and
    listing the candidates when more than one does. Never guesses --
    two matches can be a droplet and the firewall in front of it."""


def collect(
    *, keys=None, want_health=True, want_metrics=True, state_path=state.DEFAULT_STATE_PATH
) -> Collection:
    """Read tracked resources: exactly those in `keys`, or every one when
    `keys` is None. The two flags say which driver methods to call --
    `check` wants health only, `metrics` wants metrics only, `status`
    wants health. An unwanted method is never called, so no verb pays for
    a provider request it does not use. Reads state; never writes it.
    Makes zero Anthropic API calls."""


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
```

### `aiform/cli.py`

```
aiform resource check   [<name>] [--format text|json] [--state-file <path>]
aiform resource metrics [<name>] [--format text|json]
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

Each verb calls exactly what its question needs: `check` calls `health()`,
`metrics` calls `metrics()`, `status` composes both with a state lookup and a
live `read()`. An earlier draft had `metrics` call `health()` too, so its
output could carry a `up`-style series; that series belongs to the deferred
exporter (`PLAN.md` §10), and without it the extra call bought nothing.

**`check`'s exit code is the one exception in this whole surface**, and it is
deliberate. Everywhere else — `metrics`, `status` — a non-zero exit means
*aiform could not answer*, never *the answer was bad*. `status` exits 0 on a
`FAILING` resource precisely so a wrapper does not fail on one bad reading;
`metrics` never sees a health verdict at all, and exits 0 whenever it ran. `check` inverts that because it exists to be used as an assertion
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
red/green depends on which machine ran the command is worse than none.

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

A flat list of `Sample`. The driver supplies a **bare** name — `memory_bytes`, not
`aiform_memory_bytes` and not `digitalocean_droplet_memory_bytes`. Nothing
prefixes it today; both renderers print what the driver returned, beside the
resource's identity. A future exporter is where a prefix and identity labels
get applied, and it needs the name unqualified to do that.

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
- The reading happens when the command runs; a consumer that needs a
  timestamp has its own.

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
   `name` or `id` in `Sample.labels`. The output already carries the
   resource's identity beside its samples, so repeating it in every label map
   is redundant — and a future exporter, which must stamp those labels to make
   the series addressable, cannot do so safely if a driver has already put its
   own values there. A driver that sets one has **that sample** dropped (see
   Edge cases).

### `collect()`

1. Load state. Sweep the entries named by `keys`, or every entry in
   `st.resources` when `keys` is `None`.
2. Cache drivers by `(provider, resource_type)` via
   `orchestrator.load_driver()` — not a private copy of it, so these commands and
   `plan` can never disagree about which driver file they loaded — and
   credentials by provider. This mirrors `refresh_state()`'s caching shape;
   it does not call `refresh_state()`, which saves state.
3. Per resource, call the methods the flags ask for — `health()` when
   `want_health`, `metrics()` when `want_metrics` — each guarded
   independently, so one being unsupported or raising does not skip the other.
   A method the caller did not ask for is never called, and its columns in the
   table below simply do not arise for that verb.
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

All three verbs take `--format text|json`. `--output` stays on `metrics`
alone, since it is the one whose output an operator accumulates across runs.

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

**`ResourceNotFoundError` is reported by whichever verb saw it.** `check` and
`status` call `health()` and render `FAILING`. `metrics` does not call
`health()` at all, so a vanished resource would otherwise print an empty block
and exit 0 with nothing recorded — instead it records
`"resource not found"` in that resource's `errors`, which the text and JSON
forms both show. An earlier draft justified staying silent here on the
grounds that `health()` had already reported it; no verb calls both methods
any more, so nothing had.

### Rendering

`--format json` is the structured form, per the shapes below. `--format text`
is aligned columns, and the layout is a rule rather than an example, since a
test has to assert exact output:

- **`check` prints one line per resource**, never a block:
  `<status>  <key>  <summary>`. The key is inline because the line is the
  unit — a fleet check is a list you scan down.
- **`metrics` and `status` print rows.** With a `<name>` given, the rows
  alone; in the fleet form each resource's rows are preceded by a header line
  naming its key and indented two spaces beneath it, since a bare row cannot
  say which resource it belongs to. `metrics`' rows are
  `<kind>  <name>  <value>`; `status`' are `<label>  <value>` over the four
  labels.
- **Every column is padded to the widest value in that column across the
  whole output**, two spaces between columns, no trailing whitespace. In the
  fleet form that means one alignment for all resources, not per-block.
- **Numbers**: a float that is integral within 1e-6 prints without a decimal
  part; anything else prints with `repr()`'s shortest round-trip form. No
  thousands separators, no unit scaling — `2147483648`, not `2.1 GiB`. The
  name carries the unit, and a reader comparing two runs needs the digits to
  line up rather than be re-scaled between them.
- **An empty block** — a resource with no samples, or a declined capability —
  prints its header and one indented line: `unsupported: <reason>`, or
  `no samples`.

`check`'s coverage line is printed last, unindented, only in the fleet form.

There is no third format: it has no consumer until `PLAN.md` §10's "Metrics pipeline
integration" gives it one, and appending (see `--output`) would produce an
invalid document anyway.

A numeric `up`-style series derived from the health verdict belongs to that
project too. Here a verdict is a verdict: `check` prints it as a word and
returns it as an exit code, and nothing maps it to a number.

### Validation before a line is written

A driver's mistake is caught in `collect()`, before any renderer sees it.
These rules constrain what a driver may return, not what a file may contain —
they exist so a `Sample` is already valid wherever it is later sent, including
by the deferred integration project, which has no chance to renegotiate with
the driver:

- **Metric name** — must match `[a-zA-Z_:][a-zA-Z0-9_:]*`, the character set
  a metrics system will require of it later. A driver returning `cpu%` or
  `disk-free` is dropped with an error naming the driver.
- **Label names** — must match `[a-zA-Z_][a-zA-Z0-9_]*`. Label *values* are
  arbitrary UTF-8; each renderer escapes them its own way.
- **`COUNTER` whose name does not end in `_total`** — dropped with an error.
- **Non-finite value** (`NaN`, `±Inf`) — dropped with an error. The format has
  spellings for these, but a driver producing one has almost always divided by
  an unchecked zero.
- **Two drivers emitting the same bare name with different `MetricKind`** —
  the whole family is dropped with an error naming both drivers. A consumer
  that types a series by name, as Prometheus does, cannot hold both, and
  silently picking one would mistype the other's samples.

**No timestamps.** `Sample` carries none: the reading happens when the command
runs, and a consumer that needs one has its own clock.

**Where this runs: `collect()`, not a renderer.** Specifying it under
one renderer alone would let `cpu%` through unvalidated in the other,
reporting the same driver bug differently depending on a flag. Each `Sample` is validated once, on the way out of the sweep; both renderers
receive the same already-validated set.

**Where a rejection is recorded follows what it belongs to.** A per-sample
rejection — a bad name, a mistyped counter, a non-finite value, a colliding
label — goes in that resource's `ResourceReading.errors`, because there is a
resource that produced it and an operator asking "why is `web-01` missing
`memory_bytes`" looks there. Only a finding with no resource to attach to goes
to the top level, and since the file-path selector was removed there is exactly
one: a metric family rejected because two drivers gave the same name different
`MetricKind`s, which belongs to neither driver alone.

### `--output`

**Appends the rendered report to the path, and does nothing else.** No
temporary file, no atomic rename, no suffix rule, no rotation, no pruning,
no cleanup — aiform never deletes or truncates it. A run adds to the end; the
operator removes the file when they are done with it.

This is deliberately the least machinery that serves the use cases. Those are
a human confirming that `plan apply` did what it claimed, and a script a human
wrote that runs the command a few times — for which an accumulating record is
the useful shape, and file management is the operator's. Feeding a monitoring
system continuously is a separate project (`PLAN.md` §10, "Metrics pipeline
integration"), and the atomicity, suffix and lifecycle rules that use needs
belong to it.

**Each run is preceded by a delimiter line**, because an accumulating record
nobody can split is not a record:

```
=== 2026-09-15T05:41:02Z  aiform resource metrics web-01
```

UTC, ISO-8601, then the invocation. **Only with `--output`** — stdout carries
the report and nothing else, so `--format json | jq` stays valid. In a file
`--format json` uses the same delimiter: the file is a stream of runs, not one
document, and a consumer splits on the delimiter before parsing each block. Without it an operator cannot tell which
block came from which run, which is the whole point of appending.

Two further consequences of appending, stated because they are surprising
otherwise:

- **The file is not a valid exposition document**, and is not meant to be.
  Repeated runs repeat metric families, which a Prometheus parser rejects.
  That is why `--format` offers `text` and `json` only; exposition format
  arrives with the integration project that has somewhere to send it.
- **A missing parent directory or an unwritable path is an ordinary error** —
  exit 2, naming the path. aiform does not create the directory.

### stdout, and using these commands in a pipe

**Without `--output`, every format goes to stdout, and stdout is a clean
stream.** That is a contract, not an accident: it is what makes
`aiform resource metrics --format json | jq '.resources[].samples'`
work.

| Channel | Carries |
|---|---|
| stdout | the rendered report, and nothing else |
| stderr | log lines (`log.py`'s `--verbose`-gated echo) and error messages |
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
  --format json | head -20` closes the pipe early, and the default
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

## Edge cases / errors

Per-resource failures are tabulated under "Partial failure never aborts the
sweep" above; per-sample validation under "Validation before a line is
written." What remains:

- **`ResourceNotFoundError`** from `health()` renders as `FAILING`, summary
  `"resource not found"`. It does **not** mark drift: these commands never
  write state. The next `plan` is what records it.
- **A driver-supplied label colliding** with an identity label drops **that
  sample**, not the resource's whole set — matching how every other validation
  failure is scoped. Deliberately not a silent overwrite, which would hide the
  driver bug. `check`'s verdict comes from `health()` and is unaffected.
- **No tracked resources at all** — `metrics` prints an empty result and exits
  0. A reading of an empty formation is not an error.
- **`.aiform/state.json` missing** — empty result, exit 0. Not an error, and
  `metrics` adds no `exists()` check of its own: `state.load()` already returns an
  empty `State()` for a missing path, no other command treats that as a
  failure, and a fresh project should report nothing rather than fail until
  the first `apply`.
- **`.aiform/state.json` malformed** — exits 2. This one genuinely raises
  (`json.loads`, or Pydantic validation), and a command reporting zero resources
  because state failed to parse is worse than one that fails loudly.
- **`--output` to an unwritable path, or one whose parent directory does not
  exist** exits 2, naming the directory. aiform does not create it: a `mkdir
  -p` here writes metrics into a directory nothing reads, which fails silently
  rather than loudly.

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

These commands get wrapped in scripts, where the exit code is the only signal
anyone sees, so it is specified rather than left to the implementer.

**`check` is the exception** and is tabulated separately below, because its
exit code answers the health question rather than reporting whether aiform
could answer it.

For `metrics` and `status`:

| Code | When |
|---|---|
| 0 | the command ran — **including** when resources reported `FAILING`, `UNKNOWN`, or an unsupported capability |
| 2 | the command could not run: malformed state, an unwritable `--output` or one whose parent directory is missing, an unknown `--format`, or a `<name>` that is unknown or ambiguous |

**0 on `FAILING` is the important one**, for `status`. A resource's health
belongs in what the command reports, where a consumer can weigh it —
not in the exit code, where a wrapper turns one bad reading into a failure.
An implementation returning 1 on any `UNKNOWN` would fail on exactly the
transient blip the four-state design exists to absorb. There is no code 1 for
these two.

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
  known to be healthy. A future exported series may choose to say nothing for
  `UNKNOWN` and let the next reading answer; a script about to run the next
  deploy step cannot afford that, which is why the gate fails.

`DEGRADED` exits 1 because `check` answers "is this fully healthy" for a
script about to do something next, and a half-attached firewall is not a green
light for the deploy step after it. `UNKNOWN` exits 1 for the same reason: a
resource aiform could not reach is not a resource known to be healthy, and a
script gating on it cannot afford to wait for a better answer.

### `render_json` shapes

All three verbs emit a single JSON object with a `resources` array. The
per-verb differences are only in what each resource carries.

`check`:

```json
{
  "resources": [
    {"resource_key": "digitalocean.compute.web-01", "name": "web-01",
     "status": "failing", "summary": "status is \"off\"",
     "observations": {"status": "off", "locked": "false"}}
  ],
  "coverage": {"reporting": 2, "total": 3, "unsupported": 1},
  "worst_status": "failing"
}
```

`coverage` is the field the text form prints as its coverage line, so the
information is never a stray line that would make the document unparseable.
`worst_status` is the least healthy verdict seen, ordered
`ok` < `degraded` < `unknown` < `failing`, or `null` when nothing reported —
a `HealthStatus` value, not a second vocabulary. The exit code follows from
it: `0` for `ok`, `1` for anything else, `2` for `null`. An earlier draft
called this field `verdict` and gave it values that reused `failing` to mean
"any bad verdict", which collided with the per-resource `status` beside it.

`status`:

```json
{
  "resources": [
    {"resource_key": "digitalocean.compute.web-01", "name": "web-01",
     "deployed": "2026-09-10T14:02:11Z, id 123456789",
     "live": "present",
     "config": "in sync with examples/web.aiform.md",
     "health": {"status": "failing", "summary": "status is \"off\"",
                "observations": {}},
     "health_unsupported": null}
  ]
}
```

### `metrics`' `render_json` shape

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
      "samples": [{"name": "memory_bytes", "kind": "gauge",
                   "value": 2147483648.0, "labels": {}}],
      "samples_unsupported": null,
      "errors": ["dropped sample 'cpu%': name is not a valid metric name"]
    }
  ]
}
```

Names are **bare** here, as the driver returned them. A JSON consumer already
has the identity fields beside the samples, so duplicating them into every
label map would be noise; a future exporter is where prefixing and identity
labels get applied.

The top-level `errors` array carries only what has no `ResourceReading` to hang
on — today just a family-level rejection from validation. A per-sample
rejection has a resource, so it appears in that resource's own `errors`
instead. Without the top-level array a family rejection would exist only in
the log, and a JSON consumer would see a short `resources` list with no
indication anything was dropped. `render_metrics` in `text` form prints the
same list.

## Out of scope

- **Data-plane reachability** — TCP connect, HTTP request, DNS resolution
  against the resource itself. Excluded for the reason given under "What
  `health()` may look at": it makes the verdict a property of aiform's network
  location rather than of the resource. Reopening this needs a design pass that
  answers where the check runs from, not just a new method.
- **Feeding a monitoring system.** An endpoint Prometheus scrapes, exposition
  format, advertising a scrape target, service discovery, the process
  lifecycle any of that implies — all of it is `PLAN.md` §10's "Metrics
  pipeline integration", which this spec deliberately does not design. What
  lives here is the driver contract those things will export, and a
  human-facing way to read it.

- **Historical storage.** aiform holds no time series. These commands are stateless; the
  consumer stores history. This is also why a driver may not derive a
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

*Inferred, not verified* — that `DEGRADED` should fail `check`, that ≤5s is
the right per-resource target, that per-resource
credential failure (rather than `refresh_state()`'s abort-everything) is right,
and that `observations` stays small enough to render. All four need one real
driver implementation, and this spec should be edited after it.

*Recalled, not verified* — that a metrics system types a series by name, so
two drivers giving one name different kinds cannot both be represented. This
is why such a family is rejected in `collect()`; confirm it when the deferred
exporter is designed.

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

**Reasoning.** `scan` was `metrics` at a different scope and nothing more. Folding it into `metrics` with no name meaning *all* removed a
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
inconsistency with a future exported series, which may choose to say nothing
for `UNKNOWN` and let the next reading answer; a script about to run the next
deploy step cannot afford that.

### The pipeline is a separate project

**Decision.** This spec defines the driver contract and three human-facing
commands. Everything about feeding a monitoring system — exposition format,
an endpoint, advertising a scrape target, service discovery — is `PLAN.md`
§10's "Metrics pipeline integration" and is not designed here.

**Reasoning.** Earlier drafts carried the pipeline's machinery inline:
atomic writes, a `.prom` suffix rule, a file lifecycle, a scrape-cost model,
and one prescribed transport. All of it assumed an architecture the project
has not chosen, and most of it was unreachable from anything the commands
actually do today. Separating them leaves a small feature that works — verify
that `plan apply` did what it claimed — and a named future project that can
be designed against real requirements rather than guessed ones.

**Consequence.** `--output` appends and aiform manages nothing; `--format`
offers `text` and `json`; `Sample` still constrains names, kinds and units,
because those are properties of what a driver returns and the exporter will
have no chance to renegotiate them.

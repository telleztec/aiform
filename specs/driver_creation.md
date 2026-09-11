# specs/driver_creation.md — the driver-creation loop

## Purpose

How a driver for a new `(provider, resource)` pair gets made, in a few
hours, from five sources used additively: the provider's documented
signature, what previous drivers learned, live probes for what the
documentation does not say, an independent review, and a real system
test. Encode what the API actually does, and keep what was learned where
the next driver can find it.

This spans a process, a tool (`scripts/probe_api.py`), a per-driver audit
log, and a knowledge base — one spec rather than four, on the precedent
of `specs/unordered_fields.md`, which likewise spans a module plus its
integration points.

**Scope across the two mechanisms.** `PLAN.md`'s "Driver curation"
defines mechanism 1 (hand-authored via Claude Code, `PROCESS.md`'s loop)
and mechanism 2 (`aiform driver create`, §7). This loop is **the same
loop in both**. Mechanism 1 runs it with a human driving; mechanism 2
runs it with the model driving and the human approving. That is the
point: mechanism 1 is how the loop gets tuned into something mechanism 2
can execute against a different CSP, for a different end user, on a
different model. Cross-references `PLAN.md` §10's "Self-service driver
creation is not implemented, and is not currently being built" — this
spec does not build it, it specifies the loop that entry's target shape
must execute.

**Build status.** Built: the loop, the probe harness
(`probes/_probe.py`), the audit log (`probes/_audit.py`, and one opened
at `knowledge/drivers/digitalocean_firewall/`), and the firewall driver
and system test that came out of them. **Not built**: `knowledge/`'s
`INDEX.md`, `csp/`, `driver/` and `probing/` trees, `scripts/knowledge.py`,
`scripts/probe_api.py`, `probes/_template.py`, and every mechanism-2
integration below. Individual sections say which is which.

## Five sources, used additively

A driver is built from five sources, in cost order. **None replaces
another**, and reaching for a later one before an earlier one is waste:

| Source | Cost | Catches | Cannot catch |
|---|---|---|---|
| **Documented signature** (OpenAPI) | free | endpoints, field names, types, required/optional, status codes | what the API does that it does not say |
| **Knowledge recall** (`knowledge/`) | free | where this CSP has lied before, what a prior driver was bitten by | anything nobody has met yet |
| **Probing** | minutes, one live account | silent rewrites, server-added fields, real rejection shapes, relationship edges | whether the resulting *diff* is right |
| **LLM review** | minutes | diff-level errors: tautological tests, overclaims, unreachable branches | what the API actually does |
| **System test** | minutes, real resources | integration through the CLI, the zero-diff invariant end to end | the individual behaviors above, cheaply |

**The documented signature is what makes probing cheap; probing is what
makes the signature trustworthy.** A prediction written from the OpenAPI
schema is a good prediction, and only a good prediction makes a
contradiction informative — probing without the documented shape in hand
is enumerating an API blind. Docs generate the hypotheses, probes test
the ones that matter, and the knowledge base remembers which ever failed
so the next driver stops asking.

What probing adds is narrow and specific: **what the API does but does
not say.** No published schema describes a rewrite-on-store, because
from the provider's point of view nothing was violated — and
`diff_attributes()` compares `read()`'s output against the user's raw
`params` with no hook to normalize either side, so a silent rewrite is a
**permanent diff**, and a permanent diff permanently defeats the
zero-LLM-call short-circuit for that resource.

Measured, building one driver against DigitalOcean firewalls: **31
probes, 7 whose HTTP status contradicted the prediction** — where the
predictions came from DigitalOcean's own published OpenAPI spec. They
fall into three kinds, and the split matters more than the count:

| Kind | Probes | Example |
|---|---|---|
| accepted, then silently rewritten | `04`, `05` | `ports: 22` stored as `"22"` |
| rejected where the schema implied acceptance | `08`, `19`, `20` | a firewall with no rules is a 422 |
| accepted where the schema implied rejection | `12`, `18` | `action: "deny"` is stored; an empty `sources: {}` is allowed |

**The status code is not where most of the value was, and the tooling
should not be mistaken for measuring it.** `Probe.call()` compares only
`predict["status"]`; `predict["notes"]` is recorded but never compared,
so a probe can match on status and still overturn a belief. Probe `02`
is the example: it predicted 202 and got 202, while its notes predicted
`status: "waiting"` and got `"succeeded"` — and the same response
carried the undocumented `action` field. A notes-level surprise has to
be caught by whoever reads the transcript and written up as a finding,
so the `verdict=contradicted` count is a floor on the loop's yield, not
a measure of it.

The schema got the driver most of the way in minutes; the probes got the
part that decides whether it converges.

## The time budget

**A new driver must be achievable in a few hours.** An end user will not
adopt a process slower than that, and a process nobody runs protects
nobody. This is a constraint on the loop, not an aspiration: every rule
below that adds work has to earn it against this budget.

The one measurement so far: `drivers/digitalocean/firewall.py` went from
its first probe to the last review round landing in **1h18m**
(`2026-09-10T00:09:09Z`, transcript `01` **as committed in `b3d0cde`**,
to `01:27:21Z`, commit `9855f9c`). The commit matters: `ede2694`
re-recorded every transcript, so transcript `01` at HEAD carries a later
stamp, and citing it without the commit sends a reader to the wrong
number. The audit log's first and last lines are the wrong bracket too —
they span a later re-run and a later system-test session.

That is inside the budget, but it is one driver on a familiar CSP with
an unusually convenient resource — free, unattached, no convergence to
wait on. It is a floor, not a typical figure.

Concretely:

- **Probe count is bounded by the driver's own surface**, not by
  curiosity. Probe each field the driver will validate or project, each
  documented-vs-observed asymmetry the knowledge base flags for this
  CSP, and the CRUD edges (not-found, idempotent delete, whole-object
  vs partial update). That is tens of probes, not hundreds — 31 for
  firewall, executing in **12 seconds** of wall clock (essentially all
  of it HTTP round-trips). Probing is not what costs the hours;
  deciding what to ask is.
- **Stop probing when the remaining questions are either not
  code-changing, or cheaper to settle by shipping the system test.** The
  system test exercises the whole path once; it is the right instrument
  for anything that needs the resource to exist anyway.
- **Recall is the only thing that makes the budget hold as the knowledge
  base grows.** The second driver on a provider must not re-ask what the
  first settled. `probes_skipped / probes_planned` is recorded on the
  `step=recall` line for exactly this reason.
- **Elapsed wall-clock time is recorded per session**, because a budget
  nobody measures is a preference.

## Interface

### Layout

```
probes/
  _probe.py                     the harness (built)
  _template.py                  seed for a new session (not built)
  <provider>_<resource>.py      one session per driver (built for firewall)
  transcripts/<session>/NN-<slug>.json

knowledge/                      the wiki (not built)
  INDEX.md                      generated; one line per entry
  csp/<provider>.md             behaviors true of a provider, across resources
  driver/<pattern>.md           behaviors true across providers
  probing/<pattern>.md          how to ask a kind of question well
  drivers/<provider>_<resource>/
    AUDIT.log                   append-only, one line per step
    FINDINGS.md                 this driver's findings, each citing a transcript

scripts/
  probe_api.py                  one-shot single call (not built)
  knowledge.py                  index / search / recall / promote (not built)
```

### `scripts/knowledge.py`

```python
def index() -> int                    # regenerate INDEX.md from entry front-matter
def search(query, *, category=None, provider=None) -> list[Entry]
def recall(provider, resource) -> Recall     # what to skip, what to probe, what to review
def promote(finding_id, category) -> Path    # driver finding -> reusable entry
```

`Recall` is the object a session opens with: `established` (probes that
can be skipped, with the entry that settled them), `traps` (probes that
must be run because a prior driver was bitten), and `review_checks`
(assertions a reviewer should apply to this provider or shape).

### Knowledge entry

Front-matter is the index; the body is prose for a human.

```markdown
---
id: csp/digitalocean/scalar-coercion
title: DigitalOcean coerces scalar types on store rather than rejecting
category: csp                  # csp | driver | probing
provider: digitalocean         # omit for category: driver|probing
applies_to: [firewall, compute]
confidence: verified           # verified | inferred | recalled
evidence: [digitalocean_firewall/04, digitalocean_firewall/05]
first_seen: 2026-09-09
---
```

`confidence` uses the same three grades the driver specs already use in
their Knowledge-confidence sections. An entry may only claim `verified`
if `evidence` names at least one transcript.

## Behavior

### The loop

Eight steps. 0 and 1 are the ones that do not exist in an ordinary
spec-first process, and are where the value is.

0. **Recall.** `knowledge.py recall <provider> <resource>` before
   anything else. Skip probes an entry already settles for this
   provider; add the probes its `traps` name. Record what was skipped
   and why — a skipped probe is a claim, and it needs the same citation
   a run one does.
1. **Question and predict, from the documented signature.** One factual
   question, plus what it settles — a behavior, an object's shape, or a
   relationship edge. Write the expected request *and* response **before
   sending**, taking both from the provider's OpenAPI spec wherever it
   says anything. This is the step that keeps the loop inside its
   budget: the schema supplies the shape for free, so a probe is
   confirming or refuting a specific documented claim rather than
   exploring. A question the schema already answers unambiguously, and
   that no knowledge entry flags as a place this CSP lies, does not need
   a probe at all.
2. **Probe.** Run it. A transcript is written carrying both the
   prediction and the result.
3. **Compare.** A mismatch is the finding. A match is *also* recorded:
   that is what turns "recalled" into "verified".
4. **Encode into the spec.** Land it in Behavior or Edge cases, and as a
   Knowledge-confidence bullet **citing the transcript**.
5. **Encode into a failing test.** The mock payload is loaded from that
   transcript, never hand-written. Observe it fail.
6. **Implement.** Minimum code.
7. **Verify, then consolidate.** Full suite green, then re-run the probe
   and confirm the driver reproduces the transcript. Then promote any
   finding that generalizes into `knowledge/`.

### Confidence

Every step that changes what is *known* carries a **confidence score**
for each claim the driver rests on: how far the current model of this
resource is believed to match reality. The steps that only record work
done — a test written, a review run, a fix applied — carry none, and the
table below says which. Like `step=spec` and `step=learn`, and unlike anything
the harness emits on its own, it is assigned by whoever runs the loop.
The harness cannot know how much a claim deserves to be believed.

Five bands, each named for the evidence that reaches it and recorded as
that band's anchor value:

| Score | Band | Reached when |
|---|---|---|
| 10 | `guessed` | neither documentation nor a probe settles it; the behavior is assumed |
| 40 | `documented` | the schema states it, and nothing has tested whether it is true |
| 60 | `observed` | a live probe settled it — agreeing with the prediction, or contradicting it and the corrected behavior then encoded from that same transcript |
| 80 | `reproduced` | the driver reproduces the transcript, with unit tests loading their payloads from it |
| 95 | `converged` | the live system test passes and its second run is a zero-diff no-op |

`step=spec` has no uncited form: `probes/_audit.py` refuses to record
one at all, rather than recording it at a lower band. That is deliberate
— a claim with no transcript behind it is the failure this process
exists to prevent — but it means a line that asserts nothing about the
resource must not be a `step=spec`. Corrections to the record itself
(a miscounted tally, a bad timestamp) are `step=note`, which takes
neither `cites=` nor `conf=`. Without that step the only way to log one
was to attach a citation that did not support it, and a gate satisfied
by decoration is worse than no gate.

Only the transitions carry meaning. The numbers exist so a score can be
compared across passes and nothing more — **do no arithmetic on them**,
and do not record a value between the anchors, which would imply a
precision the judgement does not have.

What each step may move, and on what evidence:

| Step | Moves it to | Rubric |
|---|---|---|
| 0 Recall | the starting band | an entry that settles the question starts at `documented`; an entry whose `traps` name this behavior starts at `guessed`, however clear the schema is |
| 1 Question and predict | `documented` | the schema answers fully and unambiguously. Silent, partial or ambiguous leaves it `guessed` — and that gap is what earns the probe its call |
| 2 Probe | nothing | issuing a request is not evidence. Only comparing its result is |
| 3 Compare | `observed` on agreement, `guessed` on a contradiction | agreement raises it. A contradiction **lowers** it, because the model that produced the prediction was wrong; step 4 is where it climbs back |
| 4 Encode into the spec | `observed`, for a contradiction step 3 knocked down | this is where a corrected claim recovers, and only because the line cites the transcript that corrected it. A claim citing nothing may not be recorded at all |
| 5 Failing test | nothing | red is a property of the test, not of the knowledge |
| 6 Implement | `reproduced` | suite green, with the mock payload loaded from the transcript |
| 7 Verify, consolidate | `converged` | the re-run probe reproduces the transcript **and** the system test's second run is a no-op |

**A pass that moves no score is the red flag.** If a full turn through
steps 1–3 leaves every claim in the band it started in, the probes are
not buying information — the questions are either ones the schema
already settled or ones about something other than this driver's
surface. Change the questions rather than spending more budget. A loop
that cannot move a score is the shape a runaway budget takes, and it is
the one failure the time budget above cannot catch by itself, because
each individual pass looks affordable.

The corollary is that documentation quality is visible before any probe
runs. A resource whose schema is complete starts at `documented` across
the board and needs probes only where the knowledge base flags this CSP
as unreliable; a resource whose schema is thin starts at `guessed` and
earns its probes.

### Which probes are legitimate

Probe **behavior** — deterministic, and one observation settles it
("does this endpoint coerce an int port?"). Characterization probes
count even when no code changes today, because an accurate model of the
resource is what the next driver recalls instead of re-deriving. But
"freely" is bounded by the budget above: the surface worth probing is
the driver's own, and a question the documented signature already
settles is not worth a call.

Probe **dependency edges** — a relationship between resources the
documentation does not state, or that the implementation does not make
obvious. These earn a call even when the driver's own fields are all
settled, because an undocumented precondition does not surface at
validation time: it surfaces at apply time, as a runtime failure against
a resource the user has already been told is being created. Probes `19`
and `20` are the in-repo instance — a referenced tag must already exist,
which DigitalOcean documents nowhere, and which is the opposite of
droplet creation, where the same API auto-creates it.

Refuse a probe whose **passing** result would be read as a guarantee it
cannot support. The type is a *stability property* — "is this
collection's order stable?" — where green means only "stable at this
cardinality, on this account, this once" but lands as a green check
licensing "stable". Record those as assumptions with their reasoning
instead. `specs/unordered_fields.md` is the in-repo instance of this,
not the reason for the rule.

### The audit log

One append-only file per driver, `knowledge/drivers/<p>_<r>/AUDIT.log`,
written by **both** mechanisms. It is the human's window into a driver's
creation, and the artifact a review is conducted against.

Format follows `specs/log.md`'s convention — same UTC
`%Y-%m-%dT%H:%M:%SZ` stamp, same bare `key=value`, same trailing
`msg="free text"` — but drops `LEVEL` and `logger_name`, which would be
constant on every line, and lives in its own file rather than
`.aiform/logs/`: this is a durable record of how a driver came to exist,
not runtime diagnostics for one command.

**A line that asserts something about the resource carries `conf=`**,
the band anchor from the rubric above: `recall`, `probe`, `spec`,
`impl`, `verify` and `learn` do. `test`, `review`, `fix` and `note` do
not — none of them changes what is known, only what is written, fixed,
or said about the record itself. `probes/_audit.py` enforces the
negative half only — it refuses `conf=` where the rubric forbids it, and
does not require it where the rubric expects it. The harness sets it on
the two lines whose value is mechanical (nothing recalled, and a
contradicted prediction, are each `guessed` by definition). Its third
line, the closing `step=probe count=N` summary, carries none: a session
has no single confidence, and inventing one would be the judgement the
harness is not entitled to make.
Reading the `conf=` column down the file is how a reviewer sees at a
glance whether the loop converged or spun, which is the red flag above
made visible without re-reading every message.

```
2026-09-09T23:58:00Z step=recall  provider=digitalocean skipped=0 traps=0 conf=10 msg="no prior entries"
2026-09-10T00:05:12Z step=probe   ref=02 verdict=contradicted conf=10 msg="create unattached -> 202 but status=succeeded, not waiting"
2026-09-10T00:05:14Z step=probe   ref=04 verdict=contradicted conf=10 msg="ports 22 accepted, stored as \"22\""
2026-09-10T00:31:00Z step=spec    ref=behavior/create cites=02 conf=60 msg="unattached create returns status=succeeded, nothing to converge"
2026-09-10T00:40:11Z step=test    ref=test_does_not_poll state=red
2026-09-10T00:52:03Z step=impl    ref=create state=green tests=32 conf=80
2026-09-10T01:10:00Z step=review  round=1 model=fable findings=10
2026-09-10T01:35:00Z step=fix     round=1 fixed=10 msg="nested target types, required fields, sweep age floor"
2026-09-10T02:05:00Z step=verify  kind=live result=pass conf=95 msg="zero-diff after create and update"
2026-09-10T02:10:00Z step=learn   promoted=2 conf=95 msg="csp/digitalocean/scalar-coercion, probing/reset-vs-unchanged"
```

A contradicted probe records `conf=10`, not the band it held before: the
prediction's model was wrong, so what the schema said is no longer
evidence for anything. The `spec` line that follows is where it climbs
back, and only because that line cites the transcript.

`knowledge/drivers/digitalocean_firewall/AUDIT.log` was opened before
this field existed, so its earlier lines do not carry it and are not
backfilled — the file is append-only. Lines appended after it does.

Rules that keep it auditable:

- **One line per decision, not per action.** A 31-probe session emits a
  line per probe only for contradictions and for probes a spec claim
  cites; the rest are summarised in one `step=probe count=31` line. A
  whole driver's creation should read in **20–40 lines**.
- **Every `step=spec` line carries `cites=`**, or the claim is not
  allowed to say "verified".
- **`verdict=` is only `contradicted` or `confirmed`**, so
  `grep verdict=contradicted` is the finding list. It is a convenience,
  not a guarantee: field values are quoted, so a field-aware reader
  cannot be fooled into seeing a verdict a value merely contains — but
  grep is not field-aware, and a quoted `msg` carrying that text will
  match. Only
  `contradicted` is emitted today: a confirmation is recorded in the
  transcript's `prediction_matched`, and writing a line per confirmed
  probe would bury the findings in a file whose whole value is that it
  is short.
- **`step=learn` and `step=spec` are written by the person or agent
  running the loop, not by the harness**, which emits only `recall` and
  `probe`. The harness cannot know what a spec claim cites.
- **Never a credential, never a full payload.** Evidence is a transcript
  reference; the payload lives there.
- **Append-only.** A correction is a new line, never an edit — the
  history of what was believed is the record's value.
- **A timestamp records when the line was appended, not when the work
  happened.** Entries are commonly written in a batch at the end of a
  round, so several may sit a second apart describing work spread over
  an hour, and the file's order is the order they were written in. Read
  the sequence from the `step=` values and the `round=` numbers, not
  from the clock. Recording a stamp the writer wishes were true would be
  worse; this is the honest reading of what the field means.

### Human review and observability

Both mechanisms, same artifacts, so the two are comparable:

| | Mechanism 1 | Mechanism 2 |
|---|---|---|
| Who drives | human via Claude Code | `aiform driver create` |
| Approval | PR review, `/claude-merge-approved` | per-step prompt, live |
| Step record | `AUDIT.log`, appended by the process | `AUDIT.log`, appended by the command |
| Live view | terminal + `AUDIT.log` | `--verbose` + `AUDIT.log` |
| Gate | `/code-review` rounds until head is covered | gate #1 (`driver_gen.py`) **plus** a review round |

A probe step is the right granularity for one approval prompt: *"I want
to POST this body to this URL to find out X; may I?"* That also fits
`PLAN.md` §6's constraint that no live system test runs before approval
— probes are safe precisely because **no generated code runs**; they are
hand-driven HTTP calls whose findings are what generation is grounded on.

**The tuning signal.** Because both mechanisms write the same log, a
mechanism-2 run can be diffed against a mechanism-1 run for the same
resource: which probes it skipped, which findings it missed, how many
review rounds it needed. That diff is how mechanism 2 gets tuned, and it
is the reason the log format is fixed before mechanism 2 is built.

### What mechanism 2 must do differently

Six changes, in leverage order. Each attacks a specific, recorded
failure — `PLAN.md`'s "Driver curation" records three consecutive
generation attempts that all got the explicitly-stated credentials key
wrong and two that silently dropped a whole call sequence, with the
correct answer verified present in the prompt twice.

1. **Ground generation in transcripts *as well as* the schema.** The
   OpenAPI spec stays the primary input — it supplies the shape, for
   free, and is what makes the rest cheap. Transcripts are added for the
   narrow thing it cannot carry: what the API does but does not say. The
   difference that matters is epistemic — a stated fact is an
   instruction, and instructions compete with each other; a transcript
   is an observation, and observations do not. Every session's step 0 records `credentials_key` from a
   call that demonstrably worked. `draft_driver()` already loads
   `specs/<provider>_<resource>.md` as ground truth — it gains a
   transcript index alongside it. This is the change that structurally
   kills the credentials-key failure.
2. **Extend static validation to the whole contract.** Today
   `validate_driver_source()` checks shape — class name, method
   signatures, no `anthropic` import, logger name — and **every one of
   the seven firewall findings would pass it.** Worse,
   `UNORDERED_FIELDS` and `NON_DIFFABLE_FIELDS` appear nowhere in
   `prompts/generate_driver.md`, `prompts/review_driver.md`, or
   `driver_gen.py`, and the generation prompt closes by saying nothing
   outside `PARAM_SCHEMA`/`LIKELY_REPLACE_FIELDS` is expected — so a
   generated driver is *instructed* not to emit the attributes that
   prevent permanent diffs. Add them to both prompts, and add one
   behavioral AST check: **an array property in `PARAM_SCHEMA` must
   appear in `UNORDERED_FIELDS` or be explicitly justified.**
3. **Close the loop with an executable signal.** `generate_driver()`
   retries on a static-validation reason or a reviewer's prose, so it
   cannot learn anything empirical, and `MAX_DRAFT_ATTEMPTS = 2` is a
   coin flip. Derive tests from the transcripts, run them, and feed
   **test failures** back: "expected `ports: '22'`, got `22`" is a
   signal a model can act on.
4. **Make the zero-diff invariant a mechanical gate.**
   `create → read → diff_attributes(read_output, params) == {}` is one
   executable property that catches every phantom-diff failure at once.
   It belongs alongside gate #1, not in a reviewer's judgement. This is
   issue #114 (a GitHub issue; `PLAN.md` carries no such item); firewall
   is its third data point.
5. **Make the probe session the interactive spine** of `aiform driver
   create`, per the approval table above.
6. **Grade every generated claim.** A spec line either cites a
   transcript or is marked `inferred`. Ungraded prose is how a wrong
   belief survives review.

### How the loop learns

The knowledge base is what makes the *fifth* driver cheaper than the
first, and it is the difference between a generator that works on
DigitalOcean and one that tolerates a CSP nobody has tried.

- **Three categories.** `csp/` — true of one provider across resources
  (DigitalOcean coerces scalars on store; tags must pre-exist). `driver/`
  — true across providers (a server-added field absent from the published
  schema is a phantom-diff source; a whole-object PUT makes the ordering
  invariant trivially true). `probing/` — how to ask a kind of question
  well (to test whether an omitted field is *reset* or *left alone*, the
  resource must first hold a non-empty value).
- **Promotion has a bar: two observations.** A finding stays in the
  driver's own `FINDINGS.md` until it is seen a second time, then is
  promoted carrying both citations. One observation is an anecdote, and
  the bar is what stops the base filling with over-generalised
  singletons.
- **What counts as the second observation depends on the category**, and
  this matters because Gates #1 and #2 are deliberately run on one
  provider:
  - `csp/<provider>/` — a claim about *this* provider. Two observations
    in two of its resources. Reachable on DigitalOcean alone.
  - `driver/` — a claim about how CSP APIs behave **in general**. Two
    observations in **two different providers**. No number of
    DigitalOcean drivers can establish one, so these stay candidates
    until Gate #3. "A server-added field absent from the published
    schema is a phantom-diff source" is exactly this shape: true of
    DigitalOcean, unknown of anyone else.
  - `probing/` — a rule about *method*, not an empirical claim about any
    CSP ("to learn whether an omitted field is reset or left alone, the
    resource must first hold a non-empty value"). Two observations of
    the method paying off; same provider is fine, because the rule is
    not about the provider.
- **Recall pays out as skipped probes.** A `csp/` entry marked
  `verified` for a provider means the next driver on that provider does
  not re-ask it. The efficiency metric is
  `probes_skipped / probes_planned`, recorded on the `step=recall` line.
- **Traps pay out as added probes.** An entry may name a probe that
  *must* run for a shape — a driver whose `PARAM_SCHEMA` has a
  list-of-dicts field inherits the phantom-diff probes.
- **Review checks travel too.** A `driver/` entry may carry an assertion
  for reviewers, which is how a lesson reaches the gate that catches the
  errors probing cannot.

### What to measure

Recorded per session, on the closing `step=learn` line:

| Metric | What it tells you |
|---|---|
| contradiction rate | the value of probing. If it reaches zero, prose was already sufficient and the loop is overhead. |
| probes skipped by recall | whether the knowledge base is paying for itself |
| findings that changed the spec *after* implementation began | late findings are cycles; should fall as `_template.py` learns where surprises live |
| review findings not caught by any probe | the disjoint class — see Edge cases |
| **review rounds to a clean pass** | a Gate #1 signal: each round is a generate-fix cycle mechanism 2 would pay for |
| **system-test bugs on the first run** | the other Gate #1 signal: each one is a probe that should have been asked |
| live surprises after "done" | must reach zero; these are the rounds mechanism 2 would otherwise burn |
| **elapsed wall-clock, first probe to green suite** | the budget. A driver that takes longer than a few hours is a process failure, whatever its correctness. |

### Three gates

Readiness is not a date and not a metric threshold. Three gates, in
order, each with a different question:

**Gate #1 — is mechanism 1 smooth?** A judgement call, deliberately.
Keep building DigitalOcean drivers until the loop runs smoothly and
quickly: **minimal LLM review cycles, and zero or minimal system-test
bugs on the first run.** Those two are the signal, because they are what
mechanism 2 would otherwise burn its budget on — a review cycle is a
generate-fix round, and a system-test bug on first run is a probe that
should have been asked. The metrics above inform this call; they do not
make it.

**Gate #2 — does mechanism 2 match?** Once built, re-build one or two
*existing* drivers with mechanism 2 and compare against the mechanism-1
originals. This is the whole reason both mechanisms write the same audit
log in the same format: the comparison is a diff of two `AUDIT.log`
files for the same resource — which probes it skipped, which findings it
missed, how many review rounds it needed, how long it took.

**Gate #3 — does it port?** Build AWS drivers matching the DigitalOcean
functions. Portability across CSPs is **not** a precondition for
building mechanism 2 — it is a challenge mechanism 2 must be built to
handle, and this is where that gets tested. The same is true of other
end users driving it and other models backing it.

Staying on one provider through Gates #1 and #2 is deliberate and cheap.
It has one consequence the knowledge base must respect — see "How the
loop learns" above: a claim about CSP APIs *in general* cannot be
established by any number of DigitalOcean drivers, so those claims stay
candidates until Gate #3.

## Edge cases / errors

- **A probe cannot catch a prediction nobody made.** The firewall spec
  claimed, under *verified live*, that a PUT resets an omitted field —
  but the transcript cited had edited a firewall whose tags were already
  empty, so "reset" and "left alone" were indistinguishable. The probe
  passed. A reviewer caught it. Mitigation: a claim about a field being
  reset requires a transcript where that field was **non-empty before**,
  and this is exactly the kind of rule `probing/` entries exist to hold.
- **An edit is not a change until something re-reads it.** A fix round
  claimed in its own commit message to have replaced a tautological
  test; the edit had silently matched nothing and the claim went
  unchallenged into the log. Mitigation: an edit asserts its anchor
  matched, and a `step=fix` line is only written after the change is
  observed in the tree.
- **Review and probing catch disjoint classes.** Probing falsifies
  beliefs about the API; review falsifies beliefs about the diff.
  Building one driver, probing found seven API surprises review would
  never have seen, and review found sixteen defects no probe could have
  — including both items above. **Neither replaces the other, and gate
  #1 stays.**
- **A failed probe is a finding, not an error.** A 4xx is often the
  answer. Only a transport failure, a refused redirect, or a missing
  credential aborts a session.
- **A sweep that finds anything is a bug report**, not maintenance: warn
  loudly and exit non-zero. Identity is a name prefix **and** an age past
  the floor, so a sweep can never delete a concurrent run's resources;
  unrecognized names are skipped, never deleted.
- **A transcript that would contain a credential is not written.** The
  write aborts. Auth headers are recorded as literal placeholders and
  never as values.
- **Knowledge entries can go stale.** `PLAN.md` §10 records that driver
  correctness drifts as CSP APIs change; the same is true of what a
  probe learned. An entry carries `first_seen`, and a re-probe that
  contradicts an entry supersedes it with a new line in the driver's
  audit log — it does not silently edit the entry.

## Out of scope

- **Building `aiform driver create`.** This specifies the loop that
  command must execute; §7's CLI surface, and wiring `driver_gen.py` to
  anything, remain `PLAN.md` §10's "Self-service driver creation" item.
- **Runtime observability.** `PLAN.md` §10's "Observability" entry is a
  status URL for a live formation, and `specs/driver_observability.md`'s
  `health()`/`metrics()` are a per-resource scrape. The audit log here is
  *build-time* observability for how a driver came to exist — related in
  spirit to both, unrelated in mechanism to either.

  **But the loop itself does apply to them.** `health()` and `metrics()`
  are optional driver methods, and a driver implementing one is adding
  CSP surface like any other: it needs probes with recorded transcripts
  for the endpoints it calls, and its unit tests mock those transcripts.
  This matters more than for the four required methods, not less —
  `metrics()` in particular tends to call endpoints no existing driver
  has touched (DigitalOcean's `/v2/monitoring/metrics/droplet/*` return
  *time series* rather than instantaneous values, and only when the
  droplet was created with `monitoring: true`), so the documented
  signature is furthest from settled exactly where a guess is easiest.
  A driver may not ship `metrics()` on a prediction alone.
- **Publishing knowledge entries or drivers anywhere.** §10's "Driver
  submission and publishing" is untouched; `knowledge/` is a directory
  in this repo.
- **Probing anything that costs money or carries traffic.** Firewall was
  chosen to pilot this because an unattached firewall is free and
  affects nothing. A resource with no such shape needs its cost stated
  in its session docstring before it may run.
- **Automatic promotion.** Promotion is a human or an approved
  mechanism-2 step, never a silent side effect of a session.

## Knowledge-confidence

*Verified by building one driver this way* — `drivers/digitalocean/firewall.py`,
31 transcripts, three review rounds:

- the contradiction rate and what it caught (`probes/transcripts/digitalocean_firewall/`)
- that shape-only static validation would have passed every finding
- that `UNORDERED_FIELDS`/`NON_DIFFABLE_FIELDS` are absent from both
  prompts and the validator (checked by grep, 2026-09-10)
- the two failure modes in Edge cases, both observed in this repo

*Inferred, not verified* — that recall reduces probe count on a second
driver, that the promotion bar is set right at two observations, that
the audit log is legible enough to review against, and that the
few-hours budget holds for a resource that costs money or takes time to
converge. All three need a
second driver, and this spec should be edited after it.

*Recalled, not verified* — that this generalizes to a CSP other than
DigitalOcean. One provider is an anecdote.

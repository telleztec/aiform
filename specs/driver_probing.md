# specs/driver_probing.md — the driver-creation loop

## Purpose

How a driver for a new `(provider, resource)` pair gets made: probe the
live API, encode what it actually does, and keep what was learned where
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

**Build status.** The loop and the probe harness are real and were used
to build `drivers/digitalocean/firewall.py`. The audit log, the
knowledge base, and every mechanism-2 integration below are **specified
here and not yet built.**

## Why probing

Provider docs, OpenAPI specs, and mature Terraform providers all
systematically under-describe side effects. The only reliable way to
learn how a resource behaves is to exercise it.

The cost of not doing so is not "a bug" — it is a driver that never
converges. `aiform/planner.py`'s `diff_attributes()` compares `read()`'s
output against the user's raw `params` with no hook for a driver to
normalize either side, so any value a CSP silently rewrites on store is a
**permanent diff**, which permanently defeats the zero-LLM-call
short-circuit for that resource.

Measured, building one driver against DigitalOcean firewalls: **31
probes, 7 contradicting their prediction.** Three were silent rewrites
(an int port, an uppercase protocol, the spelling `"all"`). One was a
field — `action` — that DigitalOcean adds to every rule and that appears
nowhere in its own published OpenAPI schema. A generator working from
that schema would have produced a driver that never converges, with
nothing in the pipeline able to see why.

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
1. **Question and predict.** One factual question, plus what it settles
   — a behavior, an object's shape, or a relationship edge. Write the
   expected request *and* response **before sending**.
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

### Which probes are legitimate

Probe **behavior** freely — deterministic, and one observation settles
it ("does this endpoint coerce an int port?"). Characterization probes
count even when no code changes today: building an accurate model of the
resource is the deliverable.

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

```
2026-09-09T23:58:00Z step=recall  provider=digitalocean skipped=0 traps=0 msg="no prior entries"
2026-09-10T00:05:12Z step=probe   ref=02 verdict=contradicted msg="create unattached -> 202 but status=succeeded, not waiting"
2026-09-10T00:05:14Z step=probe   ref=04 verdict=contradicted msg="ports 22 accepted, stored as \"22\""
2026-09-10T00:31:00Z step=spec    ref=behavior/create cites=02 msg="create() does not poll"
2026-09-10T00:40:11Z step=test    ref=test_does_not_poll state=red
2026-09-10T00:52:03Z step=impl    ref=create state=green tests=32
2026-09-10T01:10:00Z step=review  round=1 model=fable findings=10
2026-09-10T01:35:00Z step=fix     round=1 fixed=10 msg="nested target types, required fields, sweep age floor"
2026-09-10T02:05:00Z step=verify  kind=live result=pass msg="zero-diff after create and update"
2026-09-10T02:10:00Z step=learn   promoted=2 msg="csp/digitalocean/scalar-coercion, probing/reset-vs-unchanged"
```

Rules that keep it auditable:

- **One line per decision, not per action.** A 31-probe session emits a
  line per probe only for contradictions and for probes a spec claim
  cites; the rest are summarised in one `step=probe count=31` line. A
  whole driver's creation should read in **20–40 lines**.
- **Every `step=spec` line carries `cites=`**, or the claim is not
  allowed to say "verified".
- **`verdict=` is only `contradicted` or `confirmed`**, so
  `grep verdict=contradicted` is the finding list.
- **Never a credential, never a full payload.** Evidence is a transcript
  reference; the payload lives there.
- **Append-only.** A correction is a new line, never an edit — the
  history of what was believed is the record's value.

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

1. **Ground generation in transcripts, not prose.** A stated fact is an
   instruction, and instructions compete; a transcript is an
   observation. Every session's step 0 records `credentials_key` from a
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
   `PLAN.md` §10's driver-conformance item (issue #114); firewall is its
   third data point.
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
- **Promotion has a bar.** A finding stays in the driver's own
  `FINDINGS.md` until it is observed a second time, in a second resource
  or a second provider, at which point it is promoted with both
  citations. One observation is an anecdote; the bar stops the base
  filling with over-generalised singletons.
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
| live surprises after "done" | must reach zero; these are the rounds mechanism 2 would otherwise burn |

**Readiness for mechanism 2** is not a date: it is a session that runs
without the process needing to change to accommodate it, on a **second
provider**, plus a run against a **different account** whose transcripts
differ only in explainable ways.

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
  status URL for a live formation. The audit log here is *build-time*
  observability for how a driver came to exist — related in spirit,
  unrelated in mechanism.
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
driver, that the promotion bar is set right at two observations, and
that the audit log is legible enough to review against. All three need a
second driver, and this spec should be edited after it.

*Recalled, not verified* — that this generalizes to a CSP other than
DigitalOcean. One provider is an anecdote.

# Plan: fix #216 — a reference cannot supply a droplet id to an integer field

**Status: approved 2026-09-27. Implemented by the PR that carries this file.**

## Context

Phase 2 shipped cross-resource references (PR #217). A user who learns the
syntax writes the obvious thing for a firewall and it fails:

```yaml
params:
  droplet_ids: ["${digitalocean.compute.web-01:id}"]
```

Issue #216 tracks it, labeled P2-usability. It is also the **named
prerequisite** for Phase 3, per `specs/dependency_detection.md` — the decision
to pause Phase 3 (PR #221, merged as `cf42399`) rests on fixing this first and
reassessing from what it teaches.

**Where it fails, precisely.** Not at plan time. The reference resolves
happily; `firewall.py:406-407`'s `create()` calls `_validate_params()` as its
first statement, before the POST at `:409` (and `update()` likewise at
`:478-489`, before the PUT at `:511`), and the orchestrator wraps the
`ValueError` as `DriverExecutionError` (`orchestrator.py:78-82`). So the user
sees it during `apply`, after the plan looked fine. Nothing is mutated — the
validator runs before any request.

**The diagnosis, which is what makes the fix small.** This is not a gap in the
reference grammar. `aiform/references.py:267-272` already returns the
attribute's own Python object for a whole-value reference — "an int stays an
int, a list stays a list. This is what lets a reference feed a non-string
field."

The string comes from the other end. `orchestrator.py:94` pops `"id"` out of
what a driver returns and moves it to `StateEntry.id`, which `models.py:222`
types `str` — necessarily, since it is aiform's primary key and must be uniform
across every provider. `PLAN.md:538` states that directly: "`id` — the CSP's
resource identifier, opaque string (DO droplet IDs are numeric-as-string)."
`orchestrator.py:104`'s `referenceable()` merges that string back into the
reference namespace under the name `id`. So `compute.py:205`'s
`str(droplet["id"])` is not a driver quirk; it satisfies the contract at
`aiform/driver.py:134` and `PLAN.md:760`.

`id` is doing two jobs under one name: **aiform's identity token**, legitimately
a string, and **a provider attribute a user wants to reference**, whose native
type is whatever the CSP says. A reference names an aiform-tracked *object* and
`:attribute` already expresses which value inside it to hand the provider. So
the fix is to make the right attribute available with the right type.

The asymmetry is already visible in the repo's own tests:
`tests/system/test_cli_digitalocean.py:422` writes `droplet_ids=[int(droplet_id)]`
— a live test casting by hand because a user cannot.

Intended outcome: `droplet_ids: ["${digitalocean.compute.web-01:provider_id}"]`
works, and a user who reaches for `:id` first gets an error that tells them why
and what to use instead.

## Decisions taken

Settled with the repo owner before this plan:

- **Name: `provider_id`.** Says *whose* type it is rather than what the type
  happens to be, so the convention still reads correctly for a uuid-keyed
  resource. Not `numeric_id` (breaks on uuids), not `droplet_id` (every driver
  invents its own name and there is no convention to document).
- **The driver hints at `provider_id`** in its error, accepting the coupling.
  Without it a user hits a bare type mismatch first and #216's own stated bar —
  "fails with an error that says references cannot reach an integer-typed field
  and names the reason" — goes half-met.
- **Convention documented, adopted only where needed.** `specs/driver.md` and
  `PLAN.md` §4 describe `provider_id`; `compute` implements it; `domain` and
  `firewall` do not, since neither is referenced *by* id (a zone's identity is
  its name). Avoids speculative adoption while making the next driver author's
  decision obvious.

Rejected, and why, so a reviewer does not re-litigate: a cast in the reference
grammar asks the user to correct aiform's own bookkeeping; coercing against
`PARAM_SCHEMA` needs a validation pass that does not exist (#108, itself
P0-safety); changing `compute`'s `id` to an int inverts the conflation and
breaks `StateEntry.id: str`, `_pop_id()` and the §4 return contract.

**Not solvable in the reference layer, by design.** `resolve()`
(`references.py:324-330`) takes `params`, `available`, `volatile`, `replaced` —
no driver, no schema, no destination type. That is a stated commitment, not an
oversight: `specs/orchestrator.md:1387-1391` and
`specs/resource_references.md:626-627` both record that resolution stays
driver-agnostic. The type knowledge lives in the driver's validator, so the
error comes from there.

## The change

### 1. `drivers/digitalocean/compute.py` — `_flatten()`

Add one key beside the existing identity, native-typed (`compute.py:204-212`):

```python
"id": str(droplet["id"]),        # unchanged: aiform's identity
"provider_id": droplet["id"],    # new: the CSP's own value, its own type
```

`PLAN.md:760-762` specifies `{"id": str, **attributes}` with the words "at
least" — a floor, not a fixed key set — so this is contract-compatible, and
`read()`/`update()` return the same shape because both route through `_flatten()`.

**Verified safe, not assumed.** Each of these was checked by reading the code:

| Consumer | Effect |
|---|---|
| `planner.diff_attributes()` (`planner.py:88-103`) | Iterates `desired.items()` and reads `current.get(key)`. A key absent from the user's `params` is unreachable — it cannot enter `diff`, flip `params_agree` (`planner.py:173-174`), defeat the zero-LLM no-op short-circuit (`:176-190`), or reach `categorize_diff()`'s payload (`:118-126`, which serializes `diff`). |
| `compare.unordered_equal()` | Called from inside `diff_attributes`'s per-key loop (`planner.py:98`), so it only ever sees a key already in `desired`. Not a second comparison surface. |
| `compute.update()` (`compute.py:412-416`) | Builds `diff_fields` from `PARAM_SCHEMA["properties"]` **and** requires `key in desired`. Doubly excluded. Everything downstream is keyed off `diff_fields`. |
| `refresh_resource()`'s `NON_DIFFABLE_FIELDS` carry-forward (`orchestrator.py:279-281`) | Iterates a fixed allowlist (`["ssh_keys"]`, `compute.py:182`), not a dict diff. No interaction. |
| `cli._plan_to_json()` (`cli.py:195-210`) | Emits action/rationale/depends_on/references, never attributes. The `--json` plan contract is untouched. |
| `observability.py:495,555,636` | `attributes` is only the `current` side of `diff_attributes`. `StatusReport` has no `attributes` field (`:505-524`), and `drifted_fields` derives from the diff. |
| State round-trip | `StateEntry.attributes: dict[str, Any]` (`models.py:223`), no `model_config`, `json.dumps`/`model_validate` (`state.py:32-41`). Checked empirically against the project's pydantic: `int` in, `int` out, no warning. |

**In-repo precedent.** `firewall.py:369-388`'s `_project()` already returns a
non-`PARAM_SCHEMA` key (`"name"`) and the comment at `:376-379` asserts exactly
this invariant: "Safe: diff_attributes() iterates desired only, so an extra key
here can never diff." Follow that comment's style for the new key.

**Two accepted, deliberate costs** — small, but they are real and should not be
discovered in review:

1. `cli.py:255`'s `_print_state()` dumps the attributes dict verbatim, so
   `aiform plan show` and `aiform plan refresh` will print
   `"provider_id": 123456789` beside `"id": "123456789"`. The same duplication
   lands in `state.json`. That is the visible surface of the fix, and arguably
   correct — the two values mean different things.
2. The first `plan`/`refresh` after this change rewrites `state.json` for every
   tracked droplet (one-time; `state.save()` writes `.backup` first,
   `state.py:38-40`). No ongoing churn, since `provider_id` is stable.
3. `references.py:201-208`'s `_require_attribute()` renders the available
   attribute names in its error, so a typo'd reference now lists `provider_id`
   too — which is the point.

### 2. `drivers/digitalocean/firewall.py` — the error hint

The raise is `firewall.py:214-221`, `_reject_wrong_scalars()`, and today
produces exactly:

```
droplet_ids[0] must be a int, got '12345'
```

`_reject_wrong_scalars` is shared by `droplet_ids`, `tags`, `addresses`,
`load_balancer_uids` and `kubernetes_ids` — but **`expected is int` already
identifies a droplet-id field uniquely**: `firewall.py:202-203` passes `int`
only for `droplet_ids`, and the nested path sets
`expected = int if key == "droplet_ids" else str` (`:335`). So the hint needs no
new parameter and no field-name test.

Narrow change: when `expected is int` and the offending item is a **string of
digits**, add that a reference cannot supply `id` here and name `provider_id`.
Keep `ValueError`, keep the raise site, and keep the house style — house style
across drivers is `<where>: <what's wrong>, got <repr>` plus, when a correct
spelling exists, "write X instead of Y" (`domain.py:236-242`,
`compute.py:463-490`, and `firewall.py:239-248`'s existing `hint` mechanism).
`firewall.py:504-509` is the closest precedent for a driver message naming an
aiform-side fact.

Fix the `a int` article bug in the same message while editing it — a typo in a
line already being changed, no issue needed. `sneaky_bool` keeps its own
message unchanged.

Deliberately a heuristic on the value, not reference-awareness in the driver:
the driver must not import or inspect `aiform/references.py`, and the reference
layer cannot detect this itself (see above).

### 3. Docs

Broader than first estimated — the pause-Phase-3 reconciliation put #216's name
in many places. Every anchor below was located; treat the list as the checklist,
and re-grep before committing rather than trusting these line numbers, which
drift.

**The limitation, stated as unsolved:**

- `specs/resource_references.md:613-618` (Out of scope: "References into
  integer-typed fields"), `:32-33`, `:622-627`, and `:275-281` (the
  referenceable-namespace section a new attribute extends).
- `specs/digitalocean_firewall.md:222-230` (Out of scope: "`droplet_ids` can
  only hold literal integers"), `:341-356` (the Phase 2 addendum), and
  `:130-137` (the validation rule — the fix works *with* it, not against it).
- `PLAN.md:1662-1668` (§10's known-limitation paragraph) and `:1670-1677`.
- `MULTI_RESOURCE_PRD.md:6-11`, `:32-37`, `:362-364`, `:382-385`, `:400-407`.
- `specs/system_test_references.md:104-106`.
- `specs/orchestrator.md:1251-1256`.
- `specs/resource_dependencies.md:58` and `:732`.

**The convention being established:**

- `specs/digitalocean_compute.md:144-172` — the `_flatten()` code fence is
  documented as a contract; add `provider_id` and why it exists beside `id`.
  This is where the convention is actually written down.
- `specs/driver.md` and `PLAN.md` §4 — describe the convention and its
  relationship to `{"id": str, **attributes}`. Note that `specs/driver.md:42`
  and `:78-80` deliberately do not restate §4's docstrings, so keep the
  addition where each file's own scope puts it. **Note the adjacency to #133**
  (P0-safety: §4 omits `UNORDERED_FIELDS`, and `specs/driver.md:44-76` is stale
  the same way). Do not fix #133 here — one issue, one PR — but do not widen
  its gap.

**`specs/dependency_detection.md`**, which is the spec waiting on this event:
`:25` (build-status row), `:12-16`, `:59-60`, `:62-105` (the whole prerequisite
analysis, including `:83-89`'s "`id` is doing two unrelated jobs" and
`:90-93`'s note that the choice among candidates belongs to #216 — this plan is
that choice), `:116`, `:256-261`, `:355-358`, `:367-373`, `:381`.

**Answer `:339-342` explicitly.** Its reopen condition is: "#216 is fixed and
detection is still the only way to get this edge — for example if the chosen fix
leaves integer-typed fields unreachable by reference." This fix makes them
reachable, so that condition is **not** met, and the spec should say so in the
same edit rather than leaving the reader to infer it.

`plans/phase2-cross-resource-references.md:300-305` and
`plans/pause-phase-3-dependency-detection.md:72-102` carry the same claim but
are historical point-in-time records; per `plans/README.md` they are **not**
updated.

**Citation drift.** Adding a line to `_flatten()` shifts every `compute.py`
line number below it — `specs/dependency_detection.md:118` cites
`compute.py:211` by number, and `specs/digitalocean_compute.md` cites the same
region. Re-verify every `compute.py:NNN` citation in `specs/` and `PLAN.md`
before committing; this has bitten three times on previous PRs.

Out of scope: #108, #133, and anything about Phase 3 itself beyond recording
that its prerequisite is met.

## Tests, red before green

Per `PROCESS.md` and `.claude/skills/tdd-workflow/SKILL.md`. Reuse existing
fixtures rather than new scaffolding — `tests/system/conftest.py` provides
`unique_droplet_name`, `write_aiform_md`, `assert_cli_ok`,
`teardown_tracked_resources`, `count_driver_reads`.

New, in `tests/drivers/test_digitalocean_compute.py`,
`tests/test_orchestrator.py`, `tests/test_references.py`,
`tests/drivers/test_digitalocean_firewall.py`:

1. `_flatten()` returns `provider_id` as an `int` and `id` as the `str` of the
   same value.
2. `_pop_id()` does not consume `provider_id` — it survives into
   `StateEntry.attributes`.
3. `referenceable()` offers both. Note `orchestrator.py:105` merges `"id"`
   *after* `**attributes`, so only a key literally named `id` is shadowed;
   assert `provider_id` comes through intact.
4. A whole-value reference to `provider_id` resolves to an `int`. **This is the
   regression #216 describes and must fail before the change.**
5. End to end: a firewall whose `droplet_ids` references a compute
   `provider_id` passes `_validate_params()` with no type error — the check
   that today raises at apply time, wrapped as `DriverExecutionError`.
6. `provider_id` never appears in a diff or a plan line — pins the
   "additive and invisible" claim rather than trusting it.
7. `expected is int` plus an all-digits string raises `ValueError` naming
   `provider_id`; a non-digit string in `droplet_ids` still raises the ordinary
   message; a string in `tags` (where `expected is str`) is unaffected; a bool
   still hits `sneaky_bool`.
8. Round-trip: an `int` attribute survives `state.save()`/`state.load()`
   uncoerced.

Existing tests that must stay green, as the regression signal:

- `tests/drivers/test_digitalocean_firewall.py:446-449`, `:451-454`,
  `:470-472`, `:474-476` — these assert only that *something* mentioning
  `droplet_ids` raises, so appending a hint does not break them. They would
  break if the fix made a string acceptable, which it must not.
- `tests/test_references.py:51-54` (`test_int_attribute_stays_an_int`),
  `:65-67`, `:102-104` (pins `${…:id}` in a list resolving to the string
  `"12345"` — still correct, `id` stays a string), `:383-390`.
- No test asserts the exact key set of the real compute driver's returned
  attributes; the five `attributes == {...}` equality assertions
  (`tests/test_orchestrator.py:607,636,1355,3054`, `tests/test_cli.py:2211`)
  all run against fake drivers. Confirmed by grep for `set(result)`,
  `sorted(result)`, `result.keys()`, `assert result ==`.

**Live test**: extend `tests/system/test_cli_references.py`'s
`TestCrossResourceReferenceLive`, which already stands up a real droplet and
verifies a reference end to end; `tests/system/test_cli_firewall.py:294-376`
has the firewall half. There is **no** live coverage of the integer case today
— the existing one is literal-only. This is the thing #216 is about, and a
mocked test cannot show DigitalOcean accepting the integer.

## Verification

- Full offline suite under `env -u DIGITALOCEAN_TOKEN` — direnv otherwise makes
  credential tests falsely green.
- `ruff format` / `ruff check` clean.
- **Live `system-test` is mandatory, not N/A.** This touches
  `drivers/**/*.py`, a runtime path, so the path check in
  `.claude/skills/github-commit-process/SKILL.md` requires a real run. Read the
  log; do not trust a shell exit code behind a pipe.
- A real `plan`/`apply` of a droplet plus a firewall referencing its
  `provider_id`, against the aiform DigitalOcean account, scoped to the aiform
  tag with the usual cleanup. The account points at the dev team;
  `telleztec-wordpress` is production and never in scope.
- Do not edit `drivers/` or `aiform/` while the live suite runs —
  `load_driver` re-execs from disk on every call.

## Pairing

**Sonnet 5 authors, Opus 5 reviews** — the repo's cheap default, and right
here: the difficulty was in the diagnosis, which is settled above, leaving a
small mechanical change plus a wide docs sweep. This differs from PR #221's
Opus/Fable pairing deliberately.

## Process

- One issue, one PR: closes **#216** only.
- Branch off current `main` (`cf42399` or later).
- Commit this plan to `plans/fix-216-reference-into-integer-field.md` per
  `plans/README.md`, and link it from the PR's `## Plan` section with the
  approval stated — the shape PR #219 introduced.
- Findings from each review round go in their own PR comment, not the
  description.
- After it lands: reassess whether Phase 3 is still worth doing, per the
  owner's direction and `specs/dependency_detection.md`'s reassessment trigger.

# Phase 2 — Cross-resource attribute references

**Status:** approved 2026-09-26. Implements #215. Phase 2 of
`MULTI_RESOURCE_PRD.md`. This is the plan `PROCESS.md`'s "Before the loop"
gate requires, as approved — a point-in-time decision record, not a living
spec. `specs/resource_references.md` is the living spec; where the two
disagree, the spec describes what the code does and this file describes what
was agreed beforehand.

What the human approved, explicitly and in this order: the colon syntax, a
reference implying the dependency edge, deferral of unknown values to apply
time, printing an unresolved reference bare with no annotation, and the
Opus 5 → Fable 5.1 pairing.

> **Why this file is in the repo at all.** Its predecessor —
> `~/.claude/plans/prepare-the-plan-for-dreamy-codd.md` — was overwritten
> mid-session by an unrelated plan for PR #208, which is issue #203's
> complaint happening for the third time in this repo. Committing it here is
> an instance of #203's part 1, on the branch that implements it. It is **not**
> an implementation of #203, which additionally asks for `PROCESS.md` changes,
> a generated-artifact rule, and answers to six open design questions, and is
> explicitly filed for later. #203 stays open.

## Context

`MULTI_RESOURCE_PRD.md`'s Phase 1 shipped as PR #204: resources can declare
`depends_on:` and the plan is ordered topologically. That delivered the
*declaration* half of a dependency — naming another resource.

It delivered no *value flow*. A DNS A record's `data` is still an IP the user
types by hand, and retyping it whenever a droplet is replaced is the pain the
PRD opens with. `specs/digitalocean_domain.md` says so outright today: record
`data` "is always a literal the user types", and "there is no way to reference
a `compute` resource's `ipv4_address`". This phase retires that sentence.

Scope stays narrow by the PRD's phasing: execution stays strictly sequential
(Phase 6 owns parallelism), edges stay user-written (Phase 3 owns inferring
them from driver metadata), and orphan refusal and partial-failure recovery
remain Phase 4's.

## Approved decisions

1. **Syntax: `${provider.resource_type.name:attribute}`** — a colon before the
   attribute, not a fourth dot.
2. **A reference implies the dependency edge.** No duplicate `depends_on:`
   entry needed; `depends_on:` remains for ordering with no value flow.
3. **Unknown values defer to apply time.** A reference whose target is created
   in the same run resolves during the apply loop. One apply stands up the
   whole graph.
4. **Plan output prints the reference bare** — no "(known after apply)"
   suffix. An unresolved reference is self-evidently unresolved.
5. **Pairing: Opus 5 authors, Fable 5.1 reviews.** See "Process".

### Why the colon

A resource `name` may legally contain dots — `digitalocean.domain.example.com`
is a real key — so `${provider.type.name.attribute}` is ambiguous:
`digitalocean.domain.example.com.ttl` could be attribute `ttl` on
`example.com`, or `com.ttl` on `example`. The colon removes that and makes the
left half **byte-identical to a `depends_on` target and to a state key**, so
`models.parse_dependency_key()` parses it unchanged.

YAML behaviour, verified against PyYAML rather than assumed:

| Written as | Result |
|---|---|
| `data: ${digitalocean.compute.web:ipv4_address}` | works unquoted |
| `data: ${digitalocean.domain.example.com:ttl}` | dotted name fine |
| `data: host-${…:ipv4_address}-end` | embedded in a string |
| `- ${digitalocean.compute.web:id}` | block sequence, unquoted |
| `ids: ["${…:id}"]` | flow sequence — must quote |
| `ids: [${…:id}]` | `ParserError` |
| `data: ${…web: ipv4_address}` | `ScannerError` — no space after colon |
| newline after the colon | `ScannerError` |

The `[ ]` failure comes from the `{}` braces and hits every candidate
delimiter equally, so it is not a cost of the colon. The no-space rule is.

## Mechanism

### New module `aiform/references.py`

Pure string/tree work — no I/O, no LLM, no `anthropic`, no `State` import.
Same shape as `aiform/graph.py` and `aiform/compare.py`, and pure for the same
reason: it takes a plain mapping of `resource_key -> attributes`, so the
orchestrator and `observability.py` can both feed it.

```python
REFERENCE_RE: re.Pattern  # ${<key>:<attribute>}


class Reference(NamedTuple):
    target_key: str
    attribute: str


def find_references(params: dict) -> dict[str, list[Reference]]:
    """Dotted param path -> references at that path. Walks dicts and lists to
    arbitrary depth; `records[0].data` is a real path."""


def reference_targets(params: dict) -> set[str]:
    """Target keys only -- what the edge pass needs, without resolving."""


def resolve(params: dict, available: Mapping[str, Mapping[str, Any]]) -> tuple[dict, list[str]]:
    """Returns (substituted params, sorted paths still unresolved). A path is
    unresolved only when its target is absent from `available`. Every other
    failure raises."""
```

Resolution rules:

- **Whole-value reference preserves type.** `data: ${…}` substitutes the
  attribute's own value with its own type.
- **Embedded reference stringifies.** `data: "web is ${…}"` interpolates;
  several references in one string all substitute.
- **Namespace is the target's state `attributes` plus `id`.** `id` is not in
  `attributes` (`_pop_id()` moves it to `StateEntry.id`) but it is the most
  useful cross-resource value, so `resolve()` is fed
  `{**entry.attributes, "id": entry.id}`.
- **A malformed `${…}` raises, never passes through.** A typo'd reference must
  not reach the CSP as a literal — the failure mode issue #108 exists for.
- **A resolved `None` raises.** `compute._flatten()` returns
  `ipv4_address: None` when no public v4 is attached (issue #178 is that race),
  and a DNS record whose `data` is `"None"` is worse than a refusal.

### Two resolution seams

**Plan time** (`orchestrator._plan_one`, before `_decide_action`), against
`st.resources`. Not optional: `planner.plan_resource()` diffs `desired_params`
against live attributes, so an unresolved `${…}` literal would defeat the no-op
short-circuit at `planner.py:153` *permanently* and bill a categorization on
every future plan.

**Apply time** (`orchestrator.apply_plan`), re-resolving from the in-loop `st`
immediately before each `driver.create`, `driver.update`, and the replace
path's create. `apply_plan()` already saves state after every resource
(`orchestrator.py:986`) and the plan is topologically ordered, so a
just-created droplet's `ipv4_address` is present by the zone's turn.

Apply re-resolves **the whole raw tree**, not only what was unknown at plan
time: one code path instead of two, and the value handed to a driver is the one
live at the moment of the call — correct precisely when a target was replaced
earlier in the same apply.

`PlannedResource` carries three fields where it carried one: `desired_params`
(resolved as far as plan time could; feeds the diff and display), `raw_params`
(references intact; what apply re-resolves), and
`unresolved_references: list[str]`.

### The tracked-dependent edge case

On a first run the dependent is untracked, so it takes
`planner.create_entry()` and no diff exists for an unknown to corrupt. The
awkward case is a *tracked* dependent whose target drifted missing and is
recreated in the same run — an unknown then lands on an update path where
`params_agree` cannot honestly be true.

Rule: **a tracked resource with any unresolved reference gets a deterministic
`UPDATE` entry and no model call**, rationale naming the unresolved path. It
joins `create_entry()`/`destroy_entry()` as a zero-LLM producer, never
short-circuits to `NO_OP`, and keeps `params_agree` `False` so the
`.aiform.md` hash is not recorded on a run whose params were never fully known.

### Edges from references

`_resolve_dependency_edges()` unions reference-derived targets with
`depends_on` ones and classifies them through the **same** table
`specs/resource_dependencies.md` already defines: a live file in this run adds
an edge; a target tracked in state resolves with no edge; a delete-marked
target is a `PlanBlockedError`; a target nowhere raises. `graph.py` does not
change — Phase 1's engine takes whatever edge set it is handed, and fan-in is
already first-class.

One new plan-blocking reason: a reference naming an attribute the target does
not expose, reported with the names that *are* available.

### Error-message mitigation

`parse_frontmatter()` already wraps a YAML failure as
`ValueError("malformed .aiform.md frontmatter: invalid YAML: …")`. When the raw
text contains `${`, append a hint naming the two sharp edges (no space after
the colon; quote inside `[ ]`), turning `mapping values are not allowed here`
into something actionable.

### What does not change

- **No state schema change.** `StateEntry.attributes` already holds what the
  CSP echoed back; the reference expression lives in the `.aiform.md`.
- **Zero-Anthropic-call property holds.** Resolution is deterministic string
  work; on unchanged input every reference resolves identically, the diff is
  empty, and the short-circuit fires as today. When a target's IP genuinely
  changes the dependent diffs and pays one categorization — correct.
- **`graph.py` untouched**, and `build_plan_summary()` stays as-is, per Phase
  1's precedent of not injecting unexplained keys into gate #2's prompt.

## Files

**New:** `aiform/references.py`, `tests/test_references.py`,
`specs/resource_references.md` (feature-named, carrying the same "Naming note"
paragraph `specs/resource_dependencies.md` uses), and
`plans/phase2-cross-resource-references.md` (this plan — see below).

| Changed file | Change |
|---|---|
| `aiform/orchestrator.py` | resolve in `_plan_one`; union reference targets in `_resolve_dependency_edges`; re-resolve before create/update/replace in `apply_plan`; three new `PlannedResource` fields |
| `aiform/planner.py` | the deterministic unresolved-reference `UPDATE` producer |
| `aiform/parser.py` | YAML-error hint when the file contains `${` |
| `aiform/observability.py` | resolve before the `_config_status` diff at `:614` — `_status_for_entry` already receives the full `State`, so no plumbing |
| `aiform/cli.py` | `_print_plan` prints one line per reference; `_plan_to_json` carries them |

**Specs to update** with addendum sections pointing at the new spec:
`orchestrator.md`, `planner.md`, `parser.md`, `cli.md`, `models.md`,
`digitalocean_domain.md` (retire the "no cross-resource references" claim),
`digitalocean_firewall.md`, `system_test_domain.md`.

**Docs to reconcile:** `PLAN.md` §10's "Dependency graph: ordering exists,
value flow does not" entry — the title stops being true; the PRD's stale
`Phase 1 is in progress` header and its open question 1; `CLAUDE.md`'s
ordering-only paragraph; `README.md`.

## Commit the plan

Per the user's instruction, and issue #203's part 1 ("plans live in source
control… on the branch that implements it"):

- Commit this document to **`plans/phase2-cross-resource-references.md`** — a
  new top-level `plans/` directory. #203 leaves the location open but notes
  `specs/` is for *living module specs* while a plan is a point-in-time
  decision record, so they are not the same thing.
- The PR's `## Plan` section links that path instead of describing a file no
  reviewer can open.
- **This does not implement #203** and must not claim to. #203 also asks for
  PROCESS.md changes, a generated-artifact rule, and answers to six open
  design questions; it is explicitly "filed for later… needs its own written
  plan and explicit approval". Committing one plan is an instance of its part
  1, not the issue. Do not close #203.

## Sequence

1. **File the Phase 2 issue** — none exists; the loop is one issue, one PR.
   Label `enhancement` + `priority: P2-usability`, matching #200 (Phase 1) and
   #198 (Phase 0).
2. Worktree off current `origin/main`: `.claude/worktrees/resource-references`.
   Note issue #128 — a fresh worktree has no venv, so use the main checkout's
   `.venv/bin/python`.
3. Commit the plan to `plans/`.
4. `specs/resource_references.md` — spec before tests, per PROCESS.md step 1,
   after grepping `PLAN.md` §10 for the pre-existing entry to cross-reference.
5. `tests/test_references.py` **red** — observed failing, not assumed.
6. Implement `aiform/references.py`, then the five changed modules.
7. Green: module tests, then the full suite. `ruff check` + `ruff format`.
8. Review on **Fable 5.1** (see below). Address findings.
9. Live suite, then PR with a `## Plan` section linking the committed plan.

## Verification

**Unit** — `tests/test_references.py` modelled on `tests/test_graph.py`
(`#` comments, not docstrings): whole-value type preservation; embedded
stringification; several references in one string; nesting in lists and dicts;
dotted target names; malformed reference raises; unknown attribute raises
*with the available names*; resolved `None` raises; unresolved target returns
its path rather than raising.

Then, in existing files: `test_orchestrator.py` (edge from a reference with no
`depends_on`; fan-in from two references; apply-time resolution against a
just-created resource; references to a delete-marked target and to nowhere
both blocked; the tracked-dependent deterministic `UPDATE`; and **unchanged
input makes zero Anthropic calls**, using the `FakeClient([])` plus empty
`drivers_dir` idiom Phase 1's tests use to prove blocking precedes any driver
load); `test_planner.py`; `test_parser.py`; `test_cli.py`;
`test_observability.py` (`resource status` reports in-sync, not permanent
drift).

**Live** — the PRD's acceptance case in a single `apply`: a droplet plus a zone
whose A record reads its `ipv4_address`. **Name the files so alphabetical order
contradicts the required order**, or the check passes against nothing — Phase
1's spec makes exactly this point. Then a second `plan` to confirm zero
Anthropic calls and a clean no-op, and a `resource status` to confirm no
phantom drift. Scoped to the aiform tag with the usual cleanup;
`telleztec-wordpress` (589098829) is production and out of bounds.

## Process

- **Pairing: Opus 5 authors, Fable 5.1 reviews**, chosen deliberately over the
  cheap Sonnet→Opus default because this changes the plan/apply core the PRD
  calls a "critical, load-bearing change".
- **Hazard:** `/code-review` inherits the invoking session's model, and this
  session is Opus. Invoking it plainly would be Opus reviewing Opus-authored
  code — self-review, forbidden absolutely since PR #205 scrapped the waiver.
  Every round must be launched explicitly on Fable 5.1.
- The pairing holds for all rounds of fix commits; changing it takes a
  re-plan. Fable rounds are expensive, so cap rounds up front rather than
  discovering the cost mid-review, and defer non-blocking findings under
  budget pressure.

## Out of scope, filed rather than fixed

- **References into integer-typed fields.** The firewall's `droplet_ids` is
  `{"type": "integer"}` while compute's `id` is `str(droplet["id"])`, so
  `droplet_ids: ["${…:id}"]` resolves to a string its own validation rejects.
  Phase 2's acceptance case is the string-valued DNS one. Documented in the
  spec and **filed as its own issue** — not solved with a cast syntax or by
  churning the compute driver's attribute types.
- **Issue #206** — a cycle recorded in state blocks `plan destroy` for the
  whole deployment. An open correctness bug in the graph this phase builds on,
  and references make a cycle easier to create by accident. Not fixed here; it
  owns a refuse-versus-degrade decision of its own. Called out in the spec.
- **Automatic edge detection** (Phase 3) — a driver class attribute declaring
  which `params` keys hold references would answer the PRD's open question 3,
  explicitly Phase 3's. Phase 2's resolution is driver-agnostic.
- **Phases 4, 5, 6** — orphan refusal and partial-failure recovery;
  concurrency-safe state; parallel execution.
- **PRD open question 2** (file-per-resource) — untouched.
- **Issue #203 itself**, and issue **#214** (the stale one-resource-kind claims
  in `PLAN.md`/`CLAUDE.md`, filed during PR #208).

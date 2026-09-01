# specs/driver_gen.md — `aiform/driver_gen.py`

## Purpose

The generation half of mechanism 2 (`PLAN.md`, "Driver curation"; §6):
draft a new `(provider, resource)` driver via the
`code-generator-model` role, statically
validate its source against `ResourceDriver`'s contract
(`aiform/driver.py`), then run it through gate #1
(`code-review-model`, `llm.review_driver()`), retrying the whole draft
once if either check fails before giving up. Returns the approved source
text and its `DriverReview` — it does not write anything to disk.

**Nothing calls this module, and nothing is meant to.** `PLAN.md`'s
"Driver curation" section and §6 describe this pipeline as the first,
minimal implementation of aiform's own agentic driver-authoring
mechanism — the earliest building block of the fuller, interactive
session §7's `aiform driver create` describes (clarifying ambiguities,
checkpointing at major steps, drawing on an OpenAPI reference). That
command doesn't exist and isn't being built, so today this module is
reachable only from its own tests.

Two things follow, and they are easy to get backwards:

- **It is not waiting to be wired into `plan`/`apply`.** Generating a
  driver mid-`plan` was designed, then **abandoned** — not deferred (see
  "Driver curation"). A missing driver is a permanent error. Adding a
  `generate_driver()` call to the plan path would be reversing a product
  decision, not finishing an unfinished one.
- **Its lack of a caller is not a gap to close in this spec.** The
  module intentionally stays a single-shot draft/validate/review call,
  per `CLAUDE.md`'s MVP-scope discipline; growing it into the
  interactive/OpenAPI-driven shape is mechanism 2's own future work,
  which hasn't started.

**Three judgment calls made explicit here** (not fully specified in
`PLAN.md`, resolved before writing this spec):

1. **Static-validation failures retry, same as gate #1's blocking_issues.**
   `PLAN.md` §5 step 3d only describes retrying once when gate #1's
   review returns `blocking_issues`. It says nothing about a static-validation
   failure (bad syntax, wrong base class, missing method). This spec
   treats that case symmetrically: one retry, feeding the failure reasons
   back into the draft prompt, with a combined budget of **2 draft
   attempts total** across both failure modes — not 2 retries *each*.
2. **This module returns, it does not write.** `draft_driver()` and
   `generate_driver()` never touch the filesystem beyond reading
   `prompts/generate_driver.md`. Writing the approved source to
   `drivers/<provider>/<resource>.py` belongs to the `aiform driver
   create` flow (`PLAN.md` §6/§7), which isn't built — this module only
   drafts, validates, and reviews, and `plan`/`apply` never write a
   driver at all. Computing a driver's `sha256` and recording it in
   `.aiform/state.json`, by contrast, are `orchestrator.py`'s job and
   are shipped today (`ensure_driver_trusted()`, `specs/orchestrator.md`)
   — that half is not waiting on anything.
3. **`draft_driver()` grounds the draft with two more pieces of
   deterministic, non-LLM context beyond `spec.params`, discovered
   necessary empirically** — the first real `generate_driver()` run
   against `digitalocean`/`compute` produced a driver that read
   `credentials["api_token"]` (the real key is `DIGITALOCEAN_TOKEN`, per
   `config.PROVIDER_TOKEN_ENV_VARS`) and skipped the entire resize
   power-cycle sequence `specs/digitalocean_compute.md` spells out in
   detail, because neither piece of information was ever in the prompt —
   `PLAN.md` §5 step 3a only promised "the desired params shape as a hint
   for `PARAM_SCHEMA`," nothing about the credentials key name or an
   existing acceptance-criteria spec. Fixed here rather than papering
   over it with one-off regeneration feedback each time a driver spec
   already exists. Both additions are looked up mechanically, no
   judgment involved in *what* to include, only *whether* it's available:
   - The credentials env var name for `spec.provider`, from
     `aiform.config.PROVIDER_TOKEN_ENV_VARS` — included only when the
     provider is a recognized key in that mapping; silently omitted
     otherwise (an unrecognized provider fails elsewhere, in
     `config.resolve_credentials()`, not here).
   - The full text of `specs/<provider>_<resource>.md`, when that file
     exists on disk (mirroring the naming convention `specs/README.md`
     already documents), framed as authoritative ground truth —
     specifically because a document like `specs/digitalocean_compute.md`
     exists precisely to be more trustworthy than the model's own
     training-data recall of a CSP's API, and the whole point of writing
     it was defeated by never showing it to the model doing the
     generating. Silently omitted when no such file exists yet, which is
     the common case for any `(provider, resource)` pair without a
     hand-written spec.
   This does not change `PLAN.md` §5 step 3a's actual generation
   sequence (draft → validate → review, same retry budget) — only what
   `draft_driver()` puts in the user message before the first draft.

**Consistency note**: `specs/driver.md`'s Behavior section states that
enforcing "every driver declares a `PARAM_SCHEMA`" is this module's job.
Static validation here therefore checks for `PARAM_SCHEMA`'s *presence*
as a class attribute (not its shape/content — that stays unvalidated, per
`PLAN.md` §4's `ResourceDriver` itself never checking it either).

## Interface

```python
EXPECTED_METHOD_PARAMS: dict[str, list[str]] = {
    "create": ["self", "params", "credentials"],
    "read": ["self", "id", "credentials"],
    "update": ["self", "id", "current", "desired", "credentials"],
    "delete": ["self", "id", "credentials"],
}

MAX_DRAFT_ATTEMPTS = 2


class DriverValidationError(Exception):
    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


class DriverGenerationFailed(Exception):
    def __init__(self, source: str, review: DriverReview | None, reasons: list[str]):
        self.source = source
        self.review = review
        self.reasons = reasons
        super().__init__("; ".join(reasons))


def validate_driver_source(source: str) -> None: ...


def draft_driver(
    spec: ResourceSpec,
    *,
    feedback: str | None = None,
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
) -> str: ...


def generate_driver(
    spec: ResourceSpec,
    *,
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
) -> tuple[str, DriverReview]: ...
```

### `validate_driver_source(source: str) -> None`

Static AST checks per `PLAN.md` §5 step 3b, plus the `PARAM_SCHEMA`
presence check noted above. Raises `DriverValidationError` (collecting
every applicable failure into `.reasons`, not just the first) if any
check fails; returns `None` on success.

### `draft_driver(spec, *, feedback=None, client=None, llm_config=None) -> str`

Calls `llm.code_generator_call()` with `prompts/generate_driver.md`
(loaded from `aiform.llm.PROMPTS_DIR`) as the system prompt, no
`output_schema` (plain-text response — Python source isn't a good fit
for `output_config.format`, per `PLAN.md` §5 step 3a), no `max_tokens`
override either — it resolves `llm_config.code_generator.max_tokens`
(`specs/config.md`'s `DEFAULT_LLM_CONFIG`, `8192` by default: a full CRUD
driver with docstrings plausibly exceeds `code_generator_call`'s old
flat default, which is exactly why this call originally hardcoded
`max_tokens=8192` here directly — now expressed as this role's own
configured budget instead of a call-site literal, so a
`.aiform/config.yaml` override actually takes effect here too; see
`specs/llm.md`'s "`max_tokens` is per-role, not a shared constant").
Returns the raw response text unmodified — no parsing, no stripping
markdown code fences.

### `generate_driver(spec, *, client=None, llm_config=None) -> tuple[str, DriverReview]`

The full pipeline. Raises `DriverGenerationFailed` if the draft is still
unacceptable after `MAX_DRAFT_ATTEMPTS`.

## Behavior

- `draft_driver()`'s user content includes `spec.provider`, `spec.resource`,
  and `spec.params` (JSON-serialized) as the shape hint for `PARAM_SCHEMA`,
  per §5 step 3a. It then appends, each only when available (see judgment
  call 3 above):
  1. The credentials env var name for `spec.provider` from
     `aiform.config.PROVIDER_TOKEN_ENV_VARS`, phrased as the exact dict
     shape `credentials` will always be (e.g. `{"DIGITALOCEAN_TOKEN":
     "<token>"}`) — omitted entirely (no placeholder text) when
     `spec.provider` isn't a key in that mapping.
  2. The full text of `specs/<provider>_<resource>.md` (`SPECS_DIR /
     f"{spec.provider}_{spec.resource}.md"`, `SPECS_DIR` computed the
     same way `llm.PROMPTS_DIR` is), introduced as authoritative ground
     truth the draft must follow exactly, more trustworthy than general
     training-data knowledge of the provider's API — omitted entirely
     when no such file exists on disk yet.
  When `feedback` is given, it's appended last, as a distinct "the
  previous draft was rejected for these reasons" block, so a retry's
  prompt is a strict superset of the first attempt's, not a replacement.
- `validate_driver_source()` checks, in order:
  1. `ast.parse(source)` succeeds. On `SyntaxError`, that's the *only*
     reason reported — no further checks run against an unparseable tree.
  2. A top-level class named exactly `Driver` exists.
  3. `Driver`'s bases include `ResourceDriver`.
  4. `Driver`'s class body directly assigns a `PARAM_SCHEMA` attribute
     (presence only — its value is never inspected).
  5. Each of `create`/`read`/`update`/`delete` exists as a method on
     `Driver`, with positional parameter names exactly matching
     `EXPECTED_METHOD_PARAMS` (annotations/defaults/return types are not
     checked, only names, in order).
  6. No `import anthropic` / `from anthropic import ...` anywhere in the
     module (any nesting depth).
  7. No string literal anywhere in the module contains the substring
     `"ANTHROPIC"` — a blunt, grep-style screen for
     `os.environ.get("ANTHROPIC_API_KEY")`/`os.getenv(...)`/
     `os.environ["ANTHROPIC_API_KEY"]` and similar, matching `PLAN.md`'s
     own literal phrasing ("no ... `os.environ.get("ANTHROPIC` pattern")
     rather than deep call-graph analysis of every way to read an env var.
  8. Every `logging.getLogger(...)`/`getLogger(...)` call anywhere in the
     module either passes a string literal starting with
     `"aiform.driver."`, or is flagged: `getLogger(__name__)` specifically
     (`"calls logging.getLogger(__name__) -- ..."`), any other literal
     string not starting with `"aiform.driver."` (`"calls
     logging.getLogger(<repr>) -- must start with 'aiform.driver.' ..."`).
     A driver that never calls `getLogger` at all passes this check
     trivially — logging is encouraged, not mandated. This exists because
     `load_driver()` execs a generated driver's source under a synthetic
     module name (`importlib.util.spec_from_file_location`) that is
     *not* a dotted descendant of the `"aiform"` logger
     `aiform/log.py`'s `configure()` attaches handlers to — the
     idiomatic `logging.getLogger(__name__)` a generated driver would
     otherwise use produces a logger whose output silently never reaches
     either sink, no exception, just missing lines (see
     `drivers/digitalocean/compute.py`'s own `logger =
     logging.getLogger("aiform.driver.digitalocean.compute")` comment,
     which documents the same hazard for the one hand-written driver).
     Caught by `/code-review`: this check didn't exist when that
     workaround first shipped, so it only protected the hand-written
     driver — a generated one would have silently hit the exact same
     hazard with nothing in the automated pipeline positioned to catch
     it. Not a full enforcement of the *exact* expected
     `"aiform.driver.<provider>.<resource>"` name (that would need
     `validate_driver_source()` to take `spec.provider`/`spec.resource`
     as parameters, a signature change judged disproportionate to what
     this check needs to prevent) — just the invariant that actually
     matters for reaching the logger hierarchy: the literal prefix.
  Checks 2–8 all run and accumulate into one `DriverValidationError` when
  the class exists (even if malformed) — the only short-circuit is a
  parse failure, since there's no tree left to inspect after that.
- `generate_driver()`'s loop, for `attempt` in `1..MAX_DRAFT_ATTEMPTS`:
  1. `source = draft_driver(spec, feedback=feedback, ...)`.
  2. `validate_driver_source(source)` — on `DriverValidationError`: if
     this was the last attempt, raise `DriverGenerationFailed(source,
     review=None, reasons=e.reasons)`; otherwise format `e.reasons` into
     `feedback` and continue to the next attempt (no `code-review-model` call this
     round — an invalid draft is never sent for review).
  3. `review = llm.review_driver(source, ...)` — the branch condition is
     `not review.approved`, **not** "`review.blocking_issues` is
     non-empty." `DriverReview`'s own validator only forbids
     `approved=True` with non-empty `blocking_issues`; it does *not*
     forbid `approved=False` with *empty* `blocking_issues` — a
     schema-valid response `code-review-model` can legitimately return. Checking
     `blocking_issues` truthiness alone would silently treat that as a
     pass. On `not review.approved`: reasons are
     `review.blocking_issues or review.concerns or ["review did not
     approve the driver"]` (falling back through whatever the review
     actually said, down to a generic message if it said nothing
     specific in either field). If this was the last attempt, raise
     `DriverGenerationFailed(source, review, reasons)`; otherwise format
     `reasons` into `feedback` and continue.
  4. Otherwise (`review.approved is True`): return `(source, review)`
     immediately — no second attempt is spent if the first succeeds.
     (`DriverReview`'s validator guarantees `blocking_issues == []`
     whenever `approved is True`, so this branch never needs to
     re-check it.)
- The retry budget is **shared** across both failure modes: one static
  failure followed by one gate #1 block still exhausts `MAX_DRAFT_ATTEMPTS`
  and raises — there is no scenario with more than 2 total calls to
  `draft_driver()`.
- **Logging** (`specs/log.md`): `generate_driver()` logs, per attempt,
  `provider=... resource=... attempt=<n>/<MAX_DRAFT_ATTEMPTS>
  outcome=validation_failed|review_rejected|approved` at INFO —
  surfaces the retry loop's progress, previously silent until it either
  succeeds or raises `DriverGenerationFailed`.

## Edge cases / errors

- A `Driver` class with extra methods/attributes beyond the four
  required methods and `PARAM_SCHEMA` is fine — nothing here rejects
  additional helper methods or an optional `LIKELY_REPLACE_FIELDS`
  override.
- `LIKELY_REPLACE_FIELDS`'s presence is *not* checked (unlike
  `PARAM_SCHEMA`) — it's genuinely optional per `aiform/driver.py`
  (defaults to `[]` on the base class), so a generated driver omitting it
  is valid.
- Dynamic or obfuscated credential access (`importlib.import_module("anthropic")`,
  a string built up piecewise instead of a literal `"ANTHROPIC..."`) is
  not caught by checks 6–7 — those are intentionally blunt static screens,
  not a data-flow analysis. Gate #1's (`code-review-model`) semantic review is the actual
  backstop for anything cleverer than a direct import or literal string.
- `DriverGenerationFailed.review` is `None` when the failure was a static
  validation failure on the final attempt (never reached gate #1), and a
  real `DriverReview` with `approved is False` when the failure was a
  gate #1 non-approval on the final attempt — `.blocking_issues` on that
  review may be empty (see the `not review.approved` note above);
  `.reasons` on the exception is never empty even then, since it falls
  back to `.concerns` and then a generic message.
- Once `exceptions.py` exists (`PLAN.md` §1), `DriverGenerationFailed`
  should presumably become `aiform.exceptions.PlanBlockedError` per
  `PLAN.md` §5 step 3d — kept as a plain module-local exception for now,
  same "don't invent exceptions.py's types ahead of it existing" stance
  already established in `config.py`/`state.py`.

## Out of scope

- **Re-reviewing an existing on-disk driver** (`PLAN.md` §5 step 3's
  second case — hash mismatch against a trusted state entry). That path
  is a direct call to `llm.review_driver()` with *no* retry on
  `blocking_issues` (per `PLAN.md`: "the re-review path never retries
  automatically") — different enough from `generate_driver()`'s retry
  loop that it doesn't need a wrapper here; `orchestrator.py` (not built
  yet) calls `llm.review_driver()` directly for that case.
- **Writing to disk, sha256 computation, state.json updates** —
  `orchestrator.py`, per the judgment call above.
- **`prompts/generate_driver.md`'s exact prose** — real production
  content written alongside the implementation (same as
  `prompts/review_driver.md`/`review_plan.md` were for `llm.py`), not
  fully specified in this document beyond what it must instruct the
  model to do: embed `PLAN.md` §4's interface contract, require exact
  argument names for all four methods, require `PARAM_SCHEMA` (and
  optionally `LIKELY_REPLACE_FIELDS`), target the named provider's real
  API, and never touch `anthropic`/credentials.
- **`PARAM_SCHEMA`'s shape/content validation** — only its *presence* is
  checked; whether it's a well-formed JSON Schema, or matches
  `spec.params`, is never validated here (same stance `driver.py`'s own
  spec takes).

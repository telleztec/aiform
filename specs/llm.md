# specs/llm.md — `aiform/llm.py`

## Purpose

The only file in this codebase that talks to a model API. Three public
functions — `implementation_call()` (generic — used for intent parsing,
plan categorization, and driver drafting), `review_driver()`, and
`review_plan()` (the two Opus-gate wrappers from `PLAN.md` §5) — none of
which hardcode a specific model or vendor. *Which* model backs each of
the two roles (`implementation`, `review`) is resolved from
`aiform/config.py` at call time, not baked into this file as a constant.
This is also the file `CLAUDE.md`'s single non-negotiable,
grep-verifiable rule is about: **no `credentials` parameter, local
variable, or import anywhere in it, ever** — model *selection* is
configuration, not a credential, and that rule is unaffected by
anything in this revision.

## Why this changed from the first draft of this spec

The original draft hardcoded `SONNET_MODEL = "claude-sonnet-5"` /
`OPUS_MODEL = "claude-opus-5"` as module constants and named the
generic function `sonnet_call()`. That bakes in both "the model is
Anthropic's Sonnet" and "there is only ever one vendor" as facts this
file can't see past. The project's actual direction: multiple resource
*kinds* need an LLM to help generate their driver eventually, model
*sources* beyond Anthropic (e.g. Bedrock) are a real near-term
possibility, and the specific model used for each of the two roles
should be something the user sets in configuration — not something
hardcoded here. MVP still ships with exactly one source (Anthropic) and
two Anthropic model defaults, but the seam for a second source has to
exist now, the same way `config.py`'s `PROVIDER_TOKEN_ENV_VARS` already
has a real (if single-entry) table instead of a hardcoded
`"digitalocean"` string sprinkled through the code.

**Scope discipline**: this is a dispatch-table seam, not a plugin
system. Adding a second source later means writing one new call
function and adding one dict entry — no abstract base classes, no
registry, no dynamic loading. Don't build more than that now.

## Two new shared concepts in `aiform/models.py`

Same reasoning as `DriverReview`/`PlanReview` (produced in one module,
consumed in another, so it belongs in `models.py`, not local to either):

```python
class ModelSource(str, Enum):
    ANTHROPIC = "anthropic"


class LLMRoleConfig(BaseModel):
    source: ModelSource
    model: str


class LLMConfig(BaseModel):
    implementation: LLMRoleConfig
    review: LLMRoleConfig
```

`ModelSource` is the literal "table that specifies Anthropic as the
only model source at this time" — a one-member enum today, extended by
adding a member (`BEDROCK = "bedrock"`, say) the day a second source is
actually built, at which point `LLMRoleConfig.source: ModelSource`
starts accepting it automatically via Pydantic. `model` stays a plain
`str`, not its own enum — model *names* change far more often than
model *sources*, and there's no fixed set to validate against (Anthropic
alone will presumably ship new model names over this project's
lifetime).

## `aiform/config.py` needs a second resolver

Alongside `resolve_credentials()` (DigitalOcean token), a new
`resolve_llm_config()` reading a **new, non-secret** config file,
`.aiform/config.yaml` — deliberately separate from
`.aiform/credentials.env`, since that file's entire reason for existing
is protecting secret material from shell history/echo (`CLAUDE.md`),
and a model name is not a secret. Full spec: `specs/config.md` (updated
alongside this one — see the implementation order below).

```yaml
# .aiform/config.yaml — optional; every field has a default
llm:
  implementation:
    source: anthropic
    model: claude-sonnet-5
  review:
    source: anthropic
    model: claude-opus-5
```

Defaults (used for any field the file omits, or for everything if the
file doesn't exist at all) preserve today's behavior exactly:
`claude-sonnet-5` for implementation, `claude-opus-5` for review, both
via Anthropic. Unlike credentials, there's a safe default here, so
requiring the user to create this file just to run the MVP would be
pure friction — it exists purely as an override point.

## Interface

```python
def _anthropic_call(
    model: str,
    system_prompt: str,
    user_content: str,
    *,
    output_schema: dict[str, Any] | None = None,
    max_tokens: int = 4096,
    client: anthropic.Anthropic | None = None,
) -> str: ...


MODEL_SOURCES: dict[ModelSource, Callable[..., str]] = {
    ModelSource.ANTHROPIC: _anthropic_call,
}

DRIVER_REVIEW_SCHEMA: dict[str, Any] = {...}  # PLAN.md §5 step 3c, verbatim, unchanged
PLAN_REVIEW_SCHEMA: dict[str, Any] = {...}  # PLAN.md §5 apply step 2, verbatim, unchanged

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def implementation_call(
    system_prompt: str,
    user_content: str,
    *,
    output_schema: dict[str, Any] | None = None,
    max_tokens: int = 4096,
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
) -> str: ...


def review_driver(
    driver_source: str,
    *,
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
) -> DriverReview: ...


def review_plan(
    plan_summary: str,
    *,
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
) -> PlanReview: ...
```

### `_anthropic_call(...)` (private)

Exactly what the old `sonnet_call()`/the shared `_call()` helper did —
the actual `client.messages.create(...)` invocation and response-text
extraction — renamed and scoped to be specifically the Anthropic-source
implementation, not a generic "the-only-vendor" helper. This is the
function a hypothetical `_bedrock_call(...)` would sit next to later.

Text extraction scans `response.content` for the first block with
`type == "text"`, not `response.content[0].text`. Both configured
default models (`claude-sonnet-5`, `claude-opus-5`) run with adaptive
thinking on whenever the `thinking` parameter is omitted (which this
call always does), so `response.content[0]` is a `ThinkingBlock` — no
`.text` attribute — on a real, non-mocked call. If no block has
`type == "text"`, raises a plain `RuntimeError`; not expected to happen
in practice, but silently returning `None`/empty would be worse than a
clear failure.

### `MODEL_SOURCES`

The dispatch table. `implementation_call()`/`review_driver()`/
`review_plan()` all resolve `(source, model)` from an `LLMConfig`, look
up `MODEL_SOURCES[source]`, and call it with that role's `model` string.
This one dict *is* the extensibility seam — nothing else about these
three public functions needs to change to add a source.

### `implementation_call(system_prompt, user_content, *, output_schema=None, max_tokens=4096, client=None, llm_config=None) -> str`

Same generic contract as the old `sonnet_call()` (still the one
function reused by `parser.py`'s intent extraction, `planner.py`'s diff
categorization, and `driver_gen.py`'s driver drafting — each supplies
its own prompt and, when it wants structured output, its own schema),
now resolving which model/source to use from `llm_config` (or
`config.resolve_llm_config()` if not given) instead of a hardcoded
constant. Still always returns the raw response text as a plain `str`
— parsing/validating it is still the caller's job, unchanged from the
original draft.

### `review_driver(driver_source, *, client=None, llm_config=None) -> DriverReview`

Same behavior as before (loads `prompts/review_driver.md` from
`PROMPTS_DIR` internally, constrains output to `DRIVER_REVIEW_SCHEMA`,
stamps `reviewed_at`/`model` onto the raw `{approved, concerns,
blocking_issues}` response to build the `DriverReview`), except the
model used for the call is `llm_config.review.model` via
`MODEL_SOURCES[llm_config.review.source]`, and the `model` field stamped
onto the resulting `DriverReview` is that resolved model string — not a
hardcoded `"claude-opus-5"`. If a different model is configured for
review, the audit trail correctly reflects what actually reviewed it.

### `review_plan(plan_summary, *, client=None, llm_config=None) -> PlanReview`

Same relationship to the old `opus_review_plan()` that `review_driver()`
has to `opus_review_driver()` — same behavior, model resolved from
`llm_config.review` instead of hardcoded.

### `llm_config` parameter (new on all three public functions)

Injectable for testing, exactly like `client` — tests construct an
in-memory `LLMConfig` directly (no temp files, no monkeypatching
`config.py`) and pass it in; production callers omit it and get
`config.resolve_llm_config()`'s real result. This mirrors why `client`
is injectable: keep the whole test suite off the network and off the
filesystem for anything that isn't the specific thing being tested.

## Behavior

- **`"credentials"` does not appear anywhere in `aiform/llm.py`'s source
  text** — unchanged from the original draft, verified the same way.
- `implementation_call()` dispatches to `_anthropic_call()` when
  `llm_config.implementation.source == ModelSource.ANTHROPIC` (the only
  case MVP can exercise), passing `llm_config.implementation.model` as
  the model string.
- `review_driver()`/`review_plan()` dispatch the same way using
  `llm_config.review`.
- With no `llm_config` argument, both roles resolve through
  `config.resolve_llm_config()` — which, with no `.aiform/config.yaml`
  present, means `implementation_call()` acts exactly as the old
  `sonnet_call()` did (`claude-sonnet-5`) and `review_driver()`/
  `review_plan()` act exactly as `opus_review_driver()`/
  `opus_review_plan()` did (`claude-opus-5`). This revision changes
  *how* the model is chosen, not the MVP's actual default behavior.
- `review_driver()`'s returned `DriverReview.model` equals whatever
  `llm_config.review.model` was resolved to, not a fixed string.
- Every function still accepts an injected `client`, and when one is
  given, no real network call is made.
- `output_schema`/no-`output_schema`, raw-text-return, and the two
  Opus-gate schemas (`DRIVER_REVIEW_SCHEMA`, `PLAN_REVIEW_SCHEMA`) are
  all unchanged from the original draft — this revision only touches
  *how the model/source is chosen*, not the calling contract's shape.

## Edge cases / errors

- `MODEL_SOURCES[role.source]` raises a plain `KeyError` if `role.source`
  is a `ModelSource` member with no matching dispatch-table entry (only
  possible if `ModelSource` is extended with a new member before its
  corresponding `_<source>_call` function and `MODEL_SOURCES` entry are
  added — a real but narrow window, not specially handled). Same
  "propagate stdlib errors, don't invent a custom exception ahead of
  `exceptions.py`" stance as `state.py`/`config.py`.
- Anthropic SDK exceptions still propagate unwrapped, unchanged from the
  original draft.
- A `.aiform/config.yaml` that fails to parse as YAML, or whose `llm:`
  section fails `LLMConfig` validation (e.g. an unsupported `source`
  string), raises from `resolve_llm_config()` in `config.py` — not this
  module's concern, since `llm.py` only ever receives an already-valid
  `LLMConfig`.

## Out of scope

- Everything already out of scope in the original draft (reading
  `ANTHROPIC_API_KEY`, driver-generation retry logic, assembling
  `plan_summary` text, loading the three non-review prompt files) —
  unchanged.
- **A second model source.** `_anthropic_call()` is the only entry in
  `MODEL_SOURCES`. Writing e.g. `_bedrock_call()` is real future work,
  not simulated or stubbed here.
- **Per-resource-kind model configuration.** The user's stated direction
  is that a *future* resource kind requiring its own LLM interaction
  should eventually be configurable independently — MVP's `LLMConfig`
  has exactly two roles (`implementation`, `review`), global to the
  whole tool, not per-resource-kind. Extending `LLMConfig` with
  per-resource-kind overrides is a real future step, deliberately not
  built now.
- **Validating `.aiform/config.yaml`'s `source` against `MODEL_SOURCES`
  at config-resolution time.** `config.py` validates `source` is a
  known `ModelSource` *enum member* (via Pydantic); it does not import
  `llm.py` to check that member has an actual dispatch-table entry —
  keeps the dependency direction one-way (`llm.py` depends on
  `config.py`, never the reverse). The narrow drift window this leaves
  is accepted, see Edge cases above.

## Suggested implementation order

1. **`PLAN.md`/`CLAUDE.md` updates first.** `PLAN.md`'s Context section
   ("Model tiering") and §1's repo-layout comment for `llm.py` still
   describe the hardcoded-Sonnet/Opus design; `CLAUDE.md`'s "Model
   tiering" non-negotiable rule needs a clarifying edit — the *defaults*
   stay non-negotiable, but user-configurability via `.aiform/config.yaml`
   is now a deliberate, intentional escape hatch, not a violation of
   that rule. Small, docs-only, but everything below depends on this
   being the agreed design before code reflects it.
2. **`specs/config.md` + `specs/models.md` updates**, formalizing
   `ModelSource`/`LLMRoleConfig`/`LLMConfig` and `resolve_llm_config()`
   — already drafted above and in the models.py section; needs
   `specs/config.md` itself updated to match (currently only documents
   `resolve_credentials()`).
3. **`models.py`**: add `ModelSource`, `LLMRoleConfig`, `LLMConfig`
   (alongside the already-in-flight `PlanReview`/`PlanReviewFlag`/
   `PlanReviewSeverity` addition from this same pass) — tests, red,
   implementation, green.
4. **`config.py`**: add `resolve_llm_config()` + `DEFAULT_LLM_CONFIG` —
   tests, red, implementation, green. Depends on step 3.
5. **`llm.py`**: rewrite `tests/test_llm.py` for the renamed
   functions/config-driven design (the current draft still tests
   `sonnet_call()`/`opus_review_driver()`/`opus_review_plan()` against
   hardcoded constants), confirm red, implement, green.
6. **One `/code-review` + one PR** for the whole pass (steps 3–5
   together, since `llm.py` can't be reviewed or merged meaningfully
   without the `models.py`/`config.py` pieces it depends on) — step 1's
   docs update can either ride in the same PR or go first as its own
   tiny PR; either is fine, your call when we get there.

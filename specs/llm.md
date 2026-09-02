# specs/llm.md — `aiform/llm.py`

## Purpose

The only file in this codebase that talks to a model API. Four public
functions — `intent_orchestration_call()` (prose Intent parsing and plan
categorization), `code_generator_call()` (driver drafting),
`review_driver()`, and `review_plan()` (the two review-gate wrappers from
`PLAN.md` §5, gate #1 and gate #2) — none of which hardcode a specific
model or vendor. *Which* model backs each of the four roles
(`intent_orchestration`, `code_generator`, `code_review`,
`review_orchestration`) is resolved from `aiform/config.py` at call time,
not baked into this file as a constant. This is also the file
`CLAUDE.md`'s single non-negotiable, grep-verifiable rule is about: **no
`credentials` parameter, local variable, or import anywhere in it,
ever** — model *selection* is configuration, not a credential, and that
rule is unaffected by anything in this revision.

## Why this changed from the first draft of this spec

The original draft hardcoded `SONNET_MODEL = "claude-sonnet-5"` /
`OPUS_MODEL = "claude-opus-5"` as module constants and named the
generic function `sonnet_call()`. That bakes in both "the model is
Anthropic's Sonnet" and "there is only ever one vendor" as facts this
file can't see past. The project's actual direction: multiple resource
*kinds* need an LLM to help generate their driver eventually, model
*sources* beyond Anthropic (e.g. Bedrock) are a real near-term
possibility, and the specific model used for each role should be
something the user sets in configuration — not something hardcoded
here. MVP still ships with exactly one source (Anthropic) and two
Anthropic model defaults across the four roles, but the seam for a
second source has to exist now, the same way `config.py`'s
`PROVIDER_TOKEN_ENV_VARS` already has a real (if single-entry) table
instead of a hardcoded `"digitalocean"` string sprinkled through the
code.

**Scope discipline**: this is a dispatch-table seam, not a plugin
system. Adding a second source later means writing one new call
function and adding one dict entry — no abstract base classes, no
registry, no dynamic loading. Don't build more than that now.

## Second revision: four roles, not two

This spec originally had `implementation_call()` serve three different
call sites (`parser.py`'s intent extraction, `planner.py`'s diff
categorization, and `driver_gen.py`'s driver drafting) under one shared
`implementation` role, paired with a single `review` role covering both
review gates. That collapsed two conceptually different jobs into each
role: "interpret this diff/prose" and "write new Python source" are not
the same kind of work, and "review a driver's source" and "review a
destructive plan" aren't either — yet a user could only tune them
together. `PLAN.md`'s "Model tiering" section now names four
independently configurable roles instead, each mapped to the prompt
file(s) that drive it — four roles covering the five prompt files under
`prompts/` (`intent_orchestration` is the one role that owns two
closely-related prompts, not a one-role-per-file split):

| Role | Prompt file(s) | Default model | Public function |
| --- | --- | --- | --- |
| `intent_orchestration` | `parse_intent.md`, `diff_plan.md` | `claude-sonnet-5` | `intent_orchestration_call()` |
| `code_generator` | `generate_driver.md` | `claude-sonnet-5` | `code_generator_call()` |
| `code_review` | `review_driver.md` | `claude-opus-5` | `review_driver()` |
| `review_orchestration` | `review_plan.md` | `claude-opus-5` | `review_plan()` |

`implementation_call()` is split into `intent_orchestration_call()` and
`code_generator_call()` — same generic raw-text-return contract as
before, just resolving a different `LLMRoleConfig` off `LLMConfig` each.
`review_driver()`/`review_plan()` keep their names (they already read as
role-specific, not generic) but now resolve `llm_config.code_review` /
`llm_config.review_orchestration` instead of `llm_config.review`.

## Two new shared concepts in `aiform/models.py`

Same reasoning as `DriverReview`/`PlanReview` (produced in one module,
consumed in another, so it belongs in `models.py`, not local to either):

```python
class ModelSource(str, Enum):
    ANTHROPIC = "anthropic"


class LLMRoleConfig(BaseModel):
    source: ModelSource
    model: str
    max_tokens: int


class LLMConfig(BaseModel):
    intent_orchestration: LLMRoleConfig
    code_generator: LLMRoleConfig
    code_review: LLMRoleConfig
    review_orchestration: LLMRoleConfig
```

`ModelSource` is the literal "table that specifies Anthropic as the
only model source at this time" — a one-member enum today, extended by
adding a member (`BEDROCK = "bedrock"`, say) the day a second source is
actually built, at which point `LLMRoleConfig.source: ModelSource`
starts accepting it automatically via Pydantic. `model` stays a plain
`str`, not its own enum — model *names* change far more often than
model *sources*, and there's no fixed set to validate against (Anthropic
alone will presumably ship new model names over this project's
lifetime). `LLMConfig`'s four fields are independent — see "Second
revision" above for why they aren't collapsed back into two.
`max_tokens` is the third independently configurable field per role
(alongside `source`/`model`), added after a live system-test run
surfaced that a single shared budget can't fit both roles' needs — see
"`max_tokens` is per-role, not a shared constant" below.

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
  intent_orchestration:
    source: anthropic
    model: claude-sonnet-5
    max_tokens: 4096
  code_generator:
    source: anthropic
    model: claude-sonnet-5
    max_tokens: 8192
  code_review:
    source: anthropic
    model: claude-opus-5
    max_tokens: 8192
  review_orchestration:
    source: anthropic
    model: claude-opus-5
    max_tokens: 8192
```

Defaults (used for any field the file omits, or for everything if the
file doesn't exist at all) preserve the historical implementation/review
split's behavior exactly: `claude-sonnet-5` for `intent_orchestration`
and `code_generator`, `claude-opus-5` for `code_review` and
`review_orchestration`, all via Anthropic. Unlike credentials, there's a
safe default here, so requiring the user to create this file just to run
the MVP would be pure friction — it exists purely as an override point.
A user who only wants to change one role (e.g. swap `code_generator` to
a newer/cheaper model as pricing changes) overrides just that one field;
the other three keep their defaults — see `resolve_llm_config()`'s
per-field-default-merge behavior below.

### `max_tokens` is per-role, not a shared constant

Originally every one of the four public call functions below took its
own `max_tokens: int = 4096` parameter, all defaulting to the same
hardcoded value regardless of role. A live system-test run against
`code_review`'s gate #1 call surfaced why that's wrong: the model
Anthropic resolves for `code_review`/`review_orchestration` runs with
adaptive thinking on by default (see `_anthropic_call`'s note below),
and that thinking output shares the same `max_tokens` budget as the
actual response text. Directly inspecting a live response's
`usage.output_tokens_details.thinking_tokens` showed thinking alone
consuming the majority of a 4096-token budget on one real call,
occasionally leaving too little room for `code_review`'s verbose
`concerns`/`blocking_issues` prose to finish generating — the JSON
response gets cut off mid-string, and `json.loads()` fails downstream
with a generic parse error that gives no hint the real cause is a token
budget, not malformed output.

`max_tokens` moved onto `LLMRoleConfig` (`specs/models.md`, `Field(gt=0)`
— zero/negative rejected at config-load time rather than surfacing as an
opaque Anthropic API error later) so each role resolves its own
configured budget instead of sharing one constant. `intent_orchestration`
keeps the original `4096` (short structured categorizations, no observed
need for more). The other three default to `8192`: `code_review`/
`review_orchestration` for the thinking-vs-prose reason above, and
`code_generator` because `aiform/driver_gen.py`'s `draft_driver()` — its
one real caller — already hardcoded `max_tokens=8192` at its call site
before this field existed, for an independent, pre-existing reason (a
full CRUD driver's Python source plausibly exceeds a smaller budget).
That hardcoded override is now removed in favor of `code_generator`'s
own role-configured `8192` — the whole point of this change is a role's
budget living in one place, not a per-call-site literal a
`.aiform/config.yaml` override would silently fail to reach; see
`specs/driver_gen.md`.
`intent_orchestration_call()`/`code_generator_call()` still accept an
explicit `max_tokens` override parameter (now `None` by default, meaning
"use the resolved role's own value" instead of a hardcoded literal);
`review_driver()`/`review_plan()` don't expose one — they resolve their
role's `max_tokens` internally, with no caller-facing override, matching
their existing signatures which don't expose `output_schema` either.

## Fourth revision: logging needs the response metadata `_anthropic_call` used to discard

`PLAN.md` §10's "Logging" item (`specs/log.md`) names this file's own
gap as its concrete motivating example: `_anthropic_call()` extracted
only the response text and threw away `response.stop_reason`/
`response.usage`, so a response truncated by hitting `max_tokens`
mid-JSON (the exact failure the `max_tokens`-is-per-role change above
was written to reduce, not eliminate) surfaced only as an opaque
`JSONDecodeError`/`ValidationError` downstream, with no hint the real
cause was a token budget.

**Rejected fix**: adding a `role_name` parameter to `_anthropic_call()`
itself, so it could log `role=<role_name>` directly. Every one of this
function's current parameters is justified above as "how do I talk to
Anthropic" — role name is a pure observability concern with no reason
to live on this specific, vendor-scoped function, and every caller
already knows its own role name without needing to hand it back in.

**Actual fix**: `_anthropic_call()`'s return type widens from `str` to
a small frozen result carrying what logging needs:

```python
@dataclass(frozen=True)
class ModelCallResult:
    text: str
    stop_reason: str
    input_tokens: int
    output_tokens: int
    thinking_tokens: int | None
    duration_ms: int
```

(This field list was written without `duration_ms` in the first pass of
this revision, even though the `_anthropic_call` prose below already
committed to measuring it around the `client.messages.create(...)`
call — caught and fixed in the same PR that implements this, per
"Specs are living docs.")

`thinking_tokens` is `getattr(response.usage, "output_tokens_details",
None)`-defensive (then `.thinking_tokens` off that, still
`getattr`-defensive) — not every response shape is guaranteed to carry
it, and this function must not crash just because the field it wants
to log is absent. `MODEL_SOURCES: dict[ModelSource, Callable[...,
str]]` becomes `Callable[..., ModelCallResult]` accordingly — the one
dispatch seam this spec already protects (see "Scope discipline"
above) stays exactly that: one seam, an updated contract, no plugin
system, no second seam introduced for logging.

The three places that already know their own role name —
`_implementation_tier_call()` (shared by `intent_orchestration_call()`/
`code_generator_call()`), `review_driver()`, `review_plan()` — each
log the call-level metadata (`role`, `model`, `stop_reason`, token
counts, `duration_ms`) at INFO, and specifically WARNING (with a
free-text `msg`) when `stop_reason == "max_tokens"`, then unwrap
`result.text` before doing whatever they already did with it. **Every
public function's own signature and return type is unchanged** —
`intent_orchestration_call()`/`code_generator_call()` still return
`str`; every existing caller in `parser.py`/`planner.py`/
`driver_gen.py` needs no change. Full behavior list: `specs/log.md`.

## Interface

```python
def _anthropic_call(
    model: str,
    system_prompt: str,
    user_content: str,
    *,
    max_tokens: int,
    output_schema: dict[str, Any] | None = None,
    client: anthropic.Anthropic | None = None,
) -> ModelCallResult: ...


MODEL_SOURCES: dict[ModelSource, Callable[..., ModelCallResult]] = {
    ModelSource.ANTHROPIC: _anthropic_call,
}

DRIVER_REVIEW_SCHEMA: dict[str, Any] = {...}  # PLAN.md §5 step 3c, verbatim, unchanged
PLAN_REVIEW_SCHEMA: dict[str, Any] = {...}  # PLAN.md §5 apply step 2, verbatim, unchanged

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def intent_orchestration_call(
    system_prompt: str,
    user_content: str,
    *,
    output_schema: dict[str, Any] | None = None,
    max_tokens: int | None = None,
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
) -> str: ...


def code_generator_call(
    system_prompt: str,
    user_content: str,
    *,
    output_schema: dict[str, Any] | None = None,
    max_tokens: int | None = None,
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
the actual `client.messages.create(...)` invocation and response
extraction — renamed and scoped to be specifically the Anthropic-source
implementation, not a generic "the-only-vendor" helper. This is the
function a hypothetical `_bedrock_call(...)` would sit next to later.
Returns a `ModelCallResult` (see "Fourth revision" above), not a bare
`str` — timed with `time.monotonic()` around the `client.messages.create(...)`
call itself, so `duration_ms` reflects only the network/inference time,
not any surrounding setup.

Text extraction scans `response.content` for the first block with
`type == "text"`, not `response.content[0].text`. Every configured
default model (`claude-sonnet-5`, `claude-opus-5`) runs with adaptive
thinking on whenever the `thinking` parameter is omitted (which this
call always does), so `response.content[0]` is a `ThinkingBlock` — no
`.text` attribute — on a real, non-mocked call. If no block has
`type == "text"`, raises a plain `RuntimeError`; not expected to happen
in practice, but silently returning `None`/empty would be worse than a
clear failure.

`max_tokens` is a required keyword-only parameter here, no fallback
default — deliberately, after the per-role `max_tokens` change above.
Every real call path (`_implementation_tier_call()`, `review_driver()`,
`review_plan()`) always resolves a role's own `max_tokens` and passes it
explicitly; a hardcoded default here would be dead code on every current
path and a silent trap for a future caller that forgets to resolve one —
exactly the shared-constant problem this change exists to eliminate,
reintroduced one call site at a time. A caller that omits it gets a
`TypeError` immediately, not a quietly-wrong budget.

### `MODEL_SOURCES`

The dispatch table. `intent_orchestration_call()`/`code_generator_call()`/
`review_driver()`/`review_plan()` all resolve `(source, model)` from an
`LLMConfig`, look up `MODEL_SOURCES[source]`, and call it with that
role's `model` string. This one dict *is* the extensibility seam —
nothing else about these four public functions needs to change to add a
source.

### `intent_orchestration_call(system_prompt, user_content, *, output_schema=None, max_tokens=None, client=None, llm_config=None) -> str`

Backs the `intent-orchestration-model` role: the one function reused by
`parser.py`'s intent extraction and `planner.py`'s diff categorization
— each supplies its own prompt and, when it wants structured output,
its own schema. Resolves which model/source to use from
`llm_config.intent_orchestration` (or `config.resolve_llm_config()` if
`llm_config` isn't given) instead of a hardcoded constant. `max_tokens`
defaults to `None`, meaning "use `llm_config.intent_orchestration.max_tokens`"
— an explicit value overrides the resolved role's own budget for that
one call, same override pattern as `output_schema`. Always returns the
raw response text as a plain `str` — parsing/validating it is the
caller's job.

### `code_generator_call(system_prompt, user_content, *, output_schema=None, max_tokens=None, client=None, llm_config=None) -> str`

Backs the `code-generator-model` role: `driver_gen.py`'s driver
drafting, the one caller of this function in the MVP (deferred, not
invoked by `plan`/`apply` — `PLAN.md`'s "Driver curation"). Same
contract as `intent_orchestration_call()` — raw text out, caller
parses/validates, `max_tokens=None` meaning "use the resolved role's own
value" — resolving `llm_config.code_generator` instead. `driver_gen.py`
calls this with no `output_schema`: Python source isn't a good fit for
`output_config.format` (`PLAN.md` §5 step 3a).

Both functions above share an identical signature and differ only in
which `LLMRoleConfig` they resolve — deliberately: a caller that needs
to route by role at runtime can hold either as a value with the same
type, rather than branching on a role enum. Neither is a thin wrapper
around the other; each resolves its own field off `LLMConfig` directly.

### `review_driver(driver_source, *, client=None, llm_config=None) -> DriverReview`

Backs the `code-review-model` role, gate #1. Loads
`prompts/review_driver.md` from `PROMPTS_DIR` internally, constrains
output to `DRIVER_REVIEW_SCHEMA`, stamps `reviewed_at`/`model` onto the
raw `{approved, concerns, blocking_issues}` response to build the
`DriverReview`. Resolves and passes `llm_config.code_review.max_tokens`
to the underlying call — no caller-facing override, unlike
`intent_orchestration_call()`/`code_generator_call()`. The model used
for the call is
`llm_config.code_review.model` via `MODEL_SOURCES[llm_config.code_review.source]`,
and the `model` field stamped onto the resulting `DriverReview` is that
resolved model string — not a hardcoded `"claude-opus-5"`. If a
different model is configured for `code_review`, the audit trail
correctly reflects what actually reviewed it.

### `review_plan(plan_summary, *, client=None, llm_config=None) -> PlanReview`

Backs the `review-orchestration-model` role, gate #2. Same shape as
`review_driver()` — model and `max_tokens` resolved from
`llm_config.review_orchestration` instead.

### `verify_api_key(*, client=None, timeout=10.0) -> KeyCheck`

Not a model call and not a fifth role — a **credential probe**, used
only by `aiform init`'s preflight (`specs/cli.md`). Issues
`client.models.list(limit=1)` (`GET /v1/models`), which is free: no
tokens billed, no message created, no state changed on Anthropic's side.

Lives here rather than in `cli.py` because this file already owns
Anthropic client construction, and nowhere else should be importing
`anthropic` to build one.

Returns a `KeyCheck` (`aiform/models.py`) — `state` plus an optional
`detail`:

| `state` | Meaning | `detail` |
|---|---|---|
| `KeyState.OK` | probe returned 2xx | `None` |
| `KeyState.MISSING` | `ANTHROPIC_API_KEY` unset | `None` |
| `KeyState.REJECTED` | API returned **400, 401 or 403** (`config.ANTHROPIC_KEY_VERDICT_STATUSES`) | the API's own error message |
| `KeyState.UNVERIFIED` | API returned **3xx** | `"unexpected redirect (HTTP {code})"` — canned, never the body |
| `KeyState.UNVERIFIED` | any other status, or unreachable | the API's error, or the connection error |

`REJECTED` covers `AuthenticationError` (401), `PermissionDeniedError`
(403) **and `BadRequestError` (400)** — the 400 case is the one that
motivated this function, since an identity-linked key 400s rather than
401s, and treating only 401/403 as rejection would miss exactly the bug
being fixed.

`UNVERIFIED` is the default: every status outside the verdict set, plus
`APIConnectionError` (timeout and DNS failure). That covers 408, 429 and
every 5xx — a rate limit or an outage is not a verdict on the key, and a
busy org key routinely 429s — and it also covers **404 and 405**, which say
nothing about the credential at all: an `ANTHROPIC_BASE_URL` pointed at a
gateway that proxies `/v1/messages` but not `/v1/models` answers 404 for a
key that then works fine on the `plan`/`apply` path. None of these may ever
be reported as `REJECTED`; telling a user to rotate a working credential is
the same class of error as passing a broken one. Constructed
with `max_retries=0` and the given `timeout` so `init` cannot hang.

**A redirect is refused, not followed** — the client is constructed with
`follow_redirects=False` (via `anthropic.DefaultHttpxClient`, which keeps the
SDK's own connection limits and keepalive socket options). This is the same
rule the provider probe applies in `specs/cli.md`, and it needs stating
separately because the reasoning recorded there does not carry over: httpx
strips `Authorization` on a cross-origin redirect, but this SDK authenticates
with **`x-api-key`**, a custom header httpx does not strip. Left at the SDK's
default the probe would follow a 3xx from a captive portal or a hostile
`ANTHROPIC_BASE_URL`, hand the key to the `Location` target, and then report
that target's 2xx as `OK` — a leaked credential and a green check for it.

Be precise about what this does *not* buy, since an unqualified sentence
here is what hid #97. A hostile `ANTHROPIC_BASE_URL` receives `x-api-key`
on the **first** hop, with no redirect involved; refusing 3xx stops it
recruiting a *second* recipient and nothing more. The base URL is trusted
input by construction — the `401` note below is the other half of that
same trust. Against a captive portal, which intercepts a request aimed at
the real API, the refusal is the whole defence.

The `detail` for a 3xx is **canned**, not the API's error message: the body
of a redirect belongs to whoever sent it, and `init` prints this string.

Known and deliberately not fixed here: the **non**-3xx paths still echo the
API's own `error.message` verbatim, unbounded and unredacted, where the
provider probe caps the read (`_MAX_PROBE_BODY`) and redacts
(`_redact(detail, token)`). A hostile `ANTHROPIC_BASE_URL` answering `401`
therefore still chooses what `init` prints. Recorded rather than left
implicit — an unqualified sentence in `specs/cli.md` is precisely what
hid #97 — and the redaction half is in tension with CLAUDE.md's rule that
`llm.py` never handles the key value.

Both halves apply only to the client this function builds. An injected
`client` carries its own transport configuration — its own redirect policy
exactly as it carries its own `timeout` — and cannot be hardened from here.

**This function takes no `credentials` parameter and introduces no
`credentials` identifier** — the SDK resolves `ANTHROPIC_API_KEY` from
the environment itself. The grep-verifiable property in this spec's
Purpose section (`"credentials"` does not appear anywhere in
`aiform/llm.py`) is unaffected, and the existing test asserting it
continues to pass unchanged.

`client` is injectable for the same reason it is on the four role
functions: the default test run must make no network calls.

### `llm_config` parameter (on all four public functions)

Injectable for testing, exactly like `client` — tests construct an
in-memory `LLMConfig` directly (no temp files, no monkeypatching
`config.py`) and pass it in; production callers omit it and get
`config.resolve_llm_config()`'s real result. This mirrors why `client`
is injectable: keep the whole test suite off the network and off the
filesystem for anything that isn't the specific thing being tested.

## Behavior

- **`"credentials"` does not appear anywhere in `aiform/llm.py`'s source
  text** — unchanged from the original draft, verified the same way.
- `intent_orchestration_call()` dispatches to `_anthropic_call()` when
  `llm_config.intent_orchestration.source == ModelSource.ANTHROPIC` (the
  only case MVP can exercise), passing
  `llm_config.intent_orchestration.model` as the model string.
- `code_generator_call()` dispatches the same way using
  `llm_config.code_generator`; `review_driver()` using
  `llm_config.code_review`; `review_plan()` using
  `llm_config.review_orchestration`. All four roles are resolved and
  dispatched independently — there is no shared "implementation" or
  "review" grouping in the dispatch logic itself, only in the informal
  implementation-tier/review-tier framing `PLAN.md` uses to describe them.
- With no `llm_config` argument, all four roles resolve through
  `config.resolve_llm_config()` — which, with no `.aiform/config.yaml`
  present, means `intent_orchestration_call()`/`code_generator_call()`
  act exactly as the old `sonnet_call()` did (`claude-sonnet-5`) and
  `review_driver()`/`review_plan()` act exactly as
  `opus_review_driver()`/`opus_review_plan()` did (`claude-opus-5`).
  This revision changes *how* the model is chosen, not the MVP's actual
  default behavior.
- `review_driver()`'s returned `DriverReview.model` equals whatever
  `llm_config.code_review.model` was resolved to, not a fixed string.
- Every function still accepts an injected `client`, and when one is
  given, no real network call is made.
- `output_schema`/no-`output_schema`, raw-text-return, and the two
  review-gate schemas (`DRIVER_REVIEW_SCHEMA`, `PLAN_REVIEW_SCHEMA`) are
  all unchanged from the original draft — this revision only touches
  *how the model/source is chosen*, not the calling contract's shape.
- **Logging** (`specs/log.md`), per the "Fourth revision" above:
  - `_implementation_tier_call()` logs `role=<role_name>
    model=<model> stop_reason=<...> input_tokens=<...>
    output_tokens=<...> duration_ms=<...>` at INFO after every call —
    WARNING instead, with `msg="response likely truncated -- max_tokens
    reached before JSON completed"`, when `result.stop_reason ==
    "max_tokens"`.
  - `review_driver()`/`review_plan()` log the same call-level line
    (their own hardcoded role name), plus a separate decision-level
    line after building their return value: `review_driver` logs
    `approved=<bool> concerns_count=<n> blocking_issues_count=<n>`;
    `review_plan` logs `safe_to_proceed=<bool> flags_count=<n>`. Never
    the free-text `concerns`/`blocking_issues`/`flag.concern` content
    itself — counts only, per `specs/log.md`'s Out of scope.
  - `"credentials"` still does not appear anywhere in this file's
    source text (see Behavior's first bullet above) — `ModelCallResult`
    and every logged field are built from response metadata and
    resolved role/model names, never from anything credential-shaped.

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
  has exactly four roles (`intent_orchestration`, `code_generator`,
  `code_review`, `review_orchestration`), global to the whole tool, not
  per-resource-kind. Extending `LLMConfig` with per-resource-kind
  overrides is a real future step, deliberately not built now.
- **Validating `.aiform/config.yaml`'s `source` against `MODEL_SOURCES`
  at config-resolution time.** `config.py` validates `source` is a
  known `ModelSource` *enum member* (via Pydantic); it does not import
  `llm.py` to check that member has an actual dispatch-table entry —
  keeps the dependency direction one-way (`llm.py` depends on
  `config.py`, never the reverse). The narrow drift window this leaves
  is accepted, see Edge cases above.

## Implementation status

The four-role migration this document specifies (second and third
revisions above) is complete. `PLAN.md`'s "Model tiering" section and §1's
repo-layout comment describe the four named roles, not the old
hardcoded-Sonnet/Opus or `implementation`/`review` designs, and
`aiform/models.py`/`config.py`/`llm.py` implement `ModelSource`/
`LLMRoleConfig`/`LLMConfig`/`resolve_llm_config()` and the four public
call functions (`intent_orchestration_call()`, `code_generator_call()`,
`review_driver()`, `review_plan()`) as specified above.
`llm.implementation_call()`/`sonnet_call()`/`opus_review_driver()`/
`opus_review_plan()` no longer exist anywhere in the codebase.
`aiform/parser.py` and `aiform/planner.py` call
`llm.intent_orchestration_call()`; `aiform/driver_gen.py` calls
`llm.code_generator_call()` — matching this spec, not the two-role design
described in earlier revisions.

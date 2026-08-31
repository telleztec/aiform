# specs/models.md — `aiform/models.py`

## Purpose

Pydantic v2 models for the data shapes that cross module boundaries in
aiform: the parsed `.aiform.md` frontmatter, one row of a printed plan,
one resource's entry in `.aiform/state.json`, and the records of the two
review-gate calls (gate #1, gate #2). Also the shared shape for *which*
model backs each of `aiform/llm.py`'s four roles
(`intent-orchestration-model`, `code-generator-model`,
`code-review-model`, `review-orchestration-model` — `PLAN.md`'s "Model
tiering"), resolved from configuration rather than hardcoded (see
`ModelSource`/`LLMRoleConfig`/`LLMConfig` below). Pure data definitions
— no file I/O, no LLM calls, no filesystem path construction.
Everything here is exactly what `PLAN.md` §1's repo-layout comment calls
out for `models.py`, plus the additions noted below.

## Interface

### `ResourceSpec`

The validated shape of one `.aiform.md` file's frontmatter (`PLAN.md`
§2, §5 step 2).

```python
class ResourceSpec(BaseModel):
    resource: str
    name: str
    provider: str
    params: dict[str, Any]
```

- `resource` / `provider` — validated lowercase, `^[a-z][a-z0-9_]*$`.
  Both end up as filesystem path segments (`drivers/<provider>/<resource>.py`,
  `PLAN.md` §1) — rejecting anything else here is a validate-at-the-boundary
  measure, not speculative hardening, since these two fields are the only
  place a hand-edited `.aiform.md` file's text flows into a path.
- `name` — non-empty string. No character restriction beyond that; it's
  used as a state-dict key component (`PLAN.md` §3), not a path segment.
- `params` — open `dict[str, Any]`, deliberately unvalidated here.
  `PLAN.md` §2 is explicit that `params`' shape is only checked later,
  against the resolved driver's `PARAM_SCHEMA` (§4) — `ResourceSpec`
  itself must not narrow it.

### `PlanAction`

```python
class PlanAction(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    DESTROY = "destroy"
    NO_OP = "no-op"
```

The four categorizations from `PLAN.md` §5 step 6.

### `PlanEntry`

One row of a printed plan (`PLAN.md` §5 step 7).

```python
class PlanEntry(BaseModel):
    resource_key: str
    action: PlanAction
    rationale: str
    likely_replace: bool = False
```

- `resource_key` — the `"<provider>.<resource>.<name>"` address (`PLAN.md` §3).
- `rationale` — always populated. For the deterministic no-op short-circuit
  (§5 step 5, zero LLM calls), this is a fixed string (e.g. `"no changes
  detected"`), not `intent-orchestration-model` output — `PlanEntry`
  doesn't distinguish the two sources, and doesn't need to.
- `likely_replace` — only meaningful when `action == PlanAction.UPDATE`.
  A model validator forces it back to `False` for every other action, so
  a caller can't construct an inconsistent `PlanEntry` (e.g. a `destroy`
  flagged `likely_replace: true`, which isn't a meaningful state per
  §5 step 6's description).

### `PlanReviewSeverity`, `PlanReviewFlag`, `PlanReview`

Added alongside `specs/llm.md`'s `review_plan()` — the gate #2
(`review-orchestration-model`) verdict shape (`PLAN.md` §5 apply step
2's `PLAN_REVIEW_SCHEMA`).
Not part of the original repo-layout comment's model list, same
situation as `DriverInfo`: `llm.py` produces this, `orchestrator.py`
branches on it (`severity: "block"` halts `apply` unconditionally), so
it crosses a module boundary the same way `DriverReview` does.

```python
class PlanReviewSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    BLOCK = "block"


class PlanReviewFlag(BaseModel):
    resource_key: str
    concern: str
    severity: PlanReviewSeverity


class PlanReview(BaseModel):
    safe_to_proceed: bool
    flags: list[PlanReviewFlag]
```

Unlike `DriverReview`, no `reviewed_at`/`model` stamping — a plan
review is never persisted to `state.json` (it's used once per `apply`
run, then discarded), so there's no audit trail to keep and `PLAN.md`
doesn't call for one.

### `ModelSource`, `LLMRoleConfig`, `LLMConfig`

Added alongside `specs/llm.md`'s config-driven model selection and
`specs/config.md`'s `resolve_llm_config()`. Same reasoning as
`DriverReview`/`PlanReview`: produced by `config.py`, consumed by
`llm.py`, so the shared shape lives here rather than in either.

```python
class ModelSource(str, Enum):
    ANTHROPIC = "anthropic"


class LLMRoleConfig(BaseModel):
    source: ModelSource
    model: str
    max_tokens: int = Field(gt=0)


class LLMConfig(BaseModel):
    intent_orchestration: LLMRoleConfig
    code_generator: LLMRoleConfig
    code_review: LLMRoleConfig
    review_orchestration: LLMRoleConfig
```

- `ModelSource` is the literal "table that specifies Anthropic as the
  only model source at this time" — a one-member enum today, mirroring
  `config.py`'s `PROVIDER_TOKEN_ENV_VARS` in spirit (a real, if
  single-entry, table rather than a hardcoded string). Extended by
  adding a member (e.g. `BEDROCK = "bedrock"`) the day a second source
  is actually built.
- `LLMRoleConfig.model` is a plain `str`, not its own enum — model
  *names* change far more often than model *sources*, and there's no
  fixed set to validate against.
- `LLMRoleConfig.max_tokens` is required (`Field(gt=0)`, no pydantic-level
  *default* — same reasoning as `source`/`model`: every default value
  lives in `config.py`'s `DEFAULT_LLM_CONFIG`, the single source of
  truth, not duplicated here). The `gt=0` bound rejects `0`/negative
  values at config-load time with a clear `ValidationError` — matching
  how other fields in this file already constrain themselves
  (`Field(min_length=1)`/`Field(pattern=...)` elsewhere) — rather than
  letting a `0` or a typo'd negative value from `.aiform/config.yaml`
  reach the Anthropic API and surface only as an opaque HTTP error deep
  inside `review_driver()`/`review_plan()`. Added after a live
  system-test run against
  `code_review`'s gate #1 call surfaced that Opus's (apparently
  automatic) extended-thinking output can consume most of a shared
  `max_tokens` budget before the actual structured JSON response even
  starts, occasionally truncating it mid-string — a real capacity bug,
  not a hypothetical one, caught by directly inspecting a live
  response's `usage.output_tokens_details.thinking_tokens` against the
  budget. A single hardcoded `max_tokens` shared across all four roles
  (the pre-existing design, still visible in `specs/llm.md`'s function
  signatures) couldn't express that `code_review`/`review_orchestration`
  need materially more headroom than `intent_orchestration`'s or
  `code_generator`'s lighter, more routine calls — this is the same
  "independently configurable per role, not a hardcoded constant"
  principle `CLAUDE.md`'s model-tiering rules already apply to
  `source`/`model`, extended to the token budget too.
- `LLMConfig` has exactly four roles — `intent_orchestration`,
  `code_generator`, `code_review`, `review_orchestration` — matching
  `PLAN.md`'s "Model tiering" design one-to-one: `intent_orchestration`
  and `code_generator` are the two implementation-tier roles (default
  `claude-sonnet-5`), `code_review` and `review_orchestration` are the
  two review-tier gates (default `claude-opus-5`, gate #1 and gate #2
  respectively). Global to the whole tool, not per-resource-kind (see
  `specs/llm.md`'s Out of scope).
- No cross-field validation between the four roles — each can
  independently use any `ModelSource`/model combination; there's no
  invariant linking any pair of them. This is deliberate: it's exactly
  what lets a user move, say, `code_generator` to a different model
  without touching the other three as model capability and pricing
  change over time.

### `LoggingConfig`

Added alongside `specs/log.md`'s dual-handler design and
`specs/config.md`'s `resolve_logging_config()`. Same reasoning as
`LLMConfig`: produced by `config.py`, consumed by `log.py`, so the
shared shape lives here.

```python
class LoggingConfig(BaseModel):
    level: str
    max_files: int = Field(gt=0)
```

- `level` is validated against the four names `aiform/log.py`'s
  `_KeyValueFormatter` actually knows how to display —
  `{"DEBUG", "INFO", "WARNING", "ERROR"}` — via a `field_validator`, not
  left as an open string. A typo (`"INOF"`) or a level this codebase
  doesn't use (`"CRITICAL"`, stdlib's fifth level) fails fast at
  config-load time with a clear message naming the valid set, the same
  "reject at the boundary, don't let a typo silently no-op" stance
  `LLMConfig`'s unknown-field rejection already takes.
- `max_files` mirrors `LLMRoleConfig.max_tokens`'s `Field(gt=0)` —
  `0` would mean "delete every log file including the one just
  written," which isn't a meaningful "keep zero" setting so much as a
  configuration error; rejected the same way a zero/negative
  `max_tokens` is.
- No `source`-style discriminator — unlike `LLMRoleConfig`, logging has
  exactly one destination shape (a rotating file), so there's nothing
  to select between.

### `DriverReview`

The persisted record of a gate #1 (`code-review-model`) review (`PLAN.md`
§3's nested `code_review` object, §5 step 3c's `DRIVER_REVIEW_SCHEMA`).

```python
class DriverReview(BaseModel):
    approved: bool
    concerns: list[str]
    blocking_issues: list[str]
    reviewed_at: datetime
    model: str
```

- `approved` and `blocking_issues` are cross-validated: `approved=True`
  with a non-empty `blocking_issues` is rejected. This is the approval
  rule from §5 step 3d stated as a structural invariant instead of
  something every caller has to remember to check.
- `reviewed_at` / `model` are stamped by the code calling `llm.review_driver()`,
  not part of `DRIVER_REVIEW_SCHEMA`'s raw structured-output shape —
  `DriverReview` is the *persisted* record, one step downstream of the
  raw API response. `model` is whatever `.aiform/config.yaml`'s
  `llm.code_review` entry resolved to at review time (default
  `claude-opus-5`), not a hardcoded string.

### `KeyState`, `KeyCheck`

The result of `aiform init`'s credential preflight — see `specs/cli.md`'s
four-state table and `specs/llm.md`'s `verify_api_key()`.

```python
class KeyState(str, Enum):
    OK = "ok"
    MISSING = "missing"
    REJECTED = "rejected"
    UNVERIFIED = "unverified"


class KeyCheck(BaseModel):
    state: KeyState
    detail: str | None = None
```

- Four states rather than a `bool` because the preflight's whole defect
  was collapsing distinct problems into one: a credential that is absent,
  one that is present but rejected, and one that cannot be checked
  because the network is down are three different things a user does
  three different things about.
- `detail` carries the provider's **own** error text on `REJECTED`, and
  the connection error on `UNVERIFIED`. Swallowing that text is what made
  the original bug expensive to diagnose. `None` for `OK` and `MISSING` —
  neither has anything to add beyond the state.
- Deliberately provider-agnostic: the same type reports both the
  Anthropic and the DigitalOcean probe, so `cli.py` formats one shape
  rather than two. Not persisted to `state.json` — a preflight result is
  a fact about this moment, not about the infrastructure.

### `DriverInfo`

Not explicitly named in `PLAN.md` §1's repo-layout comment — that
comment lists `ResourceSpec, PlanAction, PlanEntry, StateEntry,
DriverReview` as the file's contents, but §3's state schema shows a
nested `"driver": {"path", "sha256", "generated_at", "code_review"}`
object that `DriverReview` alone doesn't cover (no `path`/`sha256`/
`generated_at`). Confirmed as a genuine gap, not a duplicate — this is
a sixth model, added to hold that nesting:

```python
class DriverInfo(BaseModel):
    path: str
    sha256: str
    generated_at: datetime
    code_review: DriverReview
```

`code_review` (not `opus_review`) — named after the role that produces
it, not a specific model, since which model actually reviewed a given
driver is configurable and recorded in `DriverReview.model` instead.

### `StateEntry`

One resource's entry in `.aiform/state.json` (`PLAN.md` §3), keyed
externally by `"<provider>.<resource_type>.<name>"`.

```python
class StateEntry(BaseModel):
    provider: str = Field(pattern=RESOURCE_OR_PROVIDER_PATTERN)
    resource_type: str = Field(pattern=RESOURCE_OR_PROVIDER_PATTERN)
    name: str = Field(min_length=1)
    id: str
    attributes: dict[str, Any]
    driver: DriverInfo
    last_applied_at: datetime
    last_refreshed_at: datetime
    aiform_md_path: str
    aiform_md_sha256: str
```

`resource_type` mirrors `ResourceSpec.resource` (§3's own note: "the same
value ... just named more precisely once it's sitting next to other
fields in state") — no shared validator between the two models; they're
populated at different points in the pipeline and asserting equality
belongs to whatever assembles a `StateEntry` from a `ResourceSpec` plus
a driver's response, not to the models themselves.

`provider`/`resource_type`/`name` carry the same constraints as
`ResourceSpec`'s equivalent fields, for the same reason: `StateEntry` is
loaded from `.aiform/state.json`, which can be hand-edited or corrupted,
and `resource_type` is what fills `<resource>` in the driver path
convention (`PLAN.md` §1) just as much as `ResourceSpec.resource` is —
the validation gap between the two was a review finding on the first
implementation pass, not a deliberate asymmetry.

## Behavior

- `ResourceSpec(**data)` accepts exactly the four frontmatter fields;
  extra keys are rejected (`model_config = ConfigDict(extra="forbid")`)
  so a typo'd frontmatter key surfaces immediately instead of being
  silently dropped.
- `ResourceSpec` rejects `resource`/`provider` values containing
  uppercase letters, spaces, `/`, or leading digits, and rejects an
  empty `name`. `StateEntry` applies the identical constraints to its
  own `provider`/`resource_type`/`name` fields.
- `PlanEntry` constructed with `action=PlanAction.CREATE,
  likely_replace=True` normalizes to `likely_replace=False`.
- `DriverReview` constructed with `approved=True, blocking_issues=["x"]`
  raises a validation error.
- `StateEntry`/`DriverInfo`/`DriverReview` round-trip through
  `model_dump(mode="json")` → re-parse without loss, matching the
  literal JSON shape in `PLAN.md` §3 field-for-field (this is what
  `state.py`'s load/save will depend on).
- `PlanReview(safe_to_proceed=False, flags=[{"resource_key": ...,
  "concern": ..., "severity": "block"}])` parses `flags` into real
  `PlanReviewFlag` objects with `severity` as a `PlanReviewSeverity`
  member, not a raw string.
- `LLMConfig(intent_orchestration={"source": "anthropic", "model":
  "claude-sonnet-5", "max_tokens": 4096}, code_generator={"source":
  "anthropic", "model": "claude-sonnet-5", "max_tokens": 4096},
  code_review={"source": "anthropic", "model": "claude-opus-5",
  "max_tokens": 8192}, review_orchestration={"source": "anthropic",
  "model": "claude-opus-5", "max_tokens": 8192})` parses all four roles
  into `LLMRoleConfig` objects with `source` as a `ModelSource` member,
  not a raw string.
- `LLMRoleConfig(source="bedrock", model="...", max_tokens=4096)` raises
  a validation error today — `ModelSource` has exactly one member.
- `LLMRoleConfig(source="anthropic", model="...")` (omitting
  `max_tokens`) raises a validation error — required, same as `source`
  and `model`.
- `LoggingConfig(level="INFO", max_files=10)` constructs cleanly;
  `LoggingConfig(level="TRACE", max_files=10)` and
  `LoggingConfig(level="INFO", max_files=0)` both raise a validation
  error.

## Edge cases / errors

- All validation failures are plain Pydantic `ValidationError` — no
  custom exception types belong in this file. `exceptions.py`'s types
  (`PlanBlockedError` etc., `PLAN.md` §1) are for orchestration-level
  failures, not schema validation.
- `params` is never validated here against any resource-specific shape,
  even once a driver exists — that check happens later, against the
  driver's own `PARAM_SCHEMA` (§4), which `models.py` has no reason to
  import.

## Out of scope

- Loading/saving `.aiform/state.json`, the `aiform_state_version` +
  `resources: dict[str, StateEntry]` top-level container, and the
  backup-on-write behavior — all `state.py` (§1's own split: "state.json
  load/save, Pydantic models, backup-on-write" is one file, but the
  *shared* models referenced from elsewhere live here; the top-level
  file-shape wrapper is local to `state.py`'s own load/save logic).
- `exceptions.py`'s exception types.
- `DesiredResourceSpec` as a distinct type — confirmed a naming slip,
  not two types. `PLAN.md` §1's `parser.py` comment has been corrected
  to `ResourceSpec` to match.

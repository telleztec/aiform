# specs/models.md — `aiform/models.py`

## Purpose

Pydantic v2 models for the data shapes that cross module boundaries in
aiform: the parsed `.aiform.md` frontmatter, one row of a printed plan,
one resource's entry in `.aiform/state.json`, and the records of the
two Opus review gates. Also the shared shape for *which* model backs each of `aiform/llm.py`'s
two roles, resolved from configuration rather than hardcoded (see
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
  detected"`), not Sonnet output — `PlanEntry` doesn't distinguish the two
  sources, and doesn't need to.
- `likely_replace` — only meaningful when `action == PlanAction.UPDATE`.
  A model validator forces it back to `False` for every other action, so
  a caller can't construct an inconsistent `PlanEntry` (e.g. a `destroy`
  flagged `likely_replace: true`, which isn't a meaningful state per
  §5 step 6's description).

### `PlanReviewSeverity`, `PlanReviewFlag`, `PlanReview`

Added alongside `specs/llm.md`'s `opus_review_plan()` — the Opus gate
#2 verdict shape (`PLAN.md` §5 apply step 2's `PLAN_REVIEW_SCHEMA`).
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


class LLMConfig(BaseModel):
    implementation: LLMRoleConfig
    review: LLMRoleConfig
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
- `LLMConfig` has exactly two roles, `implementation` and `review`,
  matching `PLAN.md`'s model-tiering design — global to the whole tool,
  not per-resource-kind (see `specs/llm.md`'s Out of scope).
- No cross-field validation between `implementation`/`review` — either
  role can independently use any `ModelSource`/model combination; there's
  no invariant linking the two.

### `DriverReview`

The persisted record of an Opus gate #1 review (`PLAN.md` §3's nested
`opus_review` object, §5 step 3c's `DRIVER_REVIEW_SCHEMA`).

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
- `reviewed_at` / `model` are stamped by the code calling Opus (`llm.py`),
  not part of `DRIVER_REVIEW_SCHEMA`'s raw structured-output shape —
  `DriverReview` is the *persisted* record, one step downstream of the
  raw API response.

### `DriverInfo`

Not explicitly named in `PLAN.md` §1's repo-layout comment — that
comment lists `ResourceSpec, PlanAction, PlanEntry, StateEntry,
DriverReview` as the file's contents, but §3's state schema shows a
nested `"driver": {"path", "sha256", "generated_at", "opus_review"}`
object that `DriverReview` alone doesn't cover (no `path`/`sha256`/
`generated_at`). Confirmed as a genuine gap, not a duplicate — this is
a sixth model, added to hold that nesting:

```python
class DriverInfo(BaseModel):
    path: str
    sha256: str
    generated_at: datetime
    opus_review: DriverReview
```

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
- `LLMConfig(implementation={"source": "anthropic", "model":
  "claude-sonnet-5"}, review={"source": "anthropic", "model":
  "claude-opus-5"})` parses both roles into `LLMRoleConfig` objects with
  `source` as a `ModelSource` member, not a raw string.
- `LLMRoleConfig(source="bedrock", model="...")` raises a validation
  error today — `ModelSource` has exactly one member.

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

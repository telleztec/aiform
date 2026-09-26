# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import re
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RESOURCE_OR_PROVIDER_PATTERN = r"^[a-z][a-z0-9_]*$"
_RESOURCE_OR_PROVIDER_RE = re.compile(RESOURCE_OR_PROVIDER_PATTERN)


def parse_dependency_key(key: str) -> tuple[str, str, str]:
    """Split a fully-qualified `provider.resource_type.name` key.

    `split(".", 2)` rather than a plain split: `provider` and
    `resource_type` cannot contain dots (RESOURCE_OR_PROVIDER_PATTERN
    forbids it), but `name` legally can -- `digitalocean.domain.example.com`
    is a real key, and a naive split(".") would corrupt it.
    """
    parts = key.split(".", 2)
    if len(parts) != 3:
        raise ValueError(
            f"malformed dependency key {key!r}: expected 'provider.resource_type.name'"
        )
    provider, resource_type, name = parts
    if not _RESOURCE_OR_PROVIDER_RE.match(provider):
        raise ValueError(f"malformed dependency key {key!r}: invalid provider {provider!r}")
    if not _RESOURCE_OR_PROVIDER_RE.match(resource_type):
        raise ValueError(
            f"malformed dependency key {key!r}: invalid resource_type {resource_type!r}"
        )
    if not name:
        raise ValueError(f"malformed dependency key {key!r}: name must not be empty")
    return provider, resource_type, name


class ResourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource: str = Field(pattern=RESOURCE_OR_PROVIDER_PATTERN)
    name: str = Field(min_length=1)
    provider: str = Field(pattern=RESOURCE_OR_PROVIDER_PATTERN)
    params: dict[str, Any]
    depends_on: list[str] = Field(default_factory=list)

    @field_validator("depends_on")
    @classmethod
    def _depends_on_targets_are_valid_keys(cls, value: list[str]) -> list[str]:
        for target in value:
            parse_dependency_key(target)
        return value


class PlanAction(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    DESTROY = "destroy"
    NO_OP = "no-op"


class PlanEntry(BaseModel):
    resource_key: str
    action: PlanAction
    rationale: str
    likely_replace: bool = False

    @model_validator(mode="after")
    def _normalize_likely_replace(self) -> "PlanEntry":
        if self.action != PlanAction.UPDATE:
            self.likely_replace = False
        return self


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


class KeyState(str, Enum):
    OK = "ok"
    MISSING = "missing"
    REJECTED = "rejected"
    UNVERIFIED = "unverified"


class KeyCheck(BaseModel):
    """One credential's preflight result (`aiform init`).

    Four states rather than a bool: a credential that is absent, one that
    is present but rejected, and one that cannot be checked because the
    network is down each call for a different fix. `detail` carries the
    provider's own error text -- swallowing it is what made the original
    bug expensive to diagnose."""

    state: KeyState
    detail: str | None = None


class HealthStatus(str, Enum):
    """Four states, not two. UNKNOWN is aiform failing to observe, which
    is not evidence the resource is broken -- collapsing it into FAILING
    would page somebody every time aiform's own network hiccuped."""

    OK = "ok"
    DEGRADED = "degraded"
    FAILING = "failing"
    UNKNOWN = "unknown"


class HealthReport(BaseModel):
    """One driver's answer to "is this resource working". `observations`
    is a free-form flat map of what the driver actually saw; the contract
    cannot know which fields matter for a resource kind it has never
    seen, so a driver returning an empty map is legal."""

    status: HealthStatus
    summary: str
    observations: dict[str, str] = Field(default_factory=dict)


class MetricKind(str, Enum):
    """COUNTER is only for a value the CSP itself documents as cumulative
    and monotonic while the resource is running -- aiform holds no history
    to difference against. When in doubt, GAUGE: a wrong gauge reads as
    noise, a wrong counter makes rate() produce a plausible, silently
    false number."""

    COUNTER = "counter"
    GAUGE = "gauge"


class Sample(BaseModel):
    """No `unit` field and no timestamp, both deliberately. The unit
    lives in the name per Prometheus convention (`_bytes`, `_seconds`),
    and a second source of truth is one the renderers could disagree
    about; the reading happens when the command runs, and a consumer that
    needs a timestamp has its own clock."""

    name: str
    kind: MetricKind
    value: float
    labels: dict[str, str] = Field(default_factory=dict)


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


_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})


class LoggingConfig(BaseModel):
    level: str
    max_files: int = Field(gt=0)

    @field_validator("level")
    @classmethod
    def _level_must_be_known(cls, value: str) -> str:
        if value not in _VALID_LOG_LEVELS:
            raise ValueError(f"level must be one of {sorted(_VALID_LOG_LEVELS)}, got {value!r}")
        return value


class DriverReview(BaseModel):
    approved: bool
    concerns: list[str]
    blocking_issues: list[str]
    reviewed_at: datetime
    model: str

    @model_validator(mode="after")
    def _approved_requires_no_blocking_issues(self) -> "DriverReview":
        if self.approved and self.blocking_issues:
            raise ValueError("a review cannot be approved with non-empty blocking_issues")
        return self


class DriverInfo(BaseModel):
    path: str
    sha256: str
    generated_at: datetime


class ParsedResource(BaseModel):
    spec: ResourceSpec
    intent_notes: list[dict[str, str]]
    aiform_md_sha256: str


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
    depends_on: list[str] = Field(default_factory=list)

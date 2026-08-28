# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RESOURCE_OR_PROVIDER_PATTERN = r"^[a-z][a-z0-9_]*$"


class ResourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource: str = Field(pattern=RESOURCE_OR_PROVIDER_PATTERN)
    name: str = Field(min_length=1)
    provider: str = Field(pattern=RESOURCE_OR_PROVIDER_PATTERN)
    params: dict[str, Any]


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
    code_review: DriverReview


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

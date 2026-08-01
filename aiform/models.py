from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    opus_review: DriverReview


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

# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import json
import re
from pathlib import Path

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from aiform.exceptions import DeploymentMismatchError, StateMissingDeploymentError
from aiform.models import StateEntry

DEFAULT_STATE_PATH = Path(".aiform/state.json")
DEFAULT_DEPLOYMENT = "default"

_DEPLOYMENT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")


def _deployment_name_problems(name: str) -> list[str]:
    problems = []
    if not name:
        problems.append("must not be empty")
    if len(name) > 63:
        problems.append("longer than 63 characters")
    if re.search(r"[A-Z]", name):
        problems.append("uppercase letters are not allowed")
    if re.search(r"[^A-Za-z0-9_-]", name):
        problems.append("characters other than a-z, 0-9, '-' and '_' are not allowed")
    if name[:1] in ("-", "_"):
        problems.append("must start with a-z or 0-9")
    return problems


def validate_deployment_name(name: str) -> str:
    if not _DEPLOYMENT_NAME.fullmatch(name):
        reasons = ", ".join(_deployment_name_problems(name))
        suggestion = re.sub(r"[^a-z0-9_-]", "-", name.strip().lower())
        if _DEPLOYMENT_NAME.fullmatch(suggestion):
            reasons += f"; try {suggestion!r}"
        raise ValueError(
            f"invalid deployment name {name!r}: {reasons}. Allowed: 1 to 63 characters "
            "from a-z, 0-9, '-' or '_', starting with a-z or 0-9"
        )
    return name


class State(BaseModel):
    model_config = ConfigDict(extra="forbid")

    aiform_state_version: int = 1
    deployment: str
    resources: dict[str, StateEntry] = Field(default_factory=dict)

    @field_validator("deployment")
    @classmethod
    def _deployment_is_a_safe_name(cls, name: str) -> str:
        return validate_deployment_name(name)

    @model_validator(mode="after")
    def _keys_match_entries(self) -> "State":
        for key, entry in self.resources.items():
            expected = f"{entry.provider}.{entry.resource_type}.{entry.name}"
            if key != expected:
                raise ValueError(f"state key {key!r} does not match entry address {expected!r}")
        return self


def load(path: Path = DEFAULT_STATE_PATH, *, deployment: str) -> State:
    validate_deployment_name(deployment)
    if not path.exists():
        return State(deployment=deployment)
    raw = json.loads(path.read_text(encoding="utf-8"))
    try:
        loaded = State.model_validate(raw)
    except ValidationError as exc:
        if any(e["loc"] == ("deployment",) and e["type"] == "missing" for e in exc.errors()):
            raise StateMissingDeploymentError(path.absolute()) from None
        raise
    if loaded.deployment != deployment:
        raise DeploymentMismatchError(deployment, loaded.deployment, path.absolute())
    return loaded


def save(state: State, path: Path = DEFAULT_STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup_path = path.with_name(path.name + ".backup")
        backup_path.write_bytes(path.read_bytes())
    path.write_text(json.dumps(state.model_dump(mode="json"), indent=2), encoding="utf-8")

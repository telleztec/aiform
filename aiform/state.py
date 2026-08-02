import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aiform.models import StateEntry

DEFAULT_STATE_PATH = Path(".aiform/state.json")


class State(BaseModel):
    model_config = ConfigDict(extra="forbid")

    aiform_state_version: int = 1
    resources: dict[str, StateEntry] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _keys_match_entries(self) -> "State":
        for key, entry in self.resources.items():
            expected = f"{entry.provider}.{entry.resource_type}.{entry.name}"
            if key != expected:
                raise ValueError(f"state key {key!r} does not match entry address {expected!r}")
        return self


def load(path: Path = DEFAULT_STATE_PATH) -> State:
    if not path.exists():
        return State()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return State.model_validate(raw)


def save(state: State, path: Path = DEFAULT_STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup_path = path.with_name(path.name + ".backup")
        backup_path.write_bytes(path.read_bytes())
    path.write_text(json.dumps(state.model_dump(mode="json"), indent=2), encoding="utf-8")

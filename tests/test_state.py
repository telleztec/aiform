import json
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from aiform.models import DriverInfo, DriverReview, StateEntry
from aiform.state import DEFAULT_STATE_PATH, State, load, save


def make_state_entry(**overrides) -> StateEntry:
    defaults = dict(
        provider="digitalocean",
        resource_type="compute",
        name="telleztec-app-01",
        id="123456789",
        attributes={
            "region": "sfo3",
            "size": "s-1vcpu-2gb",
            "image": "ubuntu-24-04-x64",
        },
        driver=DriverInfo(
            path="drivers/digitalocean/compute.py",
            sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b8",
            generated_at="2026-07-30T18:22:11Z",
            opus_review=DriverReview(
                approved=True,
                concerns=[],
                blocking_issues=[],
                reviewed_at="2026-07-30T18:22:40Z",
                model="claude-opus-5",
            ),
        ),
        last_applied_at="2026-07-30T18:23:05Z",
        last_refreshed_at="2026-07-31T09:10:00Z",
        aiform_md_path="examples/compute.aiform.md",
        aiform_md_sha256="5f4dcc3b5aa765d61d8327deb882cf99",
    )
    defaults.update(overrides)
    return StateEntry(**defaults)


def make_state(**entries: StateEntry) -> State:
    return State(aiform_state_version=1, resources=entries)


class TestDefaults:
    def test_default_state_path_constant(self):
        assert DEFAULT_STATE_PATH == Path(".aiform/state.json")

    def test_state_defaults_when_constructed_directly(self):
        state = State()
        assert state.aiform_state_version == 1
        assert state.resources == {}


class TestStateKeyValidation:
    def test_accepts_matching_key(self):
        entry = make_state_entry()
        state = make_state(**{"digitalocean.compute.telleztec-app-01": entry})
        assert state.resources["digitalocean.compute.telleztec-app-01"] is entry

    def test_rejects_mismatched_key(self):
        entry = make_state_entry()
        with pytest.raises(ValidationError):
            make_state(**{"digitalocean.compute.wrong-name": entry})

    def test_rejects_unrecognized_top_level_key(self):
        with pytest.raises(ValidationError):
            State(aiform_state_version=1, resources={}, resourcess={})


class TestLoad:
    def test_missing_file_returns_empty_state(self, tmp_path: Path):
        state = load(tmp_path / "nonexistent.json")
        assert state.aiform_state_version == 1
        assert state.resources == {}

    def test_round_trips_existing_valid_file(self, tmp_path: Path):
        entry = make_state_entry()
        original = make_state(**{"digitalocean.compute.telleztec-app-01": entry})
        path = tmp_path / "state.json"
        path.write_text(json.dumps(original.model_dump(mode="json")))

        loaded = load(path)
        assert loaded == original

    def test_rejects_mismatched_resource_key_in_file(self, tmp_path: Path):
        entry = make_state_entry()
        raw = {
            "aiform_state_version": 1,
            "resources": {
                "digitalocean.compute.wrong-name": entry.model_dump(mode="json"),
            },
        }
        path = tmp_path / "state.json"
        path.write_text(json.dumps(raw))

        with pytest.raises(ValidationError):
            load(path)

    def test_malformed_json_propagates(self, tmp_path: Path):
        path = tmp_path / "state.json"
        path.write_text("{not valid json")

        with pytest.raises(json.JSONDecodeError):
            load(path)

    def test_rejects_typo_d_top_level_key(self, tmp_path: Path):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"aiform_state_version": 1, "resourcess": {}}))

        with pytest.raises(ValidationError):
            load(path)


class TestSave:
    def test_save_then_load_round_trips(self, tmp_path: Path):
        entry = make_state_entry()
        state = make_state(**{"digitalocean.compute.telleztec-app-01": entry})
        path = tmp_path / "state.json"

        save(state, path)
        loaded = load(path)

        assert loaded == state

    def test_first_save_creates_no_backup(self, tmp_path: Path):
        state = make_state()
        path = tmp_path / "state.json"

        save(state, path)

        assert path.exists()
        assert not (tmp_path / "state.json.backup").exists()

    def test_second_save_backs_up_previous_content(self, tmp_path: Path):
        path = tmp_path / "state.json"
        backup_path = tmp_path / "state.json.backup"

        first_state = make_state(**{"digitalocean.compute.telleztec-app-01": make_state_entry()})
        save(first_state, path)
        first_content = path.read_text()

        second_entry = make_state_entry(id="987654321")
        second_state = make_state(**{"digitalocean.compute.telleztec-app-01": second_entry})
        save(second_state, path)

        assert backup_path.exists()
        assert backup_path.read_text() == first_content
        assert load(path) == second_state
        assert load(backup_path) == first_state

    def test_creates_parent_directory(self, tmp_path: Path):
        path = tmp_path / "nested" / "dir" / "state.json"
        state = make_state()

        save(state, path)

        assert path.exists()

    def test_writes_pretty_printed_json(self, tmp_path: Path):
        path = tmp_path / "state.json"
        save(make_state(**{"digitalocean.compute.telleztec-app-01": make_state_entry()}), path)

        raw = path.read_text()
        assert "\n" in raw
        assert '"resources": {' in raw

    def test_backup_round_trips_non_ascii_content(self, tmp_path: Path):
        path = tmp_path / "state.json"
        entry = make_state_entry(attributes={"tags": ["café", "生産"]})
        first_state = make_state(**{"digitalocean.compute.telleztec-app-01": entry})
        save(first_state, path)

        second_state = make_state()
        save(second_state, path)

        backup_path = tmp_path / "state.json.backup"
        assert load(backup_path) == first_state

# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from aiform.exceptions import DeploymentMismatchError, StateMissingDeploymentError
from aiform.models import DriverInfo, StateEntry
from aiform.state import (
    DEFAULT_DEPLOYMENT,
    DEFAULT_STATE_PATH,
    State,
    load,
    save,
    validate_deployment_name,
)

DEPLOYMENT = "default"


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
        ),
        last_applied_at="2026-07-30T18:23:05Z",
        last_refreshed_at="2026-07-31T09:10:00Z",
        aiform_md_path="examples/compute.aiform.md",
        aiform_md_sha256="5f4dcc3b5aa765d61d8327deb882cf99",
    )
    defaults.update(overrides)
    return StateEntry(**defaults)


def make_state(**entries: StateEntry) -> State:
    return State(aiform_state_version=1, deployment=DEPLOYMENT, resources=entries)


class TestDefaults:
    def test_default_state_path_constant(self):
        assert DEFAULT_STATE_PATH == Path(".aiform/state.json")

    def test_default_deployment_constant(self):
        assert DEFAULT_DEPLOYMENT == "default"

    def test_state_defaults_when_constructed_directly(self):
        state = State(deployment=DEPLOYMENT)
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
            State(aiform_state_version=1, deployment=DEPLOYMENT, resources={}, resourcess={})


class TestLoad:
    def test_missing_file_returns_empty_state(self, tmp_path: Path):
        state = load(tmp_path / "nonexistent.json", deployment=DEPLOYMENT)
        assert state.aiform_state_version == 1
        assert state.resources == {}

    def test_round_trips_existing_valid_file(self, tmp_path: Path):
        entry = make_state_entry()
        original = make_state(**{"digitalocean.compute.telleztec-app-01": entry})
        path = tmp_path / "state.json"
        path.write_text(json.dumps(original.model_dump(mode="json")))

        loaded = load(path, deployment=DEPLOYMENT)
        assert loaded == original

    def test_rejects_mismatched_resource_key_in_file(self, tmp_path: Path):
        entry = make_state_entry()
        raw = {
            "aiform_state_version": 1,
            "deployment": DEPLOYMENT,
            "resources": {
                "digitalocean.compute.wrong-name": entry.model_dump(mode="json"),
            },
        }
        path = tmp_path / "state.json"
        path.write_text(json.dumps(raw))

        with pytest.raises(ValidationError):
            load(path, deployment=DEPLOYMENT)

    def test_malformed_json_propagates(self, tmp_path: Path):
        path = tmp_path / "state.json"
        path.write_text("{not valid json")

        with pytest.raises(json.JSONDecodeError):
            load(path, deployment=DEPLOYMENT)

    def test_rejects_typo_d_top_level_key(self, tmp_path: Path):
        path = tmp_path / "state.json"
        path.write_text(
            json.dumps({"aiform_state_version": 1, "deployment": DEPLOYMENT, "resourcess": {}})
        )

        with pytest.raises(ValidationError):
            load(path, deployment=DEPLOYMENT)

    def test_state_json_written_without_depends_on_still_loads(self, tmp_path: Path):
        entry = make_state_entry()
        raw_entry = entry.model_dump(mode="json")
        del raw_entry["depends_on"]
        raw = {
            "aiform_state_version": 1,
            "deployment": DEPLOYMENT,
            "resources": {"digitalocean.compute.telleztec-app-01": raw_entry},
        }
        path = tmp_path / "state.json"
        path.write_text(json.dumps(raw))

        loaded = load(path, deployment=DEPLOYMENT)

        assert loaded.resources["digitalocean.compute.telleztec-app-01"].depends_on == []


class TestValidateDeploymentName:
    @pytest.mark.parametrize("name", ["a", "prod", "prod-1", "my_dep", "0abc", "x" * 63])
    def test_returns_a_valid_name_unchanged(self, name: str):
        assert validate_deployment_name(name) == name

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "Prod",
            "a/b",
            "a\\b",
            ".hidden",
            "-lead",
            "_lead",
            "a b",
            "a\nb",
            "a.b",
            "x" * 64,
            "../etc",
            "café",
        ],
    )
    def test_rejects_a_name_that_is_not_a_safe_directory_segment(self, name: str):
        with pytest.raises(ValueError):
            validate_deployment_name(name)

    @pytest.mark.parametrize(
        ("name", "reason"),
        [
            ("", "must not be empty"),
            ("a" * 64, "longer than 63 characters"),
            ("Prod", "uppercase letters are not allowed; try 'prod'"),
            ("my app", "characters other than a-z, 0-9, '-' and '_' are not allowed; try 'my-app'"),
            ("a.b", "characters other than a-z, 0-9, '-' and '_' are not allowed; try 'a-b'"),
            (
                "caf\u00e9",
                "characters other than a-z, 0-9, '-' and '_' are not allowed; try 'caf-'",
            ),
            ("prod ", "characters other than a-z, 0-9, '-' and '_' are not allowed; try 'prod'"),
            ("prod\n", "characters other than a-z, 0-9, '-' and '_' are not allowed; try 'prod'"),
            (" prod", "characters other than a-z, 0-9, '-' and '_' are not allowed; try 'prod'"),
            ("   ", "characters other than a-z, 0-9, '-' and '_' are not allowed"),
            ("-prod", "must start with a-z or 0-9"),
            ("_prod", "must start with a-z or 0-9"),
            (
                "My App",
                "uppercase letters are not allowed, characters other than a-z, 0-9, '-' and '_' "
                "are not allowed; try 'my-app'",
            ),
            ("-Prod", "uppercase letters are not allowed, must start with a-z or 0-9"),
            ("A" * 64, "longer than 63 characters, uppercase letters are not allowed"),
        ],
    )
    def test_the_message_says_what_is_wrong_and_suggests_only_a_valid_name(self, name, reason):
        with pytest.raises(ValueError) as caught:
            validate_deployment_name(name)

        assert str(caught.value) == (
            f"invalid deployment name {name!r}: {reason}. Allowed: 1 to 63 characters "
            "from a-z, 0-9, '-' or '_', starting with a-z or 0-9"
        )

    @pytest.mark.parametrize("name", ["", "a" * 64, "A" * 64, "-Prod", "_x", "-", "   ", "\n"])
    def test_no_name_is_suggested_when_the_corrected_one_would_still_be_invalid(self, name):
        with pytest.raises(ValueError) as caught:
            validate_deployment_name(name)

        assert "try " not in str(caught.value)


class TestDeploymentField:
    def test_is_required(self):
        with pytest.raises(ValidationError):
            State()

    @pytest.mark.parametrize("name", ["", "Prod", "a/b", ".x", "-x", "a b", "x" * 64])
    def test_rejects_an_invalid_name(self, name: str):
        with pytest.raises(ValidationError):
            State(deployment=name)

    def test_file_without_the_key_does_not_load(self, tmp_path: Path):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"aiform_state_version": 1, "resources": {}}))

        with pytest.raises(StateMissingDeploymentError):
            load(path, deployment=DEPLOYMENT)

    def test_file_with_an_invalid_name_is_a_schema_error_not_a_mismatch(self, tmp_path: Path):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"deployment": "Bad Name", "resources": {}}))

        with pytest.raises(ValidationError):
            load(path, deployment=DEPLOYMENT)

    def test_round_trips_through_save_and_load(self, tmp_path: Path):
        path = tmp_path / "state.json"
        save(State(deployment="prod"), path)

        assert json.loads(path.read_text())["deployment"] == "prod"
        assert load(path, deployment="prod").deployment == "prod"


class TestLoadChecksTheDeployment:
    def test_deployment_is_required(self, tmp_path: Path):
        with pytest.raises(TypeError):
            load(tmp_path / "state.json")

    def test_missing_file_is_named_for_the_request(self, tmp_path: Path):
        path = tmp_path / "state.json"

        loaded = load(path, deployment="scratch")

        assert loaded == State(deployment="scratch")
        assert not path.exists()

    def test_same_name_returns_the_file(self, tmp_path: Path):
        path = tmp_path / "state.json"
        original = State(deployment="prod")
        save(original, path)

        assert load(path, deployment="prod") == original

    def test_different_name_raises_with_both_names_and_the_absolute_path(self, tmp_path: Path):
        path = tmp_path / "state.json"
        save(State(deployment="prod"), path)

        with pytest.raises(DeploymentMismatchError) as caught:
            load(path, deployment="scratch")

        assert caught.value.requested == "scratch"
        assert caught.value.found == "prod"
        assert caught.value.path == path.absolute()
        assert caught.value.path.is_absolute()

    def test_relative_path_is_reported_absolute(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        save(State(deployment="prod"), Path("state.json"))

        with pytest.raises(DeploymentMismatchError) as caught:
            load(Path("state.json"), deployment="scratch")

        assert caught.value.path == tmp_path / "state.json"

    def test_mismatch_leaves_the_file_and_its_directory_untouched(self, tmp_path: Path):
        path = tmp_path / "state.json"
        save(State(deployment="prod"), path)
        before = path.read_bytes()

        with pytest.raises(DeploymentMismatchError):
            load(path, deployment="scratch")

        assert path.read_bytes() == before
        assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]

    def test_an_invalid_requested_name_is_a_value_error(self, tmp_path: Path):
        with pytest.raises(ValueError):
            load(tmp_path / "state.json", deployment="Bad Name")


class TestSave:
    def test_save_then_load_round_trips(self, tmp_path: Path):
        entry = make_state_entry()
        state = make_state(**{"digitalocean.compute.telleztec-app-01": entry})
        path = tmp_path / "state.json"

        save(state, path)
        loaded = load(path, deployment=DEPLOYMENT)

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
        assert load(path, deployment=DEPLOYMENT) == second_state
        assert load(backup_path, deployment=DEPLOYMENT) == first_state

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

    def test_int_attribute_survives_round_trip_uncoerced(self, tmp_path: Path):
        # #216: provider_id is native-typed, not aiform's string id, and
        # must not silently become a string through the JSON round-trip.
        entry = make_state_entry(attributes={"provider_id": 123456789, "region": "sfo3"})
        state = make_state(**{"digitalocean.compute.telleztec-app-01": entry})
        path = tmp_path / "state.json"

        save(state, path)
        loaded = load(path, deployment="default")

        attrs = loaded.resources["digitalocean.compute.telleztec-app-01"].attributes
        assert attrs["provider_id"] == 123456789
        assert isinstance(attrs["provider_id"], int)

    def test_backup_round_trips_non_ascii_content(self, tmp_path: Path):
        path = tmp_path / "state.json"
        entry = make_state_entry(attributes={"tags": ["café", "生産"]})
        first_state = make_state(**{"digitalocean.compute.telleztec-app-01": entry})
        save(first_state, path)

        second_state = make_state()
        save(second_state, path)

        backup_path = tmp_path / "state.json.backup"
        assert load(backup_path, deployment=DEPLOYMENT) == first_state


class TestLoadRefusesAFileWithoutADeployment:
    MARKER = "marker-resource-name"

    def write_old_state(self, path: Path) -> None:
        path.write_text(
            json.dumps(
                {
                    "aiform_state_version": 1,
                    "resources": {
                        f"digitalocean.compute.{self.MARKER}": {"note": self.MARKER},
                    },
                }
            ),
            encoding="utf-8",
        )

    def test_raises_the_dedicated_error_with_the_absolute_path(self, tmp_path: Path):
        path = tmp_path / "state.json"
        self.write_old_state(path)

        with pytest.raises(StateMissingDeploymentError) as caught:
            load(path, deployment="default")

        assert caught.value.path == path.absolute()

    def test_relative_path_is_reported_absolute(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self.write_old_state(Path("state.json"))

        with pytest.raises(StateMissingDeploymentError) as caught:
            load(Path("state.json"), deployment="default")

        assert caught.value.path == tmp_path / "state.json"

    def test_the_message_does_not_echo_the_file(self, tmp_path: Path):
        path = tmp_path / "state.json"
        self.write_old_state(path)

        with pytest.raises(StateMissingDeploymentError) as caught:
            load(path, deployment="default")

        assert self.MARKER not in str(caught.value)
        assert self.MARKER not in repr(caught.value)
        assert caught.value.__cause__ is None
        assert caught.value.__suppress_context__

    def test_leaves_the_file_untouched(self, tmp_path: Path):
        path = tmp_path / "state.json"
        self.write_old_state(path)
        before = path.read_bytes()

        with pytest.raises(StateMissingDeploymentError):
            load(path, deployment="default")

        assert path.read_bytes() == before
        assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]

    def test_the_key_added_by_hand_loads(self, tmp_path: Path):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"aiform_state_version": 1, "deployment": "default"}))

        assert load(path, deployment="default") == State(deployment="default")

    def test_any_other_validation_error_keeps_its_behaviour(self, tmp_path: Path):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"aiform_state_version": 1, "deployment": "Bad Name"}))

        with pytest.raises(ValidationError):
            load(path, deployment="default")

    def test_malformed_json_keeps_its_behaviour(self, tmp_path: Path):
        path = tmp_path / "state.json"
        path.write_text("{not json")

        with pytest.raises(json.JSONDecodeError):
            load(path, deployment="default")

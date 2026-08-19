import json
import subprocess
import sys
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aiform import cli, llm, orchestrator, state
from aiform.models import DriverInfo, DriverReview, StateEntry


class FakeTextBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class FakeResponse:
    def __init__(self, text: str):
        self.content = [FakeTextBlock(text)]
        self.stop_reason = "end_turn"
        self.usage = types.SimpleNamespace(input_tokens=0, output_tokens=0)


class FakeMessages:
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse(self._responses.pop(0))


class FakeClient:
    def __init__(self, responses: list[str]):
        self.messages = FakeMessages(responses)


FAKE_DRIVER_SOURCE = """\
from aiform.driver import DriverUpdateNotSupported, ResourceDriver
from aiform.exceptions import ResourceNotFoundError


class Driver(ResourceDriver):
    PARAM_SCHEMA = {"type": "object", "properties": {}}
    LIKELY_REPLACE_FIELDS = ["image"]

    def create(self, name, params, credentials):
        return {"id": "new-id-1", "name": name, **params}

    def read(self, id, credentials):
        if id == "MISSING":
            raise ResourceNotFoundError(f"resource {id} not found")
        return {"id": id, "region": "sfo3", "size": "s-1vcpu-2gb"}

    def update(self, id, current, desired, credentials):
        return {"id": id, **{**current, **desired}}

    def delete(self, id, credentials):
        pass
"""


def approve_response() -> str:
    return json.dumps({"approved": True, "concerns": [], "blocking_issues": []})


def categorization_response(action="create", rationale="new resource", likely_replace=False) -> str:
    return json.dumps({"action": action, "rationale": rationale, "likely_replace": likely_replace})


def plan_review_response(safe_to_proceed=True, flags=None) -> str:
    return json.dumps({"safe_to_proceed": safe_to_proceed, "flags": flags or []})


def write_driver(drivers_dir: Path, provider: str, resource_type: str) -> Path:
    provider_dir = drivers_dir / provider
    provider_dir.mkdir(parents=True, exist_ok=True)
    path = provider_dir / f"{resource_type}.py"
    path.write_text(FAKE_DRIVER_SOURCE)
    return path


def write_aiform_md(
    path: Path,
    *,
    provider: str = "digitalocean",
    resource: str = "compute",
    name: str = "telleztec-app-01",
    params: dict | None = None,
) -> None:
    if params is None:
        params = {"region": "sfo3", "size": "s-1vcpu-2gb"}
    lines = ["---", f"resource: {resource}", f"name: {name}", f"provider: {provider}", "params:"]
    lines += [f"  {key}: {json.dumps(value)}" for key, value in params.items()]
    lines.append("---")
    path.write_text("\n".join(lines) + "\n")


def make_driver_info(sha256: str, *, path: str = "drivers/digitalocean/compute.py") -> DriverInfo:
    return DriverInfo(
        path=path,
        sha256=sha256,
        generated_at=datetime(2026, 7, 30, 18, 22, 11, tzinfo=UTC),
        code_review=DriverReview(
            approved=True,
            concerns=[],
            blocking_issues=[],
            reviewed_at=datetime(2026, 7, 30, 18, 22, 40, tzinfo=UTC),
            model="claude-opus-5",
        ),
    )


@pytest.fixture
def prompts_dir(tmp_path: Path, monkeypatch) -> Path:
    directory = tmp_path / "prompts"
    directory.mkdir()
    (directory / "diff_plan.md").write_text("Categorize the diff into a plan action.\n")
    (directory / "review_driver.md").write_text("Review the driver source for correctness.\n")
    (directory / "review_plan.md").write_text("Review the plan for safety.\n")
    monkeypatch.setattr(llm, "PROMPTS_DIR", directory)
    return directory


@pytest.fixture
def drivers_dir(tmp_path: Path, monkeypatch) -> Path:
    directory = tmp_path / "drivers"
    directory.mkdir()
    monkeypatch.setattr(orchestrator, "DRIVERS_DIR", directory)
    return directory


@pytest.fixture
def project_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


class FakeStdinTTY:
    def isatty(self) -> bool:
        return True


class FakeStdinNotTTY:
    def isatty(self) -> bool:
        return False


def patch_client(monkeypatch, responses: list[str]) -> None:
    monkeypatch.setattr(cli.anthropic, "Anthropic", lambda: FakeClient(responses))


def fail_if_anthropic_constructed(monkeypatch) -> None:
    def _boom():
        raise AssertionError("should not construct a real anthropic.Anthropic() client")

    monkeypatch.setattr(cli.anthropic, "Anthropic", _boom)


class TestArgParsing:
    def test_no_command_exits_nonzero(self):
        with pytest.raises(SystemExit):
            cli.main([])

    def test_unknown_command_exits_nonzero(self):
        with pytest.raises(SystemExit):
            cli.main(["bogus"])


class TestInit:
    def test_creates_aiform_dir(self, project_dir: Path):
        code = cli.main(["init"])
        assert code == 0
        assert (project_dir / ".aiform").is_dir()

    def test_never_writes_credentials_env(self, project_dir: Path):
        cli.main(["init"])
        assert not (project_dir / ".aiform" / "credentials.env").exists()

    def test_appends_gitignore_entries_once(self, project_dir: Path):
        cli.main(["init"])
        cli.main(["init"])
        lines = (project_dir / ".gitignore").read_text().splitlines()
        assert lines.count(".aiform/credentials.env") == 1
        assert lines.count(".aiform/state.json") == 1
        assert lines.count(".aiform/state.json.backup") == 1
        assert "__pycache__/" in lines

    def test_does_not_gitignore_trash(self, project_dir: Path):
        cli.main(["init"])
        content = (project_dir / ".gitignore").read_text()
        assert "trash" not in content

    def test_creates_example_starter_file(self, project_dir: Path):
        cli.main(["init"])
        example = project_dir / "examples" / "compute.aiform.md"
        assert example.exists()
        assert "resource: compute" in example.read_text()
        assert "provider: digitalocean" in example.read_text()

    def test_does_not_overwrite_existing_example_file(self, project_dir: Path):
        cli.main(["init"])
        example = project_dir / "examples" / "compute.aiform.md"
        example.write_text("custom content")
        cli.main(["init"])
        assert example.read_text() == "custom content"

    def test_unsupported_provider_exits_2(self, project_dir: Path, capsys):
        code = cli.main(["init", "--provider", "aws"])
        assert code == 2
        err = capsys.readouterr().err
        assert "Error:" in err
        assert "aws" in err

    def test_never_fails_on_missing_credentials(self, project_dir: Path, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        code = cli.main(["init"])
        assert code == 0

    def test_prints_credential_check_marks(self, project_dir: Path, monkeypatch, capsys):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        cli.main(["init"])
        out = capsys.readouterr().out
        assert "✗" in out
        assert "ANTHROPIC_API_KEY" in out
        assert "DIGITALOCEAN_TOKEN" in out

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        cli.main(["init"])
        out = capsys.readouterr().out
        assert "✓" in out


class TestPlanCreate:
    def test_new_resource_prints_create_plan(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        write_aiform_md(project_dir / "app.aiform.md")
        patch_client(monkeypatch, [approve_response(), categorization_response(action="create")])

        code = cli.main(["plan", "create", "--state-file", str(project_dir / ".aiform/state.json")])

        out = capsys.readouterr().out
        assert code == 0
        assert "digitalocean.compute.telleztec-app-01" in out
        assert "create" in out

    def test_json_output_is_parseable(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        write_aiform_md(project_dir / "app.aiform.md")
        patch_client(monkeypatch, [approve_response(), categorization_response(action="create")])
        state_file = project_dir / ".aiform" / "state.json"

        code = cli.main(["plan", "create", "--state-file", str(state_file), "--json"])

        out = capsys.readouterr().out
        assert code == 0
        payload = json.loads(out)
        assert payload["plan"][0]["resource_key"] == "digitalocean.compute.telleztec-app-01"
        assert payload["plan"][0]["action"] == "create"
        assert payload["warnings"] == []

    def test_missing_driver_exits_2_with_clean_error(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_aiform_md(project_dir / "app.aiform.md")
        state_file = project_dir / ".aiform" / "state.json"

        code = cli.main(["plan", "create", "--state-file", str(state_file)])

        err = capsys.readouterr().err
        assert code == 2
        assert "Error:" in err

    def test_explicit_missing_file_exits_2(self, project_dir, capsys):
        state_file = project_dir / ".aiform" / "state.json"
        code = cli.main(
            ["plan", "create", "does-not-exist.aiform.md", "--state-file", str(state_file)]
        )
        err = capsys.readouterr().err
        assert code == 2
        assert "Error:" in err

    def test_operational_error_is_also_logged(self, project_dir, capsys):
        # log.configure() (called by cli.main()) sets propagate=False on
        # the "aiform" logger, which keeps caplog's root-attached handler
        # from seeing these records -- asserting against the real stderr
        # stream instead is what this feature actually promises, and
        # sidesteps that plumbing detail entirely.
        state_file = project_dir / ".aiform" / "state.json"

        cli.main(["plan", "create", "does-not-exist.aiform.md", "--state-file", str(state_file)])

        err = capsys.readouterr().err
        assert "ERROR" in err
        assert "aiform.cli" in err
        assert "exception_type=FileNotFoundError" in err

    def test_second_run_on_unchanged_project_makes_zero_llm_calls(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        write_aiform_md(project_dir / "app.aiform.md")
        state_file = project_dir / ".aiform" / "state.json"

        patch_client(monkeypatch, [approve_response(), categorization_response(action="create")])
        code = cli.main(["plan", "apply", "--yes", "--state-file", str(state_file)])
        capsys.readouterr()
        assert code == 0

        fail_if_anthropic_constructed(monkeypatch)
        code = cli.main(["plan", "create", "--state-file", str(state_file), "--verbose"])
        out, err = capsys.readouterr()

        assert code == 0
        assert "no-op" in out or "no changes" in out.lower() or "0 to create" in out
        assert "[verbose] 0 Anthropic API call(s) made" in err


class TestPlanApply:
    def test_apply_with_yes_creates_resource_and_persists_state(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        write_aiform_md(project_dir / "app.aiform.md")
        state_file = project_dir / ".aiform" / "state.json"
        patch_client(monkeypatch, [approve_response(), categorization_response(action="create")])

        code = cli.main(["plan", "apply", "--yes", "--state-file", str(state_file)])

        out = capsys.readouterr().out
        assert code == 0
        assert "digitalocean.compute.telleztec-app-01" in out
        reloaded = state.load(state_file)
        assert "digitalocean.compute.telleztec-app-01" in reloaded.resources

    def test_apply_without_yes_and_no_tty_fails_cleanly(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        write_aiform_md(project_dir / "app.aiform.md")
        state_file = project_dir / ".aiform" / "state.json"
        patch_client(monkeypatch, [approve_response(), categorization_response(action="create")])
        monkeypatch.setattr(cli.sys, "stdin", FakeStdinNotTTY())

        code = cli.main(["plan", "apply", "--state-file", str(state_file)])

        err = capsys.readouterr().err
        assert code == 2
        assert "Error:" in err
        assert "TTY" in err or "tty" in err

    def test_apply_declined_confirmation_aborts_without_applying(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        write_aiform_md(project_dir / "app.aiform.md")
        state_file = project_dir / ".aiform" / "state.json"
        patch_client(monkeypatch, [approve_response(), categorization_response(action="create")])
        monkeypatch.setattr(cli.sys, "stdin", FakeStdinTTY())
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")

        code = cli.main(["plan", "apply", "--state-file", str(state_file)])

        out = capsys.readouterr().out
        assert code == 1
        assert "abort" in out.lower()
        reloaded = state.load(state_file)
        assert reloaded.resources == {}

    def test_apply_verbose_reports_call_count(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        write_aiform_md(project_dir / "app.aiform.md")
        state_file = project_dir / ".aiform" / "state.json"
        patch_client(monkeypatch, [approve_response(), categorization_response(action="create")])

        code = cli.main(["plan", "apply", "--yes", "--state-file", str(state_file), "--verbose"])

        err = capsys.readouterr().err
        assert code == 0
        assert "[verbose] 2 Anthropic API call(s) made" in err

    def test_verbose_promotes_structured_log_level_to_info(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        write_aiform_md(project_dir / "app.aiform.md")
        state_file = project_dir / ".aiform" / "state.json"
        patch_client(monkeypatch, [approve_response(), categorization_response(action="create")])

        cli.main(["plan", "apply", "--yes", "--state-file", str(state_file), "--verbose"])

        err = capsys.readouterr().err
        assert "aiform.llm" in err
        assert "role=code_review" in err

    def test_without_verbose_structured_info_lines_are_suppressed(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        write_aiform_md(project_dir / "app.aiform.md")
        state_file = project_dir / ".aiform" / "state.json"
        patch_client(monkeypatch, [approve_response(), categorization_response(action="create")])

        cli.main(["plan", "apply", "--yes", "--state-file", str(state_file)])

        err = capsys.readouterr().err
        assert "aiform.llm" not in err

    def test_apply_verbose_reports_call_count_even_when_blocked(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        # The driver review + categorization calls already happened (real,
        # billable calls) before gate #2 blocks the apply -- --verbose must
        # still report that count on the error exit path, not just on
        # success.
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = project_dir / "app.aiform.md"
        write_aiform_md(aiform_md, params={"region": "sfo3", "size": "s-2vcpu-4gb"})
        state_file = project_dir / ".aiform" / "state.json"
        driver_hash = orchestrator.hashlib.sha256(
            (drivers_dir / "digitalocean" / "compute.py").read_bytes()
        ).hexdigest()
        entry = StateEntry(
            provider="digitalocean",
            resource_type="compute",
            name="telleztec-app-01",
            id="123",
            attributes={"region": "sfo3", "size": "s-1vcpu-2gb"},
            driver=make_driver_info(driver_hash),
            last_applied_at=datetime(2026, 7, 30, 18, 23, 5, tzinfo=UTC),
            last_refreshed_at=datetime(2026, 7, 31, 9, 10, 0, tzinfo=UTC),
            aiform_md_path=str(aiform_md),
            aiform_md_sha256="stale-hash-forces-categorization",
        )
        state.save(
            state.State(resources={"digitalocean.compute.telleztec-app-01": entry}), state_file
        )
        patch_client(
            monkeypatch,
            [
                categorization_response(action="update", likely_replace=True),
                plan_review_response(
                    safe_to_proceed=False,
                    flags=[
                        {
                            "resource_key": "digitalocean.compute.telleztec-app-01",
                            "concern": "do not resize production",
                            "severity": "block",
                        }
                    ],
                ),
            ],
        )

        code = cli.main(["plan", "apply", "--yes", "--state-file", str(state_file), "--verbose"])

        err = capsys.readouterr().err
        assert code == 2
        assert "Error:" in err
        assert "[verbose] 2 Anthropic API call(s) made" in err


class TestPlanDestroy:
    def test_destroy_all_tracked_moves_file_to_trash(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = project_dir / "app.aiform.md"
        write_aiform_md(aiform_md)
        state_file = project_dir / ".aiform" / "state.json"
        driver_hash = orchestrator.hashlib.sha256(
            (drivers_dir / "digitalocean" / "compute.py").read_bytes()
        ).hexdigest()
        entry = StateEntry(
            provider="digitalocean",
            resource_type="compute",
            name="telleztec-app-01",
            id="123",
            attributes={"region": "sfo3", "size": "s-1vcpu-2gb"},
            driver=make_driver_info(driver_hash),
            last_applied_at=datetime(2026, 7, 30, 18, 23, 5, tzinfo=UTC),
            last_refreshed_at=datetime(2026, 7, 31, 9, 10, 0, tzinfo=UTC),
            aiform_md_path=str(aiform_md),
            aiform_md_sha256="abc123",
        )
        state.save(
            state.State(resources={"digitalocean.compute.telleztec-app-01": entry}), state_file
        )
        patch_client(monkeypatch, [plan_review_response()])

        code = cli.main(["plan", "destroy", "--yes", "--state-file", str(state_file)])

        out = capsys.readouterr().out
        assert code == 0
        assert "destroy" in out
        reloaded = state.load(state_file)
        assert reloaded.resources == {}
        assert not aiform_md.exists()
        trash_dir = project_dir / ".aiform" / "trash"
        assert any(trash_dir.iterdir())

    def test_destroy_blocked_by_gate2_exits_2(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = project_dir / "app.aiform.md"
        write_aiform_md(aiform_md)
        state_file = project_dir / ".aiform" / "state.json"
        driver_hash = orchestrator.hashlib.sha256(
            (drivers_dir / "digitalocean" / "compute.py").read_bytes()
        ).hexdigest()
        entry = StateEntry(
            provider="digitalocean",
            resource_type="compute",
            name="telleztec-app-01",
            id="123",
            attributes={"region": "sfo3", "size": "s-1vcpu-2gb"},
            driver=make_driver_info(driver_hash),
            last_applied_at=datetime(2026, 7, 30, 18, 23, 5, tzinfo=UTC),
            last_refreshed_at=datetime(2026, 7, 31, 9, 10, 0, tzinfo=UTC),
            aiform_md_path=str(aiform_md),
            aiform_md_sha256="abc123",
        )
        state.save(
            state.State(resources={"digitalocean.compute.telleztec-app-01": entry}), state_file
        )
        patch_client(
            monkeypatch,
            [
                plan_review_response(
                    safe_to_proceed=False,
                    flags=[
                        {
                            "resource_key": "digitalocean.compute.telleztec-app-01",
                            "concern": "do not destroy production",
                            "severity": "block",
                        }
                    ],
                )
            ],
        )

        code = cli.main(["plan", "destroy", "--yes", "--state-file", str(state_file)])

        err = capsys.readouterr().err
        assert code == 2
        assert "Error:" in err
        assert "do not destroy production" in err
        reloaded = state.load(state_file)
        assert "digitalocean.compute.telleztec-app-01" in reloaded.resources
        assert aiform_md.exists()


class TestPlanRefresh:
    def test_refresh_updates_attributes(self, project_dir, drivers_dir, monkeypatch, capsys):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        state_file = project_dir / ".aiform" / "state.json"
        driver_hash = orchestrator.hashlib.sha256(
            (drivers_dir / "digitalocean" / "compute.py").read_bytes()
        ).hexdigest()
        entry = StateEntry(
            provider="digitalocean",
            resource_type="compute",
            name="telleztec-app-01",
            id="123",
            attributes={"region": "old-region", "size": "old-size"},
            driver=make_driver_info(driver_hash),
            last_applied_at=datetime(2026, 7, 30, 18, 23, 5, tzinfo=UTC),
            last_refreshed_at=datetime(2026, 7, 31, 9, 10, 0, tzinfo=UTC),
            aiform_md_path="app.aiform.md",
            aiform_md_sha256="abc123",
        )
        state.save(
            state.State(resources={"digitalocean.compute.telleztec-app-01": entry}), state_file
        )

        code = cli.main(["plan", "refresh", "--state-file", str(state_file)])

        out = capsys.readouterr().out
        assert code == 0
        assert "digitalocean.compute.telleztec-app-01" in out
        reloaded = state.load(state_file)
        assert reloaded.resources["digitalocean.compute.telleztec-app-01"].attributes == {
            "region": "sfo3",
            "size": "s-1vcpu-2gb",
        }


class TestPlanShow:
    def test_show_empty_state(self, project_dir, capsys):
        state_file = project_dir / ".aiform" / "state.json"
        code = cli.main(["plan", "show", "--state-file", str(state_file)])
        out = capsys.readouterr().out
        assert code == 0
        assert "no resources tracked" in out

    def test_show_prints_tracked_resource(self, project_dir, capsys):
        state_file = project_dir / ".aiform" / "state.json"
        entry = StateEntry(
            provider="digitalocean",
            resource_type="compute",
            name="telleztec-app-01",
            id="123456789",
            attributes={"region": "sfo3"},
            driver=make_driver_info(
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b8"
            ),
            last_applied_at=datetime(2026, 7, 30, 18, 23, 5, tzinfo=UTC),
            last_refreshed_at=datetime(2026, 7, 31, 9, 10, 0, tzinfo=UTC),
            aiform_md_path="examples/compute.aiform.md",
            aiform_md_sha256="abc123",
        )
        state.save(
            state.State(resources={"digitalocean.compute.telleztec-app-01": entry}), state_file
        )

        code = cli.main(["plan", "show", "--state-file", str(state_file)])

        out = capsys.readouterr().out
        assert code == 0
        assert "digitalocean.compute.telleztec-app-01" in out
        assert "123456789" in out

    def test_show_corrupt_state_file_exits_2_with_clean_error(self, project_dir, capsys):
        state_file = project_dir / ".aiform" / "state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text("not valid json{{{")

        code = cli.main(["plan", "show", "--state-file", str(state_file)])

        err = capsys.readouterr().err
        assert code == 2
        assert "Error:" in err


class TestMainModuleEntryPoint:
    def test_python_dash_m_aiform_invokes_cli(self, tmp_path: Path):
        result = subprocess.run(
            [sys.executable, "-m", "aiform", "plan", "show"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "no resources tracked" in result.stdout

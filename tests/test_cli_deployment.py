# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import argparse
import json
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aiform import cli, config, llm, observability, orchestrator, state
from aiform.driver import ResourceDriver
from aiform.exceptions import DeploymentMismatchError
from aiform.models import DriverInfo, KeyCheck, KeyState, StateEntry

SOURCE = """\
---
resource: compute
name: web-01
provider: digitalocean
params:
  region: sfo3
  size: s-1vcpu-2gb
---
"""

KEY = "digitalocean.compute.web-01"


def make_entry() -> StateEntry:
    return StateEntry(
        provider="digitalocean",
        resource_type="compute",
        name="web-01",
        id="123456789",
        attributes={"region": "sfo3", "size": "s-1vcpu-2gb"},
        driver=DriverInfo(
            path="drivers/digitalocean/compute.py",
            sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b8",
            generated_at=datetime(2026, 9, 10, 14, 2, 11, tzinfo=UTC),
        ),
        last_applied_at=datetime(2026, 9, 10, 14, 2, 11, tzinfo=UTC),
        last_refreshed_at=datetime(2026, 9, 10, 14, 2, 11, tzinfo=UTC),
        aiform_md_path="web.aiform.md",
        aiform_md_sha256="abc123",
    )


class SpyDriver(ResourceDriver):
    PARAM_SCHEMA = {"type": "object", "properties": {}}

    def create(self, name, params, credentials):
        raise AssertionError("create() reached")

    def update(self, id, current, desired, credentials):
        raise AssertionError("update() reached")

    def delete(self, id, credentials):
        raise AssertionError("delete() reached")

    def read(self, id, credentials):
        return {"id": id, "region": "sfo3", "size": "s-1vcpu-2gb"}


class Reach:
    """Everything that would mean a command got past the deployment check."""

    def __init__(self):
        self.driver_loads: list[tuple[str, str]] = []
        self.credential_lookups: list[str] = []
        self.anthropic_constructions = 0
        self.socket_attempts: list = []

    def total(self) -> int:
        return (
            len(self.driver_loads)
            + len(self.credential_lookups)
            + self.anthropic_constructions
            + len(self.socket_attempts)
        )


@pytest.fixture(autouse=True)
def offline_preflight(monkeypatch):
    monkeypatch.setattr(llm, "verify_api_key", lambda **kwargs: KeyCheck(state=KeyState.MISSING))
    monkeypatch.setattr(
        cli, "_check_provider_token", lambda provider: KeyCheck(state=KeyState.MISSING)
    )


@pytest.fixture
def reach(monkeypatch) -> Reach:
    spy = Reach()

    def load_driver(provider, resource_type):
        spy.driver_loads.append((provider, resource_type))
        return SpyDriver()

    def resolve_credentials(provider):
        spy.credential_lookups.append(provider)
        return {"DIGITALOCEAN_TOKEN": "tok"}

    def anthropic_client(**kwargs):
        spy.anthropic_constructions += 1
        raise AssertionError("an Anthropic client must not be constructed")

    def refuse_socket(target, *args, **kwargs):
        spy.socket_attempts.append(target)
        raise OSError("network disabled for this test")

    monkeypatch.setattr(orchestrator, "load_driver", load_driver)
    monkeypatch.setattr(config, "resolve_credentials", resolve_credentials)
    monkeypatch.setattr(cli.anthropic, "Anthropic", anthropic_client)
    monkeypatch.setattr(socket, "create_connection", refuse_socket)
    monkeypatch.setattr(socket.socket, "connect", refuse_socket)
    monkeypatch.setattr(socket, "getaddrinfo", refuse_socket)
    return spy


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "web.aiform.md").write_text(SOURCE, encoding="utf-8")
    return tmp_path


@pytest.fixture
def prod_state(project) -> Path:
    path = project / ".aiform" / "state.json"
    state.save(state.State(deployment="prod", resources={KEY: make_entry()}), path)
    return path


PLAN_AND_RESOURCE_COMMANDS = [
    ["plan", "create"],
    ["plan", "apply", "--yes"],
    ["plan", "destroy", "--yes"],
    ["plan", "destroy"],
    ["plan", "refresh"],
    ["plan", "show"],
    ["resource", "check"],
    ["resource", "metrics"],
    ["resource", "status"],
    ["resource", "status", "web-01"],
]


class TestMismatchIsRefusedByEveryStateReadingCommand:
    @pytest.mark.parametrize("command", PLAN_AND_RESOURCE_COMMANDS, ids=" ".join)
    @pytest.mark.parametrize(
        "flag", [[], ["--deployment", "scratch"]], ids=["no-flag", "explicit-flag"]
    )
    def test_refuses_with_exit_2_and_names_both_deployments(
        self, prod_state, reach, capsys, command, flag
    ):
        requested = "scratch" if flag else "default"

        code = cli.main([*command, *flag])

        assert code == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "\nError: this state file belongs to deployment 'prod'" in "\n" + captured.err
        assert f"not '{requested}'" in captured.err
        assert str(prod_state.absolute()) in captured.err
        assert "Nothing was read from the provider and nothing was changed." in captured.err

    @pytest.mark.parametrize("command", PLAN_AND_RESOURCE_COMMANDS, ids=" ".join)
    def test_touches_nothing_before_refusing(self, prod_state, reach, command):
        before = prod_state.read_bytes()

        cli.main([*command, "--deployment", "scratch"])

        assert prod_state.read_bytes() == before
        assert not prod_state.with_name("state.json.backup").exists()
        assert reach.total() == 0, vars(reach)

    def test_state_file_flag_is_independent_of_the_deployment_flag(self, prod_state, reach, capsys):
        elsewhere = prod_state.parent / "other.json"
        state.save(state.State(deployment="scratch"), elsewhere)

        assert cli.main(["plan", "show", "--state-file", str(elsewhere)]) == 2
        assert "belongs to deployment 'scratch', not 'default'" in capsys.readouterr().err
        assert (
            cli.main(["plan", "show", "--state-file", str(elsewhere), "--deployment", "scratch"])
            == 0
        )


class TestMatchingNameProceeds:
    def test_plan_show_with_the_files_own_name(self, prod_state, reach, capsys):
        code = cli.main(["plan", "show", "--deployment", "prod"])

        assert code == 0
        assert KEY in capsys.readouterr().out

    def test_plan_refresh_with_the_files_own_name_reaches_the_driver(self, prod_state, reach):
        code = cli.main(["plan", "refresh", "--deployment", "prod"])

        assert code == 0
        assert reach.driver_loads == [("digitalocean", "compute")]

    def test_no_flag_means_default(self, project, reach):
        state.save(state.State(deployment="default"), project / ".aiform" / "state.json")

        assert cli.main(["plan", "show"]) == 0

    def test_missing_state_file_adopts_the_requested_name(self, project, reach, capsys):
        code = cli.main(["plan", "show", "--deployment", "scratch"])

        assert code == 0
        assert not (project / ".aiform" / "state.json").exists()


class TestFunctionsRefuseWithoutTheCli:
    """The check lives in state.load(), so a caller that skips cli.py is refused too."""

    def test_orchestrator_functions(self, prod_state, reach):
        with pytest.raises(DeploymentMismatchError):
            orchestrator.refresh_state(state_path=prod_state, deployment="scratch")
        with pytest.raises(DeploymentMismatchError):
            orchestrator.build_create_plan(state_path=prod_state, deployment="scratch")
        with pytest.raises(DeploymentMismatchError):
            orchestrator.build_destroy_plan(state_path=prod_state, deployment="scratch")
        with pytest.raises(DeploymentMismatchError):
            orchestrator.apply_plan([], state_path=prod_state, deployment="scratch")
        assert reach.total() == 0, vars(reach)

    def test_observability_functions(self, prod_state, reach):
        with pytest.raises(DeploymentMismatchError):
            observability.collect(state_path=prod_state, deployment="scratch")
        with pytest.raises(DeploymentMismatchError):
            observability.status_reports(state_path=prod_state, deployment="scratch")
        with pytest.raises(DeploymentMismatchError):
            observability.status_for(KEY, state_path=prod_state, deployment="scratch")
        assert reach.total() == 0, vars(reach)


class TestInitNamesTheDeployment:
    def test_writes_an_empty_state_named_default(self, project, reach):
        assert cli.main(["init"]) == 0

        raw = json.loads((project / ".aiform" / "state.json").read_text())
        assert raw["deployment"] == "default"
        assert raw["resources"] == {}

    def test_writes_the_requested_name_and_prints_it(self, project, reach, capsys):
        assert cli.main(["init", "--deployment", "prod"]) == 0

        raw = json.loads((project / ".aiform" / "state.json").read_text())
        assert raw["deployment"] == "prod"
        assert "Deployment: prod" in capsys.readouterr().out

    def test_first_init_leaves_no_backup(self, project, reach):
        cli.main(["init", "--deployment", "prod"])

        assert not (project / ".aiform" / "state.json.backup").exists()

    def test_rerun_with_the_same_name_leaves_the_file_untouched(self, prod_state, reach):
        before = prod_state.read_bytes()

        assert cli.main(["init", "--deployment", "prod"]) == 0

        assert prod_state.read_bytes() == before
        assert not prod_state.with_name("state.json.backup").exists()

    @pytest.mark.parametrize("flag", [[], ["--deployment", "scratch"]], ids=["no-flag", "other"])
    def test_rerun_with_another_name_is_refused_and_scaffolds_nothing(
        self, prod_state, reach, capsys, flag
    ):
        before = prod_state.read_bytes()

        code = cli.main(["init", *flag])

        assert code == 2
        assert "belongs to deployment 'prod'" in capsys.readouterr().err
        assert prod_state.read_bytes() == before
        assert not (prod_state.parent.parent / ".gitignore").exists()
        assert not (prod_state.parent.parent / "examples").exists()
        assert not (prod_state.parent / "ssh").exists()

    def test_the_named_state_is_what_the_other_commands_then_accept(self, project, reach):
        cli.main(["init", "--deployment", "prod"])

        assert cli.main(["plan", "show", "--deployment", "prod"]) == 0
        assert cli.main(["plan", "show"]) == 2


class TestStateFromBeforeDeployments:
    MARKER = "marker-resource-name"

    @pytest.fixture
    def old_state(self, project) -> Path:
        path = project / ".aiform" / "state.json"
        path.parent.mkdir()
        path.write_text(
            json.dumps(
                {
                    "aiform_state_version": 1,
                    "resources": {f"digitalocean.compute.{self.MARKER}": {"note": self.MARKER}},
                }
            ),
            encoding="utf-8",
        )
        return path

    @pytest.mark.parametrize(
        "argv",
        [["init"], *PLAN_AND_RESOURCE_COMMANDS],
        ids=lambda argv: " ".join(argv),
    )
    def test_every_command_gives_the_short_error_and_exit_2(self, old_state, reach, capsys, argv):
        before = old_state.read_bytes()

        code = cli.main(argv)

        err = capsys.readouterr().err
        assert code == 2
        assert "Error: this state file has no 'deployment' field" in err
        assert str(old_state.absolute()) in err
        assert "delete it" in err
        assert '"deployment": "default"' in err
        assert self.MARKER not in err
        assert old_state.read_bytes() == before
        assert reach.total() == 0, vars(reach)

    def test_the_contents_reach_neither_stderr_nor_any_log(self, old_state, reach, capsys):
        cli.main(["init"])

        assert self.MARKER not in capsys.readouterr().err
        logs = [p for p in old_state.parent.parent.rglob("*") if p.is_file() and p != old_state]
        assert logs
        assert not [p for p in logs if self.MARKER in p.read_text(errors="replace")]

    def test_init_scaffolds_nothing(self, old_state, reach):
        cli.main(["init"])

        root = old_state.parent.parent
        assert not (root / ".gitignore").exists()
        assert not (root / "examples").exists()


class TestNameValidation:
    BAD = [
        "Prod",
        "a/b",
        "a\\b",
        ".hidden",
        "-lead",
        "_lead",
        "",
        "a b",
        "a.b",
        "x" * 64,
        "../etc",
        "café",
    ]
    GOOD = ["a", "prod", "prod-1", "my_dep", "0abc", "x" * 63]

    @pytest.mark.parametrize("name", BAD, ids=repr)
    @pytest.mark.parametrize("command", [["init"], ["plan", "show"], ["resource", "status"]])
    def test_a_bad_name_is_a_usage_error_before_anything_runs(
        self, project, reach, capsys, command, name
    ):
        with pytest.raises(SystemExit) as caught:
            cli.main([*command, f"--deployment={name}"])

        assert caught.value.code == 2
        assert "deployment" in capsys.readouterr().err
        assert not (project / ".aiform").exists()
        assert reach.total() == 0

    @pytest.mark.parametrize("name", GOOD, ids=repr)
    def test_a_good_name_is_accepted(self, project, reach, name):
        assert cli.main(["init", f"--deployment={name}"]) == 0
        assert cli.main(["plan", "show", f"--deployment={name}"]) == 0


@pytest.fixture
def builds(monkeypatch) -> list[tuple[str, list[Path] | None, str]]:
    """Stand in for the planners so a test sees what cli.py handed them."""
    seen: list[tuple[str, list[Path] | None, str]] = []

    def create(paths, **kwargs):
        seen.append(("create", paths, kwargs["deployment"]))
        return [], []

    def destroy(paths, **kwargs):
        seen.append(("destroy", paths, kwargs["deployment"]))
        return [], []

    monkeypatch.setattr(orchestrator, "build_create_plan", create)
    monkeypatch.setattr(orchestrator, "build_destroy_plan", destroy)
    monkeypatch.setattr(
        orchestrator,
        "apply_plan",
        lambda *args, **kwargs: orchestrator.ApplyResult(
            executed=[], review_flags=[], aborted=False
        ),
    )
    return seen


class TestAtNameShorthand:
    @pytest.mark.parametrize("verb", ["create", "apply", "destroy"])
    def test_at_name_selects_the_deployment_and_is_not_a_file(self, project, builds, verb):
        cli.main(["plan", verb, "@prod", "--yes"] if verb != "create" else ["plan", verb, "@prod"])

        assert builds == [(verb if verb != "apply" else "create", None, "prod")]

    def test_keeps_the_other_positionals_as_files(self, project, builds):
        cli.main(["plan", "create", "web.aiform.md", "@prod", "db.aiform.md"])

        assert builds == [("create", [Path("web.aiform.md"), Path("db.aiform.md")], "prod")]

    def test_a_dot_slash_path_that_starts_with_at_is_still_a_file(self, project, builds):
        cli.main(["plan", "create", "./@odd.aiform.md"])

        assert builds == [("create", [Path("@odd.aiform.md")], "default")]

    def test_no_at_name_and_no_flag_still_means_default(self, project, builds):
        cli.main(["plan", "create"])

        assert builds == [("create", None, "default")]

    def test_agrees_with_an_explicit_flag_naming_the_same_deployment(self, project, builds):
        cli.main(["plan", "create", "@prod", "--deployment", "prod"])

        assert builds == [("create", None, "prod")]

    def test_the_same_at_name_twice_is_harmless(self, project, builds):
        cli.main(["plan", "create", "@prod", "@prod"])

        assert builds == [("create", None, "prod")]

    def test_conflicts_with_an_explicit_flag_naming_another(self, project, builds, capsys):
        with pytest.raises(SystemExit) as caught:
            cli.main(["plan", "create", "@prod", "--deployment", "scratch"])

        assert caught.value.code == 2
        err = capsys.readouterr().err
        assert "@prod" in err and "--deployment scratch" in err
        assert builds == []

    def test_two_different_at_names_conflict(self, project, builds, capsys):
        with pytest.raises(SystemExit) as caught:
            cli.main(["plan", "create", "@prod", "@scratch"])

        assert caught.value.code == 2
        err = capsys.readouterr().err
        assert "@prod" in err and "@scratch" in err
        assert builds == []

    @pytest.mark.parametrize("token", ["@", "@Prod", "@a/b", "@.x", "@-x"])
    def test_a_bad_at_name_is_a_usage_error(self, project, builds, capsys, token):
        with pytest.raises(SystemExit) as caught:
            cli.main(["plan", "create", token])

        assert caught.value.code == 2
        assert "deployment" in capsys.readouterr().err
        assert builds == []

    @pytest.mark.parametrize("command", [["plan", "create"], ["plan", "destroy", "--yes"]])
    def test_an_at_name_that_differs_from_the_state_file_is_refused(
        self, prod_state, reach, capsys, command
    ):
        before = prod_state.read_bytes()

        code = cli.main([*command, "@scratch"])

        assert code == 2
        assert "belongs to deployment 'prod', not 'scratch'" in capsys.readouterr().err
        assert prod_state.read_bytes() == before
        assert reach.total() == 0, vars(reach)

    @pytest.mark.parametrize(
        "argv, usage",
        [
            (["plan", "create", "@prod", "--deployment", "scratch"], "usage: aiform plan create "),
            (["plan", "apply", "@Prod"], "usage: aiform plan apply "),
            (["plan", "destroy", "@prod", "@scratch"], "usage: aiform plan destroy "),
            (
                ["resource", "check", "@prod/web", "--deployment", "x"],
                "usage: aiform resource check ",
            ),
            (["resource", "metrics", "@Prod/web"], "usage: aiform resource metrics "),
            (["resource", "status", "@prod/"], "usage: aiform resource status "),
        ],
    )
    def test_a_usage_error_prints_the_invoked_subcommands_usage(
        self, project, builds, capsys, argv, usage
    ):
        with pytest.raises(SystemExit) as caught:
            cli.main(argv)

        assert caught.value.code == 2
        assert usage in capsys.readouterr().err

    @pytest.mark.parametrize("command", [["plan", "show"], ["plan", "refresh"], ["init"]])
    def test_commands_without_a_positional_do_not_accept_it(self, project, reach, command):
        with pytest.raises(SystemExit) as caught:
            cli.main([*command, "@prod"])

        assert caught.value.code == 2


@pytest.fixture
def resource_calls(monkeypatch) -> list[tuple[str, list[str] | None, str]]:
    """Stand in for the provider-facing reads so a test sees what cli.py handed them."""
    seen: list[tuple[str, list[str] | None, str]] = []

    def collect(*, keys, deployment, **kwargs):
        seen.append(("collect", keys, deployment))
        return observability.Collection(readings=[], elapsed_seconds=0.0)

    def status_reports(keys, *, deployment, **kwargs):
        seen.append(("status", keys, deployment))
        return []

    monkeypatch.setattr(observability, "collect", collect)
    monkeypatch.setattr(observability, "status_reports", status_reports)
    return seen


def save_state_named(project: Path, deployment: str) -> Path:
    path = project / ".aiform" / "state.json"
    state.save(state.State(deployment=deployment, resources={KEY: make_entry()}), path)
    return path


RESOURCE_VERBS = pytest.mark.parametrize(
    ("verb", "reader"), [("check", "collect"), ("metrics", "collect"), ("status", "status")]
)


class TestResourceAddressing:
    @RESOURCE_VERBS
    @pytest.mark.parametrize(
        ("argv", "deployment", "keys"),
        [
            (["web-01"], "default", [KEY]),
            (["web-01", "--deployment", "prod"], "prod", [KEY]),
            (["@prod/web-01"], "prod", [KEY]),
            (["@prod"], "prod", None),
            (["@prod/web-01", "--deployment", "prod"], "prod", [KEY]),
            (["@prod", "--deployment", "prod"], "prod", None),
        ],
        ids=["bare", "flag", "address", "deployment-only", "address-and-flag", "only-and-flag"],
    )
    def test_the_grammar(self, project, resource_calls, verb, reader, argv, deployment, keys):
        save_state_named(project, deployment)

        cli.main(["resource", verb, *argv])

        assert resource_calls == [(reader, keys, deployment)]

    @RESOURCE_VERBS
    def test_a_conflicting_flag_is_refused_before_any_state_or_provider_access(
        self, prod_state, reach, resource_calls, monkeypatch, capsys, verb, reader
    ):
        loads = []
        real_load = state.load
        monkeypatch.setattr(
            state, "load", lambda *a, **k: loads.append((a, k)) or real_load(*a, **k)
        )
        before = prod_state.read_bytes()

        with pytest.raises(SystemExit) as caught:
            cli.main(["resource", verb, "@prod/web-01", "--deployment", "scratch"])

        assert caught.value.code == 2
        err = capsys.readouterr().err
        assert "@prod/web-01" in err and "--deployment scratch" in err
        assert loads == []
        assert resource_calls == []
        assert prod_state.read_bytes() == before
        assert reach.total() == 0, vars(reach)

    @RESOURCE_VERBS
    @pytest.mark.parametrize("address", ["@/web-01", "@Prod/web-01", "@", "@a.b/web-01"])
    def test_a_bad_deployment_part_states_the_name_rule(
        self, project, resource_calls, capsys, verb, reader, address
    ):
        with pytest.raises(SystemExit) as caught:
            cli.main(["resource", verb, address])

        assert caught.value.code == 2
        err = capsys.readouterr().err
        assert "use 1 to 63 lowercase letters, digits, '-' or '_'" in err
        assert resource_calls == []

    @RESOURCE_VERBS
    @pytest.mark.parametrize("address", ["@prod/", "@prod/web-01/x", "@prod//"])
    def test_a_bad_shape_states_the_grammar(
        self, project, resource_calls, capsys, verb, reader, address
    ):
        with pytest.raises(SystemExit) as caught:
            cli.main(["resource", verb, address])

        assert caught.value.code == 2
        assert "@<deployment>/<resource>" in capsys.readouterr().err
        assert resource_calls == []

    @pytest.mark.parametrize("verb", ["check", "metrics", "status"])
    def test_an_address_naming_another_deployment_than_the_state_is_refused(
        self, prod_state, reach, capsys, verb
    ):
        before = prod_state.read_bytes()

        code = cli.main(["resource", verb, "@scratch/web-01"])

        assert code == 2
        assert "belongs to deployment 'prod', not 'scratch'" in capsys.readouterr().err
        assert prod_state.read_bytes() == before
        assert reach.total() == 0, vars(reach)

    def test_an_unrecognised_resource_part_gets_the_existing_lookup_error(
        self, prod_state, reach, capsys
    ):
        code = cli.main(["resource", "status", "@prod/nope"])

        assert code == 2
        assert "no tracked resource is named 'nope'; tracked: web-01" in capsys.readouterr().err


class TestInitWritesStateLast:
    @staticmethod
    def fail_the_key_step(monkeypatch):
        def ensure_managed_key(ssh_dir):
            raise RuntimeError("ssh-keygen failed")

        monkeypatch.setattr(cli.ssh, "ensure_managed_key", ensure_managed_key)

    def test_a_failure_in_the_ssh_key_step_leaves_no_state_file(
        self, project, reach, monkeypatch, capsys
    ):
        self.fail_the_key_step(monkeypatch)

        code = cli.main(["init", "--deployment", "prod"])

        assert code == 2
        assert "ssh-keygen failed" in capsys.readouterr().err
        assert not (project / ".aiform" / "state.json").exists()

    def test_a_rerun_under_another_name_is_then_accepted(self, project, reach, monkeypatch):
        with monkeypatch.context() as failing:
            self.fail_the_key_step(failing)
            cli.main(["init", "--deployment", "prod"])

        assert cli.main(["init", "--deployment", "scratch"]) == 0

        raw = json.loads((project / ".aiform" / "state.json").read_text())
        assert raw["deployment"] == "scratch"


def leaf_commands() -> list[list[str]]:
    paths: list[list[str]] = []

    def walk(parser: argparse.ArgumentParser, prefix: list[str]) -> None:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    walk(child, [*prefix, name])
                return
        paths.append(prefix)

    walk(cli._build_parser(), [])
    return paths


def spellings(path: list[str], value: str) -> dict[str, list[str]]:
    flag = ["--deployment", value]
    spelled = {"root": [*flag, *path], "leaf": [*path, *flag]}
    if len(path) > 1:
        spelled["group"] = [path[0], *flag, *path[1:]]
    return spelled


def resolved(argv: list[str]) -> str:
    args = cli._build_parser().parse_args(argv)
    cli._resolve_deployment(args.usage_parser, args)
    return args.deployment


class TestDeploymentFlagAtAnyPosition:
    def test_every_registered_command_is_found(self):
        assert sorted(leaf_commands()) == sorted(
            [
                ["init"],
                ["plan", "create"],
                ["plan", "apply"],
                ["plan", "destroy"],
                ["plan", "refresh"],
                ["plan", "show"],
                ["resource", "check"],
                ["resource", "metrics"],
                ["resource", "status"],
            ]
        )

    @pytest.mark.parametrize("path", leaf_commands(), ids=" ".join)
    def test_every_command_accepts_it_at_every_position(self, path):
        for position, argv in spellings(path, "prod").items():
            assert resolved(argv) == "prod", position

    @pytest.mark.parametrize("path", leaf_commands(), ids=" ".join)
    def test_every_command_defaults_without_it(self, path):
        assert resolved(path) == "default"

    @pytest.mark.parametrize("path", leaf_commands(), ids=" ".join)
    def test_the_same_value_at_every_position_is_fine(self, path):
        argv = ["--deployment", "prod"]
        argv += [path[0], *(["--deployment", "prod"] if len(path) > 1 else [])]
        argv += [*path[1:], "--deployment", "prod"]

        assert resolved(argv) == "prod"

    @pytest.mark.parametrize("position", ["root", "group", "leaf"])
    def test_plan_create_reaches_the_planner_with_it(self, project, builds, position):
        cli.main(spellings(["plan", "create"], "prod")[position])

        assert builds == [("create", None, "prod")]

    def test_the_same_value_at_all_three_positions_reaches_the_planner(self, project, builds):
        cli.main(
            ["--deployment", "prod", "plan", "--deployment", "prod", "create"]
            + ["--deployment", "prod"]
        )

        assert builds == [("create", None, "prod")]

    @RESOURCE_VERBS
    @pytest.mark.parametrize("position", ["root", "group", "leaf"])
    def test_resource_verbs_reach_the_reader_with_it(
        self, project, resource_calls, verb, reader, position
    ):
        save_state_named(project, "prod")

        cli.main(spellings(["resource", verb], "prod")[position])

        assert resource_calls == [(reader, None, "prod")]

    @pytest.mark.parametrize("position", ["root", "leaf"])
    def test_init_names_the_deployment_from_either_position(self, project, reach, position):
        assert cli.main(spellings(["init"], "prod")[position]) == 0

        raw = json.loads((project / ".aiform" / "state.json").read_text())
        assert raw["deployment"] == "prod"

    @pytest.mark.parametrize(
        "argv",
        [
            ["--deployment", "prod", "plan", "create", "--deployment", "scratch"],
            ["--deployment", "prod", "plan", "--deployment", "scratch", "create"],
            ["plan", "--deployment", "prod", "create", "--deployment", "scratch"],
            ["--deployment", "prod", "plan", "--deployment", "prod", "create", "--deployment", "x"],
        ],
        ids=["root-vs-leaf", "root-vs-group", "group-vs-leaf", "two-agree-one-differs"],
    )
    def test_different_values_are_a_usage_error_naming_both(self, project, builds, capsys, argv):
        with pytest.raises(SystemExit) as caught:
            cli.main(argv)

        assert caught.value.code == 2
        err = capsys.readouterr().err
        assert "usage: aiform plan create" in err
        assert "conflicting deployments" in err
        values = [v for v in ("prod", "scratch", "x") if f"--deployment {v}" in err]
        assert len(values) >= 2
        assert builds == []

    def test_a_conflict_at_a_resource_verb_prints_that_verbs_usage(
        self, prod_state, resource_calls, capsys
    ):
        with pytest.raises(SystemExit) as caught:
            cli.main(["--deployment", "prod", "resource", "status", "--deployment", "scratch"])

        assert caught.value.code == 2
        err = capsys.readouterr().err
        assert "usage: aiform resource status" in err
        assert "--deployment prod" in err and "--deployment scratch" in err
        assert resource_calls == []

    def test_a_root_flag_conflicts_with_an_at_name(self, project, builds, capsys):
        with pytest.raises(SystemExit) as caught:
            cli.main(["--deployment", "prod", "plan", "create", "@scratch"])

        assert caught.value.code == 2
        err = capsys.readouterr().err
        assert "@scratch" in err and "--deployment prod" in err
        assert builds == []

    def test_a_root_flag_agrees_with_an_at_name(self, project, builds):
        cli.main(["--deployment", "prod", "plan", "create", "@prod"])

        assert builds == [("create", None, "prod")]

    @pytest.mark.parametrize("name", ["Prod", "a/b", "", "x" * 64])
    @pytest.mark.parametrize("position", ["root", "group"])
    def test_a_bad_name_is_the_same_usage_error_as_at_the_leaf(
        self, project, reach, capsys, name, position
    ):
        def message(argv: list[str]) -> str:
            with pytest.raises(SystemExit) as caught:
                cli.main(argv)
            assert caught.value.code == 2
            return capsys.readouterr().err.strip().splitlines()[-1].split("error: ", 1)[1]

        assert message(spellings(["plan", "show"], name)[position]) == message(
            spellings(["plan", "show"], name)["leaf"]
        )
        assert not (project / ".aiform").exists()
        assert reach.total() == 0

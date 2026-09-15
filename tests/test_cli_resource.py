# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aiform import cli, config, orchestrator, state
from aiform.driver import CapabilityNotSupported, ResourceDriver
from aiform.exceptions import ResourceNotFoundError
from aiform.models import DriverInfo, HealthReport, HealthStatus, MetricKind, Sample, StateEntry

SOURCE = """\
---
resource: compute
name: web-01
provider: digitalocean
params:
  region: sfo3
  size: s-1vcpu-2gb
---

## Intent

Runs the app.
"""


def make_state_entry(**overrides) -> StateEntry:
    defaults = dict(
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
        last_applied_at="2026-09-10T14:02:11Z",
        last_refreshed_at="2026-09-10T14:02:11Z",
        aiform_md_path="web.aiform.md",
        aiform_md_sha256="abc123",
    )
    defaults.update(overrides)
    return StateEntry(**defaults)


class StubDriver(ResourceDriver):
    PARAM_SCHEMA = {"type": "object", "properties": {}}

    def __init__(self, *, health=None, health_exc=None, samples=None, samples_exc=None, read=None):
        self._health = health
        self._health_exc = health_exc
        self._samples = samples
        self._samples_exc = samples_exc
        self._read = read

    def create(self, name, params, credentials):
        raise AssertionError("create() must never be reached by an `aiform resource` command")

    def update(self, id, current, desired, credentials):
        raise AssertionError("update() must never be reached by an `aiform resource` command")

    def delete(self, id, credentials):
        raise AssertionError("delete() must never be reached by an `aiform resource` command")

    def read(self, id, credentials):
        if self._read is None:
            raise ResourceNotFoundError("gone")
        return dict(self._read)

    def health(self, id, credentials):
        if self._health_exc is not None:
            raise self._health_exc
        if self._health is None:
            return super().health(id, credentials)
        return self._health

    def metrics(self, id, credentials):
        if self._samples_exc is not None:
            raise self._samples_exc
        if self._samples is None:
            return super().metrics(id, credentials)
        return list(self._samples)


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project directory with one tracked droplet, a driver stub, and
    credentials that resolve without touching the environment."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "web.aiform.md").write_text(SOURCE, encoding="utf-8")
    state_path = tmp_path / ".aiform" / "state.json"

    drivers: dict[tuple[str, str], ResourceDriver] = {}

    def fake_load_driver(provider, resource_type):
        return drivers[(provider, resource_type)]

    monkeypatch.setattr(orchestrator, "load_driver", fake_load_driver)
    monkeypatch.setattr(config, "resolve_credentials", lambda p: {"DIGITALOCEAN_TOKEN": "tok"})

    def install(*entries, **driver_kwargs):
        drivers[("digitalocean", "compute")] = StubDriver(**driver_kwargs)
        state.save(
            state.State(
                resources={
                    f"{e.provider}.{e.resource_type}.{e.name}": e
                    for e in (entries or (make_state_entry(),))
                }
            ),
            state_path,
        )
        return state_path

    install.drivers = drivers
    install.dir = tmp_path
    install.state_path = state_path
    return install


OK_REPORT = HealthReport(
    status=HealthStatus.OK, summary="active, public v4 203.0.113.10", observations={}
)
FAILING_REPORT = HealthReport(
    status=HealthStatus.FAILING,
    summary='status is "off"',
    observations={"status": "off", "locked": "false"},
)
LIVE = {"id": "123456789", "region": "sfo3", "size": "s-1vcpu-2gb"}


class TestParserShape:
    def test_resource_requires_a_verb(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["resource"])
        assert excinfo.value.code == 2
        # Names the dest, so this cannot pass merely because `resource`
        # is an unknown subcommand -- which is how it would read green
        # before the noun group exists at all.
        assert "resource_command" in capsys.readouterr().err

    @pytest.mark.parametrize("verb", ["check", "metrics", "status"])
    def test_each_verb_takes_an_optional_name(self, verb):
        parser = cli._build_parser()
        assert parser.parse_args(["resource", verb]).name is None
        assert parser.parse_args(["resource", verb, "web-01"]).name == "web-01"

    @pytest.mark.parametrize("verb", ["check", "metrics", "status"])
    def test_each_verb_defaults_to_text_and_accepts_json(self, verb):
        parser = cli._build_parser()
        assert parser.parse_args(["resource", verb]).format == "text"
        assert parser.parse_args(["resource", verb, "--format", "json"]).format == "json"

    @pytest.mark.parametrize("verb", ["check", "metrics", "status"])
    def test_each_verb_rejects_an_unknown_format(self, verb, capsys):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["resource", verb, "--format", "yaml"])
        assert excinfo.value.code == 2
        # On --format specifically, not on `resource` being unknown.
        err = capsys.readouterr().err
        assert "--format" in err and "yaml" in err

    @pytest.mark.parametrize("verb", ["check", "metrics", "status"])
    def test_each_verb_takes_state_file(self, verb):
        parser = cli._build_parser()
        args = parser.parse_args(["resource", verb, "--state-file", "/tmp/s.json"])
        assert args.state_file == Path("/tmp/s.json")

    @pytest.mark.parametrize("verb", ["check", "metrics", "status"])
    def test_verbose_parses_after_the_verb(self, verb):
        # specs/cli.md's addendum, note 3: every parser needs
        # parents=[global_parent, state_parent] or `-v` after the verb is
        # an unknown-flag error rather than a flag.
        parser = cli._build_parser()
        assert parser.parse_args(["resource", verb, "-v"]).verbose is True

    def test_output_is_on_metrics_alone(self):
        parser = cli._build_parser()
        assert parser.parse_args(["resource", "metrics", "--output", "m.txt"]).output == "m.txt"
        for verb in ("check", "status"):
            with pytest.raises(SystemExit):
                parser.parse_args(["resource", verb, "--output", "m.txt"])

    def test_there_is_no_all_flag(self):
        # Omitting <name> already means everything; a second spelling of
        # one meaning is what specs/driver.md's addendum warns against.
        parser = cli._build_parser()
        for verb in ("check", "metrics", "status"):
            # The verb itself must parse, or the rejection below proves
            # only that `resource` does not exist yet.
            assert parser.parse_args(["resource", verb]).name is None
            with pytest.raises(SystemExit):
                parser.parse_args(["resource", verb, "--all"])


class TestCheck:
    def test_named_ok_prints_one_line_and_exits_zero(self, project, capsys):
        path = project(health=OK_REPORT)
        code = cli.main(["resource", "check", "web-01", "--state-file", str(path)])
        assert code == 0
        assert capsys.readouterr().out == (
            "ok  digitalocean.compute.web-01  active, public v4 203.0.113.10\n"
        )

    def test_named_failing_exits_one_and_shows_observations(self, project, capsys):
        path = project(health=FAILING_REPORT)
        code = cli.main(["resource", "check", "web-01", "--state-file", str(path)])
        assert code == 1
        assert capsys.readouterr().out == (
            'failing  digitalocean.compute.web-01  status is "off"\n'
            "    status  off\n"
            "    locked  false\n"
        )

    def test_an_ok_verdict_hides_observations(self, project, capsys):
        path = project(
            health=HealthReport(
                status=HealthStatus.OK, summary="fine", observations={"status": "active"}
            )
        )
        cli.main(["resource", "check", "web-01", "--state-file", str(path)])
        assert "active" not in capsys.readouterr().out

    def test_a_declining_driver_on_a_named_resource_exits_two(self, project, capsys):
        path = project()
        code = cli.main(["resource", "check", "web-01", "--state-file", str(path)])
        assert code == 2
        assert "unsupported" in capsys.readouterr().out

    def test_the_fleet_form_prints_the_coverage_line(self, project, capsys):
        path = project(make_state_entry(), make_state_entry(name="db-01", id="99"), health=None)
        code = cli.main(["resource", "check", "--state-file", str(path)])
        assert code == 2
        assert capsys.readouterr().out.splitlines()[-1] == (
            "0 of 2 resources report health; 2 unsupported"
        )

    def test_a_one_resource_fleet_still_prints_the_coverage_line(self, project, capsys):
        # The renderers cannot read this off the list length, so the CLI
        # passes it: `<name> is None` is exactly the question.
        path = project(health=OK_REPORT)
        cli.main(["resource", "check", "--state-file", str(path)])
        assert capsys.readouterr().out.splitlines()[-1] == (
            "1 of 1 resources report health; 0 unsupported"
        )

    def test_a_named_resource_never_prints_the_coverage_line(self, project, capsys):
        path = project(health=OK_REPORT)
        cli.main(["resource", "check", "web-01", "--state-file", str(path)])
        assert "resources report health" not in capsys.readouterr().out

    def test_an_unknown_name_exits_two_naming_what_is_tracked(self, project, capsys):
        path = project(health=OK_REPORT)
        code = cli.main(["resource", "check", "nope", "--state-file", str(path)])
        assert code == 2
        err = capsys.readouterr().err
        assert "nope" in err and "web-01" in err

    def test_an_ambiguous_name_exits_two_listing_the_candidates(self, project, capsys):
        path = project(
            make_state_entry(name="web-01", resource_type="compute"),
            make_state_entry(name="web-01", resource_type="firewall", id="fw-1"),
            health=OK_REPORT,
        )
        code = cli.main(["resource", "check", "web-01", "--state-file", str(path)])
        assert code == 2
        err = capsys.readouterr().err
        assert "digitalocean.compute.web-01" in err
        assert "digitalocean.firewall.web-01" in err

    def test_json_is_a_single_document_with_coverage_and_worst_status(self, project, capsys):
        path = project(health=FAILING_REPORT)
        code = cli.main(
            ["resource", "check", "web-01", "--format", "json", "--state-file", str(path)]
        )
        doc = json.loads(capsys.readouterr().out)
        assert code == 1
        assert doc["worst_status"] == "failing"
        assert doc["coverage"] == {"reporting": 1, "total": 1, "unsupported": 0}

    def test_an_empty_state_exits_two(self, project, capsys):
        # A gate that passes because it checked nothing is the failure
        # mode worth designing against.
        path = project.state_path
        state.save(state.State(), path)
        code = cli.main(["resource", "check", "--state-file", str(path)])
        assert code == 2


class TestMetrics:
    def test_named_prints_aligned_rows_and_exits_zero(self, project, capsys):
        path = project(
            samples=[
                Sample(name="memory_bytes", kind=MetricKind.GAUGE, value=2147483648.0),
                Sample(name="cpu_percent", kind=MetricKind.GAUGE, value=41.2),
            ]
        )
        code = cli.main(["resource", "metrics", "web-01", "--state-file", str(path)])
        assert code == 0
        assert capsys.readouterr().out == (
            "gauge  memory_bytes  2147483648\ngauge  cpu_percent   41.2\n"
        )

    def test_a_declining_driver_exits_zero(self, project, capsys):
        # metrics reports no verdict, so there is nothing for its exit
        # code to carry.
        path = project()
        code = cli.main(["resource", "metrics", "web-01", "--state-file", str(path)])
        assert code == 0
        assert "unsupported:" in capsys.readouterr().out

    def test_a_failing_resource_still_exits_zero(self, project, capsys):
        path = project(samples_exc=ResourceNotFoundError("gone"))
        code = cli.main(["resource", "metrics", "web-01", "--state-file", str(path)])
        assert code == 0
        assert "resource not found" in capsys.readouterr().out

    def test_the_fleet_form_heads_each_resource_with_its_key(self, project, capsys):
        path = project(
            make_state_entry(),
            make_state_entry(name="db-01", id="99"),
            samples=[Sample(name="memory_bytes", kind=MetricKind.GAUGE, value=1.0)],
        )
        cli.main(["resource", "metrics", "--state-file", str(path)])
        lines = capsys.readouterr().out.splitlines()
        assert lines[0] == "digitalocean.compute.web-01"
        assert lines[1] == "  gauge  memory_bytes  1"
        assert lines[2] == "digitalocean.compute.db-01"

    def test_no_tracked_resources_prints_nothing_and_exits_zero(self, project, capsys):
        # A reading of an empty formation is not an error, and a blank
        # line would not survive `| jq`.
        path = project.state_path
        state.save(state.State(), path)
        code = cli.main(["resource", "metrics", "--state-file", str(path)])
        assert code == 0
        assert capsys.readouterr().out == ""

    def test_a_missing_state_file_exits_zero(self, project, capsys):
        code = cli.main(["resource", "metrics", "--state-file", str(project.dir / "absent.json")])
        assert code == 0

    def test_a_malformed_state_file_exits_two(self, project, capsys):
        path = project.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        code = cli.main(["resource", "metrics", "--state-file", str(path)])
        assert code == 2
        assert "Error:" in capsys.readouterr().err

    def test_json_carries_elapsed_seconds_and_bare_names(self, project, capsys):
        path = project(samples=[Sample(name="memory_bytes", kind=MetricKind.GAUGE, value=1.0)])
        cli.main(["resource", "metrics", "web-01", "--format", "json", "--state-file", str(path)])
        doc = json.loads(capsys.readouterr().out)
        assert isinstance(doc["elapsed_seconds"], float)
        assert doc["resources"][0]["samples"][0]["name"] == "memory_bytes"


class TestMetricsOutput:
    def _run(self, project, out, *extra):
        path = project(samples=[Sample(name="memory_bytes", kind=MetricKind.GAUGE, value=1.0)])
        return cli.main(
            ["resource", "metrics", "web-01", "--output", str(out), "--state-file", str(path)]
            + list(extra)
        )

    def test_appends_rather_than_truncating(self, project, capsys):
        out = project.dir / "metrics.log"
        assert self._run(project, out) == 0
        assert self._run(project, out) == 0
        assert out.read_text().count("gauge  memory_bytes  1") == 2

    def test_each_run_is_preceded_by_a_utc_delimiter_naming_the_invocation(self, project):
        out = project.dir / "metrics.log"
        self._run(project, out)
        first = out.read_text().splitlines()[0]
        assert re.match(
            r"^=== \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z  aiform resource metrics ", first
        )
        assert "web-01" in first

    def test_nothing_goes_to_stdout_when_output_is_given(self, project, capsys):
        out = project.dir / "metrics.log"
        self._run(project, out)
        assert capsys.readouterr().out == ""

    def test_a_missing_parent_directory_exits_two_naming_it(self, project, capsys):
        out = project.dir / "absent" / "metrics.log"
        assert self._run(project, out) == 2
        assert "absent" in capsys.readouterr().err

    def test_aiform_does_not_create_the_parent_directory(self, project, capsys):
        out = project.dir / "absent" / "metrics.log"
        self._run(project, out)
        assert not (project.dir / "absent").exists()

    def test_json_to_a_file_keeps_the_delimiter(self, project):
        # The file is a stream of runs, not one document; a consumer
        # splits on the delimiter before parsing each block.
        out = project.dir / "metrics.log"
        self._run(project, out, "--format", "json")
        text = out.read_text()
        assert text.startswith("=== ")
        json.loads(text.split("\n", 1)[1])


class TestStatus:
    def test_named_prints_the_four_labelled_rows(self, project, capsys):
        path = project(health=FAILING_REPORT, read=LIVE)
        code = cli.main(["resource", "status", "web-01", "--state-file", str(path)])
        assert code == 0
        assert capsys.readouterr().out == (
            "deployed  2026-09-10T14:02:11Z, id 123456789\n"
            "live      present\n"
            "config    in sync with web.aiform.md\n"
            'health    failing — status is "off"\n'
        )

    def test_a_failing_resource_exits_zero(self, project, capsys):
        # A resource's health belongs in what the command reports, not in
        # the exit code, where a wrapper turns one bad reading into a
        # failure.
        path = project(health=FAILING_REPORT, read=LIVE)
        assert cli.main(["resource", "status", "web-01", "--state-file", str(path)]) == 0

    def test_a_gone_resource_still_reports_when_it_was_deployed(self, project, capsys):
        path = project(health_exc=ResourceNotFoundError("gone"), read=None)
        code = cli.main(["resource", "status", "web-01", "--state-file", str(path)])
        assert code == 0
        out = capsys.readouterr().out
        assert "live      missing on the provider" in out
        assert "config    not applicable: resource is gone" in out
        assert "deployed  2026-09-10T14:02:11Z, id 123456789" in out

    def test_drift_is_named_field_by_field(self, project, capsys):
        path = project(health=OK_REPORT, read={**LIVE, "size": "s-4vcpu-8gb"})
        cli.main(["resource", "status", "web-01", "--state-file", str(path)])
        assert "config    1 field drifted: size" in capsys.readouterr().out

    def test_a_missing_source_file_says_so_rather_than_in_sync(self, project, capsys):
        (project.dir / "web.aiform.md").unlink()
        path = project(health=OK_REPORT, read=LIVE)
        cli.main(["resource", "status", "web-01", "--state-file", str(path)])
        assert "config    no source file found" in capsys.readouterr().out

    def test_the_fleet_form_heads_each_resource_with_its_key(self, project, capsys):
        path = project(
            make_state_entry(),
            make_state_entry(name="db-01", id="99"),
            health=OK_REPORT,
            read=LIVE,
        )
        cli.main(["resource", "status", "--state-file", str(path)])
        lines = capsys.readouterr().out.splitlines()
        assert lines[0] == "digitalocean.compute.web-01"
        assert lines[1].startswith("  deployed  ")

    def test_writes_no_state(self, project, capsys):
        path = project(health=OK_REPORT, read={**LIVE, "size": "s-4vcpu-8gb"})
        before = path.read_bytes()
        cli.main(["resource", "status", "web-01", "--state-file", str(path)])
        assert path.read_bytes() == before

    def test_json_shape(self, project, capsys):
        path = project(health=OK_REPORT, read=LIVE)
        cli.main(["resource", "status", "web-01", "--format", "json", "--state-file", str(path)])
        doc = json.loads(capsys.readouterr().out)
        assert doc["resources"][0]["live"] == "present"
        assert doc["resources"][0]["health"]["status"] == "ok"


class TestZeroLLMCalls:
    @pytest.mark.parametrize("verb", ["check", "metrics", "status"])
    def test_no_verb_constructs_an_llm_client(self, project, verb, forbid_llm_client, capsys):
        path = project(health=OK_REPORT, samples=[], read=LIVE)
        cli.main(["resource", verb, "web-01", "--state-file", str(path)])

    @pytest.mark.parametrize("verb", ["check", "metrics", "status"])
    def test_no_verb_is_handed_a_counting_client(self, project, verb, monkeypatch, capsys):
        # The three belong to the plain dispatch set: they make zero
        # Anthropic calls by contract, so none is ever given a client.
        built = []
        monkeypatch.setattr(
            cli, "_CountingClient", lambda *a, **k: built.append(1) or pytest.fail("client built")
        )
        path = project(health=OK_REPORT, samples=[], read=LIVE)
        cli.main(["resource", verb, "web-01", "--state-file", str(path)])
        assert built == []

    @pytest.mark.parametrize("verb", ["check", "metrics", "status"])
    def test_no_verb_prints_a_verbose_call_counter(self, project, verb, capsys):
        # There is no [verbose] line to assert for these, the same way
        # there is none for plan refresh/show.
        path = project(health=OK_REPORT, samples=[], read=LIVE)
        cli.main(["resource", verb, "web-01", "-v", "--state-file", str(path)])
        assert "Anthropic API call(s) made" not in capsys.readouterr().err


class TestStdoutIsACleanStream:
    def test_a_warning_does_not_reach_stdout(self, project, capsys):
        # An earlier version asserted only on stderr and then parsed a
        # separately rendered document, so it would have passed unchanged
        # if the warning HAD gone to stdout -- the one thing its name
        # claims to check.
        #
        # Asserted on the log line's *shape*, not on the message text: an
        # UNKNOWN verdict's summary legitimately IS the exception text,
        # and that summary is part of the report, which belongs on
        # stdout. What must never appear there is a log line -- its level
        # and its key=value fields.
        path = project(health_exc=TimeoutError("boom"))
        cli.main(["resource", "check", "web-01", "-v", "--state-file", str(path)])
        captured = capsys.readouterr()
        assert "boom" in captured.err
        # log.py renders WARNING as "WARN" (log.py's _Formatter), so the
        # marker is that, not the level's Python name.
        assert "WARN " in captured.err
        assert "WARN" not in captured.out
        assert "resource_key=" not in captured.out
        assert "operation=" not in captured.out
        # stdout is exactly the report: one line per resource, plus the
        # observations block for a non-ok verdict.
        assert captured.out.splitlines()[0].startswith("unknown  digitalocean.compute.web-01  ")

    def test_a_warning_leaves_json_on_stdout_parseable(self, project, capsys):
        # The consequence that matters: `--format json | jq` must survive
        # a resource whose driver raised.
        path = project(samples_exc=TimeoutError("boom"))
        cli.main(
            ["resource", "metrics", "web-01", "-v", "--format", "json", "--state-file", str(path)]
        )
        captured = capsys.readouterr()
        doc = json.loads(captured.out)
        assert "boom" in doc["resources"][0]["errors"][0]
        assert "boom" in captured.err
        assert "WARN" not in captured.out

    def test_json_output_is_parseable_with_verbose_on(self, project, capsys):
        path = project(samples=[Sample(name="memory_bytes", kind=MetricKind.GAUGE, value=1.0)])
        cli.main(
            ["resource", "metrics", "web-01", "-v", "--format", "json", "--state-file", str(path)]
        )
        json.loads(capsys.readouterr().out)


class TestExitCodeLogging:
    # log.configure() sets propagate=False on the "aiform" logger, so
    # caplog sees nothing; tests/test_cli.py reads the rotating file sink
    # instead and so does this.
    def _exit_line(self, project) -> str:
        logs = sorted((project.dir / ".aiform" / "logs").glob("*.log"))
        return logs[-1].read_text().splitlines()[-1]

    def test_an_unhealthy_check_is_not_logged_as_a_command_failure(self, project, capsys):
        # Exit 1 is check's verdict, not a failed command. Logging it as
        # an error would make every powered-off droplet look like an
        # aiform failure in the log file.
        path = project(health=FAILING_REPORT)
        assert cli.main(["resource", "check", "web-01", "--state-file", str(path)]) == 1
        line = self._exit_line(project)
        assert "exit_code=1" in line
        assert "outcome=unhealthy" in line
        assert line.split()[1] == "INFO"

    def test_a_real_failure_is_still_logged_as_an_error(self, project, capsys):
        path = project(health=OK_REPORT)
        assert cli.main(["resource", "check", "nope", "--state-file", str(path)]) == 2
        line = self._exit_line(project)
        assert "exit_code=2" in line
        assert "outcome=error" in line
        assert line.split()[1] == "ERROR"

    def test_a_healthy_check_is_logged_as_success(self, project, capsys):
        path = project(health=OK_REPORT)
        assert cli.main(["resource", "check", "web-01", "--state-file", str(path)]) == 0
        assert "outcome=success" in self._exit_line(project)


class TestDeclineIsNotAnError:
    def test_a_capability_decline_never_becomes_a_driver_execution_error(self, project, capsys):
        # orchestrator._call_driver()'s blanket except would turn a
        # deliberate decline into an UNKNOWN verdict and a failed check;
        # the resource commands must not route through it.
        path = project(health_exc=CapabilityNotSupported("health", "no signal here"))
        code = cli.main(["resource", "check", "web-01", "--state-file", str(path)])
        assert code == 2
        assert "unsupported  digitalocean.compute.web-01  no signal here" in capsys.readouterr().out


class TestBrokenPipe:
    """`aiform resource metrics --format json | head` closes stdout
    early. Python's default is a traceback plus a non-zero exit, for a
    command built to be piped.

    Driven through a real subprocess with a real pipe: capsys replaces
    stdout with a StringIO, which never raises BrokenPipeError, so the
    except branch is unreachable under capture.
    """

    def test_a_closed_pipe_is_a_successful_run(self, tmp_path):
        script = "import sys; from aiform import cli; cli._print_stream('x' * 5_000_000)"
        reader = subprocess.Popen(
            ["head", "-c", "10"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL
        )
        writer = subprocess.Popen(
            [sys.executable, "-c", script], stdout=reader.stdin, stderr=subprocess.PIPE
        )
        reader.stdin.close()
        _, err = writer.communicate(timeout=60)
        reader.wait(timeout=60)
        assert writer.returncode == 0, err.decode()
        # Not merely "no crash": the interpreter's own shutdown flush
        # re-raises a BrokenPipeError after the handler returns unless
        # the fd was reopened on devnull, and that shows up here.
        assert b"BrokenPipeError" not in err
        assert b"Traceback" not in err

    def test_the_same_script_without_the_handler_does_traceback(self, tmp_path):
        # The control. Without it, the test above proves only that this
        # environment happens not to raise.
        script = "print('x' * 5_000_000)"
        reader = subprocess.Popen(
            ["head", "-c", "10"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL
        )
        writer = subprocess.Popen(
            [sys.executable, "-c", script], stdout=reader.stdin, stderr=subprocess.PIPE
        )
        reader.stdin.close()
        _, err = writer.communicate(timeout=60)
        reader.wait(timeout=60)
        assert writer.returncode != 0 or b"BrokenPipeError" in err

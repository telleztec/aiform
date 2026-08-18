import sys
from pathlib import Path

import pytest

from scripts import run_system_tests


class FakeCompletedProcess:
    def __init__(self, returncode: int):
        self.returncode = returncode


class TestMissingCredentials:
    def test_both_present_returns_empty_list(self):
        env = {"ANTHROPIC_API_KEY": "sk-ant-1", "DIGITALOCEAN_TOKEN": "dop_v1_1"}
        assert run_system_tests.missing_credentials(env) == []

    def test_both_absent_returns_both_in_order(self):
        assert run_system_tests.missing_credentials({}) == [
            "ANTHROPIC_API_KEY",
            "DIGITALOCEAN_TOKEN",
        ]

    def test_one_absent_returns_just_that_one(self):
        env = {"ANTHROPIC_API_KEY": "sk-ant-1"}
        assert run_system_tests.missing_credentials(env) == ["DIGITALOCEAN_TOKEN"]

    def test_empty_string_value_counts_as_missing(self):
        env = {"ANTHROPIC_API_KEY": "", "DIGITALOCEAN_TOKEN": "dop_v1_1"}
        assert run_system_tests.missing_credentials(env) == ["ANTHROPIC_API_KEY"]

    def test_defaults_to_os_environ(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-1")
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_1")
        assert run_system_tests.missing_credentials() == []


class TestRotateLogs:
    def _make_logs(self, log_dir: Path, names: list[str]) -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        for name in names:
            (log_dir / name).write_text("x")

    def test_no_op_when_well_under_the_limit(self, tmp_path):
        log_dir = tmp_path / "testlog"
        self._make_logs(log_dir, [f"system-test-{i:02d}.log" for i in range(5)])

        run_system_tests.rotate_logs(log_dir, keep=10)

        assert len(list(log_dir.glob("*.log"))) == 5

    def test_no_op_at_exactly_keep_minus_one(self, tmp_path):
        log_dir = tmp_path / "testlog"
        self._make_logs(log_dir, [f"system-test-{i:02d}.log" for i in range(9)])

        run_system_tests.rotate_logs(log_dir, keep=10)

        assert len(list(log_dir.glob("*.log"))) == 9

    def test_trims_down_to_keep_minus_one_when_at_the_limit(self, tmp_path):
        log_dir = tmp_path / "testlog"
        self._make_logs(log_dir, [f"system-test-{i:02d}.log" for i in range(10)])

        run_system_tests.rotate_logs(log_dir, keep=10)

        assert len(list(log_dir.glob("*.log"))) == 9

    def test_deletes_oldest_first_by_filename(self, tmp_path):
        log_dir = tmp_path / "testlog"
        names = [f"system-test-{i:02d}.log" for i in range(12)]
        self._make_logs(log_dir, names)

        run_system_tests.rotate_logs(log_dir, keep=10)

        remaining = {p.name for p in log_dir.glob("*.log")}
        assert remaining == {f"system-test-{i:02d}.log" for i in range(3, 12)}

    def test_ignores_non_log_files(self, tmp_path):
        log_dir = tmp_path / "testlog"
        self._make_logs(log_dir, [f"system-test-{i:02d}.log" for i in range(10)])
        (log_dir / ".gitkeep").write_text("")

        run_system_tests.rotate_logs(log_dir, keep=10)

        assert (log_dir / ".gitkeep").exists()

    def test_missing_directory_does_not_raise(self, tmp_path):
        log_dir = tmp_path / "does-not-exist-yet"

        run_system_tests.rotate_logs(log_dir, keep=10)


class TestNewLogPath:
    def test_formats_utc_timestamp_into_filename(self):
        import datetime

        now = datetime.datetime(2026, 8, 17, 23, 59, 5, tzinfo=datetime.UTC)

        path = run_system_tests.new_log_path(Path("/tmp/testlog"), now=now)

        assert path == Path("/tmp/testlog/system-test-20260817T235905Z.log")

    def test_appends_a_counter_suffix_on_same_second_collision(self, tmp_path):
        import datetime

        now = datetime.datetime(2026, 8, 17, 23, 59, 5, tzinfo=datetime.UTC)
        (tmp_path / "system-test-20260817T235905Z.log").write_text("first run")

        path = run_system_tests.new_log_path(tmp_path, now=now)

        assert path == tmp_path / "system-test-20260817T235905Z-2.log"

    def test_counter_suffix_increments_past_multiple_collisions(self, tmp_path):
        import datetime

        now = datetime.datetime(2026, 8, 17, 23, 59, 5, tzinfo=datetime.UTC)
        (tmp_path / "system-test-20260817T235905Z.log").write_text("first")
        (tmp_path / "system-test-20260817T235905Z-2.log").write_text("second")

        path = run_system_tests.new_log_path(tmp_path, now=now)

        assert path == tmp_path / "system-test-20260817T235905Z-3.log"


@pytest.fixture
def project_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run_system_tests, "LOG_DIR", tmp_path / ".aiform" / "testlog")
    return tmp_path


@pytest.fixture
def credentials(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-1")
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_1")


def fail_if_subprocess_run_called(monkeypatch) -> None:
    def _boom(*args, **kwargs):
        raise AssertionError("subprocess.run must not be called")

    monkeypatch.setattr(run_system_tests.subprocess, "run", _boom)


class TestMain:
    def test_missing_credentials_exits_2_without_running_pytest(
        self, project_dir, monkeypatch, capsys
    ):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        fail_if_subprocess_run_called(monkeypatch)

        code = run_system_tests.main([])

        assert code == 2
        err = capsys.readouterr().err
        assert "ANTHROPIC_API_KEY" in err
        assert "DIGITALOCEAN_TOKEN" in err

    def test_missing_credentials_does_not_create_log_dir(self, project_dir, monkeypatch, capsys):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        fail_if_subprocess_run_called(monkeypatch)

        run_system_tests.main([])

        assert not run_system_tests.LOG_DIR.exists()

    def test_creates_log_dir_when_credentials_present(self, project_dir, credentials, monkeypatch):
        monkeypatch.setattr(
            run_system_tests.subprocess, "run", lambda *a, **kw: FakeCompletedProcess(0)
        )

        run_system_tests.main([])

        assert run_system_tests.LOG_DIR.is_dir()

    def test_returns_pytest_exit_code(self, project_dir, credentials, monkeypatch):
        monkeypatch.setattr(
            run_system_tests.subprocess, "run", lambda *a, **kw: FakeCompletedProcess(1)
        )

        assert run_system_tests.main([]) == 1

    def test_invokes_pytest_with_system_marker_and_tests_system_dir(
        self, project_dir, credentials, monkeypatch
    ):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return FakeCompletedProcess(0)

        monkeypatch.setattr(run_system_tests.subprocess, "run", fake_run)

        run_system_tests.main([])

        assert len(calls) == 1
        cmd, kwargs = calls[0]
        assert cmd == [sys.executable, "-m", "pytest", "-m", "system", "tests/system/", "-v"]

    def test_redirects_combined_stdout_and_stderr_into_a_new_log_file(
        self, project_dir, credentials, monkeypatch
    ):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(kwargs)
            kwargs["stdout"].write("hello from pytest\n")
            return FakeCompletedProcess(0)

        monkeypatch.setattr(run_system_tests.subprocess, "run", fake_run)

        run_system_tests.main([])

        kwargs = calls[0]
        assert kwargs["stderr"] == run_system_tests.subprocess.STDOUT
        log_files = list(run_system_tests.LOG_DIR.glob("*.log"))
        assert len(log_files) == 1
        assert log_files[0].read_text() == "hello from pytest\n"

    def test_rotates_before_starting_a_new_run(self, project_dir, credentials, monkeypatch):
        run_system_tests.LOG_DIR.mkdir(parents=True)
        for i in range(10):
            (run_system_tests.LOG_DIR / f"system-test-{i:02d}.log").write_text("x")
        monkeypatch.setattr(
            run_system_tests.subprocess, "run", lambda *a, **kw: FakeCompletedProcess(0)
        )

        run_system_tests.main([])

        assert len(list(run_system_tests.LOG_DIR.glob("*.log"))) == 10

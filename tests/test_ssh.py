# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import stat
import subprocess
from pathlib import Path

import pytest

from aiform import ssh


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class TestManagedKeyExists:
    def test_false_before_any_key_is_generated(self, tmp_path):
        assert ssh.managed_key_exists(tmp_path / "ssh") is False

    def test_true_after_ensure_managed_key(self, tmp_path):
        ssh_dir = tmp_path / "ssh"
        ssh.ensure_managed_key(ssh_dir)

        assert ssh.managed_key_exists(ssh_dir) is True

    def test_does_not_itself_create_anything(self, tmp_path):
        ssh_dir = tmp_path / "ssh"

        ssh.managed_key_exists(ssh_dir)

        assert not ssh_dir.exists()


class TestEnsureManagedKey:
    def test_writes_a_gitignore_covering_the_whole_directory(self, tmp_path):
        ssh_dir = tmp_path / ".aiform" / "ssh"

        ssh.ensure_managed_key(ssh_dir)

        gitignore_path = ssh_dir / ".gitignore"
        assert gitignore_path.exists()
        assert gitignore_path.read_text().strip() == "*"

    def test_creates_a_keypair_under_ssh_dir(self, tmp_path):
        ssh_dir = tmp_path / ".aiform" / "ssh"

        private_key_path, public_key_path = ssh.ensure_managed_key(ssh_dir)

        assert private_key_path == ssh_dir / "aiform_managed_key"
        assert public_key_path == ssh_dir / "aiform_managed_key.pub"
        assert private_key_path.exists()
        assert public_key_path.exists()
        assert "OPENSSH PRIVATE KEY" in private_key_path.read_text()
        assert public_key_path.read_text().startswith("ssh-ed25519 ")

    def test_private_key_is_chmod_600(self, tmp_path):
        private_key_path, _ = ssh.ensure_managed_key(tmp_path / "ssh")

        assert _mode(private_key_path) == 0o600

    def test_second_call_reuses_the_existing_keypair(self, tmp_path):
        ssh_dir = tmp_path / "ssh"
        private_key_path, public_key_path = ssh.ensure_managed_key(ssh_dir)
        first_private = private_key_path.read_bytes()
        first_public = public_key_path.read_bytes()

        ssh.ensure_managed_key(ssh_dir)

        assert private_key_path.read_bytes() == first_private
        assert public_key_path.read_bytes() == first_public

    def test_recovers_a_missing_public_key_from_the_private_key(self, tmp_path):
        ssh_dir = tmp_path / "ssh"
        private_key_path, public_key_path = ssh.ensure_managed_key(ssh_dir)
        original_public = public_key_path.read_text()
        public_key_path.unlink()

        recovered_private, recovered_public = ssh.ensure_managed_key(ssh_dir)

        assert recovered_private == private_key_path
        assert recovered_public.exists()
        # The re-derived public key must be the actual pair of the
        # existing private key, not a freshly generated, unrelated one --
        # a mismatch here would silently orphan every droplet already
        # carrying the original public key.
        assert recovered_public.read_text().split()[1] == original_public.split()[1]

    def test_does_not_regenerate_when_private_key_already_exists(self, tmp_path, monkeypatch):
        ssh_dir = tmp_path / "ssh"
        ssh.ensure_managed_key(ssh_dir)

        real_run = subprocess.run

        def _forbid_keygen(argv, *args, **kwargs):
            if argv[0] == "ssh-keygen" and "-y" not in argv:
                raise AssertionError("ssh-keygen -f called again on an existing private key")
            return real_run(argv, *args, **kwargs)

        monkeypatch.setattr(ssh.subprocess, "run", _forbid_keygen)

        ssh.ensure_managed_key(ssh_dir)


class TestGenerateBackupScript:
    def test_writes_two_executable_scripts_under_ssh_dir(self, tmp_path):
        ssh_dir = tmp_path / "ssh"
        ssh_dir.mkdir(parents=True)
        private_key_path = ssh_dir / "aiform_managed_key"
        private_key_path.write_text("fake-private-key")

        keychain_path, onepassword_path = ssh.generate_backup_script(ssh_dir, private_key_path)

        assert keychain_path == ssh_dir / "backup_key_to_keychain.sh"
        assert onepassword_path == ssh_dir / "backup_key_to_1password.sh"
        assert keychain_path.exists()
        assert onepassword_path.exists()
        assert _mode(keychain_path) == 0o700
        assert _mode(onepassword_path) == 0o700

    def test_each_script_references_its_own_backend(self, tmp_path):
        ssh_dir = tmp_path / "ssh"
        ssh_dir.mkdir(parents=True)
        private_key_path = ssh_dir / "aiform_managed_key"
        private_key_path.write_text("fake-private-key")

        keychain_path, onepassword_path = ssh.generate_backup_script(ssh_dir, private_key_path)
        keychain_content = keychain_path.read_text()
        onepassword_content = onepassword_path.read_text()

        assert str(private_key_path) in keychain_content
        assert "security add-generic-password" in keychain_content
        assert str(ssh_dir.resolve()) in keychain_content

        assert str(private_key_path) in onepassword_content
        assert "op item create" in onepassword_content
        assert "op read" in onepassword_content

    def test_resolves_a_relative_private_key_path(self, tmp_path, monkeypatch):
        # A relative KEY_FILE would fail with a confusing `cat:` error the
        # moment the script is run from anywhere but the project root, and
        # the restore line it echoes would write the key back to the
        # wrong place.
        ssh_dir = tmp_path / "ssh"
        ssh_dir.mkdir(parents=True)
        (ssh_dir / "aiform_managed_key").write_text("fake-private-key")
        monkeypatch.chdir(tmp_path)

        keychain_path, onepassword_path = ssh.generate_backup_script(
            ssh_dir, Path("ssh/aiform_managed_key")
        )
        resolved = str((tmp_path / "ssh" / "aiform_managed_key").resolve())

        keychain_content = keychain_path.read_text()
        onepassword_content = onepassword_path.read_text()
        assert resolved in keychain_content
        assert 'KEY_FILE="ssh/aiform_managed_key"' not in keychain_content
        assert resolved in onepassword_content
        assert 'KEY_FILE="ssh/aiform_managed_key"' not in onepassword_content

    def test_never_executes_either_script(self, tmp_path, monkeypatch):
        ssh_dir = tmp_path / "ssh"
        ssh_dir.mkdir(parents=True)
        private_key_path = ssh_dir / "aiform_managed_key"
        private_key_path.write_text("fake-private-key")

        def _forbid_run(*args, **kwargs):
            raise AssertionError("generate_backup_script must never execute anything")

        monkeypatch.setattr(ssh.subprocess, "run", _forbid_run)

        ssh.generate_backup_script(ssh_dir, private_key_path)

    def test_overwrites_existing_scripts(self, tmp_path):
        ssh_dir = tmp_path / "ssh"
        ssh_dir.mkdir(parents=True)
        private_key_path = ssh_dir / "aiform_managed_key"
        private_key_path.write_text("fake-private-key")
        keychain_path = ssh_dir / "backup_key_to_keychain.sh"
        keychain_path.write_text("stale content")
        onepassword_path = ssh_dir / "backup_key_to_1password.sh"
        onepassword_path.write_text("stale content")

        ssh.generate_backup_script(ssh_dir, private_key_path)

        assert "stale content" not in keychain_path.read_text()
        assert "stale content" not in onepassword_path.read_text()


class _FakeCompleted:
    def __init__(self, returncode=0):
        self.returncode = returncode


class _FakeClock:
    """A monotonic clock only time.sleep() advances -- lets a test whose
    connect_timeout_budget genuinely requires several retries stay
    instant and deterministic, instead of depending on real wall-clock
    time actually elapsing (which is what a mocked-only time.sleep with
    real time.monotonic() left it doing -- 12 real seconds for one test,
    caught while verifying the wall-clock-bounding fix below)."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fake_clock(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(ssh.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(ssh.time, "sleep", clock.sleep)
    return clock


class TestShutdownViaSsh:
    def test_returns_true_on_a_clean_zero_exit(self, tmp_path, monkeypatch):
        calls = []

        def _fake_run(argv, **kwargs):
            calls.append(argv)
            return _FakeCompleted(returncode=0)

        monkeypatch.setattr(ssh.subprocess, "run", _fake_run)

        result = ssh.shutdown_via_ssh(
            "203.0.113.10",
            tmp_path / "aiform_managed_key",
            tmp_path / "known_hosts",
            connect_timeout_budget=30.0,
        )

        assert result is True
        assert len(calls) == 1
        argv = calls[0]
        assert argv[0] == "ssh"
        assert "-i" in argv
        assert str(tmp_path / "aiform_managed_key") in argv
        assert f"UserKnownHostsFile={tmp_path / 'known_hosts'}" in argv
        assert "StrictHostKeyChecking=accept-new" in argv
        assert "BatchMode=yes" in argv
        assert argv[-2] == "root@203.0.113.10"
        assert "shutdown" in argv[-1]
        # Without this, ssh also offers any identity already loaded in an
        # agent before trying -i's key, so an operator with several keys
        # loaded can exhaust the server's MaxAuthTries before the managed
        # key is ever tried.
        assert "IdentitiesOnly=yes" in argv

    def test_returns_true_on_timeout_expired(self, tmp_path, monkeypatch):
        # The expected, successful shape per the live diagnostic: the guest
        # tears the connection down as it shuts down before ssh can read a
        # clean exit status.
        def _fake_run(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 12))

        monkeypatch.setattr(ssh.subprocess, "run", _fake_run)

        result = ssh.shutdown_via_ssh(
            "203.0.113.10",
            tmp_path / "aiform_managed_key",
            tmp_path / "known_hosts",
            connect_timeout_budget=30.0,
        )

        assert result is True

    def test_retries_on_connection_failure_then_succeeds(self, tmp_path, monkeypatch):
        attempts = []

        def _fake_run(argv, **kwargs):
            attempts.append(argv)
            if len(attempts) < 3:
                return _FakeCompleted(returncode=255)
            return _FakeCompleted(returncode=0)

        monkeypatch.setattr(ssh.subprocess, "run", _fake_run)
        monkeypatch.setattr(ssh.time, "sleep", lambda seconds: None)

        result = ssh.shutdown_via_ssh(
            "203.0.113.10",
            tmp_path / "aiform_managed_key",
            tmp_path / "known_hosts",
            connect_timeout_budget=30.0,
        )

        assert result is True
        assert len(attempts) == 3

    def test_returns_false_once_the_budget_is_exhausted(self, tmp_path, monkeypatch, fake_clock):
        def _fake_run(argv, **kwargs):
            return _FakeCompleted(returncode=255)

        monkeypatch.setattr(ssh.subprocess, "run", _fake_run)

        result = ssh.shutdown_via_ssh(
            "203.0.113.10",
            tmp_path / "aiform_managed_key",
            tmp_path / "known_hosts",
            connect_timeout_budget=12.0,
        )

        assert result is False

    def test_worst_case_wall_time_is_bounded_by_the_budget(self, tmp_path, monkeypatch, fake_clock):
        # Regression test for the bug caught during /code-review: an
        # earlier version derived a fixed attempt count from
        # connect_timeout_budget / _RETRY_DELAY_SECONDS alone, and gave
        # every attempt the full _PER_ATTEMPT_TIMEOUT_SECONDS ceiling
        # regardless of what was left of the budget -- a 45s budget
        # could in the worst case spend upwards of 150s of wall time
        # before giving up, silently regressing the fallback-latency
        # problem #171/#172/#174 were rejected for.
        #
        # The fake `run` below consumes simulated time equal to its own
        # `timeout=` kwarg before failing -- the worst case, where every
        # attempt hangs for its full ceiling. A fake that returns
        # instantly (an earlier version of this test did) never advances
        # the clock during an attempt at all, which makes this assertion
        # trivially true regardless of whether the retry loop's own math
        # is actually bounded -- caught on a second /code-review pass,
        # which proved this by splicing the pre-fix implementation back
        # in and confirming the old test still passed against it.
        def _fake_run(argv, **kwargs):
            fake_clock.sleep(kwargs["timeout"])
            return _FakeCompleted(returncode=255)

        monkeypatch.setattr(ssh.subprocess, "run", _fake_run)

        result = ssh.shutdown_via_ssh(
            "203.0.113.10",
            tmp_path / "aiform_managed_key",
            tmp_path / "known_hosts",
            connect_timeout_budget=45.0,
        )

        assert result is False
        # No attempt after the first is ever started without enough
        # budget left to give it the full ceiling, so total time can
        # only exceed the budget via the mandatory first attempt, and
        # only by up to one ceiling's worth.
        assert fake_clock.now <= 45.0 + ssh._PER_ATTEMPT_TIMEOUT_SECONDS

    def test_every_attempt_gets_the_full_ceiling_never_truncated(
        self, tmp_path, monkeypatch, fake_clock
    ):
        # A subprocess.TimeoutExpired is only a meaningful "ssh had the
        # full ceiling and still didn't return -- the guest is likely
        # tearing the connection down as it shuts down" signal if the
        # attempt actually ran for the full ceiling. An earlier version
        # truncated an attempt's own timeout to whatever budget remained
        # (min(_PER_ATTEMPT_TIMEOUT_SECONDS, remaining)) to keep total
        # wall time bounded -- which meant a late attempt could time out
        # after only a few seconds and still be read as a successful
        # shutdown, when ssh may simply not have finished connecting
        # yet. Caught by /code-review.
        timeouts = []

        def _fake_run(argv, **kwargs):
            timeouts.append(kwargs["timeout"])
            fake_clock.sleep(kwargs["timeout"])
            return _FakeCompleted(returncode=255)

        monkeypatch.setattr(ssh.subprocess, "run", _fake_run)

        ssh.shutdown_via_ssh(
            "203.0.113.10",
            tmp_path / "aiform_managed_key",
            tmp_path / "known_hosts",
            connect_timeout_budget=45.0,
        )

        assert timeouts
        assert all(t == ssh._PER_ATTEMPT_TIMEOUT_SECONDS for t in timeouts)

    def test_always_makes_at_least_one_attempt(self, tmp_path, monkeypatch):
        attempts = []

        def _fake_run(argv, **kwargs):
            attempts.append(argv)
            return _FakeCompleted(returncode=255)

        monkeypatch.setattr(ssh.subprocess, "run", _fake_run)
        monkeypatch.setattr(ssh.time, "sleep", lambda seconds: None)

        ssh.shutdown_via_ssh(
            "203.0.113.10",
            tmp_path / "aiform_managed_key",
            tmp_path / "known_hosts",
            connect_timeout_budget=0.0,
        )

        assert len(attempts) == 1

    def test_never_polls_any_http_endpoint(self, tmp_path, monkeypatch):
        import urllib.request

        def _forbid_urlopen(*args, **kwargs):
            raise AssertionError("shutdown_via_ssh must not make any HTTP call itself")

        monkeypatch.setattr(urllib.request, "urlopen", _forbid_urlopen)
        monkeypatch.setattr(
            ssh.subprocess, "run", lambda argv, **kwargs: _FakeCompleted(returncode=0)
        )

        ssh.shutdown_via_ssh(
            "203.0.113.10",
            tmp_path / "aiform_managed_key",
            tmp_path / "known_hosts",
            connect_timeout_budget=30.0,
        )


@pytest.mark.parametrize("attr", ["DEFAULT_SSH_DIR"])
def test_default_ssh_dir_is_project_relative(attr):
    value = getattr(ssh, attr)
    assert value == Path(".aiform/ssh")
    assert not value.is_absolute()

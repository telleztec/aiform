# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import stat
import subprocess

import pytest

from aiform import ssh


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class TestEnsureManagedKey:
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
    def test_writes_an_executable_script_under_ssh_dir(self, tmp_path):
        ssh_dir = tmp_path / "ssh"
        ssh_dir.mkdir(parents=True)
        private_key_path = ssh_dir / "aiform_managed_key"
        private_key_path.write_text("fake-private-key")

        script_path = ssh.generate_backup_script(ssh_dir, private_key_path)

        assert script_path == ssh_dir / "backup_key_to_keychain.sh"
        assert script_path.exists()
        assert _mode(script_path) == 0o700

    def test_script_references_the_private_key_path_and_keychain(self, tmp_path):
        ssh_dir = tmp_path / "ssh"
        ssh_dir.mkdir(parents=True)
        private_key_path = ssh_dir / "aiform_managed_key"
        private_key_path.write_text("fake-private-key")

        script_path = ssh.generate_backup_script(ssh_dir, private_key_path)
        content = script_path.read_text()

        assert str(private_key_path) in content
        assert "security add-generic-password" in content
        assert str(ssh_dir.resolve()) in content
        assert "op item create" in content  # 1Password fallback, commented out

    def test_never_executes_the_script(self, tmp_path, monkeypatch):
        ssh_dir = tmp_path / "ssh"
        ssh_dir.mkdir(parents=True)
        private_key_path = ssh_dir / "aiform_managed_key"
        private_key_path.write_text("fake-private-key")

        def _forbid_run(*args, **kwargs):
            raise AssertionError("generate_backup_script must never execute anything")

        monkeypatch.setattr(ssh.subprocess, "run", _forbid_run)

        ssh.generate_backup_script(ssh_dir, private_key_path)

    def test_overwrites_an_existing_script(self, tmp_path):
        ssh_dir = tmp_path / "ssh"
        ssh_dir.mkdir(parents=True)
        private_key_path = ssh_dir / "aiform_managed_key"
        private_key_path.write_text("fake-private-key")
        script_path = ssh_dir / "backup_key_to_keychain.sh"
        script_path.write_text("stale content")

        ssh.generate_backup_script(ssh_dir, private_key_path)

        assert "stale content" not in script_path.read_text()


class _FakeCompleted:
    def __init__(self, returncode=0):
        self.returncode = returncode


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

    def test_returns_false_once_the_budget_is_exhausted(self, tmp_path, monkeypatch):
        def _fake_run(argv, **kwargs):
            return _FakeCompleted(returncode=255)

        monkeypatch.setattr(ssh.subprocess, "run", _fake_run)
        monkeypatch.setattr(ssh.time, "sleep", lambda seconds: None)

        result = ssh.shutdown_via_ssh(
            "203.0.113.10",
            tmp_path / "aiform_managed_key",
            tmp_path / "known_hosts",
            connect_timeout_budget=12.0,
        )

        assert result is False

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
    from pathlib import Path

    value = getattr(ssh, attr)
    assert value == Path(".aiform/ssh")
    assert not value.is_absolute()

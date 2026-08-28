# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import logging as stdlib_logging
from io import StringIO
from pathlib import Path

from aiform import log
from aiform.models import LoggingConfig
from tests.conftest import _log_file_haystacks, find_leaked_credential


def _logging_config(level: str = "DEBUG", max_files: int = 10) -> LoggingConfig:
    return LoggingConfig(level=level, max_files=max_files)


class TestFindLeakedCredential:
    def test_no_secrets_configured_returns_none(self):
        secrets = {"ANTHROPIC_API_KEY": None, "DIGITALOCEAN_TOKEN": None}
        assert find_leaked_credential(secrets, ["hello world"]) is None

    def test_secret_absent_from_every_haystack_returns_none(self):
        secrets = {"ANTHROPIC_API_KEY": "sk-ant-real-value"}
        assert find_leaked_credential(secrets, ["nothing sensitive here"]) is None

    def test_secret_present_in_a_haystack_returns_its_var_name(self):
        secrets = {"ANTHROPIC_API_KEY": "sk-ant-real-value"}
        haystacks = ["oops sk-ant-real-value leaked into stderr"]
        assert find_leaked_credential(secrets, haystacks) == "ANTHROPIC_API_KEY"

    def test_checks_every_haystack_not_just_the_first(self):
        secrets = {"DIGITALOCEAN_TOKEN": "dop_v1_real_value"}
        haystacks = ["clean output", "dop_v1_real_value shows up here instead"]
        assert find_leaked_credential(secrets, haystacks) == "DIGITALOCEAN_TOKEN"

    def test_empty_string_value_never_matches(self):
        secrets = {"ANTHROPIC_API_KEY": ""}
        assert find_leaked_credential(secrets, [""]) is None

    def test_none_value_never_matches(self):
        secrets = {"ANTHROPIC_API_KEY": None}
        assert find_leaked_credential(secrets, ["ANTHROPIC_API_KEY"]) is None

    def test_first_leaking_var_in_iteration_order_wins(self):
        # dict iteration order is insertion order -- deterministic, so
        # this exercises "checks every configured secret", not luck.
        secrets = {
            "DIGITALOCEAN_TOKEN": "dop_v1_real_value",
            "ANTHROPIC_API_KEY": "sk-ant-real-value",
        }
        haystacks = ["dop_v1_real_value and sk-ant-real-value both leaked here"]
        assert find_leaked_credential(secrets, haystacks) == "DIGITALOCEAN_TOKEN"


class TestLogFileHaystacks:
    def test_no_configure_call_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(log, "_installed_file_handler", None)
        assert _log_file_haystacks() == []

    def test_returns_content_of_the_file_configure_wrote(self, tmp_path: Path):
        log.configure(stream=StringIO(), logging_config=_logging_config(), log_dir=tmp_path)
        stdlib_logging.getLogger("aiform.orchestrator").info(
            "", extra={"marker": "in-the-haystack"}
        )

        haystacks = _log_file_haystacks()

        assert any("in-the-haystack" in haystack for haystack in haystacks)

    def test_second_call_without_a_new_configure_returns_empty_not_stale_content(
        self, tmp_path: Path
    ):
        # Regression for a /code-review finding on this module's own PR:
        # aiform.log._installed_file_handler is a module-level global
        # _reset_aiform_logger never nulls out, so without this reset a
        # later test that never calls configure() again would still have
        # this function re-read (and could misattribute a leak to) a
        # prior test's already-checked log directory.
        log.configure(stream=StringIO(), logging_config=_logging_config(), log_dir=tmp_path)
        stdlib_logging.getLogger("aiform.orchestrator").info(
            "", extra={"marker": "consumed-on-first-read"}
        )

        first_read = _log_file_haystacks()
        second_read = _log_file_haystacks()

        assert any("consumed-on-first-read" in haystack for haystack in first_read)
        assert second_read == []

    def test_picks_up_every_log_file_from_multiple_configure_calls(self, tmp_path: Path):
        log.configure(stream=StringIO(), logging_config=_logging_config(), log_dir=tmp_path)
        stdlib_logging.getLogger("aiform.orchestrator").info("", extra={"marker": "first-file"})

        log.configure(stream=StringIO(), logging_config=_logging_config(), log_dir=tmp_path)
        stdlib_logging.getLogger("aiform.orchestrator").info("", extra={"marker": "second-file"})

        haystacks = _log_file_haystacks()

        assert any("first-file" in haystack for haystack in haystacks)
        assert any("second-file" in haystack for haystack in haystacks)

    def test_uses_the_handlers_own_absolute_path_not_cwd(self, tmp_path: Path, monkeypatch):
        # Regression for the exact bug specs/conftest.md documents: a
        # cwd-relative Path(".aiform/logs").glob() would resolve against
        # whatever directory happens to be current *after* this fixture's
        # sibling fixtures (e.g. tests/system/conftest.py's project_dir)
        # have already reverted their own chdir. Anchoring on the
        # handler's own absolute baseFilename must be immune to that --
        # simulate it directly by chdir-ing away *before* calling the
        # helper, exactly as teardown ordering would.
        log_dir = tmp_path / "logs"
        log.configure(stream=StringIO(), logging_config=_logging_config(), log_dir=log_dir)
        stdlib_logging.getLogger("aiform.orchestrator").info("", extra={"marker": "still-findable"})

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        haystacks = _log_file_haystacks()

        assert any("still-findable" in haystack for haystack in haystacks)

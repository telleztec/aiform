import logging
import time
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

from aiform import log
from aiform.models import LoggingConfig


def _logging_config(level: str = "DEBUG", max_files: int = 10) -> LoggingConfig:
    return LoggingConfig(level=level, max_files=max_files)


def _make_record(*, level=logging.INFO, name="aiform.llm", msg="", extra=None, created=0):
    record = logging.LogRecord(name, level, __file__, 0, msg, (), None)
    record.created = created
    for key, value in (extra or {}).items():
        setattr(record, key, value)
    return record


def _ts(created: float) -> str:
    return datetime.fromtimestamp(created, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestElapsedMs:
    def test_returns_nonnegative_int(self):
        start = time.monotonic()
        assert isinstance(log.elapsed_ms(start), int)
        assert log.elapsed_ms(start) >= 0

    def test_reflects_actual_elapsed_time(self):
        start = time.monotonic() - 0.05
        assert log.elapsed_ms(start) >= 40


class TestFormatter:
    def test_basic_line_with_extra_fields_in_order(self):
        record = _make_record(
            extra={"role": "code_review", "model": "claude-opus-5", "duration_ms": 1834},
            created=1786600000,
        )

        formatted = log._KeyValueFormatter().format(record)

        assert formatted == (
            f"{_ts(1786600000)} {'INFO':<5} {'aiform.llm':<20} "
            "role=code_review model=claude-opus-5 duration_ms=1834"
        )

    def test_warning_level_displays_as_warn_not_warning(self):
        record = _make_record(level=logging.WARNING)

        formatted = log._KeyValueFormatter().format(record)

        assert formatted.split(" ", 2)[1] == "WARN"

    def test_error_and_debug_level_names_unaffected(self):
        assert (
            log._KeyValueFormatter().format(_make_record(level=logging.ERROR)).split(" ", 2)[1]
            == "ERROR"
        )
        assert (
            log._KeyValueFormatter().format(_make_record(level=logging.DEBUG)).split(" ", 2)[1]
            == "DEBUG"
        )

    def test_logger_name_left_justified_to_width_20(self):
        record = _make_record(name="aiform.orchestrator")

        formatted = log._KeyValueFormatter().format(record)

        assert f"{'aiform.orchestrator':<20}" in formatted

    def test_none_value_omits_the_key_entirely(self):
        record = _make_record(extra={"thinking_tokens": None, "output_tokens": 512})

        formatted = log._KeyValueFormatter().format(record)

        assert "thinking_tokens" not in formatted
        assert "output_tokens=512" in formatted

    def test_msg_quoted_and_appended_after_extra_fields(self):
        record = _make_record(extra={"role": "code_review"}, msg="response likely truncated")

        formatted = log._KeyValueFormatter().format(record)

        assert formatted.endswith('role=code_review msg="response likely truncated"')

    def test_empty_msg_is_omitted_entirely(self):
        record = _make_record(extra={"role": "code_review"}, msg="")

        formatted = log._KeyValueFormatter().format(record)

        assert "msg=" not in formatted

    def test_msg_escapes_embedded_quote_and_newline(self):
        record = _make_record(msg='bad "value"\nsecond line')

        formatted = log._KeyValueFormatter().format(record)

        assert formatted.endswith('msg="bad \\"value\\" second line"')

    def test_empty_extra_and_empty_msg_produces_no_trailing_field(self):
        record = _make_record()

        formatted = log._KeyValueFormatter().format(record)

        assert formatted == f"{_ts(0)} {'INFO':<5} {'aiform.llm':<20}"

    def test_extra_only_no_msg_has_no_trailing_field(self):
        record = _make_record(extra={"role": "code_review"})

        formatted = log._KeyValueFormatter().format(record)

        assert formatted == f"{_ts(0)} {'INFO':<5} {'aiform.llm':<20} role=code_review"


class TestConfigure:
    def test_default_stream_level_is_warning_info_suppressed(self, tmp_path: Path):
        stream = StringIO()
        log.configure(stream=stream, logging_config=_logging_config(), log_dir=tmp_path)
        logger = logging.getLogger("aiform.orchestrator")

        logger.info("", extra={"marker": "info-line"})
        logger.warning("", extra={"marker": "warn-line"})

        output = stream.getvalue()
        assert "info-line" not in output
        assert "warn-line" in output

    def test_verbose_promotes_stream_threshold_to_info(self, tmp_path: Path):
        stream = StringIO()
        log.configure(
            verbose=True, stream=stream, logging_config=_logging_config(), log_dir=tmp_path
        )
        logger = logging.getLogger("aiform.orchestrator")

        logger.info("", extra={"marker": "info-line"})

        assert "info-line" in stream.getvalue()

    def test_idempotent_does_not_duplicate_handlers(self, tmp_path: Path):
        stream = StringIO()
        log.configure(stream=stream, logging_config=_logging_config(), log_dir=tmp_path)
        log.configure(stream=stream, logging_config=_logging_config(), log_dir=tmp_path)
        logger = logging.getLogger("aiform.orchestrator")

        logger.warning("", extra={"marker": "once"})

        assert stream.getvalue().count("once") == 1

    def test_configure_called_with_nothing_previously_installed(self, tmp_path: Path):
        stream = StringIO()
        log.configure(stream=stream, logging_config=_logging_config(), log_dir=tmp_path)
        logger = logging.getLogger("aiform.orchestrator")

        logger.warning("", extra={"marker": "first-call"})

        assert "first-call" in stream.getvalue()

    def test_sets_propagate_false(self, tmp_path: Path):
        log.configure(logging_config=_logging_config(), log_dir=tmp_path)

        assert logging.getLogger("aiform").propagate is False

    def test_default_stream_resolves_sys_stderr_at_call_time_not_import_time(
        self, tmp_path: Path, monkeypatch
    ):
        # Regression: `stream: TextIO = sys.stderr` as a parameter default
        # is bound once, when the module is imported -- long before this
        # test (or pytest's own capsys) ever runs. configure() must read
        # sys.stderr itself, inside the function, so a stream swapped in
        # after import (exactly what capsys does every test) is the one
        # actually used.
        substitute = StringIO()
        monkeypatch.setattr("sys.stderr", substitute)

        log.configure(logging_config=_logging_config(), log_dir=tmp_path)
        logging.getLogger("aiform.orchestrator").warning("", extra={"marker": "routed-here"})

        assert "routed-here" in substitute.getvalue()

    def test_second_configure_call_still_only_writes_to_its_own_stream(self, tmp_path: Path):
        first_stream = StringIO()
        second_stream = StringIO()
        log.configure(stream=first_stream, logging_config=_logging_config(), log_dir=tmp_path)
        log.configure(stream=second_stream, logging_config=_logging_config(), log_dir=tmp_path)
        logger = logging.getLogger("aiform.orchestrator")

        logger.warning("", extra={"marker": "routed"})

        assert "routed" not in first_stream.getvalue()
        assert "routed" in second_stream.getvalue()

    def test_default_logging_config_resolves_from_real_config_when_omitted(
        self, tmp_path: Path, monkeypatch
    ):
        # No logging_config passed -- configure() must fall back to
        # config.resolve_logging_config(), not crash or silently use
        # something else. chdir into tmp_path so that resolution reads
        # (or, here, doesn't find) a sandboxed .aiform/config.yaml rather
        # than whatever happens to be in the real repo's cwd -- exercises
        # resolve_logging_config()'s own missing-file default
        # (DEFAULT_LOGGING_CONFIG, level="INFO").
        monkeypatch.chdir(tmp_path)

        log.configure(stream=StringIO(), log_dir=tmp_path)

        assert logging.getLogger("aiform").level == logging.INFO


class TestConfigureFileSink:
    def test_file_created_in_log_dir(self, tmp_path: Path):
        log.configure(stream=StringIO(), logging_config=_logging_config(), log_dir=tmp_path)

        log_files = list(tmp_path.glob("*.log"))
        assert len(log_files) == 1
        assert log_files[0].name.startswith("aiform-")

    def test_file_captures_info_regardless_of_verbose(self, tmp_path: Path):
        log.configure(
            verbose=False,
            stream=StringIO(),
            logging_config=_logging_config(level="INFO"),
            log_dir=tmp_path,
        )
        logging.getLogger("aiform.orchestrator").info("", extra={"marker": "in-the-file"})

        log_file = next(tmp_path.glob("*.log"))
        assert "in-the-file" in log_file.read_text()

    def test_stream_stays_quiet_while_file_still_captures(self, tmp_path: Path):
        stream = StringIO()
        log.configure(
            verbose=False,
            stream=stream,
            logging_config=_logging_config(level="INFO"),
            log_dir=tmp_path,
        )
        logging.getLogger("aiform.orchestrator").info("", extra={"marker": "quiet-on-screen"})

        log_file = next(tmp_path.glob("*.log"))
        assert "quiet-on-screen" not in stream.getvalue()
        assert "quiet-on-screen" in log_file.read_text()

    def test_file_level_independent_of_verbose_flag(self, tmp_path: Path):
        # File threshold comes from logging_config, never from -v --
        # configured to WARNING here, INFO log calls must not reach the
        # file even though verbose=True widens the *stream*.
        log.configure(
            verbose=True,
            stream=StringIO(),
            logging_config=_logging_config(level="WARNING"),
            log_dir=tmp_path,
        )
        logging.getLogger("aiform.orchestrator").info("", extra={"marker": "should-not-appear"})

        log_file = next(tmp_path.glob("*.log"))
        assert "should-not-appear" not in log_file.read_text()

    def test_logger_level_is_minimum_of_the_two_handlers(self, tmp_path: Path):
        # logging_config more permissive (INFO) than the default stream
        # threshold (WARNING, verbose=False) -- the logger's own level
        # must not be the stream's WARNING, or the file would never see
        # INFO records either (a handler can't process what the logger
        # itself already dropped).
        log.configure(
            verbose=False,
            stream=StringIO(),
            logging_config=_logging_config(level="INFO"),
            log_dir=tmp_path,
        )

        assert logging.getLogger("aiform").level == logging.INFO

    def test_second_configure_call_creates_a_second_file_not_appending(self, tmp_path: Path):
        log.configure(
            stream=StringIO(), logging_config=_logging_config(max_files=10), log_dir=tmp_path
        )
        log.configure(
            stream=StringIO(), logging_config=_logging_config(max_files=10), log_dir=tmp_path
        )

        assert len(list(tmp_path.glob("*.log"))) == 2

    def test_log_dir_created_if_missing(self, tmp_path: Path):
        log_dir = tmp_path / "does" / "not" / "exist" / "yet"

        log.configure(stream=StringIO(), logging_config=_logging_config(), log_dir=log_dir)

        assert log_dir.is_dir()
        assert len(list(log_dir.glob("*.log"))) == 1


class TestRotateLogs:
    def test_no_op_when_dir_does_not_exist(self, tmp_path: Path):
        log._rotate_logs(tmp_path / "nonexistent", keep=10)

    def test_no_op_when_under_the_keep_limit(self, tmp_path: Path):
        (tmp_path / "a.log").write_text("")
        (tmp_path / "b.log").write_text("")

        log._rotate_logs(tmp_path, keep=10)

        assert len(list(tmp_path.glob("*.log"))) == 2

    def test_prunes_oldest_first_down_to_keep_minus_one(self, tmp_path: Path):
        names = [f"aiform-2026081{i}T000000Z.log" for i in range(5)]
        for name in names:
            (tmp_path / name).write_text("")

        log._rotate_logs(tmp_path, keep=3)

        remaining = sorted(p.name for p in tmp_path.glob("*.log"))
        assert remaining == names[-2:]

    def test_non_log_files_are_ignored(self, tmp_path: Path):
        (tmp_path / "aiform-20260810T000000Z.log").write_text("")
        (tmp_path / "notes.txt").write_text("")

        log._rotate_logs(tmp_path, keep=1)

        assert (tmp_path / "notes.txt").exists()


class TestNewLogPath:
    def test_builds_expected_filename(self, tmp_path: Path):
        now = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)

        path = log._new_log_path(tmp_path, now=now)

        assert path == tmp_path / "aiform-20260819T120000Z.log"

    def test_same_second_collision_appends_numeric_suffix(self, tmp_path: Path):
        now = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
        (tmp_path / "aiform-20260819T120000Z.log").write_text("")

        path = log._new_log_path(tmp_path, now=now)

        assert path == tmp_path / "aiform-20260819T120000Z-2.log"

    def test_multiple_collisions_increment_the_suffix(self, tmp_path: Path):
        now = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
        (tmp_path / "aiform-20260819T120000Z.log").write_text("")
        (tmp_path / "aiform-20260819T120000Z-2.log").write_text("")

        path = log._new_log_path(tmp_path, now=now)

        assert path == tmp_path / "aiform-20260819T120000Z-3.log"

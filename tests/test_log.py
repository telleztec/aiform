import logging
from datetime import UTC, datetime
from io import StringIO

from aiform import log


def _make_record(*, level=logging.INFO, name="aiform.llm", msg="", extra=None, created=0):
    record = logging.LogRecord(name, level, __file__, 0, msg, (), None)
    record.created = created
    for key, value in (extra or {}).items():
        setattr(record, key, value)
    return record


def _ts(created: float) -> str:
    return datetime.fromtimestamp(created, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    def test_default_level_is_warning_info_suppressed(self):
        stream = StringIO()
        log.configure(stream=stream)
        logger = logging.getLogger("aiform.orchestrator")

        logger.info("", extra={"marker": "info-line"})
        logger.warning("", extra={"marker": "warn-line"})

        output = stream.getvalue()
        assert "info-line" not in output
        assert "warn-line" in output

    def test_verbose_promotes_threshold_to_info(self):
        stream = StringIO()
        log.configure(verbose=True, stream=stream)
        logger = logging.getLogger("aiform.orchestrator")

        logger.info("", extra={"marker": "info-line"})

        assert "info-line" in stream.getvalue()

    def test_idempotent_does_not_duplicate_handlers(self):
        stream = StringIO()
        log.configure(stream=stream)
        log.configure(stream=stream)
        logger = logging.getLogger("aiform.orchestrator")

        logger.warning("", extra={"marker": "once"})

        assert stream.getvalue().count("once") == 1

    def test_configure_called_with_nothing_previously_installed(self):
        stream = StringIO()
        log.configure(stream=stream)
        logger = logging.getLogger("aiform.orchestrator")

        logger.warning("", extra={"marker": "first-call"})

        assert "first-call" in stream.getvalue()

    def test_sets_propagate_false(self):
        log.configure()

        assert logging.getLogger("aiform").propagate is False

    def test_default_stream_resolves_sys_stderr_at_call_time_not_import_time(self, monkeypatch):
        # Regression: `stream: TextIO = sys.stderr` as a parameter default
        # is bound once, when the module is imported -- long before this
        # test (or pytest's own capsys) ever runs. configure() must read
        # sys.stderr itself, inside the function, so a stream swapped in
        # after import (exactly what capsys does every test) is the one
        # actually used.
        substitute = StringIO()
        monkeypatch.setattr("sys.stderr", substitute)

        log.configure()
        logging.getLogger("aiform.orchestrator").warning("", extra={"marker": "routed-here"})

        assert "routed-here" in substitute.getvalue()

    def test_second_configure_call_still_only_writes_to_its_own_stream(self):
        first_stream = StringIO()
        second_stream = StringIO()
        log.configure(stream=first_stream)
        log.configure(stream=second_stream)
        logger = logging.getLogger("aiform.orchestrator")

        logger.warning("", extra={"marker": "routed"})

        assert "routed" not in first_stream.getvalue()
        assert "routed" in second_stream.getvalue()

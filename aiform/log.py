import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from aiform import config
from aiform.models import LoggingConfig


def elapsed_ms(start: float) -> int:
    return round((time.monotonic() - start) * 1000)


_RESERVED_RECORD_ATTRS = frozenset(vars(logging.makeLogRecord({})))


def _format_extra_value(value: object) -> str:
    text = str(value)
    # Bare key=value only holds as a one-token-per-field contract when
    # the value itself has no whitespace to split on -- quote/escape it
    # the same way msg is escaped below, otherwise leave scalars
    # (numbers, bools, identifiers) exactly as they render today.
    if any(ch.isspace() for ch in text) or '"' in text:
        escaped = text.replace('"', '\\"').replace("\n", " ")
        return f'"{escaped}"'
    return text


class _KeyValueFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Computed locally rather than via logging.addLevelName(WARNING,
        # "WARN") -- that mutates a process-wide stdlib table as a side
        # effect of merely importing this module, changing how WARNING
        # renders for every logger in the process, including third-party
        # ones, even when configure() is never called. Caught by
        # /code-review.
        levelname = "WARN" if record.levelno == logging.WARNING else record.levelname
        parts = [timestamp, f"{levelname:<5}", f"{record.name:<20}"]

        extra_fields = [
            f"{key}={_format_extra_value(value)}"
            for key, value in record.__dict__.items()
            if key not in _RESERVED_RECORD_ATTRS and value is not None
        ]
        if extra_fields:
            parts.append(" ".join(extra_fields))

        message = record.getMessage()
        if message:
            escaped = message.replace('"', '\\"').replace("\n", " ")
            parts.append(f'msg="{escaped}"')

        return " ".join(parts)


def _rotate_logs(log_dir: Path, *, keep: int) -> None:
    if not log_dir.is_dir():
        return
    # mtime, not filename: a plain lexicographic sort puts a same-second
    # collision suffix ("aiform-<ts>-2.log") before the unsuffixed file
    # it collided with ('-' is 0x2D, '.' is 0x2E), inverting which one
    # is actually older.
    existing = sorted(log_dir.glob("*.log"), key=lambda path: path.stat().st_mtime)
    overflow = len(existing) - (keep - 1)
    for path in existing[: max(overflow, 0)]:
        path.unlink()


def _new_log_path(log_dir: Path, *, now: datetime | None = None) -> Path:
    now = now or datetime.now(UTC)
    timestamp = f"{now:%Y%m%dT%H%M%SZ}"

    # Mirrors scripts/run_system_tests.py's new_log_path() (itself
    # mirroring orchestrator.py's move_to_trash()) -- same one-second
    # filename resolution, same collision-avoidance fix.
    path = log_dir / f"aiform-{timestamp}.log"
    counter = 2
    while path.exists():
        path = log_dir / f"aiform-{timestamp}-{counter}.log"
        counter += 1
    return path


_installed_file_handler: logging.FileHandler | None = None
_installed_stream_handler: logging.StreamHandler | None = None


def configure(
    *,
    verbose: bool = False,
    stream: TextIO | None = None,
    logging_config: LoggingConfig | None = None,
    log_dir: Path | None = None,
) -> None:
    global _installed_file_handler, _installed_stream_handler
    logger = logging.getLogger("aiform")

    if _installed_file_handler is not None:
        logger.removeHandler(_installed_file_handler)
        # removeHandler() alone doesn't release the FileHandler's OS file
        # descriptor -- only close() does (see tests/conftest.py's own
        # teardown fixture, which exists for the identical reason).
        _installed_file_handler.close()
    if _installed_stream_handler is not None:
        logger.removeHandler(_installed_stream_handler)

    # Resolved here, not as parameter defaults -- a default is bound
    # once, at module-import time. sys.stderr swapped out later (pytest's
    # capsys) or a config file that doesn't exist yet at import time both
    # need to be read at call time, not import time, to actually take
    # effect.
    if stream is None:
        stream = sys.stderr
    if logging_config is None:
        logging_config = config.resolve_logging_config()
    if log_dir is None:
        log_dir = Path(".aiform/logs")

    formatter = _KeyValueFormatter()

    log_dir.mkdir(parents=True, exist_ok=True)
    _rotate_logs(log_dir, keep=logging_config.max_files)
    file_level = getattr(logging, logging_config.level)
    file_handler = logging.FileHandler(_new_log_path(log_dir))
    file_handler.setFormatter(formatter)
    file_handler.setLevel(file_level)
    logger.addHandler(file_handler)

    stream_level = logging.INFO if verbose else logging.WARNING
    stream_handler = logging.StreamHandler(stream)
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(stream_level)
    logger.addHandler(stream_handler)

    logger.setLevel(min(file_level, stream_level))
    logger.propagate = False

    _installed_file_handler = file_handler
    _installed_stream_handler = stream_handler

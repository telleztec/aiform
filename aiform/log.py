import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from aiform import config
from aiform.models import LoggingConfig

logging.addLevelName(logging.WARNING, "WARN")


def elapsed_ms(start: float) -> int:
    return round((time.monotonic() - start) * 1000)


_RESERVED_RECORD_ATTRS = frozenset(vars(logging.makeLogRecord({})))


class _KeyValueFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        parts = [timestamp, f"{record.levelname:<5}", f"{record.name:<20}"]

        extra_fields = [
            f"{key}={value}"
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
    existing = sorted(log_dir.glob("*.log"))
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

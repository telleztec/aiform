import logging
import sys
from datetime import UTC, datetime
from typing import TextIO

logging.addLevelName(logging.WARNING, "WARN")

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


_installed_handler: logging.StreamHandler | None = None


def configure(*, verbose: bool = False, stream: TextIO | None = None) -> None:
    global _installed_handler
    logger = logging.getLogger("aiform")

    if _installed_handler is not None:
        logger.removeHandler(_installed_handler)

    # sys.stderr resolved here, not as a `= sys.stderr` default -- a
    # default is bound once, at module-import time, to whatever object
    # sys.stderr happened to be then. Anything that later swaps
    # sys.stderr for a different object (pytest's capsys chief among
    # them) would be silently invisible to this handler, since it would
    # still be writing to the original, no-longer-current stream.
    if stream is None:
        stream = sys.stderr

    handler = logging.StreamHandler(stream)
    handler.setFormatter(_KeyValueFormatter())
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.INFO if verbose else logging.WARNING)

    _installed_handler = handler

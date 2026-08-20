import os
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

REQUIRED_ENV_VARS = ("ANTHROPIC_API_KEY", "DIGITALOCEAN_TOKEN")
LOG_DIR = Path(".aiform/testlog")
MAX_LOG_FILES = 10


def missing_credentials(env: Mapping[str, str] | None = None) -> list[str]:
    env = env if env is not None else os.environ
    return [var for var in REQUIRED_ENV_VARS if not env.get(var)]


def rotate_logs(log_dir: Path, *, keep: int = MAX_LOG_FILES) -> None:
    if not log_dir.is_dir():
        return
    # mtime, not filename: a plain lexicographic sort puts a same-second
    # collision suffix ("system-test-<ts>-2.log") before the unsuffixed
    # file it collided with ('-' is 0x2D, '.' is 0x2E), inverting which
    # one is actually older. Mirrors aiform/log.py's _rotate_logs().
    existing = sorted(log_dir.glob("*.log"), key=lambda path: path.stat().st_mtime)
    overflow = len(existing) - (keep - 1)
    for path in existing[: max(overflow, 0)]:
        path.unlink()


def new_log_path(log_dir: Path, *, now: datetime | None = None) -> Path:
    now = now or datetime.now(UTC)
    timestamp = f"{now:%Y%m%dT%H%M%SZ}"

    # Mirrors aiform/orchestrator.py's move_to_trash() -- same one-second
    # filename resolution, same collision-avoidance fix.
    path = log_dir / f"system-test-{timestamp}.log"
    counter = 2
    while path.exists():
        path = log_dir / f"system-test-{timestamp}-{counter}.log"
        counter += 1
    return path


def main(argv: list[str] | None = None) -> int:
    missing = missing_credentials()
    if missing:
        print(
            f"Error: missing required environment variable(s): {', '.join(missing)} -- "
            "set them before running the live system-test suite (see specs/system_test.md)",
            file=sys.stderr,
        )
        return 2

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    rotate_logs(LOG_DIR)
    log_path = new_log_path(LOG_DIR)

    with log_path.open("w", encoding="utf-8") as log_file:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-m", "system", "tests/system/", "-v"],
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())

# specs/run_system_tests.md — `scripts/run_system_tests.py`

## Purpose

A thin wrapper around `pytest -m system tests/system/` (`specs/system_test.md`)
that makes the live suite safe and convenient to run by hand: it refuses
to start without both required credentials present, and it captures the
run's output to a rotating log file instead of only the terminal, so a
multi-minute live run against real DigitalOcean/Anthropic infrastructure
leaves a record behind without accumulating unbounded log files under
`.aiform/`.

This is an ops tool, not part of `aiform`'s own runtime — it lives in
`scripts/`, outside `tests/`, the same way `specs/system_test.md`'s
still-unimplemented `scripts/sweep_system_test_droplets.py` does, and for
the same reason: it must not be collected or executed by a plain `pytest`
run.

## Interface

`scripts/run_system_tests.py`, invoked directly (`python
scripts/run_system_tests.py`) or via `.venv/bin/python
scripts/run_system_tests.py`. No CLI arguments.

```python
REQUIRED_ENV_VARS: tuple[str, ...]  # ("ANTHROPIC_API_KEY", "DIGITALOCEAN_TOKEN")
LOG_DIR: Path  # .aiform/testlog
MAX_LOG_FILES: int  # 10


def missing_credentials(env: Mapping[str, str] | None = None) -> list[str]:
    """REQUIRED_ENV_VARS entries absent or empty in `env` (defaults to
    os.environ), in REQUIRED_ENV_VARS order."""


def rotate_logs(log_dir: Path, *, keep: int = MAX_LOG_FILES) -> None:
    """Deletes the oldest *.log files in log_dir, if needed, so that at
    most keep - 1 remain -- leaving room for exactly one new log file to
    bring the total back up to keep."""


def new_log_path(log_dir: Path, *, now: datetime | None = None) -> Path:
    """log_dir / f"system-test-{now:%Y%m%dT%H%M%SZ}.log", UTC. On a
    same-second collision (a file already exists at that path), appends
    a "-2", "-3", ... counter suffix until the path is free -- mirrors
    aiform/orchestrator.py's move_to_trash()."""


def main(argv: list[str] | None = None) -> int:
    """Entry point; returns the process exit code (see Behavior)."""
```

`if __name__ == "__main__": raise SystemExit(main())`.

## Behavior

- Checks `missing_credentials()` first, before touching the filesystem
  at all. If non-empty: print `Error: missing required environment
  variable(s): <name>[, <name>...] -- set them before running the live
  system-test suite (see specs/system_test.md)` to stderr and return `2`
  without creating `LOG_DIR`, rotating anything, or invoking `pytest`.
- Otherwise: `LOG_DIR.mkdir(parents=True, exist_ok=True)`, then
  `rotate_logs(LOG_DIR)`, then open `new_log_path(LOG_DIR)` for writing.
- Runs `[sys.executable, "-m", "pytest", "-m", "system", "tests/system/",
  "-v"]` via `subprocess.run`, with `stdout` and `stderr` both redirected
  into the new log file (`stderr=subprocess.STDOUT` — one combined
  stream, matching how a person watching a terminal would read it).
  `sys.executable` (not a hardcoded interpreter path) so this always
  runs under whatever Python — and therefore whatever venv — invoked
  this script.
- Returns the subprocess's exit code as-is, so scripting/automation
  around this wrapper can tell pass from fail without opening the log.
- Log rotation keeps at most `MAX_LOG_FILES` (10) `*.log` files in
  `LOG_DIR` *including* the run just started — i.e. it trims down to at
  most 9 existing files before creating the 10th. Oldest-first, by
  **mtime**, not filename. Plain filenames (`system-test-<UTC
  timestamp>.log`) do sort lexically = chronologically, but a
  same-second collision suffix doesn't: `-2.log` sorts *before* the
  unsuffixed file it collided with (`-` is `0x2D`, `.` is `0x2E`),
  even though the `-2` file is the newer of the two — inverting which
  one rotation treats as oldest. Caught by `/code-review` on
  `aiform/log.py`'s twin implementation of this same rotation scheme
  (`specs/log.md`) and backported here so the "mirrors ... exactly"
  claim above stays true.

## Edge cases / errors

- **Both credentials missing, one missing, or one present but empty
  (`""`)** — `missing_credentials()` treats an empty string the same as
  absent (`env.get(var)` falsy check), naming every missing var in one
  message, not just the first.
- **`LOG_DIR` doesn't exist yet** (fresh checkout) — created on demand;
  this PR also commits `.aiform/testlog/.gitkeep` so the directory is
  present from a fresh clone without needing a first run to create it.
- **`LOG_DIR` has 10+ existing `*.log` files** — `rotate_logs` deletes
  the oldest down to 9 before this run's file is added, never leaving
  more than `MAX_LOG_FILES` total.
- **`LOG_DIR` has fewer than 9 existing `*.log` files, or none** —
  `rotate_logs` is a no-op; nothing to delete.
- **Two invocations within the same UTC second** (a quick Ctrl-C and
  rerun, or two automation triggers firing close together) — the
  timestamp alone isn't unique enough; `new_log_path` appends a
  counter suffix on collision (see Interface) so the second run gets
  its own file rather than truncating the first run's still-being-written
  log via `open(..., "w")`.
- **The underlying `pytest` invocation itself fails to start** (e.g. a
  broken environment) — `subprocess.run` surfaces that as a non-zero
  return code the same way a real test failure would; this script
  doesn't distinguish "pytest ran and failed" from "pytest couldn't
  run," since the log file has whatever `pytest` itself printed either
  way.

## Out of scope

- **Credential *sourcing*.** This script only checks presence in the
  environment — it never reads a keychain, prompts, or falls back to
  `.aiform/credentials.env` the way `aiform/config.py`'s
  `resolve_credentials()` does for the CLI's own DO calls. The human
  invoking this script is responsible for having both env vars already
  set (e.g. `DIGITALOCEAN_TOKEN=$(security find-generic-password ...)
  .venv/bin/python scripts/run_system_tests.py`), matching this
  project's existing "the user handles credential values directly,
  never a script" convention (`CLAUDE.md`'s credentials section).
- **Live terminal output while the suite runs.** Output goes to the log
  file only, not also to the terminal (`tee`-style dual output) — a
  person running this interactively who wants to watch progress live
  can `tail -f` the newest file in `LOG_DIR` themselves. Not done here
  to keep this pass minimal; a real, easy follow-up if wanted.
- **The orphan-cleanup sweep script** (`scripts/sweep_system_test_droplets.py`,
  `specs/system_test.md`'s "Orphan cleanup" section) — a separate,
  not-yet-implemented tool with a different purpose (catching leaked
  droplets, not running the suite).
- **Passing arguments through to `pytest`** (e.g. `-k` to filter which
  system-test case runs) — this script always runs the full suite;
  narrowing it is a real, easy follow-up if wanted, not designed here.

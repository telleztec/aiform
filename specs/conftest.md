# specs/conftest.md — `tests/conftest.py`

## Purpose

A permanent, general-purpose backstop that fails any test in the suite if
a real `DIGITALOCEAN_TOKEN`/`ANTHROPIC_API_KEY` value leaks into that
test's captured stdout/stderr or into a `.aiform/logs/*.log` file it
wrote. Closes the gap named in issue #55: the existing protections
(`tests/system/conftest.py`'s `RedactedSecret` wrapper and
`pytest_runtest_makereport` scrub hook) only cover `tests/system/`, only
fire on an already-failing test report, and never look at
`aiform/log.py`'s persistent file sink at all. This fixture is a second,
independent layer that applies to every test in the suite (unit and
system alike, since `tests/system/` is a subdirectory of `tests/`) and
checks a passing test's output too, not just a failure report.

## Interface

- `find_leaked_credential(secrets: dict[str, str | None], haystacks: list[str]) -> str | None`
  — pure matcher. Returns the env var name of the first configured,
  non-empty secret found as an exact substring of any haystack, in
  `secrets`' iteration order; `None` if none leaked. Extracted as a pure
  function (mirrors `aiform/log.py`'s `_rotate_logs`/`_new_log_path`) so
  the matching logic is unit-testable without a real pytest subprocess.
- `_scan_for_leaked_credentials` — `autouse=True`, function-scoped
  fixture, requests `capsys`. After each test, gathers haystacks (captured
  stdout/stderr plus every `.aiform/logs/*.log` file written by an
  `aiform.log.configure()` call during the test, via `_log_file_haystacks()`)
  and asserts `find_leaked_credential(...) is None`.
- `_log_file_haystacks() -> list[str]` — internal helper. Returns the
  decoded contents of every `*.log` file in the directory of
  `aiform.log._installed_file_handler` (the module-level handle
  `aiform/log.py`'s `configure()` sets on every call), or `[]` if
  `configure()` was never called during the test. Consumes what it
  reads: resets `aiform.log._installed_file_handler` to `None` as part
  of the same call, same drain-on-read contract as `capsys.readouterr()`.

## Behavior

- Real credential values are snapshotted once, at module import time
  (`_SECRETS = {var: os.environ.get(var) for var in _SECRET_ENV_VARS}`,
  `_SECRET_ENV_VARS = ("DIGITALOCEAN_TOKEN", "ANTHROPIC_API_KEY")`) —
  identical rationale to `tests/system/conftest.py`'s `_SECRETS`: a test
  that later `monkeypatch.setenv`s a fake value over one of these vars
  (e.g. `test_bad_token_fails_cleanly_without_leaking_or_tracking`) must
  not blind this check to the *real* value for its duration.
- If neither var is set in the real environment, the fixture is a no-op
  (nothing to check) — matches every unit test's actual environment
  today and issue #55's own sketch.
- Log-file discovery reads the directory from
  `aiform.log._installed_file_handler.baseFilename`, an **absolute**
  path resolved by the stdlib `FileHandler` at the moment
  `configure()` ran — not a `Path(".aiform/logs")` glob relative to the
  fixture's own cwd at teardown time. This matters concretely: this
  fixture is declared with no dependency on `tests/system/conftest.py`'s
  `project_dir` fixture (which `monkeypatch.chdir()`s into a tmp dir for
  the test's duration), and pytest tears down same-scope fixtures in
  reverse of their setup order — since this fixture is autouse it is
  set up *before* `project_dir` and therefore torn down *after* it, so
  by the time this fixture's post-yield code runs, `project_dir`'s
  `chdir` has already been reverted. A cwd-relative glob at that point
  would silently miss every log file from a system test — exactly the
  gap this fixture exists to close. Anchoring on the handler's own
  already-absolute path sidesteps the ordering issue entirely.
- `_log_file_haystacks()` resets `aiform.log._installed_file_handler` to
  `None` after reading it, in the same call. Without this,
  `_installed_file_handler` — a module-level global `_reset_aiform_logger`
  clears from the logger's handler list every test but never nulls out
  itself — would stay pointed at the last test that called `configure()`;
  every subsequent test that never calls it again would still have this
  fixture re-read (and, if it ever genuinely contained a leak, fail on
  behalf of) a prior test's already-checked log directory. Found via
  `/code-review` on this module's own PR.
- Multiple `aiform.log.configure()` calls within one test (e.g.
  `test_full_lifecycle`'s sequence of `init`/`plan create`/`plan
  apply`/`plan destroy`, each a separate `cli.main()` invocation) each
  write a new, distinctly-timestamped file into the same log dir
  (`_rotate_logs` notwithstanding, within `max_files`) — `glob("*.log")`
  on that directory picks up all of them, not just the last.
- A leak fails the test with a plain `assert`, naming which var leaked.
  Hard fail, not a warning: a false positive here means a test
  legitimately produced a value that happens to collide with a live
  secret, which is itself worth surfacing loudly rather than silently
  swallowing — and is expected to be rare in practice.

## Edge cases / errors

- A test that calls `capsys.readouterr()` itself before this fixture's
  post-yield code runs will have already drained whatever it read —
  this fixture only sees output captured *since* the last read. Known,
  accepted limitation (same one issue #55's own sketch flagged as an
  open question) rather than something this fixture works around; the
  `tests/system/conftest.py` layers (`RedactedSecret`,
  `pytest_runtest_makereport`) remain the primary defense for exactly
  this case, since they don't depend on an un-drained `capsys` buffer.
- `_log_file_haystacks()` returns `[]`, not an error, when
  `aiform.log.configure()` was never called during the test (the
  overwhelming majority of the unit suite) — `_installed_file_handler`
  is `None` in that case.

## Out of scope

- Fragment/truncation-aware matching (`tests/system/conftest.py`'s
  `_scrub()`/`_MIN_LEAKED_FRAGMENT` approach). That was built for a
  different purpose — sanitizing a report already known to contain a
  leak so it's safe to display — not for detecting one in the first
  place; exact-substring matching is sufficient for a detector, whose
  job is only to fail loudly, never to render the leak legibly.
- Scanning `.aiform/state.json`/`.aiform/state.json.backup`. Deliberately
  deferred: state's schema (`PLAN.md` §3) has no field that ever holds a
  raw credential value by design, so there's currently no code path that
  could put one there.
- Log rotation interaction: `_log_file_haystacks()` reads whatever
  `*.log` files are in the directory at teardown time; if a single test
  somehow triggered enough `configure()` calls to rotate an earlier file
  out (`LoggingConfig.max_files`), that file's content is unrecoverable
  by this fixture — not a realistic scenario for a single test today.

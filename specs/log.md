# specs/log.md — `aiform/log.py`

## Purpose

Closes `PLAN.md` §10's "Logging" item: one predictable log-line format
across `plan`/`apply`/`destroy`/`refresh`, covering both mechanical
driver calls and LLM-driven steps. The concrete gap this exists to
close: `aiform/llm.py`'s `_anthropic_call()` used to discard
`response.stop_reason`/`response.usage`, so a truncated response
(`max_tokens` reached mid-JSON) surfaced only as a downstream
`JSONDecodeError`/`ValidationError` with no hint the real cause was a
token budget.

Serves two distinct use cases, which turned out to need two
independent destinations, not one:

1. **Interactive** — a human watching a command run right now, who
   wants to see *some* of what's happening live without the terminal
   flooding.
2. **Non-interactive / IaC** — `aiform` invoked by a runner (CI, a test
   harness), where a human or machine inspects a *stored* log
   afterward for diagnostics, with nobody watching a terminal at all.

An earlier revision of this module only ever wrote to `sys.stderr`,
gated behind `--verbose` — it addressed use case 1 and left use case 2
with no durable record whatsoever, found only once a live system-test
run left nothing behind to diagnose it with. See "Two handlers, one
logger" below for the fix.

Built on stdlib `logging`, not a new dependency — every existing module
gets a logger the idiomatic way, `logging.getLogger(__name__)` (module
names already resolve to `aiform.llm`, `aiform.orchestrator`, etc.), so
this module's only job is formatting and wiring, not providing a
`get_logger()` wrapper.

Named `log.py`, not `logging.py`, to avoid shadowing the stdlib module
for any file that does `import logging` (Python 3's absolute-import
semantics would actually resolve this correctly even so, but the
filename is free real estate not worth spending on a collision risk
for zero benefit). Needs no `driver_gen.py` `RESERVED_MODULE_SPEC_NAMES`
entry — that set only guards spec filenames containing an underscore
(per that file's own comment), and `log.md` has none.

## Interface

```python
def configure(
    *,
    verbose: bool = False,
    stream: TextIO | None = None,
    logging_config: LoggingConfig | None = None,
    log_dir: Path | None = None,
) -> None: ...


def elapsed_ms(start: float) -> int: ...
```

`configure()`: attaches **two** independent `logging.Handler`s to the
`"aiform"` logger — every `aiform.*` child logger inherits both via
normal stdlib logger-hierarchy propagation. Called exactly once, from
`cli.py`'s `main()`, immediately after argument parsing, before any
subcommand dispatch. See "Two handlers, one logger" below for what
each one does.

- `logging_config`: injectable for testing, same pattern as `llm.py`'s
  `client`/`llm_config` params — a test constructs a `LoggingConfig`
  directly and passes it in; `main()` omits it and gets
  `config.resolve_logging_config()`'s real result. `None` (the
  default) resolves the real config **inside `configure()`**, not as a
  parameter default, for the identical reason `stream` does (below) —
  a test-time file read must not happen at import time.
- `log_dir`: injectable for testing so a test never writes into a real
  project's `.aiform/logs/`. `None` (the default) resolves to
  `Path(".aiform/logs")` inside the function body.
- `stream=None` (the default) resolves to `sys.stderr` **inside the
  function body**, not as a `= sys.stderr` parameter default — a
  default value is bound once, at module-import time, to whatever
  object `sys.stderr` happened to be at that moment. Anything that
  later replaces `sys.stderr` with a different object (pytest's
  `capsys` is the concrete case that surfaced this) would be writing
  to a stream this handler is no longer attached to, and the resulting
  output would be invisible to whatever captured the replacement —
  silently, since no exception occurs either way. Resolving it at call
  time inside `configure()` means it always picks up whichever
  `sys.stderr` is current when a command actually runs.

`elapsed_ms(start)`: `round((time.monotonic() - start) * 1000)` — the
one piece of arithmetic every `duration_ms=` field in this codebase
computes, factored out after `/code-review` flagged it duplicated
identically across five call sites (`llm.py`'s `_anthropic_call()`,
`orchestrator.py`'s `_call_driver()` twice, and the `update()` branch's
success/error paths). Each caller still calls `time.monotonic()` itself
to capture `start` — this only removes the duplicated back half of the
computation, not the timing itself.

## Two handlers, one logger

The `"aiform"` logger gets exactly two handlers, each with its own
independent severity floor — the idiomatic stdlib `logging` fan-out
pattern for "the same event needs to go to two places at two different
verbosities," not a bespoke mechanism:

| Handler | Destination | Level | Governed by |
|---|---|---|---|
| `logging.FileHandler` | `log_dir/<timestamp>.log` | `logging_config.level` (default `INFO`) | `.aiform/config.yaml`'s `logging:` key — **always attached**, independent of `verbose` |
| `logging.StreamHandler(stream)` | terminal (`stderr`) | `logging.WARNING`, or `logging.INFO` if `verbose=True` | `-v`/`--verbose` — unchanged from the original single-handler design |

The `"aiform"` logger's own level is set to
`min(file_level, stream_level)` so neither handler drops a record the
other one wants — each handler still applies its own threshold
independently via its own `setLevel()`. Both handlers share the same
`_KeyValueFormatter` — one line format, two destinations, not two
formats to keep in sync.

**`-v` is a live-echo toggle, not a severity filter.** Severity is
decided once, by `logging_config.level`, and applies uniformly to what
gets captured (the file, always). `-v` only widens what's
*additionally* mirrored to the screen right now, on top of what's
already always being written to disk — it does not change what the
file captures, and the file's completeness never depends on whether
whoever ran the command remembered to pass it.

## Behavior

- **Idempotent.** Calling `configure()` twice does not duplicate
  output — it detaches both handlers a *previous* `configure()` call
  installed (tracked via module-level references, not by scanning
  `logger.handlers` for a type match, so a handler installed by
  something else entirely is never accidentally removed) before
  attaching new ones. A second `configure()` call also creates a
  *new* log file (see rotation below) — it does not reopen or append
  to the file the first call created.
- **File sink: always attached, level from config, never gated on
  `verbose`.** Default `logging_config.level` is `INFO` — deliberately
  generous, not `WARNING`: these operations spend their time waiting
  on a CSP or an LLM API, not CPU-bound work, so the marginal cost of
  writing an extra line to a file that's already open is not a
  real consideration the way it might be in a hot loop. "No value in
  not logging" only has one carve-out — see "Busy-wait loops" below.
- **Stream sink (stderr): `logging.WARNING` by default, `logging.INFO`
  when `verbose=True`.** Unchanged from the original design, now
  understood as a live-echo toggle rather than a severity filter (see
  "Two handlers, one logger" above): a plain interactive run stays
  quiet except for genuinely actionable signals (a truncated-response
  warning, a driver error); `-v` widens the live view to match the
  file's full detail. No third `DEBUG` tier exists yet in the
  product's own output routing for either handler — not needed, not
  added speculatively (a test may still use
  `caplog.set_level(logging.DEBUG, ...)` to capture everything
  regardless of what `configure()` set, since `caplog` attaches its
  own handler independently of both of these).
- **`propagate = False`** on the `"aiform"` logger — cheap insurance
  against a future dependency that calls `logging.basicConfig()` and
  doubles output on the root logger. Irrelevant to `caplog`-based
  tests, which attach their own handler directly to `"aiform"`
  regardless of propagation.
- **One file per invocation, timestamp-named, rotated.** Mirrors
  `scripts/run_system_tests.py`'s `rotate_logs()`/`new_log_path()`
  exactly (same private-helper shape inside this module instead of a
  dev script, same collision-avoidance approach — that script's own
  comment already credits `orchestrator.py`'s `move_to_trash()` as the
  original source of the pattern):
  - Filename: `aiform-<UTC timestamp>.log`,
    `%Y%m%dT%H%M%SZ`-formatted, matching this codebase's other
    timestamp conventions. A same-UTC-second collision (two
    invocations in the same second) appends a numeric suffix
    (`-2`, `-3`, ...) rather than overwriting.
  - Rotation runs *before* the new file is created: existing `*.log`
    files in `log_dir`, oldest first, are pruned down to
    `logging_config.max_files - 1` entries, so that after the new
    file is written there are exactly `max_files` total (barring a
    concurrent writer — no locking, matching `PLAN.md` §10's existing
    "single local state file, no locking" MVP stance applied here
    too).
  - `log_dir` (default `.aiform/logs/`) is created
    (`mkdir(parents=True, exist_ok=True)`) if it doesn't exist yet.
  - The file is created **unconditionally**, even for an invocation
    that ends up logging nothing (e.g. `aiform plan show` on an empty
    state, which per `specs/cli.md` never touches the orchestrator at
    all) — "one file per session," consistently, is simpler than
    deferring creation until the first actual log call, and an
    occasional empty file costs nothing worth avoiding the complexity
    for.
- **Busy-wait loops are not logged per-iteration.** The one busy-wait
  in this codebase, `drivers/digitalocean/compute.py`'s
  `_poll_until()` (used by `create()`'s convergence poll and
  `update()`'s resize poll), is not itself instrumented — only the
  *outer* operation's start/end is logged, via
  `orchestrator.py`'s `_call_driver()` or the `update()` branch's own
  explicit calls. Logging each poll attempt would flood the file with
  zero diagnostic value beyond what the single start/end pair already
  gives; this is the one exception to "there's no value in not
  logging."
- **Line format**, one line per event, fully pinned:
  ```
  TIMESTAMP LEVEL logger_name          key=value key=value ...  msg="free text"
  ```
  - `TIMESTAMP`: `%Y-%m-%dT%H:%M:%SZ`, UTC, whole-second precision —
    matches `datetime.now(UTC)`'s formatting convention used everywhere
    else in this codebase. Multiple lines sharing a timestamp is
    expected and fine; each line's own `duration_ms` field (where
    present) disambiguates ordering/duration within that second.
  - `LEVEL`: stdlib's real levelno/levelname are untouched — only the
    *displayed* text for `WARNING` is remapped to `WARN` via
    `logging.addLevelName(logging.WARNING, "WARN")`, so all four
    displayed level strings (`INFO`, `WARN`, `ERROR`, `DEBUG`) are ≤5
    characters. Left-justified to width 5.
  - `logger_name`: left-justified to width 20 (fits
    `aiform.orchestrator`, the longest of this project's logger names,
    plus a separating space).
  - Extra fields (passed via a logging call's `extra={...}`) render as
    bare `key=value`, space-separated, in the order given. A `None`
    value **omits the key entirely** — `grep thinking_tokens=` then
    always means a real number when it matches, never a stringified
    `None`.
  - `msg`: `record.getMessage()`, double-quoted, appended last, and
    **omitted entirely when empty** — most log calls in this codebase
    pass `""` and put everything into `extra=`; only a genuinely
    free-text line (e.g. the truncation warning) carries a message.
    Embedded `"` characters are backslash-escaped; embedded newlines
    are replaced with a single space — pinned now, even though every
    current caller passes a fixed, developer-authored string, so the
    one-line-per-event guarantee holds if a future caller ever passes
    dynamic text.
- **`--no-color` does not affect log output** — a separate stderr
  channel from the colorized plan table `cli.py` prints; there are no
  ANSI codes in this format at all.

## Edge cases / errors

- `configure()` called with no prior `configure()` call installs fresh
  handlers normally — the "detach the previous ones" step is a no-op,
  not an error, when there is nothing to detach.
- A `msg` string containing both an embedded `"` and a newline applies
  both escaping rules together, in either order (they don't interact).
- An `extra=` dict with zero keys renders as just
  `TIMESTAMP LEVEL logger_name` with no trailing space before `msg=`
  (or nothing at all, if `msg` is also empty) — not a stray double
  space.
- `logging_config.level` resolves to `logging.WARNING` and the stream
  handler's `verbose=True` resolves to `logging.INFO` — the logger's
  own level is `min(logging.WARNING, logging.INFO)` = `logging.INFO`
  (lower numeric value = more permissive in stdlib `logging`), so the
  stream handler still gets everything it wants even though the file
  handler is configured more conservatively than usual. The two levels
  are never assumed to be in any particular order relative to each
  other.
- `log_dir` not existing yet (first-ever `aiform` invocation in a
  project) is not an error — created via `mkdir(parents=True,
  exist_ok=True)` before rotation runs.
- `log_dir` containing fewer than `max_files - 1` existing `*.log`
  files is not an error — rotation's prune step is a no-op when
  there's nothing to prune.

## Out of scope

- **A `redact()`/`_redact(d)` helper.** `PLAN.md` §8 names a future
  helper for `--verbose` output that dumps request/response payloads.
  No call site added in the PR that introduces this module logs a raw
  dict that could carry credentials or params — every logged field is
  a named scalar or a count, never `**params`/`**credentials`/free-text
  review content. Building this helper now, with no caller, would be
  exactly the speculative abstraction `CLAUDE.md` forbids. Named here,
  pointing at `PLAN.md` §8, so it isn't lost — the same "named here so
  it isn't lost" treatment `PLAN.md` itself uses for deferred items.
- **Logging the free-text content of an LLM decision** — a
  driver review's `concerns`/`blocking_issues`, or a plan review's
  `flag.concern` text. Call sites log only counts
  (`concerns_count=<n>`, `flags_count=<n>`) and booleans
  (`approved=<bool>`, `safe_to_proceed=<bool>`). The actual text still
  reaches the human via `cli.py`'s existing `_print_apply_result`
  `review_flags` print loop — logging doesn't duplicate it.
- **A third `DEBUG` output tier in the product's own level routing.**
  `DEBUG` is a legal `logging_config.level` value (`specs/models.md`'s
  `LoggingConfig`), so a user *can* configure the file handler down to
  it, but no product code path currently emits a `logger.debug(...)`
  call — configuring `level: DEBUG` today would not surface anything
  finer than `INFO` already does, since there's nothing lower to
  capture yet.
- **JSON Lines / structured machine-parseable output.** Considered and
  explicitly rejected in favor of the plain-text `key=value` format
  above — greppable without `jq`, readable in an interactive terminal,
  consistent with Terraform's own `TF_LOG` convention. Revisiting this
  would be a new format decision, not an extension of this spec.
- **Pruning/compacting `.aiform/logs/` beyond the `max_files` rotation
  cap** — no size-based rotation, no compression of old files, no
  cross-project cleanup. Same "cheapest possible mitigation, not a
  real history/rollback mechanism" stance `PLAN.md` §10 already takes
  for `.aiform/state.json.backup` and `.aiform/trash/`.
- **Locking against concurrent `aiform` invocations writing to the
  same `log_dir`.** Two simultaneous invocations could both compute
  the same rotation snapshot and both prune/write independently —
  matches `PLAN.md` §10's existing "single local state file, no
  locking" MVP stance, extended here rather than solved fresh.

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
def configure(*, verbose: bool = False, stream: TextIO | None = None) -> None: ...
def elapsed_ms(start: float) -> int: ...
```

`configure()`: attaches a single `logging.StreamHandler(stream)`,
formatted by this module's private `Formatter`, to the `"aiform"`
logger — every `aiform.*` child logger inherits it via normal stdlib
logger-hierarchy propagation. Called exactly once, from `cli.py`'s
`main()`, immediately after argument parsing, before any subcommand
dispatch.

`elapsed_ms(start)`: `round((time.monotonic() - start) * 1000)` — the
one piece of arithmetic every `duration_ms=` field in this codebase
computes, factored out after `/code-review` flagged it duplicated
identically across five call sites (`llm.py`'s `_anthropic_call()`,
`orchestrator.py`'s `_call_driver()` twice, and the `update()` branch's
success/error paths). Each caller still calls `time.monotonic()` itself
to capture `start` — this only removes the duplicated back half of the
computation, not the timing itself.

`stream=None` (the default) resolves to `sys.stderr` **inside the
function body**, not as a `= sys.stderr` parameter default — a default
value is bound once, at module-import time, to whatever object
`sys.stderr` happened to be at that moment. Anything that later
replaces `sys.stderr` with a different object (pytest's `capsys` is
the concrete case that surfaced this) would be writing to a stream
this handler is no longer attached to, and the resulting output would
be invisible to whatever captured the replacement — silently, since no
exception occurs either way. Resolving it at call time inside
`configure()` means it always picks up whichever `sys.stderr` is
current when a command actually runs.

## Behavior

- **Idempotent.** Calling `configure()` twice does not duplicate
  output — it detaches any handler a *previous* `configure()` call
  installed (tracked via a module-level reference, not by scanning
  `logger.handlers` for a type match, so a handler installed by
  something else entirely is never accidentally removed) before
  attaching the new one.
- **Level: `logging.WARNING` by default, `logging.INFO` when
  `verbose=True`.** This is a real behavior decision, not an
  arbitrary default: `INFO`-by-default would make every `plan
  create`/`apply`/`destroy` invocation unconditionally emit one line
  per LLM call and one per driver call to stderr, even with no `-v` —
  a user-visible change to the tool's default output, not merely
  "additive" logging alongside existing behavior. `WARNING`-by-default
  keeps a plain run silent except for genuinely actionable signals (a
  truncated-response warning, a driver error); `-v` promotes to `INFO`
  for the full call-level trail. Matches this project's existing
  `--verbose` discipline (quiet by default, detail on request). No
  third `DEBUG` tier exists yet in the product's own output routing —
  not needed, not added speculatively (a test may still use
  `caplog.set_level(logging.DEBUG, ...)` to capture everything
  regardless of what `configure()` set, since `caplog` attaches its own
  handler independently of this one).
- **`propagate = False`** on the `"aiform"` logger — cheap insurance
  against a future dependency that calls `logging.basicConfig()` and
  doubles output on the root logger. Irrelevant to `caplog`-based
  tests, which attach their own handler directly to `"aiform"`
  regardless of propagation.
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

- `configure()` called with no prior `configure()` call installs a
  fresh handler normally — the "detach the previous one" step is a
  no-op, not an error, when there is nothing to detach.
- A `msg` string containing both an embedded `"` and a newline applies
  both escaping rules together, in either order (they don't interact).
- An `extra=` dict with zero keys renders as just
  `TIMESTAMP LEVEL logger_name` with no trailing space before `msg=`
  (or nothing at all, if `msg` is also empty) — not a stray double
  space.

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
  Only `WARNING` (default) and `INFO` (`--verbose`) are wired to
  `configure()`'s `verbose` flag. `logging.DEBUG` exists as a stdlib
  level and is used by tests (`caplog.set_level(logging.DEBUG, ...)`
  captures everything regardless of the handler's own threshold), but
  no product code path currently sets the handler to it.
- **JSON Lines / structured machine-parseable output.** Considered and
  explicitly rejected in favor of the plain-text `key=value` format
  above — greppable without `jq`, readable in an interactive terminal,
  consistent with Terraform's own `TF_LOG` convention. Revisiting this
  would be a new format decision, not an extension of this spec.

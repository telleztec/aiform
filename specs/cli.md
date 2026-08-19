# specs/cli.md — `aiform/cli.py`

## Purpose

The `aiform` command-line surface (`PLAN.md` §7, minus the `driver ...`
subcommands, which are deferred — see Out of scope): `init`, `plan
create`, `plan apply`, `plan destroy`, `plan refresh`, `plan show`.
Argument parsing, plan/state output formatting, error-message
formatting, and process exit codes all live here — `orchestrator.py`
does none of this (`specs/orchestrator.md`'s "Out of scope": "All CLI
argument parsing, output formatting/printing... `cli.py`, not built
yet"). `aiform/__main__.py` is the one-line `python -m aiform` entry
point that calls `cli.main()`.

## Interface

```python
def main(argv: list[str] | None = None) -> int: ...
```

`argv=None` → `sys.argv[1:]` (argparse's own default). Returns a
process exit code; `aiform/__main__.py` is `sys.exit(main())`. Never
calls `sys.exit()` itself — kept a plain function so tests call it
directly and assert on the return value plus captured stdout/stderr,
without a `SystemExit` to catch.

Everything else in this module (the argparse construction, the five
per-subcommand handlers, the plan/state/apply-result printers, the
verbose Anthropic-call counter) is a private implementation detail of
`main()`, not a public interface another module imports — `cli.py` is
a leaf: every other `aiform/*.py` module is called *by* this one,
nothing calls back into it. Tests exercise it exclusively through
`main(argv)` plus `capsys`, the same way a real invocation would.

## Behavior

### Global flags, and where they attach

`-v`/`--verbose`, `--no-color` are accepted both before and after the
subcommand token (`aiform -v plan create` and `aiform plan create -v`
both work) via a shared argparse parent parser attached at every level.
`--state-file PATH` (default `state.DEFAULT_STATE_PATH`, i.e.
`.aiform/state.json`) is accepted on every subcommand that touches
state (`create`/`apply`/`destroy`/`refresh`/`show`) — not on `init`,
which never reads or writes state.

### `aiform init [--provider digitalocean]`

- `--provider` must name a provider `config.PROVIDER_TOKEN_ENV_VARS`
  knows about (today: only `digitalocean`) — an unrecognized value is a
  clean `Error: ...` (exit 2), not a stack trace. Deliberately reads the
  supported-provider set from `config.py` rather than hardcoding it a
  second time here, so a future second provider doesn't need a matching
  edit in this module.
- Creates `.aiform/` (`mkdir(parents=True, exist_ok=True)`) — **never**
  `.aiform/credentials.env` itself, and never with any credential value
  or interactive prompt (`PLAN.md` §8, `CLAUDE.md`'s Credentials
  section): the user creates that file by hand.
- Appends the standard entries (`PLAN.md` §8) to the repo-root
  `.gitignore` if missing — `.aiform/credentials.env`,
  `.aiform/state.json`, `.aiform/state.json.backup`, `.env`,
  `__pycache__/`, `*.pyc`. Idempotent: a line already present (exact
  string match) is not duplicated on a second `init` run. Creates
  `.gitignore` if it doesn't exist yet. Deliberately **excludes**
  `.aiform/trash/` — `PLAN.md`'s "Trash directory" section states it is
  "Not gitignored."
- Writes `examples/compute.aiform.md` (creating `examples/`) **only**
  when it doesn't already exist — never overwrites a user's edited
  starter file on a repeat `init`. Only written for `--provider
  digitalocean` (the only driver that exists); an unsupported provider
  already failed above, so this is unconditional once past the check.
- Prints instructions: how to set `ANTHROPIC_API_KEY` (environment
  variable, never a CLI flag or file) and the resolved provider's token
  env var (`config.PROVIDER_TOKEN_ENV_VARS[provider]`) — env var first,
  else hand-created `.aiform/credentials.env` at
  `config.DEFAULT_CREDENTIALS_PATH`, in `KEY=value` form. Never
  scaffolds that file with a value, never prompts for one interactively
  — same rule stated three times over in `CLAUDE.md`/`PLAN.md` for a
  reason.
- **"Verifies that the credentials work" (`PLAN.md` §7) is narrowed
  here exactly the way `specs/orchestrator.md`'s judgment call 3
  narrows the identical phrase for `plan create`'s step 3**: there is
  no side-effect-free way to confirm a token is *valid* against either
  API without spending a real call, so this command instead reports,
  per credential, only whether something is *configured* at all —
  `ANTHROPIC_API_KEY` via `os.environ.get`, the provider token via
  `config.resolve_credentials(provider)` succeeding or raising
  `RuntimeError`. Printed as a `✓`/`✗` pair; **never fails `init`
  itself** on a `✗` — a brand-new project legitimately hasn't set
  credentials up yet, and `init`'s job is to scaffold and inform, not
  gate. A real flagged divergence from `PLAN.md`'s literal "verifies
  that the credentials work," same treatment as the orchestrator spec's
  judgment call 3.
- Exit 0 on a successful scaffold (regardless of the credential
  check's ✓/✗ outcome); exit 2 on an unsupported `--provider`.

### `aiform plan create [FILE.aiform.md ...] [--state-file PATH] [--json]`

- `files` (positional, `nargs="*"`) → `None` when empty, so
  `orchestrator.build_create_plan` falls through to its own
  cwd-glob discovery, exactly matching `PLAN.md` §5 step 1's
  "default: all `*.aiform.md` in cwd."
- Calls `orchestrator.build_create_plan(paths, state_path=..., client=<counting client>)`
  (see "The verbose Anthropic-call counter" below).
- Default (non-`--json`) output: one line per planned resource in a
  Terraform-style summary (`+`/`~`/`-`/`=` for
  create/update/destroy/no-op) followed by its rationale, then a
  one-line tally (`N to create, N to update, N to destroy, N no-op.`),
  then any warnings (`PLAN.md` §5's "left alone... reported with a
  warning" case) each on their own line. `update` entries additionally
  print `(likely replace)` when `entry.likely_replace` is set.
- `--json`: prints `{"plan": [...], "warnings": [...]}` instead, one
  `{"resource_key", "action", "rationale", "likely_replace"}` object
  per planned resource, `warnings` as given by `build_create_plan`.
  Nothing else is printed to stdout in this mode (the verbose call
  count, if requested, still goes to stderr — see below — so `--json
  --verbose` output stays parseable).
- Exit 0 on a successfully printed plan (regardless of what it
  contains — a plan showing destroys is not itself an error). Exit 2
  on `PlanBlockedError`/`DriverExecutionError`/`ValueError`
  (`parser`/pydantic validation failures propagate as `ValueError`
  subclasses, per `specs/orchestrator.md`'s "Edge cases")/
  `FileNotFoundError` (an explicitly-named file that doesn't exist) —
  `main()`'s shared error formatting, see below.

### `aiform plan apply [FILE.aiform.md ...] [--yes] [--state-file PATH]`

Re-plans in full immediately before executing — `specs/orchestrator.md`'s
"Out of scope" names this as `cli.py`'s job (`apply_plan()` only ever
takes an already-built plan):

1. `orchestrator.build_create_plan(...)`, same as `plan create`, using
   the **same** counting client as step 2 (one running tally covers the
   whole `apply` invocation, not two separate ones).
2. Prints the plan the same way `plan create` does (no `--json` option
   here — `PLAN.md` §7 doesn't list one for `apply`), so the user sees
   what's about to happen before any confirmation prompt.
3. `orchestrator.apply_plan(planned, state_path=..., yes=args.yes, confirm=_confirm, client=<same counting client>)` —
   `_confirm` is this module's own confirmation function (see "Confirmation and
   non-interactive runs" below), always passed regardless of `--yes`,
   since `apply_plan`'s single-resource `DriverUpdateNotSupported`
   fallback confirmation is never skippable by `yes=True`
   (`specs/orchestrator.md` judgment call 7) and needs a sane behavior
   if that path is hit with no TTY attached.
4. Prints the `ApplyResult`: one line per executed `PlanEntry`
   (`resource_key: action` — a replace is reported as `update (replaced)`
   when `likely_replace` is `True` on the returned entry, distinguishing
   it from a plain in-place update), then any non-blocking
   `review_flags` (`resource_key: concern [severity]`), then, if
   `aborted`, a final `Apply aborted.` line.
- Exit 0 if `apply_plan` returns `aborted=False`. Exit 1 if
  `aborted=True` (the user declined, or a mid-loop replace confirmation
  declined — a legitimate, non-exceptional outcome, but not "success"
  for a script's purposes). Exit 2 on the same exception set `plan
  create` uses, from either the planning or the apply call.

### `aiform plan destroy [FILE.aiform.md ...] [--yes] [--state-file PATH]`

Mechanism A (`PLAN.md` "Resource deletion"): plans and applies in one
pass, unconditionally subject to gate #2 by construction (every entry
`build_destroy_plan` produces is `action=DESTROY`, and `apply_plan`'s
`needs_review` is true whenever any entry is a destroy).

1. `orchestrator.build_destroy_plan(paths, state_path=...)` — no `client`
   parameter (`build_destroy_plan` never calls an LLM — Mechanism A
   skips categorization entirely, `specs/orchestrator.md`).
2. Prints the plan the same way `plan create`/`apply` do.
3. `orchestrator.apply_plan(planned, state_path=..., yes=args.yes, confirm=_confirm, client=<counting client>)` —
   the counting client is still passed here even though step 1 made
   no LLM calls, since `apply_plan` itself may (gate #2's batch review
   always fires for a destroy plan).
4. Same `ApplyResult` printing as `plan apply`.
- Same exit-code convention as `plan apply` (0 / 1 aborted / 2 error).

### `aiform plan refresh [--state-file PATH]`

- `orchestrator.refresh_state(state_path=...)` — no `client` argument
  passed or accepted here (`refresh_state` takes none; `PLAN.md` §7:
  "no LLM calls at all").
- Prints the refreshed `State` the same way `plan show` does (see
  below) — this command differs from `show` only in that it calls
  `refresh_state()` (which also durably writes the refreshed
  attributes) instead of `state.load()`.
- Exit 0 on success, 2 on `PlanBlockedError`/`DriverExecutionError`
  (a missing driver or bad credential for a tracked resource).

### `aiform plan show [--state-file PATH]`

- `state.load(args.state_file)` directly — no orchestrator
  involvement at all (`specs/orchestrator.md`'s "Out of scope": "`plan
  show`... needs no orchestrator involvement... a direct `state.load()`
  plus formatting, entirely in `cli.py`").
- Prints, per tracked resource: `resource_key`, `id`, `attributes`
  (pretty-printed JSON), driver `path` and a short (12-character)
  prefix of `sha256`, `last_applied_at`/`last_refreshed_at`, and
  `aiform_md_path`. An empty `state.resources` prints a single `no
  resources tracked` line rather than an empty table.
- Exits 0 on a successfully loaded, printed state. A corrupt or
  schema-violating `state.json` is **not** a special case this command
  adds — but it isn't uncaught either: `state.load`/pydantic's
  `ValidationError` (a `ValueError` subclass) and a malformed file's
  `json.JSONDecodeError` (likewise) both fall through to `main()`'s
  shared `_HANDLED_EXCEPTIONS` handling (see "Error formatting and exit
  codes" below) the same as every other command's operational errors —
  printed as `Error: ...` on stderr, exit 2. Corrected from an earlier
  draft of this spec that claimed these propagate uncaught: `main()`'s
  error handling is a single, command-agnostic `try`/`except` around
  all dispatch, so no per-command carve-out like that is actually
  possible without deliberately narrowing that `except` clause, which
  this module doesn't do — a clean, consistent error message beats an
  uncaught traceback here, same as everywhere else in this module.

### Confirmation and non-interactive runs

`_confirm(prompt: str) -> bool` is this module's confirmation callback,
passed to every `apply_plan()` call — **not** `orchestrator.default_confirm`.
It checks `sys.stdin.isatty()` first: if `False`, raises `RuntimeError`
naming the prompt and telling the user `--yes` is required for
non-interactive runs, **without ever calling `input()`**. If `True`,
delegates to the same `y`/`N` `input()` prompt `orchestrator.default_confirm`
uses. This exists specifically for `specs/orchestrator.md` judgment call
7's scenario: a fully non-interactive `--yes` run that still hits the
single-resource `DriverUpdateNotSupported` fallback confirmation (never
skippable by `--yes`) needs to fail cleanly instead of hanging forever
on `input()` with no TTY attached to answer it. The `RuntimeError` this
raises is caught by `main()`'s shared error handling (exit 2), same as
any other operational error.

### The verbose Anthropic-call counter

`CLAUDE.md`'s implementation-order section calls out, by name, verifying
`PLAN.md`'s "second `plan create` run makes zero Anthropic API calls"
claim with "`--verbose` logging or a request counter... don't just
assume it." This module implements exactly that, narrowly — not the
fuller, not-yet-designed logging system `PLAN.md` §10 names as a future
item (line format, log levels, output routing are all still open
questions there; this feature answers one specific question — "how many
model calls did this invocation make" — and nothing broader):

- A private `_CountingClient` wraps the real `anthropic.Anthropic()`
  SDK client, exposing the same `.messages.create(**kwargs)` shape
  `llm._anthropic_call` actually calls (duck-typed — `llm.py` never
  imports or checks against a stricter interface than that). It
  increments an internal counter on every `.create()` call and only
  constructs the real `anthropic.Anthropic()` **lazily, on the first
  such call** — never at `_CountingClient()` construction time.
- This laziness is load-bearing, not an optimization: constructing
  `anthropic.Anthropic()` eagerly reads `ANTHROPIC_API_KEY` at
  construction, which would make a truly zero-call `plan create` run
  newly *require* that variable to be set — silently regressing the
  exact cost/environment-footprint property this counter exists to
  verify. `llm._anthropic_call` already gets this right for the same
  reason (`client = anthropic.Anthropic()` only inside the function
  actually making a call); `_CountingClient` preserves it end to end.
- One `_CountingClient` instance is created per CLI invocation that
  might need it (`create`/`apply`/`destroy` — never `init`/`refresh`/`show`,
  none of which ever call an LLM) and passed as every relevant
  orchestrator call's `client=` argument, so one running total covers
  the whole invocation (e.g. `apply`'s re-plan step and its execute
  step share one count, per that subcommand's Behavior entry above).
- When `--verbose` is set, the final count is printed to **stderr**
  (`[verbose] N Anthropic API call(s) made`) after the command's normal
  stdout output — stderr specifically so `--json --verbose` together
  still yield parseable stdout, and so `--verbose` never changes a
  command's exit code or its stdout contents.
- **This mechanism is unchanged by, and stays completely separate
  from, the structured logging described below** — it predates
  `specs/log.md` and continues to answer its own one narrow question
  ("how many model calls did this invocation make"), not folded into
  the newer system.

### Structured logging (`specs/log.md`)

`main()` calls `log.configure(verbose=args.verbose)` immediately after
argument parsing, before any subcommand dispatch — the only wiring this
module does; every other module's own logger (`aiform.llm`,
`aiform.orchestrator`, `aiform.planner`, `aiform.parser`,
`aiform.driver_gen`) does its own logging independently once this is
called. **Additive, not a replacement**, for this module's existing
`print()`-based human-facing output (`_print_plan`/
`_print_apply_result`/`_print_state`) and for the verbose Anthropic-call
counter above — logging is a parallel stderr channel for
debugging/operational visibility, not a UX replacement; nothing in this
module's existing print-based output changes.

One further, in-scope addition beyond pure wiring: `main()`'s own
exception handler (see "Error formatting and exit codes" below) also
calls `logger.error(...)` — naming the exception type and its
formatted message — immediately alongside the existing `Error: ...`
stderr `print`, not instead of it. This is the single most likely place
a real user hits a failure during `plan`/`apply`/`destroy`/`refresh`,
and structured logging covering it was part of the point of building
this module at all.

### Error formatting and exit codes

`main()` wraps subcommand dispatch in one `try`/`except` over a fixed,
named set of "expected, operational" exception types —
`PlanBlockedError`, `DriverExecutionError`, `ValueError` (covers
pydantic's `ValidationError`, a `ValueError` subclass), `FileNotFoundError`,
`RuntimeError` (only ever actually raised here by `_confirm`'s no-TTY
case, since `config.resolve_credentials`'s `RuntimeError` is already
translated to `PlanBlockedError` before it reaches this module, per
`specs/orchestrator.md`) — printing `Error: <message>` to stderr and
returning exit code 2. `PlanBlockedError.reason` is used as the message
verbatim (its `str()` is the same text, but going through `.reason`
documents the intent); every other caught type uses `str(exc)`.
**Anything else propagates uncaught** — an unrecognized exception is a
real bug, not a formattable operational condition, matching this
codebase's consistent "let it fail loudly" stance
(`specs/orchestrator.md`, `specs/parser.md`, `specs/planner.md`) rather
than papering over it with a generic `except Exception`.

## Edge cases / errors

- `argparse`'s own error handling (missing required subcommand, unknown
  flag) is used as-is — it prints its usage message to stderr and calls
  `sys.exit(2)` itself, *inside* `parser.parse_args()`, before `main()`'s
  `try` block is even reached. Not reimplemented or caught here.
- `plan show`/`plan refresh` on a `--state-file` that doesn't exist yet:
  `state.load()` returns an empty `State()` (its own documented
  behavior, `specs/state.md`) rather than raising — printed as `no
  resources tracked`, exit 0. Consistent with `state.load`'s existing
  contract; `cli.py` adds no special-case here.
- A `plan create --json` run that also hits a warning (a tracked
  resource left alone) still reports it, inside the JSON envelope's
  `"warnings"` array — `--json` changes the plan's own representation,
  not whether warnings are surfaced at all.
- `init` run a second time in the same directory: `.gitignore` entries
  aren't duplicated, `examples/compute.aiform.md` isn't overwritten,
  `.aiform/` already existing is fine (`exist_ok=True`) — every step is
  independently idempotent, so re-running `init` is always safe.

## Out of scope

- **Every `aiform driver ...` subcommand** (`create`/`refresh`/`show`/
  `delete`/`publish`) — `PLAN.md` §7 lists these under its explicit "Not
  yet implemented... driver_gen.py implements only the minimal
  draft/validate/review pipeline these commands are meant to grow into"
  banner; §10 confirms none of the interactive session shape is
  designed yet. Not part of this module.
- **The fuller logging system** named in `PLAN.md` §10's "Logging" item
  is now designed and wired (`specs/log.md`, "Structured logging"
  above) — this bullet is left here, corrected, rather than deleted, so
  the history of "this was once explicitly out of scope" isn't lost.
  What remains genuinely out of scope: a third `DEBUG` output tier
  (`specs/log.md`'s own Out of scope), and a `redact()`/`_redact()`
  helper for dumping raw request/response payloads under `--verbose`
  (`PLAN.md` §8) — no call site in this module logs one.
- **Colorized/`--no-color` output beyond plain ANSI codes on the plan's
  action markers** (`+`/`~`/`-`/`=`) — no theming, no configurability
  beyond the on/off switch `PLAN.md` §7 already names.
- **A `--json` mode for `apply`/`destroy`/`refresh`/`show`** — `PLAN.md`
  §7 only lists `--json` under `plan create`; not extended to the other
  commands here.
- **Concurrent-invocation safety** — `PLAN.md` §10's "Single local state
  file, no locking" limitation is orchestrator/state-level and applies
  unchanged here; this module adds no locking of its own.

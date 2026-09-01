# CLAUDE.md — Development guidelines for aiform

This file is written for the next Claude Code instance picking up this
project cold. Read it before writing any code.

## Source of truth

**`PLAN.md` is the architecture spec.** It was produced by a dedicated
design pass (research into Terraform's actual pain points, prior-art check,
explicit user decisions on runtime/state/scope/model-tiering) and reviewed.
Don't redesign from scratch — implement what's there. If something in
`PLAN.md` turns out to be wrong or incomplete once you're actually building
it, that's expected (it's a design doc, not a spec written by staring at the
finished system) — flag the discrepancy and propose the change explicitly
rather than silently diverging from it.

**`PROCESS.md` is the development workflow spec** — the how, as opposed to
`PLAN.md`'s what. Every module gets built spec-first, test-first (red
before green), then reviewed via `/code-review` (Opus 5 or newer) before
it's a PR.
`.claude/skills/tdd-workflow/SKILL.md` operationalizes this loop; use it
whenever starting or resuming work on a module. Per-module specs live in
`specs/`, one file per module — see `specs/README.md` for the format.

Current status: **MVP walkthrough end to end.** `pyproject.toml`,
`aiform/models.py`, `state.py`, `config.py`, `llm.py`, `log.py`,
`exceptions.py`, `driver.py`, `driver_gen.py`, `parser.py`, `planner.py`,
`orchestrator.py`, `cli.py`, `__main__.py`, and
`drivers/digitalocean/compute.py` are all written, and `python -m aiform`
exposes `init` plus `plan create`/`apply`/`destroy`/`refresh`/`show`.
The "Suggested implementation order" below is now a record of how it was
built, not a list of what's left.

**On-the-fly driver generation is abandoned, not pending.** A missing
`(provider, resource)` driver is a permanent error — `plan` never
generates one, and no future version will. `driver_gen.py` exists and is
tested, but **no code path calls it**; it is retained as the earliest
building block of the deliberate `aiform driver create` flow
(`PLAN.md`'s mechanism 2), which is a committed direction that **nobody
is building right now**. Today a driver comes into existence exactly one
way: a developer on this repo hand-authors it through `PROCESS.md`'s
loop. Don't treat `driver_gen.py`'s lack of a caller as a gap to close —
see `PLAN.md`'s "Driver curation" section.

## Non-negotiable design rules

These aren't stylistic preferences — they're the properties that make the
whole "AI-driven but bounded-cost" premise hold together. Don't relax them
to make something easier to build.

### Model tiering
- Four roles, each **independently** configurable via `.aiform/config.yaml`
  (`resolve_llm_config()` in `aiform/config.py`, `LLMConfig` in
  `aiform/models.py`) rather than hardcoded constants — see `specs/llm.md`
  and `specs/config.md`. The MVP default — and the only model source
  implemented right now — is Anthropic (`ModelSource.ANTHROPIC`) for all
  four. This is deliberate: as model capability and pricing change over
  time, a user (or a future default-tuning pass) adjusts `.aiform/config.yaml`
  per role, not the code — see `PLAN.md`'s "Model tiering" section for the
  full rationale and the mapping from each role to the prompt file(s) it
  drives.
- **`intent-orchestration-model`**, default **Claude Sonnet 5**
  (`claude-sonnet-5`): parses the prose Intent section into `intent_notes[]`
  and categorizes plan actions (create/update/no-op) against the raw diff.
  Everything routine and repeated on the `plan` hot path runs through this
  role — it's the one that must cost zero tokens on an unchanged second run.
- **`code-generator-model`**, default **Claude Sonnet 5** (`claude-sonnet-5`):
  drafts a new resource driver's Python source. Exercised only by
  `aiform/driver_gen.py`, which nothing calls; reserved for the
  deliberate `aiform driver create` flow, not reachable from a normal
  `plan`/`apply` — see `PLAN.md`'s "Driver curation".
- **`code-review-model`**, default **Claude Opus 5** (`claude-opus-5`):
  gate #1 — approving a driver before it's trusted for reuse. Live today
  in one case only: the `plan`-time re-review of an on-disk driver whose
  hash doesn't match its trusted record. It is also the gate a draft
  passes through inside `driver_gen.py`.
- **`review-orchestration-model`**, default **Claude Opus 5**
  (`claude-opus-5`): gate #2 — reviewing a plan before `apply` executes
  anything destructive.
- Do not change any of the four *defaults* for cost reasons without asking
  first. This split was chosen deliberately after an explicit
  cost/capability tradeoff discussion — it's not a default that happened to
  be picked. A user overriding their own `.aiform/config.yaml` is an
  intentional escape hatch, not a violation of this rule — don't add a
  second, uninstructed override of your own.
- These four roles are distinct from — and not configured the same way
  as — the `/code-review` gate this project's own build process
  (`PROCESS.md`) runs against every module's PR, including a curated
  driver's. That gate runs on Opus 5 or newer, never on the model that
  authored the diff, and it is recorded on the PR as the `llm-review`
  status. It's a development-time tool for building `aiform` itself, not
  one of `aiform`'s own runtime roles; `PROCESS.md` explains why the two
  are deliberately not the same mechanism.
- Adding a new model source (e.g. Bedrock) is a `MODEL_SOURCES` dispatch-table
  entry in `aiform/llm.py`, not a reason to introduce a plugin system or ABC
  hierarchy — keep it to that one seam.
- On repeat `apply`/`plan` runs against unchanged input, the execution path
  must make **zero** Anthropic API calls (see `PLAN.md` §5 step 5, §8 step 4).
  If you find yourself adding an LLM call inside the driver-execution path,
  stop — that call belongs in the planning phase, not here.

### Credentials
- `aiform/llm.py` (every model call, regardless of configured source) must
  **never** have a `credentials` parameter, local variable, or import
  anywhere in it. This is meant to be literally grep-verifiable:
  `grep -n credentials aiform/llm.py` should return nothing, ever. All
  credential-bearing code lives in `orchestrator.py`'s driver-execution path.
- `ANTHROPIC_API_KEY` — env var only, never a CLI flag. Which *model* to call
  is separate from this and lives in `.aiform/config.yaml` — a model name
  isn't a secret, don't conflate the two files.
- `DIGITALOCEAN_TOKEN` — env var first, else `.aiform/credentials.env`
  (dotenv-style). `aiform init` prints instructions for creating this file
  but **never** writes a value into it or prompts for the token
  interactively — the user creates it by hand with a text editor. Why:
  a value typed directly into a file by the user never passes through
  terminal echo, shell history, or the output of any command — including
  one an AI agent is driving. This pattern was proven on a separate local
  project (`~/src/telleztec-infra`, using Terraform's equivalent
  `terraform.tfvars`) before being adopted here; don't reinvent it
  differently, but don't treat that other repo as required reading either
  — the reasoning above is the actual rule.
  On macOS there is now a third path, `.envrc` (direnv), which exports both
  `DIGITALOCEAN_TOKEN` (from Keychain entry `DIGITALOCEAN_TOKEN_AIFORM`) and
  `ANTHROPIC_API_KEY` on `cd` into this repo. It exists because a *global* export makes one project's
  token ambient in every shell, and this machine has two DigitalOcean projects
  on different accounts. State the tradeoff honestly rather than pretending
  there isn't one: under `.aiform/credentials.env` the token was read only by
  aiform's own process, whereas under direnv it is
  in the environment of **every** process started in this directory -- pytest,
  pip install hooks, editor tasks, every command an AI agent runs here. That is
  a materially wider surface. It buys, in exchange, that the token which *is*
  present is the right account's -- but only for processes descended from an
  interactive shell with direnv's prompt hook installed. It does *not* fire for
  `bash script.sh`, `ssh host 'cd ~/src/aiform && ...'`, `(cd ~/src/aiform &&
  pytest)`, cron, or an agent session whose environment was populated
  elsewhere; those inherit whatever the parent had, and `resolve_credentials()`
  reads the env var before the file. The window is narrowed, not closed. Note also that direnv's `DIRENV_DIFF` carries
  shadowed values, so a global `DIGITALOCEAN_TOKEN` or `ANTHROPIC_API_KEY` must not coexist with it --
  see the comment block in `.envrc`.
- A driver (`drivers/<provider>/<resource>.py`) that imports `anthropic` or
  reads `ANTHROPIC_API_KEY` is a hard failure at `code-review-model` review
  time (gate #1) — this is explicitly one of the review checklist items in
  `prompts/review_driver.md`.

### State handling
- Write `.aiform/state.json.backup` before every overwrite of
  `.aiform/state.json`.
- Refresh before diff: call `driver.read()` for tracked resources before
  computing a diff, every time, even on a plain `plan` with no changes
  expected. State is a cache of live reality, not a source of truth in
  itself.
- The no-op short-circuit (`PLAN.md` §5 step 5) — deterministic dict-diff
  first, only call the `intent-orchestration-model` when there's something
  to actually interpret — is what keeps `plan` cheap on unchanged input.
  Don't route the diff step through an LLM call unconditionally "for
  simplicity."

## Coding conventions

- Formatting and lint are `ruff format`/`ruff check`, enforced by a local
  `pre-commit` hook and again in CI — don't hand-format against a
  different style, the tooling is the source of truth here.
- No comments unless they explain a non-obvious *why* (a CSP API quirk, a
  workaround, an invariant that isn't visible from the code itself). Don't
  narrate what the code does — identifiers should do that.
- Don't add abstractions, config knobs, or error handling for scenarios
  that can't happen yet. The MVP is single-provider, single-resource-kind,
  no dependency graph — build for that, not for a hypothetical future
  multi-cloud graph engine. `PLAN.md` §9 already names what's deferred and
  why; don't quietly start building toward it early.
- Follow the `ResourceDriver` interface in `PLAN.md` §4 exactly — method
  names, argument order, exception type and its two fields (`reason`,
  `unsupported_fields`), the two schema class attributes. Every future
  driver depends on this contract being stable.
- Tests live in `tests/`, mirroring the module they test
  (`tests/test_state.py` for `aiform/state.py`, etc.) — see `PLAN.md` §1 for
  the full layout, including `tests/drivers/test_digitalocean_compute.py`
  for the first curated driver.

## Implementation order (historical)

The order these were actually built in — roughly bottom-up, so each piece
could be tested without needing the LLM-driven parts to exist yet. All of
it is done; kept because the rationale still explains why the modules
depend on each other the way they do, and because a new resource driver or
provider follows the same shape:

1. `aiform/models.py`, `aiform/state.py`, `aiform/config.py` — no LLM
   involvement, pure data/IO, easiest to get right and test first.
2. `aiform/llm.py` — the four role-based functions
   (`intent_orchestration_call`/`code_generator_call`/`review_driver`/
   `review_plan`), dispatching on configured model source via
   `MODEL_SOURCES`, with the structured-output schemas from `PLAN.md`
   §2/§5. Verify the credentials-never-touch-this-file property from day
   one.
3. `aiform/driver.py` — the `ResourceDriver` ABC + `DriverUpdateNotSupported`,
   then `aiform/driver_gen.py` — driver generation + AST validation + gate
   #1 (`code-review-model`).
4. `drivers/digitalocean/compute.py` — the first curated driver,
   hand-authored through `PROCESS.md`'s loop (not generated: three real
   `generate_driver()` attempts against it failed, which is why drivers
   are curated at all — see `PLAN.md`'s "Driver curation"). Read it
   yourself; it establishes the pattern every future driver follows.
5. `aiform/planner.py`, `aiform/orchestrator.py`, `aiform/cli.py` — wire
   everything into the `plan`/`apply` commands and validate against the full
   MVP walkthrough in `PLAN.md` §8, including the "second plan run makes
   zero Anthropic API calls" claim — actually verify that with `--verbose`
   logging or a request counter, don't just assume it.

## Git workflow

See `.claude/skills/github-commit-process/SKILL.md` — short version:
feature branches, PRs, and **nothing merges without explicit human
approval**. This repo itself was bootstrapped that way; keep doing it that
way.

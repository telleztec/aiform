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

Current status: **no implementation exists yet.** `pyproject.toml`,
`aiform/*.py`, and `modules/digitalocean/droplet.py` all still need to be
written from `PLAN.md` §1–§4.

## Non-negotiable design rules

These aren't stylistic preferences — they're the properties that make the
whole "AI-driven but bounded-cost" premise hold together. Don't relax them
to make something easier to build.

### Model tiering
- **Claude Sonnet 5** (`claude-sonnet-5`) for everything routine and repeated:
  parsing prose intent, diffing, plan categorization, drafting new modules.
- **Claude Opus 5** (`claude-opus-5`) *only* at the two review gates: (1)
  approving a newly-generated resource module before it's trusted for reuse,
  (2) reviewing a plan before `apply` executes anything destructive.
- Do not swap either tier for cost reasons without asking first. This split
  was chosen deliberately after an explicit cost/capability tradeoff
  discussion — it's not a default that happened to be picked.
- On repeat `apply`/`plan` runs against unchanged input, the execution path
  must make **zero** Anthropic API calls (see `PLAN.md` §5 step 5, §8 step 4).
  If you find yourself adding an LLM call inside the module-execution path,
  stop — that call belongs in the planning phase, not here.

### Credentials
- `aiform/llm.py` (all Sonnet/Opus calls) must **never** have a `credentials`
  parameter, local variable, or import anywhere in it. This is meant to be
  literally grep-verifiable: `grep -n credentials aiform/llm.py` should
  return nothing, ever. All credential-bearing code lives in
  `orchestrator.py`'s module-execution path.
- `ANTHROPIC_API_KEY` — env var only, never a CLI flag.
- `DIGITALOCEAN_TOKEN` — env var first, else `.aiform/credentials.env`
  (dotenv-style). `aiform init` prints instructions for creating this file
  but **never** writes a value into it or prompts for the token
  interactively — the user creates it by hand with a text editor. This
  mirrors the proven pattern from `~/src/telleztec-infra`'s
  `terraform.tfvars` handling; don't reinvent it differently here.
- A module (`modules/<provider>/<resource>.py`) that imports `anthropic` or
  reads `ANTHROPIC_API_KEY` is a hard failure at Opus review time — this is
  explicitly one of the review checklist items in `prompts/review_module.md`.

### State handling
- Write `.aiform/state.json.backup` before every overwrite of
  `.aiform/state.json`.
- Refresh before diff: call `module.read()` for tracked resources before
  computing a diff, every time, even on a plain `plan` with no changes
  expected. State is a cache of live reality, not a source of truth in
  itself.
- The no-op short-circuit (`PLAN.md` §5 step 5) — deterministic dict-diff
  first, only call Sonnet when there's something to actually interpret — is
  what keeps `plan` cheap on unchanged input. Don't route the diff step
  through an LLM call unconditionally "for simplicity."

## Coding conventions

- No comments unless they explain a non-obvious *why* (a CSP API quirk, a
  workaround, an invariant that isn't visible from the code itself). Don't
  narrate what the code does — identifiers should do that.
- Don't add abstractions, config knobs, or error handling for scenarios
  that can't happen yet. The MVP is single-provider, single-resource-type,
  no dependency graph — build for that, not for a hypothetical future
  multi-cloud graph engine. `PLAN.md` §9 already names what's deferred and
  why; don't quietly start building toward it early.
- Follow the module interface in `PLAN.md` §4 exactly — function names,
  argument order, exception type and its two fields (`reason`,
  `unsupported_fields`), the two schema constants. Every future generated
  module depends on this contract being stable.
- Tests live in `tests/`, mirroring the module they test
  (`tests/test_state.py` for `aiform/state.py`, etc.) — see `PLAN.md` §1 for
  the full layout, including `tests/modules/test_droplet_do.py` for the
  first generated module.

## Suggested implementation order

Roughly bottom-up, so each piece can be tested without needing the LLM-driven
parts to exist yet:

1. `aiform/models.py`, `aiform/state.py`, `aiform/config.py` — no LLM
   involvement, pure data/IO, easiest to get right and test first.
2. `aiform/llm.py` — the three wrapper functions
   (`sonnet_call`/`opus_review_module`/`opus_review_plan`), with the
   structured-output schemas from `PLAN.md` §2/§5. Verify the
   credentials-never-touch-this-file property from day one.
3. `aiform/module_gen.py` — module generation + AST validation + Opus review
   gate #1.
4. `modules/digitalocean/droplet.py` — the first generated module. Even
   though Opus reviews it automatically, read it yourself the first time;
   it establishes the pattern every future module follows.
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

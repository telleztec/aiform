# Plan: give a deployment an identity (#201)

**Status: approved 2026-09-29 by the repo owner, as a three-PR sequence. This
file is committed by PR 1 and describes all three.**

## Context

`aiform` supports several independent deployments, each in its own directory.
The isolation is positional: nothing in a state file says which deployment it
belongs to, so `cd` into the wrong directory and a no-argument
`aiform plan destroy` empties the wrong deployment with a listing that looks
exactly like the expected one. Issue #201 asks for a deployment identity. Its
own comment adds that the resource key carries no account or team either; that
is recorded below as deferred.

## Decisions

- The concept is called **deployment**, not "workspace".
- The name is set at `aiform init` and stored as a **required** field
  `deployment` on `State` in `state.json`. Migration for old state files is an
  explicit non-requirement: tests and fixtures are updated instead, and no
  compatibility shim is added.
- **Model 1.** The current directory holds the state; the requested name is
  checked against the name inside the loaded state file. No environment
  variable and no `~/aiform` home lookup.
- Every command that reads state accepts `--deployment NAME`; unspecified means
  `default`. The flag is declared once, on the shared `state_parent`.
- The check lives in one choke point, `state.load(...)`, which takes the
  requested name and raises on mismatch, so no call site can forget it. It
  fires before any provider call and before any LLM call. A missing state file
  returns a fresh `State` named for the requested deployment.
- The name must be safe as a directory-name path segment, because a deferred
  ticket will use it as a directory under `~/aiform/`.

## Sequence

1. **PR 1 — name a deployment and check it everywhere** (`Refs #201`). The
   `deployment` field, `--deployment` on every state-reading command, the
   mismatch exception, `aiform init --deployment`. A separate, droppable commit
   adds the `@name` positional shorthand.
2. **PR 2 — make destroy-all explicit.** A no-argument `plan destroy` requires
   `--all` and an explicit deployment name. `--yes` never satisfies the name.
   Interactive runs prompt for the typed name.
3. **PR 3 — say which deployment you are about to act on** (`Closes #201`).
   Print the deployment, the state-file path and the resource count as a header
   on every `plan` and `apply`.

## Deferred, each its own ticket

- Home lookup: resolving a deployment by name through an environment variable
  or `~/aiform`, instead of by the current directory.
- One deployment spanning several files or directories (#210).
- Binding state to the cloud account or team it was applied against, and the
  resource key's lack of an account component (the comment on #201).
- `@name` beyond the CLI.

## Not in PR 1

`--all`, typed-name prompts and header printing (PR 2 and PR 3). The existing
`plan destroy --force` is unrelated and untouched.

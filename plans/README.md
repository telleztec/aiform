# plans/

Approved implementation plans, one file per change, committed on the branch
that implements them. `PROCESS.md`'s "Before the loop: plan and get explicit
approval" gate requires a written plan the human approved before any code was
written; this directory is where that plan lives so a reviewer can open it from
the PR.

A plan is a **point-in-time decision record**, not a living document. That is
what separates it from `specs/`, whose files are updated in place forever to
describe the module as it actually is (`specs/README.md`'s "Lifecycle"). A plan
records what was agreed *beforehand*, so it is not rewritten as implementation
proceeds — if implementation shows the approved approach was wrong, the fix is
to re-plan and get approval again, per `PROCESS.md`, and the new plan is a new
file.

## Naming

`plans/<short-feature-name>.md`, descriptive — never the generated filename
from `~/.claude/plans/`, which is unguessable by a fresh session and is exactly
what issue #203 is about.

## Status

This directory exists because a plan was committed here on request, which is an
instance of **issue #203's part 1**. It is not an implementation of #203, and
#203 remains open: that issue also asks for `PROCESS.md` to bind this, for
plans to be presented as generated HTML artifacts, and for the same treatment
for any multi-way decision — plus it leaves six design questions open,
including whether plans merge to `main` or stay on the branch, and what happens
to a plan whose phase was re-planned. None of those are decided here.

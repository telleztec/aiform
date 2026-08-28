---
name: tdd-workflow
description: The spec-first, test-first, Opus-reviewed loop this project builds every module with. Use whenever you're about to start work on a new aiform module (models.py, state.py, llm.py, the digitalocean compute driver, etc.) or resume mid-module.
---

# tdd-workflow

Full rationale lives in `PROCESS.md` at the repo root — read it once if
you haven't. This file is the short operational checklist for each pass.

One pass = one module = one PR. Do not fold multiple modules into one
pass.

## Checklist, in order

1. **Spec.** Check `specs/<module>.md` exists and is accurate. If not,
   write or update it first — half a page, following `specs/README.md`'s
   format. Don't start writing tests or code before this exists.
2. **Red.** Write the test file in `tests/` against the spec. Run it.
   Confirm it actually fails (import error counts). If it passes with no
   implementation, the test is wrong — fix it before continuing.
3. **Implement.** Minimum code to satisfy the spec and pass the tests.
   No speculative abstractions, config knobs, or unreachable error
   handling.
4. **Green.** Rerun this module's tests, then the full suite (`pytest`).
   All green.
5. **Review.** Run `/code-review` (Opus 5 or newer) on the diff yourself —
   it does not wait on the human. Fix what it
   flags, or note explicitly in the PR why something is deferred.
6. **PR.** Follow `.claude/skills/github-commit-process/SKILL.md`
   exactly — branch, commits, PR body, and critically: never merge
   without explicit human approval, same as every other change in this
   repo.

## If something breaks the loop

- Test won't go red no matter what → the test isn't exercising the code
  path it claims to. Stop and fix the test, don't proceed to implementation.
- Implementing this module reveals the *previous* module's interface was
  wrong → don't quietly patch around it. Go back, fix that module's spec
  and code in a small follow-up, flag it to the human.
- Spec turns out incomplete once you're implementing → update the spec
  in the same PR, don't let it drift out of sync with the code.

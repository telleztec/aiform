# specs/merge_gate.md — `scripts/merge_gate.py`

## Purpose

Answer one question before a merge: **how many GitHub issues will this PR
actually close?** More than one requires the human's
`/claude-merge-approved-multi` acknowledgement
(`.claude/skills/github-commit-process/SKILL.md`, "One issue, one PR").

It exists as a script rather than a snippet in that document because a
merge gate is code. The shell fragment that preceded it produced
high-severity review findings in three consecutive rounds — reading commit
bodies but not subjects, halving its result when a lookup failed, and
taking the else branch on an empty count — none of which a document can
have tests for.

## Interface

```sh
.venv/bin/python scripts/merge_gate.py <PR> [--multi]
```

`--multi` asserts the human approved with `/claude-merge-approved-multi`.

| Exit | Meaning |
|---|---|
| 0 | may merge: one issue or fewer, or several with `--multi` |
| 1 | blocked: several issues without `--multi` |
| 2 | could not determine — a usage error or a failed lookup |

**2 is distinct from 1 on purpose.** A caller reading any non-zero as
"blocked" would tell the human to post `-multi` on a clean PR when `gh` is
merely unavailable.

## Behavior

- **Two sources, unioned.** `closingIssuesReferences` covers the PR
  description only; a closing keyword in a commit message still closes the
  issue on merge to the default branch and never appears there. Neither is
  sufficient alone.
- **Only issues currently `OPEN` are counted.** A reference to an
  already-closed issue closes nothing on merge, and counting it demands a
  waiver for a PR that will close one issue or none — which is what a
  commit message quoting closing-keyword syntax produces.
- Commits come from `gh api .../pulls/<pr>/commits --paginate`, not
  `gh pr view --json commits`: the latter is a GraphQL connection capped at
  one page. **The REST endpoint caps at 250 commits even with
  `--paginate`**, so this narrows the fail-open window rather than closing
  it. A PR with more than 250 commits can still drop later references.
- **Fails closed.** Any lookup problem raises rather than returning an
  empty set, including `gh` being absent (an `OSError`, not a non-zero
  exit).
- Recognized forms: `#12`, `owner/repo#12`, an issue URL, and the `GH-12`
  autolink; `close/closes/closed`, `fix/fixes/fixed`,
  `resolve/resolves/resolved`, each optionally followed by a colon. Word
  boundaries matter: `prefix #95` must not match as `fix #95`.

## Edge cases

- `Closes #A and #B` closes **one** issue — GitHub needs the keyword
  before each number. The gate reports what GitHub will do, not what the
  author meant, so this returns 1 and passes. The trap is documented in
  `SKILL.md`; catching it is out of scope here (see below).
- A single-commit PR has its body prefilled from the commit message, so
  the same keyword appears in both sources. Results are deduplicated.

## Out of scope

- **Warning about `Closes #A, #B`.** The gate answers what *will* close,
  not what the author intended to close. A bare `#\d+` scan as a
  non-blocking warning would catch it and is worth considering separately.
- **Verifying the acknowledgement.** `--multi` is asserted by the calling
  agent; the script never reads PR comments, so nothing binds the flag to a
  human having posted `/claude-merge-approved-multi`. An agent that
  mis-reads the loop's signal self-grants the waiver. **Neither half of the
  rule is mechanically checked** — not the trigger, not the disclosure in
  the description. Having the script verify the literal itself would close
  this; it already shells out to `gh`.
- Posting statuses or merging. This reports; the caller decides.

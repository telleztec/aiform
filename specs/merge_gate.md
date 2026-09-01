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
# From the repo root: gh resolves the repository from the working directory.
# Two commands, and `cd` to the printed path -- not `cd "$(...)"`, whose
# empty substitution is a silent no-op in sh, zsh and bash 3.2.
git rev-parse --show-toplevel
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
- **Of this PR's own repository, only currently open issues are
  counted**, obtained as one `gh issue list --repo <the PR's repo>
  --state open` and intersected. Cross-repo entries skip this filter
  entirely and are counted in any state — see below. A
  reference to an already-closed issue closes nothing on merge, and
  counting it demands a waiver for a PR that closes one issue or none —
  which is what a commit message quoting closing-keyword syntax produces.

  Asking for the whole set rather than each number in turn is deliberate.
  It is one round trip instead of N; `gh issue list` excludes pull
  requests, where `gh issue view <n>` resolves them and would count an open
  PR as an open issue; and a reference to something that is not an issue
  here — a placeholder in an example, a number from another project — is
  simply absent rather than an error. If the list comes back at the limit
  it may be truncated, so the gate raises rather than undercounting.
- **In commit messages**, a reference qualified with `owner/repo` or an
  issue URL is dropped unless it names this PR's repository — a keyword
  there does not close another project's issue. The repository is read
  from the PR's own url, not from the working directory, since in a fork
  clone those differ. **Every lookup derived from the resolved
  repository is then scoped to it explicitly** — the commits endpoint by
  path, the open-issue list with `--repo`. Leaving the open-issue list to
  `gh`'s working-directory resolution intersects against the wrong issues
  and reports "closes none"; leaving the commits endpoint to it fetches
  another repo's commits.

  **Scoped means host-qualified.** `_repo_of` returns `(host, owner/name)`
  and both lookups carry the host — `--repo host/owner/name`, and
  `--hostname` on `gh api`. A bare `owner/name` resolves against `gh`'s
  *default* host, so on a GitHub Enterprise clone a scoped lookup would
  query github.com: the same silent drop, reintroduced by the fix for it.
  Bare `owner/name` is still what commit-message references and the result
  pairs use; the host reaches only the lookups. A reference written as an
  issue *url* is therefore matched on `owner/name` alone, so one naming the
  same path on another host counts as local — an over-count, which fails
  closed.

  **The PR itself is still resolved from the working directory.** Not
  because it must be — `gh pr view` does accept `--repo` — but because the
  first of those two calls is the one that *answers* which repository this
  is; pinning it would require its own result. The second could be pinned
  afterwards and is not, so the two agree by construction instead. The
  caller must therefore run from a clone of the PR's repo, which is why the
  snippet in `github-commit-process` cds to the repo root before its first
  `gh` call. This narrows the fork-clone hazard rather than closing it: run
  from the wrong clone, and the gate answers confidently about a different
  PR.
- **In `closingIssuesReferences`**, a cross-repo entry is *counted*, not
  dropped. GitHub lists it because merging really does close it, and it
  cannot be checked against this repo's open issues — so intersecting
  would silently lose it. Results are `(repo, number)` pairs for that
  reason: a foreign `#4` and a local `#4` are different issues.
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
- **State-checking cross-repo references.** Local references are
  intersected with the open-issue list; foreign ones are counted as-is, so
  an *already-closed* foreign issue still counts toward the total and can
  demand a waiver for a PR that really closes one issue. That is the false
  positive the open-issue filter exists to remove, surviving for foreign
  refs only. `gh issue list --repo <host>/<owner/name> --state open` would
  close it, at a round trip per distinct foreign repo and a hard failure
  when that repo is not readable. Note the `<host>/` — a bare `owner/name`
  there resolves against gh's *default* host and would reintroduce the
  silent enterprise drop described above, in the very fix for it. Left
  undone because it fails closed: it costs a review round, never a bad
  merge.
- Posting statuses or merging. This reports; the caller decides.

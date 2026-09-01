# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Check that a PR closes at most one issue, or carries the -multi acknowledgement.

Usage: python scripts/merge_gate.py <PR> [--multi]

Exits 0 when the PR may be merged, 1 when it may not, 2 on a usage or
lookup failure. Pass --multi when the human's approval was
/claude-merge-approved-multi.

Lives here rather than as a snippet in SKILL.md because a merge gate is
code: it needs tests, and a shell fragment in a document has none.
"""

import argparse
import json
import re
import subprocess
import sys
import urllib.parse

# GitHub's closing keywords, each optionally followed by a colon, then an
# issue reference: #12, owner/repo#12, or a full issue URL.
_TIMEOUT = 30

# Above this the list may be truncated, so the gate refuses rather than
# undercounting.
_ISSUE_LIMIT = 500

_CLOSING = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b:?\s+"
    r"(?:([\w.-]+/[\w.-]+))?(?:#|GH-)(\d+)"
    r"|\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b:?\s+"
    r"https?://\S*?/(?:([\w.-]+/[\w.-]+))/issues/(\d+)",
    re.IGNORECASE,
)


def closing_refs(text: str, repo: str | None = None) -> set[int]:
    """Issue numbers closed by keywords in `text`.

    A reference naming another repository closes nothing here, so it is
    dropped when `repo` is known. With `repo` unset every reference counts,
    which is only right for testing the pattern itself.
    """
    found: set[int] = set()
    for match in _CLOSING.finditer(text or ""):
        owner, number = (match.group(1), match.group(2))
        if number is None:
            owner, number = (match.group(3), match.group(4))
        if owner and repo and owner.lower() != repo.lower():
            continue
        found.add(int(number))
    return found


def _run(command: list[str]) -> str:
    """Fail closed: any lookup problem is an error, never an empty result."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{command[0]} timed out after {_TIMEOUT}s") from exc
    except OSError as exc:
        raise RuntimeError(f"could not run {command[0]}: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(command)} failed: {result.stderr.strip()}")
    return result.stdout


def _linked_refs(pr: str, repo: str) -> tuple[set[int], set[tuple[str, int]]]:
    """GitHub's own linked-issue list, split by whether it targets `repo`.

    Split because only same-repo numbers can be checked against this repo's
    open issues; the caller treats the two halves differently.
    """
    data = _parse_json(
        _run(["gh", "pr", "view", pr, "--json", "closingIssuesReferences"]), f"PR {pr}"
    )

    same_repo: set[int] = set()
    cross_repo: set[tuple[str, int]] = set()
    for ref in data.get("closingIssuesReferences") or []:
        owner = (ref.get("repository") or {}).get("owner") or {}
        name = (ref.get("repository") or {}).get("name")
        where = f"{owner.get('login')}/{name}" if name else repo
        if where.lower() == repo.lower():
            same_repo.add(ref["number"])
        else:
            cross_repo.add((where, ref["number"]))
    return same_repo, cross_repo


def _commit_messages(pr: str, host: str, repo: str) -> str:
    """Every commit message on the PR, subject included.

    The REST endpoint with --paginate, not `gh pr view --json commits`: that
    is a GraphQL connection capped at one page, so a long PR would silently
    drop later commits and the gate would fail open.
    """
    return _run(
        [
            "gh",
            "api",
            "--hostname",
            host,
            "repos/" + repo + "/pulls/" + pr + "/commits",
            "--paginate",
            "--jq",
            ".[].commit.message",
        ]
    )


def issues_closed_by(pr: str, resolved: tuple[str, str] | None = None) -> set[tuple[str, int]]:
    """The issues merging this PR will close, as (repo, number) pairs.

    The gate only asks how many there are — one is fine, more needs a human
    waiver — but it names them when it reports, so identities are kept
    rather than a count returned.

    Three rules decide the answer, and the last two are deliberately not
    symmetric:

    - **Two sources.** GitHub's linked-issue list covers the PR description
      only; a closing keyword in a commit message closes the issue on merge
      without ever appearing in that list.
    - **Same-repo references are narrowed to issues that are still open.**
      An already-closed one would otherwise inflate the count and demand a
      waiver for a PR that really closes one issue.
    - **Cross-repo references are counted whatever state they are in**,
      because that state cannot be read from here. It over-counts rather
      than under-counts, so it costs a review round and never a bad merge.
      Recorded in `specs/merge_gate.md` under "Out of scope".

    `resolved` is `_repo_of`'s answer, looked up here when the caller has
    not already. Bare `owner/name` is what commit messages and the result
    pairs use; the host only ever reaches the lookups. A reference written
    as an issue *url* is matched on `owner/name` alone, so one naming the
    same path on another host counts as same-repo — again an over-count.
    """
    host, repo = resolved or _repo_of(pr)

    same_repo, cross_repo = _linked_refs(pr, repo)
    same_repo |= closing_refs(_commit_messages(pr, host, repo), repo)

    still_open = same_repo & _open_issues(host, repo)
    return {(repo, n) for n in still_open} | cross_repo


def _repo_of(pr: str) -> tuple[str, str]:
    """(host, owner/name) read from the PR's url.

    `gh` resolves the bare PR number from the working directory; this
    reads back what it actually resolved, so the two repository-scoped
    lookups are pinned to it rather than resolved again. The other two
    calls are `gh pr view` itself and stay cwd-resolved. Not because they
    must -- `gh pr view` does take `--repo` -- but because the first of
    them is what answers this question: pinning it would need the answer
    it produces. The second could be pinned once this has returned, and
    is not, so both agree by construction instead.

    The host comes too: `--repo owner/name` targets gh's *default* host,
    so on a GitHub Enterprise clone a bare name queries github.com. That
    usually errors and exits 2; it answers the wrong repository silently
    only when the same owner/name also exists there.
    """
    url = _parse_json(_run(["gh", "pr", "view", pr, "--json", "url"]), f"PR {pr}")["url"]
    parsed = urllib.parse.urlparse(url)
    parts = parsed.path.strip("/").split("/")
    if not parsed.netloc or len(parts) < 2:
        raise RuntimeError(f"could not read a repository from the PR url: {url}")
    return parsed.netloc, f"{parts[0]}/{parts[1]}"


def _open_issues(host: str, repo: str) -> set[int]:
    """Every open issue in the PR's repository, in one call.

    Scoped with --repo rather than left to gh's working-directory
    resolution: in a fork clone those differ, and intersecting against the
    wrong repo's issues drops live references and reports "closes none".
    Host-qualified for the same reason -- see `_repo_of`.

    A reference to an already-closed issue closes nothing on merge, and
    counting it demands a waiver for a PR that closes one or none -- which
    is what a commit message quoting closing-keyword syntax produces.

    Asking for the whole set rather than each number in turn also means a
    reference to something that is not an issue here at all -- a pull
    request, a number from another project, a placeholder in an example --
    is simply absent rather than an error or a wrong answer.
    """
    raw = _run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            f"{host}/{repo}",
            "--state",
            "open",
            "--limit",
            str(_ISSUE_LIMIT),
            "--json",
            "number",
        ]
    )
    listed = _parse_json(raw, "the open-issue list")
    if len(listed) >= _ISSUE_LIMIT:
        raise RuntimeError(
            f"more than {_ISSUE_LIMIT} open issues: the list may be truncated, "
            f"which would silently undercount"
        )
    return {entry["number"] for entry in listed}


def _parse_json(raw: str, what: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gh returned unparseable JSON for {what}: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="merge_gate")
    parser.add_argument("pr")
    parser.add_argument(
        "--multi",
        action="store_true",
        help="the human approved with /claude-merge-approved-multi",
    )
    args = parser.parse_args(argv)

    try:
        host, here = _repo_of(args.pr)
        issues = issues_closed_by(args.pr, (host, here))
        # Formatting stays inside the guard: sorted() compares the repo
        # strings first and reaches the numbers only when they tie, and a
        # foreign ref's number comes verbatim from gh's JSON. Outside, that
        # TypeError would escape as exit 1 -- read as "needs -multi".
        listed = (
            ", ".join(
                f"#{n}" if r.lower() == here.lower() else f"{r}#{n}" for r, n in sorted(issues)
            )
            or "none"
        )
    except Exception as exc:
        # Exit 1 means "blocked"; an unexpected failure must not be
        # mistaken for a multi-issue PR, so everything becomes exit 2.
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if len(issues) <= 1:
        print(f"OK: PR {args.pr} closes {listed}")
        return 0

    if args.multi:
        print(f"OK: PR {args.pr} closes {len(issues)} issues ({listed}), acknowledged with --multi")
        return 0

    print(
        f"BLOCKED: PR {args.pr} closes {len(issues)} issues ({listed}) "
        f"but was approved with the plain trigger.\n"
        f"Split it into one PR per issue if you can -- that is the default.\n"
        f"If it genuinely cannot be split, ask the human to re-read the "
        f"description and post /claude-merge-approved-multi, then re-run "
        f"with --multi.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())

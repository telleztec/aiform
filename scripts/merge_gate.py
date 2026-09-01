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


def issues_closed_by(pr: str) -> set[int]:
    """Union of both sources, because neither is complete on its own.

    GitHub's linked-issue list covers the PR description only; a closing
    keyword in a commit message still closes the issue on merge to the
    default branch but never appears there.
    """
    data = _parse_json(
        _run(["gh", "pr", "view", pr, "--json", "closingIssuesReferences"]), f"PR {pr}"
    )
    found = {ref["number"] for ref in data.get("closingIssuesReferences") or []}

    # The REST endpoint with --paginate, not `gh pr view --json commits`:
    # that is a GraphQL connection capped at one page, so a long PR would
    # silently drop later commits and the gate would fail open. This also
    # returns the whole message, subject included.
    messages = _run(
        [
            "gh",
            "api",
            "repos/{owner}/{repo}/pulls/" + pr + "/commits",
            "--paginate",
            "--jq",
            ".[].commit.message",
        ]
    )
    return (found | closing_refs(messages, _repo_of(pr))) & _open_issues()


def _repo_of(pr: str) -> str:
    """The PR's own repository, not the working directory's.

    In a fork clone those differ, and using the wrong one drops a
    legitimate same-repo reference as if it named somewhere else.
    """
    url = _parse_json(_run(["gh", "pr", "view", pr, "--json", "url"]), f"PR {pr}")["url"]
    owner, _, name = url.split("/github.com/")[-1].partition("/")
    return f"{owner}/{name.split('/')[0]}"


def _open_issues() -> set[int]:
    """Every open issue, in one call.

    A reference to an already-closed issue closes nothing on merge, and
    counting it demands a waiver for a PR that closes one or none -- which
    is what a commit message quoting closing-keyword syntax produces.

    Asking for the whole set rather than each number in turn also means a
    reference to something that is not an issue here at all -- a pull
    request, a number from another project, a placeholder in an example --
    is simply absent rather than an error or a wrong answer.
    """
    raw = _run(
        ["gh", "issue", "list", "--state", "open", "--limit", str(_ISSUE_LIMIT), "--json", "number"]
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
        issues = issues_closed_by(args.pr)
    except Exception as exc:  # noqa: BLE001 -- exit 1 means "blocked"; an
        # unexpected failure must not be mistaken for a multi-issue PR.
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    listed = ", ".join(f"#{n}" for n in sorted(issues)) or "none"
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

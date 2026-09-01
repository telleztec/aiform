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
_CLOSING = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b:?\s+"
    r"(?:[\w.-]+/[\w.-]+)?(?:#|GH-)(\d+)"
    r"|\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b:?\s+"
    r"https?://\S*?/issues/(\d+)",
    re.IGNORECASE,
)


def closing_refs(text: str) -> set[int]:
    return {int(n) for match in _CLOSING.finditer(text or "") for n in match.groups() if n}


def _run(command: list[str]) -> str:
    """Fail closed: any lookup problem is an error, never an empty result."""
    try:
        result = subprocess.run(command, capture_output=True, text=True)
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
    linked = _run(["gh", "pr", "view", pr, "--json", "closingIssuesReferences"])
    try:
        data = json.loads(linked)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gh returned unparseable JSON for PR {pr}: {exc}") from exc
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
    return found | closing_refs(messages)


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
    except RuntimeError as exc:
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

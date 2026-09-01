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
    r"(?:[\w.-]+/[\w.-]+)?#(\d+)"
    r"|\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b:?\s+"
    r"https?://\S*?/issues/(\d+)",
    re.IGNORECASE,
)


def closing_refs(text: str) -> set[int]:
    return {int(n) for match in _CLOSING.finditer(text or "") for n in match.groups() if n}


def _gh_json(pr: str, fields: str) -> dict:
    """Fail closed: any lookup problem is an error, never an empty result."""
    result = subprocess.run(
        ["gh", "pr", "view", pr, "--json", fields],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh pr view {pr} --json {fields} failed: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gh returned unparseable JSON for PR {pr}: {exc}") from exc


def issues_closed_by(pr: str) -> set[int]:
    """Union of both sources, because neither is complete on its own.

    GitHub's linked-issue list covers the PR description only; a closing
    keyword in a commit message still closes the issue on merge to the
    default branch but never appears there. Commit subjects live in
    messageHeadline, separately from messageBody.
    """
    data = _gh_json(pr, "closingIssuesReferences,commits")
    found = {ref["number"] for ref in data.get("closingIssuesReferences") or []}
    for commit in data.get("commits") or []:
        found |= closing_refs(commit.get("messageHeadline", ""))
        found |= closing_refs(commit.get("messageBody", ""))
    return found


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
        f"Ask the human to re-read the description and post "
        f"/claude-merge-approved-multi, then re-run with --multi.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())

# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import json
import subprocess

import pytest

from scripts import merge_gate


class TestClosingRefs:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Closes #12", {12}),
            ("closes #12", {12}),
            ("Fixes #12", {12}),
            ("fixed #12", {12}),
            ("Resolves #12", {12}),
            ("Closes: #12", {12}),
            ("Closes  #12", {12}),
            ("Closes #12, closes #13", {12, 13}),
            ("fixes telleztec/aiform#92", {92}),
            ("closes https://github.com/telleztec/aiform/issues/93", {93}),
            ("Fixes GH-74", {74}),
        ],
    )
    def test_forms_github_honours(self, text, expected):
        assert merge_gate.closing_refs(text) == expected

    def test_one_keyword_closes_one_issue(self):
        # The trap the rule documents: without a repeated keyword only the
        # first number closes.
        assert merge_gate.closing_refs("Closes #12 and #13") == {12}

    @pytest.mark.parametrize("text", ["prefix #95 here", "see #12", "affix #3", "#12"])
    def test_no_false_positives(self, text):
        # "prefix" must not match as "fix" -- a false positive blocks a
        # clean PR.
        assert merge_gate.closing_refs(text) == set()

    def test_empty_and_none(self):
        assert merge_gate.closing_refs("") == set()
        assert merge_gate.closing_refs(None) == set()


def _stub_gh(monkeypatch, payload, commits="", returncode=0, stderr="", open_issues=(83,)):
    """Models the four call shapes: linked issues, the PR url, commit
    messages, and the open-issue list."""

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        if "issue" in cmd and "list" in cmd:
            out = json.dumps([{"number": n} for n in open_issues])
        elif "url" in cmd:
            out = json.dumps({"url": "https://github.com/telleztec/aiform/pull/84"})
        elif "api" in cmd:
            out = commits
        else:
            out = json.dumps(payload)
        return subprocess.CompletedProcess(cmd, returncode, out, stderr)

    monkeypatch.setattr(merge_gate.subprocess, "run", fake_run)


class TestIssuesClosedBy:
    def test_unions_linked_issues_and_commit_messages(self, monkeypatch):
        # Neither source is complete: GitHub's list covers the description
        # only, and a keyword in a commit closes on merge without appearing
        # there.
        _stub_gh(
            monkeypatch,
            {
                "closingIssuesReferences": [{"number": 83}],
            },
            commits="x\n\nCloses #74",
            open_issues=(83, 74),
        )

        assert merge_gate.issues_closed_by("84") == {
            ("telleztec/aiform", 83),
            ("telleztec/aiform", 74),
        }

    def test_reads_the_commit_subject_not_only_the_body(self, monkeypatch):
        # gh puts the subject in messageHeadline; scanning only messageBody
        # misses `Fixes #74: ...` entirely.
        _stub_gh(
            monkeypatch,
            {
                "closingIssuesReferences": [],
            },
            commits="Fixes #74: stop the leak",
            open_issues=(74,),
        )

        assert merge_gate.issues_closed_by("84") == {("telleztec/aiform", 74)}

    def test_deduplicates_across_sources(self, monkeypatch):
        # A single-commit PR has its body prefilled from the commit, so the
        # same keyword legitimately appears twice.
        _stub_gh(
            monkeypatch,
            {
                "closingIssuesReferences": [{"number": 83}],
            },
            commits="Closes #83",
            open_issues=(83,),
        )

        assert merge_gate.issues_closed_by("84") == {("telleztec/aiform", 83)}

    def test_already_closed_issues_are_not_counted(self, monkeypatch):
        # A commit quoting closing-keyword syntax attaches a reference to an
        # issue that is already closed. Merging closes nothing, so demanding
        # a waiver for it is a false positive -- this blocked the PR that
        # introduced the gate.
        _stub_gh(
            monkeypatch,
            {"closingIssuesReferences": [{"number": 83}]},
            commits="`Closes #73, closes #74` is the correct form",
            open_issues=(83,),
        )

        assert merge_gate.issues_closed_by("84") == {("telleztec/aiform", 83)}

    def test_cross_repo_reference_is_not_a_local_close(self, monkeypatch):
        _stub_gh(
            monkeypatch,
            {"closingIssuesReferences": []},
            commits="fixes otherorg/otherrepo#5",
            open_issues=(5, 83),
        )

        assert merge_gate.issues_closed_by("84") == set()

    def test_same_repo_prefix_still_counts(self, monkeypatch):
        _stub_gh(
            monkeypatch,
            {"closingIssuesReferences": []},
            commits="fixes telleztec/aiform#92",
            open_issues=(92,),
        )

        assert merge_gate.issues_closed_by("84") == {("telleztec/aiform", 92)}

    def test_cross_repo_linked_issue_is_counted_not_dropped(self, monkeypatch):
        # GitHub lists cross-repo closing references and merging closes
        # them. They cannot be checked against this repo's open issues, so
        # intersecting would silently drop them -- fail open, on the branch
        # whose purpose is not dropping references.
        _stub_gh(
            monkeypatch,
            {
                "closingIssuesReferences": [
                    {
                        "number": 83,
                        "repository": {"name": "aiform", "owner": {"login": "telleztec"}},
                    },
                    {"number": 4, "repository": {"name": "other", "owner": {"login": "telleztec"}}},
                ]
            },
            open_issues=(83,),
        )

        found = merge_gate.issues_closed_by("84")

        assert found == {("telleztec/aiform", 83), ("telleztec/other", 4)}
        assert merge_gate.main(["84"]) == 1

    def test_timeout_raises_rather_than_hanging(self, monkeypatch):
        def hang(cmd, capture_output=True, text=True, timeout=None):
            raise merge_gate.subprocess.TimeoutExpired(cmd, timeout or 30)

        monkeypatch.setattr(merge_gate.subprocess, "run", hang)

        with pytest.raises(RuntimeError, match="timed out"):
            merge_gate.issues_closed_by("84")

    def test_reference_to_a_nonexistent_number_is_simply_absent(self, monkeypatch):
        # A placeholder in a doc example, or a number from another project.
        # It closes nothing here, so it must neither count nor be an error.
        _stub_gh(
            monkeypatch,
            {"closingIssuesReferences": [{"number": 83}]},
            commits="Closes #99999",
            open_issues=(83,),
        )

        assert merge_gate.issues_closed_by("84") == {("telleztec/aiform", 83)}

    def test_truncated_issue_list_raises_rather_than_undercounting(self, monkeypatch):
        _stub_gh(
            monkeypatch,
            {"closingIssuesReferences": []},
            # --limit caps exactly, so at-limit is the only truncation signal.
            open_issues=tuple(range(1, merge_gate._ISSUE_LIMIT + 1)),
        )

        with pytest.raises(RuntimeError, match="truncated"):
            merge_gate.issues_closed_by("84")

    def test_lookup_failure_raises_rather_than_returning_empty(self, monkeypatch):
        # Failing open here would post human-approval on an unchecked PR.
        _stub_gh(monkeypatch, {}, returncode=1, stderr="rate limited")

        with pytest.raises(RuntimeError, match="rate limited"):
            merge_gate.issues_closed_by("84")

    def test_unparseable_output_raises(self, monkeypatch):
        def fake_run(cmd, capture_output=True, text=True, timeout=None):
            return subprocess.CompletedProcess(cmd, 0, "not json", "")

        monkeypatch.setattr(merge_gate.subprocess, "run", fake_run)

        with pytest.raises(RuntimeError, match="unparseable"):
            merge_gate.issues_closed_by("84")

    def test_null_fields_are_not_a_crash(self, monkeypatch):
        _stub_gh(monkeypatch, {"closingIssuesReferences": None}, open_issues=())

        assert merge_gate.issues_closed_by("84") == set()


class TestMain:
    def test_single_issue_passes_without_multi(self, monkeypatch, capsys):
        _stub_gh(monkeypatch, {"closingIssuesReferences": [{"number": 83}]})

        assert merge_gate.main(["84"]) == 0
        assert "#83" in capsys.readouterr().out

    def test_zero_issues_passes(self, monkeypatch):
        _stub_gh(monkeypatch, {"closingIssuesReferences": []})

        assert merge_gate.main(["84"]) == 0

    def test_multiple_issues_blocked_without_multi(self, monkeypatch, capsys):
        _stub_gh(
            monkeypatch,
            {"closingIssuesReferences": [{"number": 83}, {"number": 74}]},
            open_issues=(83, 74),
        )

        assert merge_gate.main(["84"]) == 1
        err = capsys.readouterr().err
        assert "/claude-merge-approved-multi" in err
        assert "#74" in err and "#83" in err

    def test_multiple_issues_allowed_with_multi(self, monkeypatch):
        _stub_gh(
            monkeypatch,
            {"closingIssuesReferences": [{"number": 83}, {"number": 74}]},
            open_issues=(83, 74),
        )

        assert merge_gate.main(["84", "--multi"]) == 0

    def test_missing_gh_exits_two_not_one(self, monkeypatch):
        # Exit 1 means BLOCKED, which callers read as "needs -multi". A
        # broken toolchain must not masquerade as a multi-issue PR.
        def boom(cmd, capture_output=True, text=True, timeout=None):
            raise FileNotFoundError(2, "No such file or directory", "gh")

        monkeypatch.setattr(merge_gate.subprocess, "run", boom)

        assert merge_gate.main(["84"]) == 2

    def test_blocked_message_names_splitting_before_the_waiver(self, monkeypatch, capsys):
        # The rule says splitting is the default and the waiver a last
        # resort; the gate must not advertise the escape hatch first.
        _stub_gh(
            monkeypatch,
            {"closingIssuesReferences": [{"number": 83}, {"number": 74}]},
            open_issues=(83, 74),
        )

        merge_gate.main(["84"])
        err = capsys.readouterr().err

        assert err.index("Split it") < err.index("/claude-merge-approved-multi")

    def test_unexpected_exception_exits_two_not_one(self, monkeypatch, capsys):
        # Exit 1 means BLOCKED. Any unexpected failure reaching the caller
        # as 1 becomes a false request for /claude-merge-approved-multi.
        def boom(cmd, capture_output=True, text=True, timeout=None):
            raise AttributeError("gh returned null")

        monkeypatch.setattr(merge_gate.subprocess, "run", boom)

        assert merge_gate.main(["84"]) == 2

    def test_lookup_failure_exits_two_not_zero(self, monkeypatch):
        # Distinct from BLOCKED so a caller cannot mistake an outage for a pass.
        _stub_gh(monkeypatch, {}, returncode=1, stderr="boom")

        assert merge_gate.main(["84"]) == 2


class TestRepoOf:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://github.com/telleztec/aiform/pull/84", ("github.com", "telleztec/aiform")),
            # A GitHub Enterprise host must parse the same way; splitting on
            # "/github.com/" returned garbage and then dropped every
            # qualified reference as if it named another repo. The host is
            # returned too: a bare owner/name sent to --repo resolves
            # against gh's default host, so dropping it here queries
            # github.com for a PR that lives on the enterprise instance.
            (
                "https://github.example.com/telleztec/aiform/pull/84",
                ("github.example.com", "telleztec/aiform"),
            ),
            ("https://ghe.corp.net/org/repo/pull/1", ("ghe.corp.net", "org/repo")),
        ],
    )
    def test_parses_any_host(self, monkeypatch, url, expected):
        def fake_run(cmd, capture_output=True, text=True, timeout=None):
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"url": url}), "")

        monkeypatch.setattr(merge_gate.subprocess, "run", fake_run)

        assert merge_gate._repo_of("84") == expected

    def test_unparseable_url_raises(self, monkeypatch):
        def fake_run(cmd, capture_output=True, text=True, timeout=None):
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"url": "https://x/"}), "")

        monkeypatch.setattr(merge_gate.subprocess, "run", fake_run)

        with pytest.raises(RuntimeError, match="could not read a repository"):
            merge_gate._repo_of("84")


class TestRepositoryScoping:
    """The open-issue list decides the verdict, so it must name the PR's repo.

    Both bugs survived the 42-test suite: _stub_gh answers the issue list
    whatever repo is asked for, and the display lookup sat outside main's
    try. Assert on the emitted argv and the exit code rather than on the
    result, or the regression is invisible again.
    """

    def test_open_issue_list_is_scoped_to_the_prs_repository(self, monkeypatch):
        # In a fork clone gh's cwd-resolved repo is not the PR's. Left
        # unscoped, live references are intersected against the fork's
        # issues, vanish, and the gate reports "closes none" and exits 0.
        seen = []

        def fake_run(cmd, capture_output=True, text=True, timeout=None):
            seen.append(cmd)
            if "issue" in cmd and "list" in cmd:
                assert "--repo" in cmd, "the open-issue list must name a repository"
                repo = cmd[cmd.index("--repo") + 1]
                out = json.dumps([{"number": 83}] if repo == "github.com/upstream/aiform" else [])
            elif "url" in cmd:
                out = json.dumps({"url": "https://github.com/upstream/aiform/pull/84"})
            elif "api" in cmd:
                out = ""
            else:
                out = json.dumps(
                    {
                        "closingIssuesReferences": [
                            {
                                "number": 83,
                                "repository": {"name": "aiform", "owner": {"login": "upstream"}},
                            }
                        ]
                    }
                )
            return subprocess.CompletedProcess(cmd, 0, out, "")

        monkeypatch.setattr(merge_gate.subprocess, "run", fake_run)

        assert merge_gate.issues_closed_by("84") == {("upstream/aiform", 83)}

        listing = next(c for c in seen if "issue" in c and "list" in c)
        assert listing[listing.index("--repo") + 1] == "github.com/upstream/aiform"

        # The other half of the same guarantee. Asserting only on the issue
        # list is how "the fix was half applied" recurred twice already:
        # hardcoding a wrong path here left all 46 tests green.
        api = next(c for c in seen if "api" in c)
        assert "repos/upstream/aiform/pulls/84/commits" in api
        assert api[api.index("--hostname") + 1] == "github.com"

    def test_the_repo_is_resolved_once(self, monkeypatch):
        # main used to look the url up a second time purely to format the
        # output; that call is what sat outside the guard.
        _stub_gh(monkeypatch, {"closingIssuesReferences": [{"number": 83}]})
        calls = []
        original = merge_gate.subprocess.run

        def counting(cmd, **kwargs):
            if "url" in cmd:
                calls.append(cmd)
            return original(cmd, **kwargs)

        monkeypatch.setattr(merge_gate.subprocess, "run", counting)

        assert merge_gate.main(["84"]) == 0
        assert len(calls) == 1

    def test_a_second_url_lookup_cannot_escape_main(self, monkeypatch):
        # main used to resolve the url again outside its try, catching only
        # RuntimeError. A KeyError there escaped, Python exited 1, and
        # SKILL.md reads 1 as "needs the -multi acknowledgement" -- the
        # false waiver request the 1-vs-2 split exists to prevent. So fail
        # only the second lookup: pre-fix that escapes, post-fix there is
        # no second lookup to fail.
        seen = {"n": 0}

        def fake_run(cmd, capture_output=True, text=True, timeout=None):
            if "issue" in cmd and "list" in cmd:
                out = json.dumps([{"number": 83}])
            elif "url" in cmd:
                seen["n"] += 1
                out = json.dumps(
                    {"url": "https://github.com/telleztec/aiform/pull/84"} if seen["n"] == 1 else {}
                )
            elif "api" in cmd:
                out = ""
            else:
                out = json.dumps({"closingIssuesReferences": [{"number": 83}]})
            return subprocess.CompletedProcess(cmd, 0, out, "")

        monkeypatch.setattr(merge_gate.subprocess, "run", fake_run)

        assert merge_gate.main(["84"]) == 0

    def test_a_lookup_failure_is_exit_2_not_1(self, monkeypatch):
        # The contract SKILL.md keys off: 2 is "could not run", never 1.
        def fake_run(cmd, capture_output=True, text=True, timeout=None):
            if "issue" in cmd and "list" in cmd:
                out = json.dumps([{"number": 83}])
            elif "url" in cmd:
                out = json.dumps({})
            elif "api" in cmd:
                out = ""
            else:
                out = json.dumps({"closingIssuesReferences": []})
            return subprocess.CompletedProcess(cmd, 0, out, "")

        monkeypatch.setattr(merge_gate.subprocess, "run", fake_run)

        assert merge_gate.main(["84"]) == 2

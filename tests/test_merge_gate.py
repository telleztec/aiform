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


def _stub_gh(monkeypatch, payload, returncode=0, stderr=""):
    def fake_run(cmd, capture_output=True, text=True):
        return subprocess.CompletedProcess(cmd, returncode, json.dumps(payload), stderr)

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
                "commits": [{"messageHeadline": "x", "messageBody": "Closes #74"}],
            },
        )

        assert merge_gate.issues_closed_by("84") == {83, 74}

    def test_reads_the_commit_subject_not_only_the_body(self, monkeypatch):
        # gh puts the subject in messageHeadline; scanning only messageBody
        # misses `Fixes #74: ...` entirely.
        _stub_gh(
            monkeypatch,
            {
                "closingIssuesReferences": [],
                "commits": [{"messageHeadline": "Fixes #74: stop the leak", "messageBody": ""}],
            },
        )

        assert merge_gate.issues_closed_by("84") == {74}

    def test_deduplicates_across_sources(self, monkeypatch):
        # A single-commit PR has its body prefilled from the commit, so the
        # same keyword legitimately appears twice.
        _stub_gh(
            monkeypatch,
            {
                "closingIssuesReferences": [{"number": 83}],
                "commits": [{"messageHeadline": "Closes #83", "messageBody": ""}],
            },
        )

        assert merge_gate.issues_closed_by("84") == {83}

    def test_lookup_failure_raises_rather_than_returning_empty(self, monkeypatch):
        # Failing open here would post human-approval on an unchecked PR.
        _stub_gh(monkeypatch, {}, returncode=1, stderr="rate limited")

        with pytest.raises(RuntimeError, match="rate limited"):
            merge_gate.issues_closed_by("84")

    def test_unparseable_output_raises(self, monkeypatch):
        def fake_run(cmd, capture_output=True, text=True):
            return subprocess.CompletedProcess(cmd, 0, "not json", "")

        monkeypatch.setattr(merge_gate.subprocess, "run", fake_run)

        with pytest.raises(RuntimeError, match="unparseable"):
            merge_gate.issues_closed_by("84")

    def test_null_fields_are_not_a_crash(self, monkeypatch):
        _stub_gh(monkeypatch, {"closingIssuesReferences": None, "commits": None})

        assert merge_gate.issues_closed_by("84") == set()


class TestMain:
    def test_single_issue_passes_without_multi(self, monkeypatch, capsys):
        _stub_gh(monkeypatch, {"closingIssuesReferences": [{"number": 83}], "commits": []})

        assert merge_gate.main(["84"]) == 0
        assert "#83" in capsys.readouterr().out

    def test_zero_issues_passes(self, monkeypatch):
        _stub_gh(monkeypatch, {"closingIssuesReferences": [], "commits": []})

        assert merge_gate.main(["84"]) == 0

    def test_multiple_issues_blocked_without_multi(self, monkeypatch, capsys):
        _stub_gh(
            monkeypatch,
            {"closingIssuesReferences": [{"number": 83}, {"number": 74}], "commits": []},
        )

        assert merge_gate.main(["84"]) == 1
        err = capsys.readouterr().err
        assert "/claude-merge-approved-multi" in err
        assert "#74" in err and "#83" in err

    def test_multiple_issues_allowed_with_multi(self, monkeypatch):
        _stub_gh(
            monkeypatch,
            {"closingIssuesReferences": [{"number": 83}, {"number": 74}], "commits": []},
        )

        assert merge_gate.main(["84", "--multi"]) == 0

    def test_lookup_failure_exits_two_not_zero(self, monkeypatch):
        # Distinct from BLOCKED so a caller cannot mistake an outage for a pass.
        _stub_gh(monkeypatch, {}, returncode=1, stderr="boom")

        assert merge_gate.main(["84"]) == 2

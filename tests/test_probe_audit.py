# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Tests for probes/_audit.py -- see specs/driver_probing.md's
"The audit log".

The format is fixed before mechanism 2 exists precisely so a
mechanism-2 run can be diffed against a mechanism-1 run for the same
resource, so these tests pin the shape, not just the behavior.
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "probes"))

from _audit import Audit, format_line  # noqa: E402

LINE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z step=[a-z]+(?: [a-z_]+=\S+)*(?: msg=\".*\")?$"
)


class TestLineFormat:
    def test_matches_the_specs_log_convention(self):
        line = format_line("probe", {"ref": "02", "verdict": "contradicted"}, msg="status settled")
        assert LINE.match(line), line
        assert " step=probe " in line
        assert line.endswith('msg="status settled"')

    def test_timestamp_is_whole_second_utc(self):
        # Same %Y-%m-%dT%H:%M:%SZ convention as specs/log.md, so the two
        # records can be read side by side.
        assert format_line("impl", {}).split(" ")[0].endswith("Z")

    def test_fields_keep_the_order_given(self):
        line = format_line("review", {"round": 1, "model": "fable", "findings": 10})
        assert "round=1 model=fable findings=10" in line

    def test_msg_is_always_last(self):
        line = format_line("spec", {"cites": "02"}, msg="create() does not poll")
        assert line.index("cites=") < line.index("msg=")

    def test_a_quote_in_msg_is_escaped_not_dropped(self):
        line = format_line("probe", {}, msg='ports 22 stored as "22"')
        assert r"\"22\"" in line
        assert LINE.match(line), line

    def test_a_newline_in_msg_cannot_forge_a_second_line(self):
        line = format_line("probe", {}, msg="first\nsecond")
        assert "\n" not in line

    def test_no_msg_means_no_trailing_msg_field(self):
        assert "msg=" not in format_line("test", {"ref": "t", "state": "red"})


class TestAudit:
    def test_appends_and_never_rewrites(self, tmp_path):
        audit = Audit(tmp_path / "AUDIT.log")
        audit.append("recall", provider="digitalocean", skipped=0)
        audit.append("impl", ref="create", state="green")
        first = (tmp_path / "AUDIT.log").read_text().splitlines()

        audit.append("verify", kind="live", result="pass")
        after = (tmp_path / "AUDIT.log").read_text().splitlines()

        assert after[: len(first)] == first
        assert len(after) == 3

    def test_creates_the_driver_directory(self, tmp_path):
        target = tmp_path / "knowledge" / "drivers" / "digitalocean_firewall" / "AUDIT.log"
        Audit(target).append("recall", provider="digitalocean")
        assert target.is_file()

    def test_refuses_a_line_carrying_a_credential(self, tmp_path):
        audit = Audit(tmp_path / "AUDIT.log", secret="dop_v1_supersecret")
        with pytest.raises(ValueError, match="credential"):
            audit.append("probe", ref="01", msg="sent dop_v1_supersecret")
        assert not (tmp_path / "AUDIT.log").exists()

    def test_refuses_a_verdict_that_is_not_confirmed_or_contradicted(self, tmp_path):
        # `grep verdict=contradicted` is the finding list, so a third
        # spelling would silently hide findings.
        audit = Audit(tmp_path / "AUDIT.log")
        with pytest.raises(ValueError, match="verdict"):
            audit.append("probe", ref="01", verdict="maybe")

    def test_a_spec_step_must_cite_a_transcript(self, tmp_path):
        # specs/driver_probing.md: a spec claim without `cites=` is not
        # allowed to say "verified".
        audit = Audit(tmp_path / "AUDIT.log")
        with pytest.raises(ValueError, match="cites"):
            audit.append("spec", ref="behavior/create", msg="create() does not poll")
        audit.append("spec", ref="behavior/create", cites="02", msg="ok")

    def test_rejects_an_unknown_step(self, tmp_path):
        audit = Audit(tmp_path / "AUDIT.log")
        with pytest.raises(ValueError, match="step"):
            audit.append("ponder", msg="hmm")

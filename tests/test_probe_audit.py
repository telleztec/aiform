# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Tests for probes/_audit.py -- see specs/driver_creation.md's
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
        stamp = format_line("impl", {}).split(" ")[0]
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", stamp), stamp

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
        # specs/driver_creation.md: a spec claim without `cites=` is not
        # allowed to say "verified".
        audit = Audit(tmp_path / "AUDIT.log")
        with pytest.raises(ValueError, match="cites"):
            audit.append("spec", ref="behavior/create", msg="create() does not poll")
        audit.append("spec", ref="behavior/create", cites="02", msg="ok")

    def test_conf_must_be_a_band_anchor(self, tmp_path):
        # A value between the anchors would imply a precision the
        # judgement does not have -- specs/driver_creation.md's
        # Confidence rubric fixes five and only five.
        audit = Audit(tmp_path / "AUDIT.log")
        with pytest.raises(ValueError, match="conf must be one of"):
            audit.append("probe", ref="02", conf=73)

    def test_each_band_anchor_is_accepted(self, tmp_path):
        audit = Audit(tmp_path / "AUDIT.log")
        for anchor in (10, 40, 60, 80, 95):
            assert f"conf={anchor}" in audit.append("impl", ref="create", conf=anchor)

    def test_a_step_that_changes_nothing_known_may_not_claim_confidence(self, tmp_path):
        # test/review/fix record work done, not knowledge gained. Letting
        # them carry conf= would let the column climb on a pass that
        # learned nothing, which is exactly the red flag it exists to show.
        audit = Audit(tmp_path / "AUDIT.log")
        for step in ("test", "review", "fix"):
            with pytest.raises(ValueError, match="may not carry conf="):
                audit.append(step, conf=60)

    def test_rejects_an_unknown_step(self, tmp_path):
        audit = Audit(tmp_path / "AUDIT.log")
        with pytest.raises(ValueError, match="step"):
            audit.append("ponder", msg="hmm")


class TestFieldValuesCannotForgeStructure:
    """The cites= and verdict= rules inspect the fields dict, so a value
    that renders into extra structure would slip past them."""

    def test_a_space_in_a_value_cannot_forge_a_second_field(self):
        # Asserted on the rendered text, not a slice of it: slicing to
        # the first whitespace-delimited token passes against the very
        # unquoted renderer this is written to catch.
        line = format_line("impl", {"ref": "x verdict=contradicted"})
        assert line.endswith('ref="x verdict=contradicted"'), line

    def test_a_forged_field_does_not_survive_a_field_aware_read(self):
        # Note what is and is not claimed. A naive `grep
        # verdict=contradicted` DOES still match the forged line, because
        # the text sits inside a quoted value -- quoting cannot hide a
        # substring. What quoting buys is that any reader which respects
        # quotes (shlex, or the same rules specs/log.md's format implies)
        # sees one field, not two.
        import shlex

        forged = shlex.split(format_line("impl", {"ref": "x verdict=contradicted"}))
        real = shlex.split(format_line("probe", {"ref": "04", "verdict": "contradicted"}))

        assert "verdict=contradicted" in real
        assert "verdict=contradicted" not in forged
        assert "ref=x verdict=contradicted" in forged

    def test_a_newline_in_a_value_cannot_forge_a_line(self):
        line = format_line("impl", {"ref": "x\nstep=spec"})
        assert "\n" not in line

    def test_an_ordinary_value_is_not_quoted(self):
        assert "ref=02" in format_line("probe", {"ref": "02"})

    def test_a_quote_in_a_value_is_escaped(self):
        line = format_line("impl", {"ref": 'a"b'})
        assert r"\"" in line
        assert LINE.match(line), line

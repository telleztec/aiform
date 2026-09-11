# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""The per-driver audit log -- see specs/driver_creation.md.

One append-only file per driver recording how it came to exist: what was
probed, what contradicted a prediction, which spec claim cites which
transcript, when tests went red then green, what review found, and what
was learned. It is the human's window into a driver's creation and the
artifact a review is conducted against.

Both curation mechanisms write it. That is deliberate: because the
format is identical, a mechanism-2 run can be diffed against a
mechanism-1 run for the same resource -- which probes it skipped, which
findings it missed, how many review rounds it needed. That diff is how
the generator gets tuned, which is why this format is fixed before
mechanism 2 exists.

Format follows specs/log.md's convention -- same UTC
%Y-%m-%dT%H:%M:%SZ stamp, bare space-separated key=value, trailing
msg="free text" -- minus LEVEL and logger_name, which would be constant
on every line. It lives in knowledge/ rather than .aiform/logs/ because
it is a durable record of how a driver was built, not runtime
diagnostics for one command.
"""

import datetime
from pathlib import Path
from typing import Any

# One line per DECISION, not per action. A 31-probe session emits a line
# per contradiction and per probe a spec claim cites, plus one summary
# line for the rest -- a whole driver should read in 20-40 lines.
STEPS = (
    "recall",  # what prior knowledge let this session skip, and what it must probe
    "probe",  # a contradiction, a cited probe, or the closing count
    "spec",  # a claim landed in the driver spec; must carry cites=
    "test",  # state=red / state=green
    "impl",
    "review",  # round=N model=... findings=N
    "fix",
    "verify",  # kind=live/unit result=pass/fail
    "learn",  # promoted=N -- what generalised into knowledge/
    "note",  # a correction to the record itself; asserts nothing about the resource
)

# `grep verdict=contradicted` is the finding list for a whole driver, so
# a third spelling would silently hide findings.
VERDICTS = ("confirmed", "contradicted")

# specs/driver_creation.md's Confidence rubric. Five anchors and nothing
# between them: the score is a judgement on a five-point ladder, and a
# value like 73 would read as a measurement that no step produces.
CONF_GUESSED, CONF_DOCUMENTED, CONF_OBSERVED, CONF_REPRODUCED, CONF_CONVERGED = (
    10,
    40,
    60,
    80,
    95,
)
CONF_ANCHORS = (CONF_GUESSED, CONF_DOCUMENTED, CONF_OBSERVED, CONF_REPRODUCED, CONF_CONVERGED)

# Steps that record work, or the record itself, rather than knowledge.
# Letting them carry conf= would let the column climb on a pass that
# learned nothing -- the exact red flag reading that column down the file
# is meant to expose.
STEPS_WITHOUT_CONF = ("test", "review", "fix", "note")

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _quote(msg: str) -> str:
    # A newline would forge a second log line, and an unescaped quote
    # would break the trailing msg="..." field. Both are collapsed rather
    # than rejected: a message is free text and should never be the
    # reason an audit entry fails to record.
    flattened = " ".join(msg.split())
    return '"' + flattened.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _field(value: Any) -> str:
    """Render one field value.

    Quoted whenever it contains whitespace or a quote, matching
    specs/log.md's own key=value convention. Not cosmetic: an unquoted
    value carrying a space could otherwise forge a second field, and one
    carrying a newline could forge a whole line -- past the cites= and
    verdict= checks, which look at the dict, not the rendered text.
    """
    rendered = str(value)
    if rendered and not any(c.isspace() or c == '"' for c in rendered):
        return rendered
    return _quote(rendered)


def format_line(step: str, fields: dict[str, Any], msg: str | None = None) -> str:
    stamp = datetime.datetime.now(datetime.UTC).strftime(TIMESTAMP_FORMAT)
    parts = [stamp, f"step={step}"]
    parts += [f"{key}={_field(value)}" for key, value in fields.items()]
    if msg:
        parts.append(f"msg={_quote(msg)}")
    return " ".join(parts)


class Audit:
    """Append-only. A correction is a new line, never an edit -- the
    history of what was believed is the record's value."""

    def __init__(self, path: Path | str, *, secret: str | None = None):
        self.path = Path(path)
        self._secret = secret or None

    def append(self, step: str, msg: str | None = None, **fields: Any) -> str:
        if step not in STEPS:
            raise ValueError(f"unknown step {step!r}; expected one of {list(STEPS)}")

        verdict = fields.get("verdict")
        if verdict is not None and verdict not in VERDICTS:
            raise ValueError(f"verdict must be one of {list(VERDICTS)}, got {verdict!r}")

        conf = fields.get("conf")
        if conf is not None:
            # bool is an int subclass, and 60.0 == 60 passes a membership
            # test then renders as conf=60.0 -- a spelling no band has.
            if type(conf) is not int or conf not in CONF_ANCHORS:
                raise ValueError(f"conf must be one of {list(CONF_ANCHORS)}, got {conf!r}")
            if step in STEPS_WITHOUT_CONF:
                raise ValueError(
                    f"a step={step} entry may not carry conf=; it records work, not knowledge"
                )

        # A spec claim with no transcript behind it is exactly the
        # failure this process exists to prevent, so it cannot be
        # recorded at all.
        if step == "spec" and "cites" not in fields:
            raise ValueError(
                "a step=spec entry must carry cites=<transcript prefix>; a claim with "
                "no transcript behind it may not be recorded as one"
            )

        line = format_line(step, fields, msg)
        if self._secret and self._secret in line:
            raise ValueError("refusing to write an audit line containing a credential")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return line

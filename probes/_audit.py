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
)

# `grep verdict=contradicted` is the finding list for a whole driver, so
# a third spelling would silently hide findings.
VERDICTS = ("confirmed", "contradicted")

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _quote(msg: str) -> str:
    # A newline would forge a second log line, and an unescaped quote
    # would break the trailing msg="..." field. Both are collapsed rather
    # than rejected: a message is free text and should never be the
    # reason an audit entry fails to record.
    flattened = " ".join(msg.split())
    return '"' + flattened.replace("\\", "\\\\").replace('"', '\\"') + '"'


def format_line(step: str, fields: dict[str, Any], msg: str | None = None) -> str:
    stamp = datetime.datetime.now(datetime.UTC).strftime(TIMESTAMP_FORMAT)
    parts = [stamp, f"step={step}"]
    parts += [f"{key}={value}" for key, value in fields.items()]
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

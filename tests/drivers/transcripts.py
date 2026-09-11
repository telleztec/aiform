# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Load a recorded probe transcript as a unit-test payload.

This is deliberately *not* a cassette system. A cassette intercepts the
HTTP layer and replays recorded interactions, which couples tests to a
recorded call order and makes the recording authoritative over the code.
This is a one-way data flow -- reality to JSON to a dict a test hands to
the FakeUrlopen it still writes itself. No interception, no replay, no
ordering coupling, no dependency.

The single property gained is the one that matters: a mock payload
cannot encode a belief nobody checked.
"""

import json
from pathlib import Path

TRANSCRIPTS_ROOT = Path(__file__).resolve().parents[2] / "probes" / "transcripts"


def _load(session: str, prefix: str) -> dict:
    directory = TRANSCRIPTS_ROOT / session
    matches = sorted(directory.glob(f"{prefix}*.json"))
    if not matches:
        raise FileNotFoundError(
            f"no transcript {prefix!r} in {directory}; re-run "
            f"probes/{session}.py --mutate to regenerate"
        )
    return json.loads(matches[0].read_text(encoding="utf-8"))


def response_body(session: str, prefix: str) -> dict:
    """The response body a probe actually received."""
    return _load(session, prefix)["response"]["body"]


def request_body(session: str, prefix: str) -> dict:
    """The request body a probe actually sent."""
    return _load(session, prefix)["request"]["body"]

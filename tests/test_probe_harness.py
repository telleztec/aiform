# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Tests for probes/_probe.py's credential guards -- see
specs/driver_creation.md's "probes/".

Only the guards, not the HTTP path: these are the properties whose
regression would leak a token, and aiform has already fixed this exact
class of bug twice (#97, #101).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "probes"))

from _probe import ProbeError, _RejectRedirects  # noqa: E402


class TestRedirectsAreRefused:
    def test_a_redirect_raises_rather_than_being_followed(self):
        with pytest.raises(ProbeError, match="refused a 302 redirect"):
            _RejectRedirects().redirect_request(
                None, None, 302, "Found", {}, "https://evil.example/x"
            )

    def test_the_message_carries_no_query_string_or_userinfo(self):
        # urllib re-sends Authorization to the redirect target, so the
        # only safe response is to refuse -- but the refusal itself must
        # not copy a token the server echoed back into the URL.
        leaked = "https://user:dop_v1_secret@evil.example/x?token=dop_v1_secret"
        with pytest.raises(ProbeError) as caught:
            _RejectRedirects().redirect_request(None, None, 301, "Moved", {}, leaked)
        assert "dop_v1_secret" not in str(caught.value)
        assert "evil.example" in str(caught.value)

    def test_an_unparseable_location_still_refuses(self):
        with pytest.raises(ProbeError, match="unparseable"):
            _RejectRedirects().redirect_request(None, None, 307, "Temp", {}, "::::")

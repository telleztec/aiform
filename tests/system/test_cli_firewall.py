# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Live end-to-end system test for drivers/digitalocean/firewall.py
against the real DigitalOcean and Anthropic APIs, per
specs/system_test_firewall.md. Excluded from the default `pytest` run
(pyproject.toml's `addopts`); run explicitly with:

    pytest -m system tests/system/

**This suite creates nothing billable and touches no traffic.** Every
firewall it creates is unattached (`droplet_ids: []`), which
DigitalOcean hosts for free and which cannot affect any droplet's
connectivity. The only real cost is Anthropic-priced gate calls, so it
still must never run on the default pull_request/push CI trigger.

The point is not to re-prove the CLI, orchestrator, gates or state
machinery -- test_cli_digitalocean.py does that. It is to settle the
assumptions about DO's firewall API that firewall.py hardcodes and that
a mock structurally cannot falsify, because the mock encodes the same
assumption the driver does. Those assumptions came from
probes/digitalocean_firewall.py; this suite is what keeps them true.
"""

import pytest

from aiform import cli, state
from tests.system.conftest import (
    SYSTEM_TEST_TAG,
    assert_cli_ok,
    count_driver_reads,
    get_firewall_or_none,
    live_token,
    token_has_firewall_scope,
    unique_firewall_name,
    write_firewall_aiform_md,
)

pytestmark = pytest.mark.system

SSH_RULE = {
    "protocol": "tcp",
    "ports": "22",
    "action": "allow",
    "sources": {"addresses": ["0.0.0.0/0"]},
}
HTTPS_RULE = {
    "protocol": "tcp",
    "ports": "443",
    "action": "allow",
    "sources": {"addresses": ["0.0.0.0/0", "::/0"]},
}
DNS_OUT_RULE = {
    "protocol": "udp",
    "ports": "53",
    "action": "allow",
    "destinations": {"addresses": ["0.0.0.0/0"]},
}


def _resource_key(name: str) -> str:
    return f"digitalocean.firewall.{name}"


def _skip_without_firewall_scope(token) -> None:
    # Not redundant with init's [✓]: cli.py's preflight probes
    # GET /v2/droplets only, so a droplet-scoped token earns a green
    # check here and then fails at the first firewall apply.
    if not token_has_firewall_scope(token):
        pytest.skip(
            "this DIGITALOCEAN_TOKEN cannot read /v2/firewalls -- the firewall suite needs a "
            "token with `firewall` scope; aiform init's preflight only checks droplet access"
        )


class TestFirewallLifecycle:
    def test_full_lifecycle(self, project_dir, teardown_tracked_resources, monkeypatch, capsys):
        token = live_token()
        _skip_without_firewall_scope(token)

        state_path = project_dir / ".aiform" / "state.json"
        name = unique_firewall_name("lifecycle")
        key = _resource_key(name)
        reads = count_driver_reads(monkeypatch)

        write_firewall_aiform_md(
            project_dir, name=name, inbound_rules=[SSH_RULE], outbound_rules=[DNS_OUT_RULE]
        )

        # Exactly one Anthropic call, and it is worth being precise
        # about which. An untracked resource skips categorization (#118)
        # and gate #1 is gone from this path (#119) -- but parse_file()
        # still calls extract_intent_notes() whenever the .aiform.md
        # sha256 differs from the tracked one, which on a first plan it
        # always does. So the floor for a brand-new resource is one
        # intent parse, not zero.
        #
        # tests/system/test_cli_domain.py asserts zero here and is red on
        # main for exactly this reason: its comment reasons about
        # categorization and gate #1 and overlooks intent parsing.
        code = cli.main(["plan", "create", "--state-file", str(state_path), "--verbose"])
        captured = capsys.readouterr()
        assert_cli_ok(code, captured, "plan create")
        assert f"+ {key}: create" in captured.out
        assert "[verbose] 1 Anthropic API call(s) made" in captured.err

        # One again, and for the same reason: `plan create` does not
        # write the tracked sha256 -- only a completed apply does -- so
        # apply re-parses a file it still considers new. A CREATE action
        # never triggers gate #2 (apply_plan()'s needs_review covers
        # DESTROY and likely-replace UPDATE only), so the intent parse is
        # the whole cost.
        code = cli.main(["plan", "apply", "--yes", "--state-file", str(state_path), "--verbose"])
        captured = capsys.readouterr()
        assert_cli_ok(code, captured, "plan apply")
        assert "[verbose] 1 Anthropic API call(s) made" in captured.err

        st = state.load(state_path)
        assert key in st.resources
        firewall_id = st.resources[key].id

        live = get_firewall_or_none(token, firewall_id)
        assert live is not None, "firewall was not created on DigitalOcean's side"

        # The premise this whole suite rests on: nothing was attached, so
        # nothing can have been affected.
        assert live["droplet_ids"] == [], (
            f"expected an unattached firewall, got droplet_ids={live['droplet_ids']!r} -- "
            "this suite must never attach to a real droplet"
        )
        assert SYSTEM_TEST_TAG in live["tags"], "the sweep backstop keys on this tag"

        # `action` is added by DigitalOcean and absent from its published
        # OpenAPI schema. If DO ever stops returning it, or the driver
        # stops projecting it, read() no longer equals params and this
        # resource diffs forever.
        assert live["inbound_rules"][0]["action"] == "allow", (
            "DigitalOcean no longer returns the undocumented `action` field; "
            "specs/digitalocean_firewall.md's projection is built on it"
        )

        # The zero-diff invariant, live: re-planning an unchanged
        # resource must be a no-op, and must cost nothing.
        code = cli.main(["plan", "create", "--state-file", str(state_path), "--verbose"])
        captured = capsys.readouterr()
        assert_cli_ok(code, captured, "re-plan after apply")
        assert f"{key}: no-op" in captured.out, (
            f"expected a no-op re-plan; got:\n{captured.out}\n"
            "a permanent diff here means read() no longer round-trips against params"
        )
        # THE assertion of this suite. The sha now matches, so no intent
        # parse; the diff is empty, so no categorization. Zero is only
        # reachable if read() round-trips exactly against the params the
        # user wrote -- which is what every rejection in _validate_rule()
        # exists to guarantee.
        assert "[verbose] 0 Anthropic API call(s) made" in captured.err

        # Update: add a rule, apply, and require convergence again.
        write_firewall_aiform_md(
            project_dir,
            name=name,
            inbound_rules=[SSH_RULE, HTTPS_RULE],
            outbound_rules=[DNS_OUT_RULE],
        )
        # Two now: the edited file is a new sha (intent parse) and the
        # non-empty diff reaches categorize_diff. Measured, not assumed.
        code = cli.main(["plan", "create", "--state-file", str(state_path), "--verbose"])
        captured = capsys.readouterr()
        assert_cli_ok(code, captured, "plan create after edit")
        assert f"~ {key}: update" in captured.out
        assert "[verbose] 2 Anthropic API call(s) made" in captured.err

        code = cli.main(["plan", "apply", "--yes", "--state-file", str(state_path)])
        captured = capsys.readouterr()
        assert_cli_ok(code, captured, "plan apply after edit")

        live = get_firewall_or_none(token, firewall_id)
        assert len(live["inbound_rules"]) == 2

        # PUT is a whole-object replace (probe transcripts 24-26): the
        # tag was not in the diff, and must still survive the update.
        assert SYSTEM_TEST_TAG in live["tags"], (
            "the update reset tags that were not part of the diff -- _wire_body() must send "
            "every managed field on every write"
        )

        code = cli.main(["plan", "create", "--state-file", str(state_path), "--verbose"])
        captured = capsys.readouterr()
        assert_cli_ok(code, captured, "re-plan after update")
        assert f"{key}: no-op" in captured.out
        assert "[verbose] 0 Anthropic API call(s) made" in captured.err

        assert reads, "expected the orchestrator to have called driver.read()"

        # Destroy, and confirm it is really gone rather than merely
        # untracked.
        code = cli.main(["plan", "destroy", "--yes", "--state-file", str(state_path)])
        captured = capsys.readouterr()
        assert_cli_ok(code, captured, "plan destroy")
        assert get_firewall_or_none(token, firewall_id) is None, (
            "firewall still exists on DigitalOcean after destroy"
        )


class TestEverySupportedRuleShape:
    """One rule per shape the PARAM_SCHEMA accepts, required to converge
    to a stable no-op. This is the case that would surface a field the
    spec calls required but DO rejects, or demands but the spec omits --
    the shape of failure a hand-written mock cannot produce."""

    def test_every_shape_converges(self, project_dir, teardown_tracked_resources, capsys):
        token = live_token()
        _skip_without_firewall_scope(token)

        state_path = project_dir / ".aiform" / "state.json"
        name = unique_firewall_name("shapes")
        key = _resource_key(name)

        inbound = [
            SSH_RULE,
            {  # a port range, stored verbatim
                "protocol": "tcp",
                "ports": "8000-9000",
                "action": "allow",
                "sources": {"addresses": ["10.0.0.0/8"]},
            },
            {  # "0" is how DigitalOcean spells "all ports"
                "protocol": "udp",
                "ports": "0",
                "action": "allow",
                "sources": {"addresses": ["0.0.0.0/0"]},
            },
            {  # icmp, where DO synthesizes "0" when ports is omitted --
                # so the driver requires it written explicitly
                "protocol": "icmp",
                "ports": "0",
                "action": "allow",
                "sources": {"addresses": ["0.0.0.0/0"]},
            },
            {  # sources by tag rather than address: a live-membership
                # relationship, and the tag must already exist (probe 20)
                "protocol": "tcp",
                "ports": "9200",
                "action": "allow",
                "sources": {"tags": [SYSTEM_TEST_TAG]},
            },
            {  # IPv6, returned verbatim rather than renormalized
                "protocol": "tcp",
                "ports": "8443",
                "action": "allow",
                "sources": {"addresses": ["2001:db8::/32"]},
            },
        ]

        write_firewall_aiform_md(
            project_dir, name=name, inbound_rules=inbound, outbound_rules=[DNS_OUT_RULE]
        )

        code = cli.main(["plan", "create", "--state-file", str(state_path), "--verbose"])
        captured = capsys.readouterr()
        assert_cli_ok(code, captured, "plan create (all shapes)")
        assert "[verbose] 1 Anthropic API call(s) made" in captured.err

        code = cli.main(["plan", "apply", "--yes", "--state-file", str(state_path)])
        captured = capsys.readouterr()
        assert_cli_ok(code, captured, "plan apply (all shapes)")

        st = state.load(state_path)
        live = get_firewall_or_none(token, st.resources[key].id)
        assert live is not None
        assert live["droplet_ids"] == [], "this suite must never attach to a real droplet"
        assert len(live["inbound_rules"]) == len(inbound)

        # The assertion that matters: every shape above round-trips, so a
        # second plan sees no work to do.
        code = cli.main(["plan", "create", "--state-file", str(state_path), "--verbose"])
        captured = capsys.readouterr()
        assert_cli_ok(code, captured, "re-plan (all shapes)")
        assert f"{key}: no-op" in captured.out, (
            f"a supported rule shape does not round-trip; got:\n{captured.out}"
        )
        assert "[verbose] 0 Anthropic API call(s) made" in captured.err

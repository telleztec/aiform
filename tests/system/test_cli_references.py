# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Live end-to-end system test for cross-resource attribute references
(specs/resource_references.md, Phase 2 of specs/MULTI_RESOURCE_PRD.md) against the
real DigitalOcean and Anthropic APIs. Excluded from the default `pytest` run
(see pyproject.toml's `addopts`); run explicitly with:

    pytest -m system tests/system/

Requires ANTHROPIC_API_KEY and DIGITALOCEAN_TOKEN, and a token carrying
`domain`, droplet and firewall scope.

**This one is billable.** It creates a real droplet, unlike the domain suite,
because the whole point is a value that only the provider can supply: a
droplet's `ipv4_address` is assigned at create time and cannot be predicted,
mocked, or asserted from a fixture. A mock would encode the same assumption
the code does.

What this settles that no unit test can:

- that `plan` can be built and applied in ONE pass when the referenced value
  does not exist yet, against a provider that assigns it asynchronously;
- that the address the zone ends up publishing is the address the droplet
  actually got, read back from DigitalOcean rather than from aiform's state;
- that a second `plan` over the applied pair is a clean no-op costing zero
  Anthropic calls, which is the property the whole reference design is built
  around and which a unit test can only prove against a fake;
- (#216) that DigitalOcean actually accepts the integer a `:provider_id`
  reference resolves to for `droplet_ids`, and hands that same integer back
  rather than a stringified copy of it. A mock can only assert the value
  aiform sent; it cannot show what DigitalOcean does with it.

Deliberately NOT re-proved here: ordering. Phase 1 orders by resource *key*,
and `digitalocean.compute.*` sorts before both `digitalocean.domain.*` and
`digitalocean.firewall.*` regardless of whether an edge exists, so neither
pairing in this file can distinguish a derived edge from the plain sorted()
drain. tests/test_orchestrator.py does that, with a dependent whose key sorts
*before* its target. What is proved live is value flow, which no ordering
accident can fake.
"""

import pytest

from aiform import cli, state
from tests.system.conftest import (
    assert_cli_ok,
    ensure_system_test_tag,
    get_domain_or_none,
    get_droplet_or_none,
    get_firewall_or_none,
    list_domain_records,
    live_token,
    token_has_domain_scope,
    token_has_firewall_scope,
    token_owns_zone_parent,
    unique_droplet_name,
    unique_firewall_name,
    unique_zone_name,
    verbose_call_count,
    wait_until_domain_gone,
    wait_until_droplet_gone,
    write_aiform_md,
    write_domain_aiform_md,
    write_firewall_aiform_md,
)

pytestmark = pytest.mark.system

TTL = 1800
RECORD_NAME = "www"
SSH_RULE = {
    "protocol": "tcp",
    "ports": "22",
    "action": "allow",
    "sources": {"addresses": ["0.0.0.0/0"]},
}


def _resource_key(name: str) -> str:
    return f"digitalocean.firewall.{name}"


def _skip_without_firewall_scope(token) -> None:
    # Mirrors test_cli_firewall.py's own helper of the same name: aiform
    # init's preflight probes GET /v2/droplets only, so a droplet-scoped
    # token earns a green check and then fails at the first firewall apply.
    if not token_has_firewall_scope(token):
        pytest.skip("this DIGITALOCEAN_TOKEN cannot read /v2/firewalls")


def _skip_without_domain_scope(token) -> None:
    # #216 review (F6): this used to be a module-scoped autouse fixture,
    # which meant a firewall-scoped token on a team without the zone parent
    # silently skipped the whole module -- including the droplet_ids/
    # provider_id test below, which needs neither `domain` scope nor the
    # zone parent. Each live test now gates on exactly what it needs.
    if not token_has_domain_scope(token):
        pytest.skip("token lacks `domain` scope")
    if not token_owns_zone_parent(token):
        pytest.skip("account does not own the system-test zone parent")


class TestCrossResourceReferenceLive:
    def test_one_apply_publishes_the_droplets_real_address(
        self, project_dir, teardown_tracked_resources, capsys
    ):
        # live_token() returns a RedactedSecret, a str subclass -- passed
        # through rather than str()'d, so a traceback frame cannot hold
        # the bare token.
        token = live_token()
        _skip_without_domain_scope(token)
        # unique_droplet_name(), NOT unique_name("aiform-system-test-droplet-..."):
        # is_sweepable_droplet() keys off SYSTEM_TEST_DROPLET_PREFIX, which is
        # deliberately not a prefix of the compute suite's own names, so a
        # droplet named that way has no sweep backstop at all. This one is
        # billable, so it gets one. The prefix reads "fwdrop" for historical
        # reasons -- the firewall suite introduced it -- but the sweep is what
        # matters here, not the spelling.
        droplet_name = unique_droplet_name("ref")
        zone = unique_zone_name("ref")

        # Filenames are irrelevant to ordering (keys decide), so they are named
        # for legibility rather than to stage an ordering trap -- see the module
        # docstring.
        write_aiform_md(project_dir, name=droplet_name, filename="droplet.aiform.md")
        write_domain_aiform_md(
            project_dir,
            name=zone,
            records=[
                {
                    "type": "A",
                    "name": RECORD_NAME,
                    "ttl": TTL,
                    "data": f"${{digitalocean.compute.{droplet_name}:ipv4_address}}",
                }
            ],
            filename="zone.aiform.md",
        )

        # Step 1: one plan, one apply, for both resources together. The A
        # record's value does not exist anywhere at this point.
        code = cli.main(["plan", "create"])
        first_plan = capsys.readouterr()
        assert_cli_ok(code, first_plan, "plan create")
        # The unresolved reference prints verbatim, with no annotation.
        assert f"${{digitalocean.compute.{droplet_name}:ipv4_address}}" in first_plan.out
        assert "known after apply" not in first_plan.out

        code = cli.main(["plan", "apply", "--yes"])
        assert_cli_ok(code, capsys.readouterr(), "plan apply")

        # Step 2: the address the zone published must be the one DigitalOcean
        # gave the droplet. Both sides are read back from the provider, not
        # from aiform's state -- state agreeing with itself proves nothing.
        droplet_key = f"digitalocean.compute.{droplet_name}"
        tracked = state.load(state.DEFAULT_STATE_PATH, deployment="default")
        droplet_ip = tracked.resources[droplet_key].attributes["ipv4_address"]
        assert droplet_ip, "droplet has no public v4 address to reference"

        assert get_domain_or_none(token, zone) is not None
        published = [
            record
            for record in list_domain_records(token, zone)
            if record["type"] == "A" and record["name"] == RECORD_NAME
        ]
        assert len(published) == 1, f"expected exactly one A record, got {published}"
        assert published[0]["data"] == droplet_ip
        # The literal must never have reached the provider.
        assert "${" not in published[0]["data"]

        # Step 3: the cost guarantee, on real input rather than a fake. A
        # resolved reference diffs clean, so the no-op short-circuit fires and
        # nothing is billed.
        # `--verbose` TRAILING, after the subcommand. `aiform -v plan create`
        # silently leaves verbose off (issue #134), which would make
        # verbose_call_count() raise on a missing [verbose] line and fail this
        # test on a correct implementation -- after the droplet had already
        # been paid for. Verified empirically, both placements. Do not "tidy"
        # this to the global flag until #134 is fixed.
        code = cli.main(["plan", "create", "--verbose"])
        second_plan = capsys.readouterr()
        assert_cli_ok(code, second_plan, "second plan create")
        # The per-resource marker line, not the bare substring: _print_plan's
        # summary tally always contains the words "no-op", so `"no-op" in out`
        # passes for an all-update plan too.
        assert f"= {droplet_key}: no-op" in second_plan.out
        assert f"= digitalocean.domain.{zone}: no-op" in second_plan.out
        assert verbose_call_count(second_plan) == 0
        # Resolved now, so the value shows rather than the reference.
        assert droplet_ip in second_plan.out

        # Step 4: `resource status` must read the zone as in sync. Asserted
        # positively rather than as "drift" being absent: _config_value()
        # renders the healthy case as "in sync with <path>", so the positive
        # form pins the verdict instead of the wording of its opposite.
        code = cli.main(["resource", "status", zone])
        status = capsys.readouterr()
        assert_cli_ok(code, status, "resource status")
        assert "in sync" in status.out
        assert "drifted" not in status.out

        # Step 5: destroy both, the zone first in reverse dependency order,
        # because the reference alone established that edge. If this step fails,
        # teardown_tracked_resources destroys both; if the process is killed
        # before that, the droplet's name and the zone's both match what the
        # session sweeps parse.
        code = cli.main(["plan", "destroy", "--all", "--deployment", "default", "--yes"])
        assert_cli_ok(code, capsys.readouterr(), "plan destroy")
        wait_until_domain_gone(token, zone)
        assert get_domain_or_none(token, zone) is None

    def test_droplet_ids_reference_publishes_the_droplets_provider_id(
        self, project_dir, teardown_tracked_resources, capsys
    ):
        # #216: a user reaching for the obvious ${...:id} syntax on an
        # int-typed field like droplet_ids gets a string, and
        # firewall.py's create() calls _validate_params() as its first
        # statement (before the POST), so it raises ValueError -- wrapped
        # by the orchestrator as DriverExecutionError -- without
        # DigitalOcean ever seeing the request. This settles the fix live:
        # droplet_ids can reference :provider_id and DigitalOcean accepts
        # the real int it resolves to -- the one case a mock cannot show,
        # since a mock encodes the same assumption the driver does.
        token = live_token()
        _skip_without_firewall_scope(token)
        # The firewall below carries SYSTEM_TEST_TAG, and firewall creation
        # -- unlike droplet creation -- does not auto-create a referenced
        # tag; it 422s "tag <name> does not exist" if the tag isn't already
        # there. Relying on the droplet being applied first in the same
        # plan to create it as a side effect would be an undocumented,
        # order-dependent accident.
        ensure_system_test_tag(token)

        droplet_name = unique_droplet_name("providerid")
        firewall_name = unique_firewall_name("providerid")
        firewall_key = _resource_key(firewall_name)

        write_aiform_md(project_dir, name=droplet_name, filename="droplet.aiform.md")
        write_firewall_aiform_md(
            project_dir,
            name=firewall_name,
            inbound_rules=[SSH_RULE],
            # A reference string, not a literal int -- write_firewall_aiform_md's
            # own type hint says list[int], but nothing at the YAML layer
            # enforces it, and this is exactly the value #216 is about: it
            # must resolve to a real int before DigitalOcean ever sees it.
            droplet_ids=[f"${{digitalocean.compute.{droplet_name}:provider_id}}"],
        )

        # Step 1: one plan, one apply, for both resources together. The
        # droplet's provider_id does not exist anywhere at this point.
        code = cli.main(["plan", "create"])
        first_plan = capsys.readouterr()
        assert_cli_ok(code, first_plan, "plan create")
        assert f"${{digitalocean.compute.{droplet_name}:provider_id}}" in first_plan.out

        code = cli.main(["plan", "apply", "--yes"])
        assert_cli_ok(code, capsys.readouterr(), "plan apply")

        # Step 2: the firewall's droplet_ids, read back from DigitalOcean
        # rather than aiform's state, must be the droplet's real int id.
        droplet_key = f"digitalocean.compute.{droplet_name}"
        tracked = state.load(state.DEFAULT_STATE_PATH, deployment="default")
        provider_id = tracked.resources[droplet_key].attributes["provider_id"]
        assert isinstance(provider_id, int)

        live_firewall = get_firewall_or_none(token, tracked.resources[firewall_key].id)
        assert live_firewall is not None
        assert live_firewall["droplet_ids"] == [provider_id]

        # Step 3: the cost guarantee, on real input. A resolved reference
        # diffs clean, so the no-op short-circuit fires and nothing is billed.
        code = cli.main(["plan", "create", "--verbose"])
        second_plan = capsys.readouterr()
        assert_cli_ok(code, second_plan, "second plan create")
        assert f"= {droplet_key}: no-op" in second_plan.out
        assert f"= {firewall_key}: no-op" in second_plan.out
        assert verbose_call_count(second_plan) == 0

        # Step 4: destroy both.
        code = cli.main(["plan", "destroy", "--all", "--deployment", "default", "--yes"])
        assert_cli_ok(code, capsys.readouterr(), "plan destroy")


class TestReverseDependentDestroyRefusalLive:
    """#225: `_build_destroy_plan_from_paths()` must refuse a paths-driven
    destroy that would orphan a tracked dependent outside the run, with
    --force as the escape hatch. Both the owner's scenarios in sequence:

    (1) drop one of a firewall's two droplet_ids references -- which also
        settles, live, that DigitalOcean actually detaches on a
        whole-object PUT carrying the shorter list (the driver's own
        comment marks that INFERRED for droplet_ids, verified only for
        tags);
    (2) then try to destroy the surviving referenced droplet, which must
        be refused, then proceed and warn under --force.
    """

    def test_dropping_a_reference_then_destroying_the_survivor_is_refused_until_forced(
        self, project_dir, teardown_tracked_resources, capsys
    ):
        token = live_token()
        _skip_without_firewall_scope(token)
        ensure_system_test_tag(token)

        droplet_a = unique_droplet_name("revdep-a")
        droplet_b = unique_droplet_name("revdep-b")
        firewall_name = unique_firewall_name("revdep")
        firewall_key = _resource_key(firewall_name)
        droplet_a_key = f"digitalocean.compute.{droplet_a}"
        droplet_b_key = f"digitalocean.compute.{droplet_b}"
        droplet_a_path = project_dir / "droplet-a.aiform.md"

        write_aiform_md(project_dir, name=droplet_a, filename="droplet-a.aiform.md")
        write_aiform_md(project_dir, name=droplet_b, filename="droplet-b.aiform.md")
        # depends_on declared on both, AND droplet_ids referencing both --
        # the owner's scenario exercises the union in _dependency_targets(),
        # not only the reference-implies-the-edge half droplet_ids alone
        # would exercise.
        write_firewall_aiform_md(
            project_dir,
            name=firewall_name,
            inbound_rules=[SSH_RULE],
            droplet_ids=[
                f"${{digitalocean.compute.{droplet_a}:provider_id}}",
                f"${{digitalocean.compute.{droplet_b}:provider_id}}",
            ],
            depends_on=[droplet_a_key, droplet_b_key],
        )

        # Two droplets plus a firewall referencing both -- no live test has
        # ever created a firewall with two referenced droplets before.
        code = cli.main(["plan", "create"])
        assert_cli_ok(code, capsys.readouterr(), "plan create")
        code = cli.main(["plan", "apply", "--yes"])
        assert_cli_ok(code, capsys.readouterr(), "plan apply")

        tracked = state.load(state.DEFAULT_STATE_PATH, deployment="default")
        provider_id_a = tracked.resources[droplet_a_key].attributes["provider_id"]
        provider_id_b = tracked.resources[droplet_b_key].attributes["provider_id"]
        firewall_id = tracked.resources[firewall_key].id
        assert tracked.resources[firewall_key].depends_on == sorted([droplet_a_key, droplet_b_key])

        live = get_firewall_or_none(token, firewall_id)
        assert live is not None
        assert sorted(live["droplet_ids"]) == sorted([provider_id_a, provider_id_b])

        # Scenario 1: rewrite the firewall to both declare and reference
        # only droplet_a -- droplet_b drops out of both halves of the union
        # at once, which is what lets the shrink assertions below tell
        # apart "still depended on" from "no longer depended on at all".
        write_firewall_aiform_md(
            project_dir,
            name=firewall_name,
            inbound_rules=[SSH_RULE],
            droplet_ids=[f"${{digitalocean.compute.{droplet_a}:provider_id}}"],
            depends_on=[droplet_a_key],
        )
        code = cli.main(["plan", "apply", "--yes"])
        assert_cli_ok(code, capsys.readouterr(), "plan apply (drop reference)")

        live = get_firewall_or_none(token, firewall_id)
        assert live is not None
        assert live["droplet_ids"] == [provider_id_a]

        tracked = state.load(state.DEFAULT_STATE_PATH, deployment="default")
        assert tracked.resources[firewall_key].depends_on == [droplet_a_key]

        # Scenario 2: destroying the surviving referenced droplet must be
        # refused -- the firewall still depends on it -- and the refusal
        # must not be a partial apply: both stay live and tracked.
        code = cli.main(["plan", "destroy", str(droplet_a_path), "--yes"])
        blocked = capsys.readouterr()
        assert code == 2
        assert "Error:" in blocked.err
        assert firewall_key in blocked.err
        assert droplet_a_key in blocked.err

        tracked = state.load(state.DEFAULT_STATE_PATH, deployment="default")
        assert droplet_a_key in tracked.resources
        assert firewall_key in tracked.resources
        assert get_droplet_or_none(token, str(provider_id_a)) is not None
        live = get_firewall_or_none(token, firewall_id)
        assert live is not None
        assert live["droplet_ids"] == [provider_id_a]

        # --force drops the edge, warns, and proceeds.
        code = cli.main(["plan", "destroy", str(droplet_a_path), "--yes", "--force"])
        forced = capsys.readouterr()
        assert_cli_ok(code, forced, "plan destroy --force")
        assert "Warning:" in forced.out
        assert firewall_key in forced.out

        tracked = state.load(state.DEFAULT_STATE_PATH, deployment="default")
        assert droplet_a_key not in tracked.resources
        assert firewall_key in tracked.resources
        # Polled, not checked once: DO's delete is async (see
        # conftest.wait_until_droplet_gone).
        leftover = wait_until_droplet_gone(token, str(provider_id_a))
        assert leftover is None, f"droplet {provider_id_a} still live: {leftover}"
        # #225 F15: the warning above says "dropping the edge" -- pin that
        # it actually happened, or the state-driven cleanup destroy below
        # (run without --force) hits the exact same refusal
        # again for a droplet the user just deliberately destroyed.
        assert tracked.resources[firewall_key].depends_on == []

        # Destroy everything else that remains. No --force here: F14 found
        # this step blocked (and the firewall plus droplet B left live)
        # when the pruning above didn't happen.
        code = cli.main(["plan", "destroy", "--all", "--deployment", "default", "--yes"])
        assert_cli_ok(code, capsys.readouterr(), "plan destroy (cleanup)")
        assert get_firewall_or_none(token, tracked.resources[firewall_key].id) is None

# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Live end-to-end system test for cross-resource attribute references
(specs/resource_references.md, Phase 2 of MULTI_RESOURCE_PRD.md) against the
real DigitalOcean and Anthropic APIs. Excluded from the default `pytest` run
(see pyproject.toml's `addopts`); run explicitly with:

    pytest -m system tests/system/

Requires ANTHROPIC_API_KEY and DIGITALOCEAN_TOKEN, and a token carrying both
`domain` and droplet scope.

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
  around and which a unit test can only prove against a fake.

Deliberately NOT re-proved here: ordering. Phase 1 orders by resource *key*,
and `digitalocean.compute.*` sorts before `digitalocean.domain.*` regardless of
whether an edge exists, so this pairing cannot distinguish a derived edge from
the plain sorted() drain. tests/test_orchestrator.py does that, with a
dependent whose key sorts *before* its target. What is proved live is value
flow, which no ordering accident can fake.
"""

import pytest

from aiform import cli, state
from tests.system.conftest import (
    assert_cli_ok,
    get_domain_or_none,
    list_domain_records,
    live_token,
    token_has_domain_scope,
    token_owns_zone_parent,
    unique_name,
    unique_zone_name,
    verbose_call_count,
    wait_until_domain_gone,
    write_aiform_md,
    write_domain_aiform_md,
)

pytestmark = pytest.mark.system

TTL = 1800
RECORD_NAME = "www"


@pytest.fixture(scope="module", autouse=True)
def _require_domain_scope():
    token = live_token()
    if not token_has_domain_scope(str(token)):
        pytest.skip("token lacks `domain` scope")
    if not token_owns_zone_parent(str(token)):
        pytest.skip("account does not own the system-test zone parent")


class TestCrossResourceReferenceLive:
    def test_one_apply_publishes_the_droplets_real_address(
        self, project_dir, teardown_tracked_resources, capsys
    ):
        token = str(live_token())
        droplet_name = unique_name("aiform-system-test-droplet-ref")
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
        tracked = state.load(state.DEFAULT_STATE_PATH)
        droplet_key = f"digitalocean.compute.{droplet_name}"
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
        code = cli.main(["-v", "plan", "create"])
        second_plan = capsys.readouterr()
        assert_cli_ok(code, second_plan, "second plan create")
        assert "no-op" in second_plan.out.lower()
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
        # because the reference alone established that edge. If this step fails
        # the droplet is still reclaimable -- write_aiform_md() always tags it
        # SYSTEM_TEST_TAG, which the session sweep keys off, and the zone name
        # carries the prefix and timestamp the zone sweep parses.
        code = cli.main(["plan", "destroy", "--yes"])
        assert_cli_ok(code, capsys.readouterr(), "plan destroy")
        wait_until_domain_gone(token, zone)
        assert get_domain_or_none(token, zone) is None

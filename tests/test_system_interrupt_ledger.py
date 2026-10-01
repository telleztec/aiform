# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Offline tests for `tests/system/provider_ledger.py` -- see
specs/system_test_interrupt.md, "Teardown".

The ledger decides which live droplets and firewalls the interrupt suite's
teardown deletes. An interrupted create leaves a droplet no state file knows
about, so the ledger is the only record of it. A name or id matched too loosely
deletes something the suite did not create, on an account that also hosts a
production droplet; matched too narrowly, it leaves a billing droplet behind.
"""

import types

import pytest

from tests.system.provider_ledger import Ledger


def an_entry(resource_type: str, id: str, name: str):
    return types.SimpleNamespace(resource_type=resource_type, id=id, name=name)


def a_state(**entries):
    return types.SimpleNamespace(resources=entries)


class TestNotingState:
    def test_droplet_and_firewall_ids_are_filed_separately(self):
        ledger = Ledger()
        ledger.note_state(
            a_state(
                a=an_entry("compute", "11", "web"),
                b=an_entry("firewall", "ab-cd", "fw"),
            )
        )
        assert ledger.droplet_ids == {"11"}
        assert ledger.firewall_ids == {"ab-cd"}

    def test_a_resource_type_the_suite_does_not_create_is_ignored(self):
        ledger = Ledger()
        ledger.note_state(a_state(a=an_entry("domain", "zone.example.com", "zone.example.com")))
        assert ledger.droplet_ids == set()
        assert ledger.firewall_ids == set()

    def test_an_id_stays_recorded_after_state_forgets_it(self):
        ledger = Ledger()
        ledger.note_state(a_state(a=an_entry("compute", "11", "web")))
        ledger.note_state(a_state())
        assert ledger.droplet_ids == {"11"}

    def test_the_entry_name_is_recorded_too(self):
        ledger = Ledger()
        ledger.note_state(a_state(a=an_entry("compute", "11", "web")))
        assert "web" in ledger.droplet_names


class TestNotingAResponse:
    def test_a_droplet_create_response_yields_its_id(self):
        ledger = Ledger()
        ledger.note_response({"droplet": {"id": 4242, "name": "web"}})
        assert ledger.droplet_ids == {"4242"}

    def test_a_firewall_response_yields_its_id(self):
        ledger = Ledger()
        ledger.note_response({"firewall": {"id": "ab-cd"}})
        assert ledger.firewall_ids == {"ab-cd"}

    @pytest.mark.parametrize("body", [None, {}, {"droplet": {}}, {"droplet": None}, {"x": 1}])
    def test_a_response_without_an_id_is_ignored(self, body):
        ledger = Ledger()
        ledger.note_response(body)
        assert ledger.droplet_ids == set()
        assert ledger.firewall_ids == set()


class TestWhichDropletsToDelete:
    def test_recorded_ids_are_always_included(self):
        ledger = Ledger()
        ledger.droplet_ids.add("7")
        assert ledger.droplet_ids_to_delete(lambda: []) == {"7"}

    def test_a_droplet_with_a_generated_name_and_no_recorded_id_is_found(self):
        ledger = Ledger()
        ledger.droplet_names.add("mine")
        listing = [{"id": 5, "name": "mine"}, {"id": 6, "name": "mine"}]
        assert ledger.droplet_ids_to_delete(lambda: listing) == {"5", "6"}

    @pytest.mark.parametrize(
        "name", ["mine-2", "min", "MINE", "telleztec-wordpress", "", None], ids=repr
    )
    def test_a_name_that_is_not_exactly_a_generated_one_is_left_alone(self, name):
        ledger = Ledger()
        ledger.droplet_names.add("mine")
        assert ledger.droplet_ids_to_delete(lambda: [{"id": 9, "name": name}]) == set()

    def test_nothing_is_listed_when_no_name_was_generated(self):
        ledger = Ledger()

        def listing():
            raise AssertionError("must not list the account without a name to look for")

        assert ledger.droplet_ids_to_delete(listing) == set()


class TestWhichFirewallsToDelete:
    def test_recorded_ids_and_exact_name_matches_are_included(self):
        ledger = Ledger()
        ledger.firewall_ids.add("a")
        ledger.firewall_names.add("fw")
        listing = [{"id": "b", "name": "fw"}, {"id": "c", "name": "fw-other"}]
        assert ledger.firewall_ids_to_delete(lambda: listing) == {"a", "b"}

    def test_a_deployment_prefixed_name_is_matched(self):
        ledger = Ledger()
        ledger.firewall_names.add("aiform-system-test-fw-x-1")
        listing = [
            {"id": "a", "name": "aiform-default-aiform-system-test-fw-x-1"},
            {"id": "b", "name": "aiform-system-test-fw-x-1"},
            {"id": "c", "name": "aiform-default-aiform-system-test-fw-x-12"},
            {"id": "d", "name": "other-aiform-system-test-fw-x-1"},
        ]
        assert ledger.firewall_ids_to_delete(lambda: listing) == {"a", "b"}

    def test_a_droplet_name_is_never_matched_by_suffix(self):
        ledger = Ledger()
        ledger.droplet_names.add("mine")
        assert ledger.droplet_ids_to_delete(lambda: [{"id": 9, "name": "aiform-x-mine"}]) == set()

    def test_nothing_is_listed_when_no_name_was_generated(self):
        ledger = Ledger()

        def listing():
            raise AssertionError("must not list the account without a name to look for")

        assert ledger.firewall_ids_to_delete(listing) == set()

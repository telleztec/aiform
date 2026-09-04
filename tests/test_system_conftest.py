# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Mocked unit tests for the pure helpers in `tests/system/conftest.py`
-- see specs/system_test_domain.md.

These run in the DEFAULT pytest run, deliberately: the code under test
here decides which live DNS zones the orphan sweep deletes, on an account
that also hosts a production zone. Gating its only coverage behind
`-m system` and live credentials would mean the one function that can
destroy production DNS is exercised solely by the suite it exists to
clean up after. Mirrors specs/conftest.md's reasoning for extracting
`find_leaked_credential()` as a pure, separately-tested matcher.
"""

import pytest
import yaml

from tests.system.conftest import (
    SYSTEM_TEST_ZONE_PARENT,
    SYSTEM_TEST_ZONE_PREFIX,
    unique_zone_name,
    write_domain_aiform_md,
    zone_created_at,
)


class TestZoneCreatedAtRefusesForeignNames:
    """A None return means "not ours, don't touch it". Every case here is
    a name the sweep must refuse, because a false positive deletes a zone
    this suite did not create."""

    @pytest.mark.parametrize(
        "name",
        [
            # The real zone on the account this suite runs against.
            "telleztec.com",
            "www.telleztec.com",
            # Prefix-adjacent but carrying no parsable timestamp.
            "systest.telleztec.com",
            "systest-.telleztec.com",
            "systest-notatimestamp-abc.telleztec.com",
            # Right shape, wrong parent -- the suffix guard is what
            # catches this one, independently of the prefix guard.
            "systest-20260904t000759z-abc.example.com",
            # Suffix spoofing: contains the parent, does not end with it.
            "systest-20260904t000759z-abc.telleztec.com.evil.com",
            # Prefix present but not at position 0.
            "prod-systest-20260904t000759z-abc.telleztec.com",
            # Uppercase. DigitalOcean folds a stored zone name, so this
            # spelling never comes back from the API -- but if it somehow
            # did, refusing is the safe answer.
            "SYSTEST-20260904T000759Z-abc.telleztec.com",
            "",
        ],
    )
    def test_foreign_name_is_not_claimed(self, name):
        assert zone_created_at(name) is None


class TestZoneCreatedAtClaimsOurOwn:
    def test_parses_the_encoded_timestamp(self):
        created = zone_created_at("systest-20260904t000759z-1cb41c-lifecycle.telleztec.com")
        assert created is not None
        assert (created.year, created.month, created.day) == (2026, 9, 4)
        assert (created.hour, created.minute, created.second) == (0, 7, 59)

    def test_returns_an_aware_datetime(self):
        # The sweep compares this against a timezone-aware `now`; a naive
        # return would raise TypeError mid-teardown and skip the cleanup.
        created = zone_created_at("systest-20260904t000759z-1cb41c-lifecycle.telleztec.com")
        assert created.tzinfo is not None
        assert created.utcoffset().total_seconds() == 0

    def test_a_generated_name_round_trips(self):
        # The property that actually matters: whatever unique_zone_name()
        # produces, the sweep must be able to claim back. If these two
        # ever drift, every leaked zone silently survives.
        name = unique_zone_name("lifecycle")
        assert zone_created_at(name) is not None

    def test_generated_names_are_lowercase(self):
        # DigitalOcean lowercases a stored zone name (verified live), and
        # unique_name() embeds an uppercase T/Z. Lowercasing at generation
        # keeps requested == stored, so a direct comparison against DO's
        # listing is exact. Note this is hygiene, not a parsing fix:
        # strptime matches format literals case-insensitively, so
        # zone_created_at() would read either spelling -- which is why the
        # case above asserts a mixed-case name is refused on the strength
        # of the prefix guard, not the timestamp.
        name = unique_zone_name("lifecycle")
        assert name == name.lower()
        assert name.startswith(SYSTEM_TEST_ZONE_PREFIX)
        assert name.endswith(f".{SYSTEM_TEST_ZONE_PARENT}")


class TestAllTypeRecordsCoversTheDriver:
    """Both specs claim the live suite verifies the per-type
    required-field table for *every* type the driver supports. Nothing
    enforced that: adding a ninth type to `_RECORD_TYPES` would leave the
    claim silently false with the suite still green.

    A real test rather than a module-level `assert` in the system suite.
    That assert ran at *import* time, so it fired during collection of
    the default `pytest` run — turning a ninth type into "1 error during
    collection" with zero tests executed — and it would vanish entirely
    under `python -O`.
    """

    def test_covers_every_supported_record_type(self):
        from drivers.digitalocean import domain as do_domain
        from tests.system.test_cli_domain import ALL_TYPE_RECORDS

        assert {r["type"] for r in ALL_TYPE_RECORDS} == set(do_domain._RECORD_TYPES)


class TestWriteDomainAiformMd:
    """The fixture writer's YAML must survive a real parse. It emits each
    record as a JSON object, relying on JSON being a YAML subset -- so the
    case that matters is a value whose own text contains quotes."""

    def _frontmatter(self, path):
        content = path.read_text(encoding="utf-8")
        _, frontmatter, _ = content.split("---", 2)
        return yaml.safe_load(frontmatter)

    def test_records_round_trip_through_a_real_yaml_parse(self, tmp_path):
        records = [
            {"type": "A", "name": "@", "data": "203.0.113.10", "ttl": 1800},
            {"type": "MX", "name": "@", "data": "mail.example.com", "ttl": 1800, "priority": 10},
        ]
        path = write_domain_aiform_md(tmp_path, name="zone.example.com", records=records)
        assert self._frontmatter(path)["params"]["records"] == records

    def test_a_txt_record_containing_quotes_survives_verbatim(self, tmp_path):
        # The live suite asserts DO stores TXT data verbatim; that
        # assertion is worthless if the fixture mangled the quotes on the
        # way out. Hand-rolled YAML quoting is exactly where that happens.
        data = '"v=spf1 include:_spf.example.com -all"'
        records = [{"type": "TXT", "name": "@", "data": data, "ttl": 1800}]
        path = write_domain_aiform_md(tmp_path, name="zone.example.com", records=records)
        assert self._frontmatter(path)["params"]["records"][0]["data"] == data

    def test_record_order_is_preserved(self, tmp_path):
        # Case 7d edits the order deliberately to prove UNORDERED_FIELDS
        # works; a helper that reordered would make that case vacuous.
        records = [
            {"type": "TXT", "name": "b", "data": "second", "ttl": 1800},
            {"type": "TXT", "name": "a", "data": "first", "ttl": 1800},
        ]
        path = write_domain_aiform_md(tmp_path, name="zone.example.com", records=records)
        written = self._frontmatter(path)["params"]["records"]
        assert [r["name"] for r in written] == ["b", "a"]

    def test_empty_records_is_an_empty_list_not_none(self, tmp_path):
        # `records: []` is a meaningful state (an empty zone), and YAML's
        # `records:` with nothing after it parses as None -- which the
        # driver would reject as "not a list".
        path = write_domain_aiform_md(tmp_path, name="zone.example.com", records=[])
        assert self._frontmatter(path)["params"]["records"] == []

    def test_frontmatter_declares_the_domain_resource(self, tmp_path):
        path = write_domain_aiform_md(tmp_path, name="zone.example.com", records=[])
        frontmatter = self._frontmatter(path)
        assert frontmatter["resource"] == "domain"
        assert frontmatter["provider"] == "digitalocean"
        assert frontmatter["name"] == "zone.example.com"
